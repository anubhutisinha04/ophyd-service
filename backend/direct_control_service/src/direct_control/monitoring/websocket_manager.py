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

from ..config import Settings
from ..models import (
    DeviceCommandRequest,
    DeviceLockedError,
    PVSetRequest,
    PVUpdate,
    WebSocketAction,
)
from ..registry_client import RegistryClient, RegistryValidationError
from ._envelopes import (
    LockedWS,
    WebSocketResponseTooLarge,
    heartbeat_loop,
    log_threadsafe_future_exceptions,
    send_error,
    send_event,
)


# Hoisted out of `_make_pv_callback` so the EPICS-callback hot path doesn't
# allocate a new closure per fired update.
_PV_BROADCAST_DONE_CB = partial(log_threadsafe_future_exceptions, where="pv_broadcast_update")

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor


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
        registry_client: Optional[RegistryClient] = None,
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
        for ws in sockets:
            try:
                await ws.close(code=1001, reason="Service shutting down")
            except Exception:  # noqa: BLE001
                pass

    async def subscribe_pvs(self, client_id: str, pv_names: list[str]):
        """Subscribe a client to PVs; runs blocking EPICS subscribes off-loop."""
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return

            new_pvs: list[tuple[str, Callable[[PVUpdate], None]]] = []
            for pv_name in pv_names:
                self._subscriptions[client_id].add(pv_name)
                if pv_name not in self._pv_clients:
                    self._pv_clients[pv_name] = set()
                    callback = self._make_pv_callback(pv_name)
                    self._pv_callbacks[pv_name] = callback
                    new_pvs.append((pv_name, callback))
                self._pv_clients[pv_name].add(client_id)

        # Run blocking EPICS subscribes outside the asyncio lock.
        for pv_name, callback in new_pvs:
            try:
                await asyncio.to_thread(self.pv_monitor.subscribe, pv_name, callback)
                logger.info("subscribed_to_pv", pv_name=pv_name, client_id=client_id)
            except Exception as e:  # noqa: BLE001
                logger.error("pv_subscription_failed", pv_name=pv_name, error=str(e))
                async with self._lock:
                    self._pv_callbacks.pop(pv_name, None)
                    self._pv_clients.pop(pv_name, None)

        # Send current values in parallel.
        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for pv_name in pv_names),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(value, BaseException) or value is None:
                continue
            await self._send_to_client(client_id, PVUpdate.from_value(value))

            # Send a finch-compatible meta message so the UI receives
            # units, precision, and control limits.
            meta_msg: dict = {
                "sub_type": "meta",
                "pv": value.pv_name,
                "connected": value.connected,
                "read_access": value.read_access,
                "write_access": value.write_access,
                "timestamp": value.timestamp.isoformat(),
                "status": value.status,
                "severity": value.severity,
                "precision": value.precision,
                "units": value.units or "",
                "lower_ctrl_limit": value.lower_ctrl_limit,
                "upper_ctrl_limit": value.upper_ctrl_limit,
                "enum_strs": getattr(value, "enum_strs", None),
            }
            try:
                async with self._lock:
                    ws = self._connections.get(client_id)
                if ws:
                    await ws.send_json(meta_msg)
            except WebSocketResponseTooLarge:
                logger.warning(
                    "meta_message_too_large",
                    client_id=client_id,
                    pv_name=value.pv_name,
                    sub_type="meta",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "meta_message_send_failed",
                    client_id=client_id,
                    pv_name=value.pv_name,
                    sub_type="meta",
                    error=str(exc),
                )

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

    async def _broadcast_update(self, pv_name: str, update: PVUpdate):
        async with self._lock:
            client_ids = self._pv_clients.get(pv_name, set()).copy()
        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _send_to_client(
        self, client_id: str, update: PVUpdate, websocket: Optional[WebSocket] = None
    ):
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)

        if not websocket:
            return

        try:
            payload = update.model_dump(mode="json", by_alias=True, exclude_none=True)
            payload.setdefault("pv_name", update.pv_name)
            await websocket.send_json(payload)
        except TimeoutError:
            logger.warning(
                "websocket_send_timeout",
                client_id=client_id,
                pv_name=update.pv_name,
                timeout=self.settings.ws_send_timeout,
            )
        except WebSocketResponseTooLarge as e:
            logger.warning(
                "websocket_payload_too_large",
                client_id=client_id,
                pv_name=update.pv_name,
                error=str(e),
            )
            # Error envelope itself must fit under the cap; keep message short
            # and hoist PV name into a structured field.
            try:
                await send_error(
                    websocket,
                    "payload exceeds size limit; update dropped",
                    pv_name=update.pv_name,
                )
            except Exception as inner_err:  # noqa: BLE001
                logger.debug(
                    "websocket_send_error_envelope_failed",
                    client_id=client_id,
                    pv_name=update.pv_name,
                    error=str(inner_err),
                )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "websocket_send_error",
                client_id=client_id,
                pv_name=update.pv_name,
                error=str(e),
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
                PVUpdate.from_value(value, read_access=True, write_access=True),
            )

        await send_event(websocket, "refreshed", pv_names=pv_names)

    async def _handle_set(self, client_id: str, websocket: WebSocket, data: dict):
        """Set PV value via DeviceControl (inherits coordination check)."""
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
