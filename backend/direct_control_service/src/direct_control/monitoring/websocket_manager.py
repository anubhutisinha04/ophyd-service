"""
WebSocket connection manager for PV updates.

Manages WebSocket connections and routes PV updates to connected clients.
Write operations (set/stop) are routed through the DeviceControl protocol
so they inherit coordination (A4) checks.
"""

import asyncio
import uuid
from functools import partial
from typing import Callable, Dict, Optional, Set, TYPE_CHECKING

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from ..config import READ_ONLY_MESSAGE, Settings
from ..models import (
    DeviceCommandRequest,
    DeviceLockedError,
    PVSetRequest,
    PVUpdate,
    WebSocketAction,
)
from ..registry_client import RegistryValidationError
from ._envelopes import (
    LockedWS,
    close_connections,
    fanout_error,
    heartbeat_loop,
    log_threadsafe_future_exceptions,
    send_error,
    send_event,
    send_payload_or_size_error,
)


SUB_TYPE_META = "meta"


# Hoisted out of `_make_pv_callback` so the EPICS-callback hot path doesn't
# allocate a new closure per fired update.
_PV_BROADCAST_DONE_CB = partial(log_threadsafe_future_exceptions, where="pv_broadcast_update")
_PV_CALLBACK_ERROR_DONE_CB = partial(log_threadsafe_future_exceptions, where="pv_callback_error")

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor, RegistryProvider


logger = structlog.get_logger(__name__)


class WebSocketManager:
    """
    Manages WebSocket connections and PV update routing.

    Uses PVMonitor protocol for subscription management and DeviceControl
    protocol for coordination-checked write operations.
    """

    def __init__(
        self,
        pv_monitor: "PVMonitor",
        device_controller: "DeviceControl",
        settings: Settings,
        registry_client: "Optional[RegistryProvider]" = None,
    ):
        self.pv_monitor = pv_monitor
        self.device_controller = device_controller
        self.settings = settings
        self.registry_client = registry_client
        self._connections: Dict[str, LockedWS] = {}
        self._subscriptions: Dict[str, Set[str]] = {}
        self._pv_clients: Dict[str, Set[str]] = {}
        self._pv_callbacks: Dict[str, Callable[[PVUpdate], None]] = {}
        self._heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, websocket: WebSocket) -> tuple[str, LockedWS]:
        """Accept the WS, wrap it for serialized sends, and register the client."""
        await websocket.accept()
        wrapped = LockedWS(
            websocket,
            max_message_bytes=self.settings.response_bytesize_limit,
            send_timeout=self.settings.ws_send_timeout,
        )
        client_id = str(uuid.uuid4())

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        async with self._lock:
            self._connections[client_id] = wrapped
            self._subscriptions[client_id] = set()
            if self.settings.ws_heartbeat_interval > 0:
                self._heartbeat_tasks[client_id] = asyncio.create_task(
                    heartbeat_loop(wrapped, self.settings.ws_heartbeat_interval)
                )

        logger.info("websocket_connected", client_id=client_id)
        return client_id, wrapped

    async def disconnect(self, client_id: str):
        to_teardown: list[tuple[str, Optional[Callable]]] = []
        async with self._lock:
            self._connections.pop(client_id, None)
            pv_names = self._subscriptions.pop(client_id, set())
            heartbeat = self._heartbeat_tasks.pop(client_id, None)

            for pv_name in pv_names:
                if pv_name in self._pv_clients:
                    self._pv_clients[pv_name].discard(client_id)
                    if not self._pv_clients[pv_name]:
                        self._pv_clients.pop(pv_name)
                        callback = self._pv_callbacks.pop(pv_name, None)
                        to_teardown.append((pv_name, callback))

        if heartbeat and not heartbeat.done():
            heartbeat.cancel()

        # pv_monitor.unsubscribe does blocking CA teardown; run off-loop.
        for pv_name, callback in to_teardown:
            await asyncio.to_thread(self.pv_monitor.unsubscribe, pv_name, callback)

        logger.info("websocket_disconnected", client_id=client_id, pv_count=len(pv_names))

    async def close_all(self):
        """Close every open client connection (invoked on service shutdown)."""
        async with self._lock:
            sockets = list(self._connections.values())
            self._connections.clear()
        await close_connections(sockets)

    async def subscribe_pvs(self, client_id: str, pv_names: list[str]):
        """Subscribe a client to PVs; runs blocking EPICS subscribes off-loop."""
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return

            new_pvs: list[
                tuple[str, Callable[[PVUpdate], None], Callable[[BaseException], None]]
            ] = []
            for pv_name in pv_names:
                self._subscriptions[client_id].add(pv_name)
                if pv_name not in self._pv_clients:
                    self._pv_clients[pv_name] = set()
                    callback = self._make_pv_callback(pv_name)
                    on_error = self._make_pv_error_handler(pv_name)
                    self._pv_callbacks[pv_name] = callback
                    new_pvs.append((pv_name, callback, on_error))
                self._pv_clients[pv_name].add(client_id)

        # Run blocking EPICS subscribes outside the asyncio lock.
        for pv_name, callback, on_error in new_pvs:
            try:
                await asyncio.to_thread(
                    self.pv_monitor.subscribe, pv_name, callback, on_error=on_error
                )
                logger.info("subscribed_to_pv", pv_name=pv_name, client_id=client_id)
            except Exception as e:  # noqa: BLE001
                logger.error("pv_subscription_failed", pv_name=pv_name, error=str(e))
                async with self._lock:
                    self._pv_callbacks.pop(pv_name, None)
                    self._pv_clients.pop(pv_name, None)
                    # Roll back the speculative add to the client's subscription
                    # set; otherwise a never-subscribed PV counts toward the
                    # per-client cap and later refresh/unsubscribe paths treat
                    # the client as actually subscribed.
                    if client_id in self._subscriptions:
                        self._subscriptions[client_id].discard(pv_name)

        # Send current values in parallel. Read the connection once so the
        # per-PV value+meta sends don't reacquire the manager lock 2N times.
        async with self._lock:
            websocket = self._connections.get(client_id)
        if websocket is None:
            return

        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for pv_name in pv_names),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, BaseException) or value is None:
                continue
            await self._send_to_client(client_id, PVUpdate.from_value(value), websocket=websocket)
            await self._send_meta_to_client(client_id, value, websocket=websocket)

        logger.info("client_subscribed", client_id=client_id, pv_count=len(pv_names))

    async def unsubscribe_pvs(self, client_id: str, pv_names: list[str]):
        to_teardown: list[tuple[str, Optional[Callable]]] = []
        async with self._lock:
            if client_id not in self._subscriptions:
                return

            for pv_name in pv_names:
                self._subscriptions[client_id].discard(pv_name)
                if pv_name in self._pv_clients:
                    self._pv_clients[pv_name].discard(client_id)
                    if not self._pv_clients[pv_name]:
                        self._pv_clients.pop(pv_name)
                        callback = self._pv_callbacks.pop(pv_name, None)
                        to_teardown.append((pv_name, callback))

        for pv_name, callback in to_teardown:
            await asyncio.to_thread(self.pv_monitor.unsubscribe, pv_name, callback)
            logger.info("unsubscribed_from_pv", pv_name=pv_name)

        logger.info("client_unsubscribed", client_id=client_id, pv_count=len(pv_names))

    def _make_pv_callback(self, pv_name: str) -> Callable[[PVUpdate], None]:
        def callback(update: PVUpdate) -> None:
            if self._loop is None:
                logger.warning("callback_before_loop_initialized", pv_name=pv_name)
                return
            fut = asyncio.run_coroutine_threadsafe(
                self._broadcast_update(pv_name, update), self._loop
            )
            fut.add_done_callback(_PV_BROADCAST_DONE_CB)

        return callback

    def _make_pv_error_handler(self, pv_name: str) -> Callable[[BaseException], None]:
        """Build an ``on_error`` for ``PVMonitor.subscribe`` that fans out a
        ``pv_error`` envelope to every client subscribed to ``pv_name``.

        Runs on the CA listener thread, so the broadcast is scheduled
        threadsafe onto the event loop. Without this hook, a callback
        exception would be log-only and the WS subscriber would assume
        the PV was simply quiet (M9, 2026-05-01 silent-failure audit).
        """

        def on_error(exc: BaseException) -> None:
            if self._loop is None:
                logger.warning(
                    "pv_callback_error_before_loop_initialized",
                    pv_name=pv_name,
                    error=str(exc),
                )
                return
            fut = asyncio.run_coroutine_threadsafe(
                self._broadcast_pv_callback_error(pv_name, exc), self._loop
            )
            fut.add_done_callback(_PV_CALLBACK_ERROR_DONE_CB)

        return on_error

    async def _broadcast_update(self, pv_name: str, update: PVUpdate):
        async with self._lock:
            client_ids = self._pv_clients.get(pv_name, set()).copy()
        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _broadcast_pv_callback_error(self, pv_name: str, exc: BaseException) -> None:
        async with self._lock:
            client_ids = self._pv_clients.get(pv_name, set()).copy()
            ws_by_client = {cid: self._connections.get(cid) for cid in client_ids}
        await fanout_error(
            ws_by_client,
            f"PV callback failed: {exc}",
            log_event="pv_callback_error_envelope_send_failed",
            error_envelope_fields={"pv": pv_name},
            log_fields={"pv_name": pv_name},
        )

    async def _send_to_client(
        self, client_id: str, update: PVUpdate, websocket: Optional[LockedWS] = None
    ):
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)
        if not websocket:
            return

        await send_payload_or_size_error(
            websocket,
            update.model_dump(mode="json", exclude_none=True),
            log_event="websocket_send",
            log_fields={"client_id": client_id, "pv": update.pv},
            oversize_message="payload exceeds size limit; update dropped",
            error_envelope_fields={"pv": update.pv},
        )

    async def _send_meta_to_client(
        self, client_id: str, value, websocket: Optional[LockedWS] = None
    ) -> None:
        """Send a finch-compatible ``sub_type: meta`` message.

        Per ``finch/src/api/ophyd/ophydPVSocketTypes.ts`` the meta envelope
        carries units, precision, control limits, and enum strings; finch's
        hook keys it on ``sub_type === 'meta'`` and reads ``message.pv``.

        Routes through the size-cap-aware send path so oversize meta
        produces a structured error envelope instead of a silent
        server-side warning. ``notify_on_generic_exception=True`` because
        meta is a one-shot send at subscribe time — losing it silently
        leaves the UI's units/limits empty forever.
        """
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)
        if not websocket:
            return

        meta_msg: dict = {
            "sub_type": SUB_TYPE_META,
            "pv": value.pv_name,
            "connected": value.connected,
            "read_access": value.read_access,
            "write_access": value.write_access,
            "timestamp": value.timestamp.timestamp(),
            "status": value.status,
            "severity": value.severity,
            "precision": value.precision,
            "units": value.units or "",
            "lower_ctrl_limit": value.lower_ctrl_limit,
            "upper_ctrl_limit": value.upper_ctrl_limit,
            "enum_strs": getattr(value, "enum_strs", None),
        }
        await send_payload_or_size_error(
            websocket,
            meta_msg,
            log_event="meta_send",
            log_fields={"client_id": client_id, "pv": value.pv_name},
            oversize_message="meta payload exceeds size limit; metadata dropped",
            error_envelope_fields={"pv": value.pv_name, "sub_type": SUB_TYPE_META},
            notify_on_generic_exception=True,
        )

    async def handle_client(self, websocket: WebSocket):
        client_id, ws = await self.connect(websocket)

        try:
            while True:
                data = await ws.receive_json()
                action = data.get("action") or data.get("type")

                if action in ("subscribe", WebSocketAction.SUBSCRIBE.value):
                    await self._handle_subscribe(client_id, ws, data)
                elif action in ("unsubscribe", WebSocketAction.UNSUBSCRIBE.value):
                    await self._handle_unsubscribe(client_id, ws, data)
                elif action == WebSocketAction.SUBSCRIBE_SAFELY.value:
                    await self._handle_subscribe_safely(client_id, ws, data)
                elif action == WebSocketAction.SUBSCRIBE_READ_ONLY.value:
                    await self._handle_subscribe_read_only(client_id, ws, data)
                elif action == WebSocketAction.REFRESH.value:
                    await self._handle_refresh(client_id, ws, data)
                elif action == WebSocketAction.SET.value:
                    await self._handle_set(client_id, ws, data)
                elif action in ("stop", WebSocketAction.STOP.value):
                    await self._handle_stop(ws, data)
                elif action == "ping":
                    await send_event(ws, "pong")
                else:
                    logger.warning("unknown_message_type", type=action, client_id=client_id)
                    await send_error(ws, f"Unknown action: {action}")

        except WebSocketDisconnect:
            logger.info("websocket_disconnect", client_id=client_id)
        except Exception as e:  # noqa: BLE001
            logger.error("websocket_error", client_id=client_id, error=str(e), exc_info=True)
        finally:
            await self.disconnect(client_id)

    async def _within_cap(self, client_id: str, websocket: WebSocket, requested: int) -> bool:
        """Reject with a WS error if subscribing `requested` more PVs would exceed the cap."""
        cap = self.settings.max_subscriptions_per_client
        if cap <= 0:
            return True
        async with self._lock:
            current = len(self._subscriptions.get(client_id, set()))
        if current + requested > cap:
            await send_error(
                websocket,
                (
                    f"Subscribe would exceed max_subscriptions_per_client "
                    f"(cap={cap}, current={current}, requested={requested})."
                ),
                cap=cap,
                current=current,
                requested=requested,
            )
            return False
        return True

    async def _handle_subscribe(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        valid_pvs = await self._validate_pvs(websocket, pv_names)
        if not valid_pvs:
            return
        if not await self._within_cap(client_id, websocket, len(valid_pvs)):
            return
        await send_event(websocket, "subscribed", pv_names=valid_pvs)
        await self.subscribe_pvs(client_id, valid_pvs)

    async def _handle_unsubscribe(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        await self.unsubscribe_pvs(client_id, pv_names)
        await send_event(websocket, "unsubscribed", pv_names=pv_names)

    async def _handle_subscribe_safely(self, client_id: str, websocket: WebSocket, data: dict):
        pv = data.get("pv")
        if not pv:
            await send_error(websocket, "pv field required for subscribeSafely")
            return

        if not await self._validate_single_pv(websocket, pv):
            return
        if not await self._within_cap(client_id, websocket, 1):
            return

        try:
            if not await asyncio.to_thread(self.pv_monitor.is_connected, pv):
                await asyncio.to_thread(self.pv_monitor.subscribe, pv)

            value = await asyncio.to_thread(self.pv_monitor.get_value, pv)
            if value is None or not value.connected:
                await send_error(websocket, f"PV {pv} not connected", pv=pv, connected=False)
                return

            await self.subscribe_pvs(client_id, [pv])
            await send_event(websocket, "subscribed", pv_names=[pv], connected=True)

        except Exception as e:  # noqa: BLE001
            await send_error(websocket, str(e), pv=pv)

    async def _handle_subscribe_read_only(self, client_id: str, websocket: WebSocket, data: dict):
        pv_names = [data["pv"]] if data.get("pv") else data.get("pv_names", [])

        valid_pvs = await self._validate_pvs(websocket, pv_names)
        if not valid_pvs:
            return
        if not await self._within_cap(client_id, websocket, len(valid_pvs)):
            return
        await self.subscribe_pvs(client_id, valid_pvs)
        await send_event(websocket, "subscribed", pv_names=valid_pvs, read_only=True)

    async def _handle_refresh(self, client_id: str, websocket: WebSocket, data: dict):
        pv = data.get("pv")

        async with self._lock:
            if pv:
                pv_names = [pv] if pv in self._subscriptions.get(client_id, set()) else []
            else:
                pv_names = list(self._subscriptions.get(client_id, set()))

        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for pv_name in pv_names),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, BaseException) or value is None:
                continue
            await self._send_to_client(
                client_id,
                PVUpdate.from_value(value),
            )

        await send_event(websocket, "refreshed", pv_names=pv_names)

    async def _handle_set(self, client_id: str, websocket: WebSocket, data: dict):
        """Set PV value via DeviceControl (inherits coordination check)."""
        if self.settings.global_read_only:
            await send_error(websocket, READ_ONLY_MESSAGE)
            return

        pv = data.get("pv")
        value = data.get("value")
        timeout = data.get("timeout")
        use_put = bool(data.get("use_put", False))

        if not pv or value is None:
            await send_error(websocket, "pv and value fields required for set")
            return

        if not await self._validate_single_pv(websocket, pv):
            return

        try:
            response = await self.device_controller.set_pv(
                PVSetRequest(pv_name=pv, value=value, wait=not use_put, timeout=timeout)
            )
            await send_event(
                websocket,
                "set_complete",
                pv=pv,
                value=value,
                success=response.success,
                message=response.message,
                use_put=use_put,
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), pv=pv, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("pv_set_error", pv=pv, value=value, error=str(e))
            await send_error(websocket, str(e), pv=pv)

    async def _handle_stop(self, websocket: WebSocket, data: dict):
        """Stop a device via DeviceControl (inherits coordination check)."""
        if self.settings.global_read_only:
            await send_error(websocket, READ_ONLY_MESSAGE)
            return

        device = data.get("device")

        if not device:
            await send_error(websocket, "device field required for stop")
            return

        try:
            response = await self.device_controller.execute_device_method(
                DeviceCommandRequest(device_name=device, method="stop", args=[], kwargs={})
            )
            await send_event(
                websocket,
                "stop_complete",
                device=device,
                success=response.success,
                message=response.message or "Device stopped",
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), device=device, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("pv_stop_error", device=device, error=str(e))
            await send_error(websocket, str(e), device=device)

    async def _validate_pvs(self, websocket: WebSocket, pv_names: list[str]) -> list[str]:
        if not self.registry_client:
            return list(pv_names)

        results = await asyncio.gather(
            *(self.registry_client.validate_pv(p) for p in pv_names),
            return_exceptions=True,
        )
        valid: list[str] = []
        for pv_name, result in zip(pv_names, results):
            if isinstance(result, (RegistryValidationError, RuntimeError)):
                await send_error(websocket, str(result), pv=pv_name)
            elif isinstance(result, Exception):
                raise result
            else:
                valid.append(pv_name)
        return valid

    async def _validate_single_pv(self, websocket: WebSocket, pv: str) -> bool:
        if not self.registry_client:
            return True
        try:
            await self.registry_client.validate_pv(pv)
            return True
        except (RegistryValidationError, RuntimeError) as e:
            await send_error(websocket, str(e), pv=pv)
            return False

    def get_stats(self) -> dict:
        return {
            "active_connections": len(self._connections),
            "total_pvs": len(self._pv_clients),
            "connected_pvs": len(self.pv_monitor.get_connected_pvs()),
        }
