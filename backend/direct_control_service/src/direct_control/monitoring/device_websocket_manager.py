"""
Device WebSocket manager for ophyd-websocket compatible device monitoring.

Manages WebSocket connections for device-level subscriptions, recursively
subscribing to all PVs associated with a device from the configuration service.
Write/stop operations are routed through DeviceControl for coordination checks.
"""

import asyncio
import uuid
from functools import partial
from typing import Callable, Dict, Optional, Set, TYPE_CHECKING

import httpx
import structlog
from fastapi import WebSocket, WebSocketDisconnect

from ..config import Settings
from ..models import (
    DeviceCommandRequest,
    DeviceInfo,
    DeviceLockedError,
    DeviceUpdate,
    PVUpdate,
    WebSocketAction,
)
from ._envelopes import (
    LockedWS,
    heartbeat_loop,
    log_threadsafe_future_exceptions,
    send_error,
    send_event,
    send_payload_or_size_error,
)
from .websocket_manager import SUB_TYPE_META

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor


logger = structlog.get_logger(__name__)


# Hoisted out of `_make_device_callback` so the EPICS-callback hot path
# doesn't allocate a new closure per fired update.
_DEVICE_BROADCAST_DONE_CB = partial(
    log_threadsafe_future_exceptions, where="device_broadcast_update"
)


class DeviceWebSocketManager:
    """
    Manages WebSocket connections for device-level subscriptions.

    Implements ophyd-websocket compatible device-socket protocol. Writes/stops
    are routed through the DeviceControl protocol so they inherit A4
    coordination checks.
    """

    def __init__(
        self,
        pv_monitor: "PVMonitor",
        device_controller: "DeviceControl",
        settings: Settings,
    ):
        self.pv_monitor = pv_monitor
        self.device_controller = device_controller
        self.settings = settings
        self._connections: Dict[str, LockedWS] = {}
        self._device_subscriptions: Dict[str, Set[str]] = {}
        self._device_pvs: Dict[str, Dict[str, str]] = {}
        self._pv_callbacks: Dict[str, Callable[[PVUpdate], None]] = {}
        self._device_clients: Dict[str, Set[str]] = {}
        self._heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def cleanup(self) -> None:
        """Close the pooled HTTP client and open WebSocket connections."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

        async with self._lock:
            sockets = list(self._connections.values())
            self._connections.clear()
        for ws in sockets:
            try:
                await ws.close(code=1001, reason="Service shutting down")
            except Exception:  # noqa: BLE001
                pass

    async def _fetch_device_info(
        self, device_name: str
    ) -> tuple[Optional[DeviceInfo], Optional[str]]:
        """Fetch device info from configuration_service.

        Returns (info, reason). On success: (DeviceInfo, None). On failure
        the reason distinguishes three classes that pre-S7 collapsed to a
        misleading "not_found":
          - "not_found"           — 404 (the device really isn't registered)
          - "upstream_error"      — non-2xx, non-404 (config_service is
                                    reachable but rejected/erred)
          - "upstream_unreachable" — network/timeout/connection failure
        """
        config_url = self.settings.configuration_service_url
        try:
            client = await self._get_http_client()
            response = await client.get(f"{config_url}/api/v1/devices/{device_name}")
        except httpx.RequestError as exc:
            logger.error(
                "device_info_unreachable", device_name=device_name, error=str(exc)
            )
            return None, "upstream_unreachable"

        if response.status_code == 200:
            data = response.json()
            return (
                DeviceInfo(
                    name=data.get("name", device_name),
                    device_type=data.get("device_type", "unknown"),
                    ophyd_class=data.get("ophyd_class"),
                    pvs=data.get("pvs", {}),
                    is_movable=data.get("is_movable", False),
                    is_readable=data.get("is_readable", True),
                ),
                None,
            )

        if response.status_code == 404:
            logger.info("device_info_not_found", device_name=device_name)
            return None, "not_found"

        logger.warning(
            "device_info_upstream_error",
            device_name=device_name,
            status=response.status_code,
        )
        return None, "upstream_error"

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
            self._device_subscriptions[client_id] = set()
            if self.settings.ws_heartbeat_interval > 0:
                self._heartbeat_tasks[client_id] = asyncio.create_task(
                    heartbeat_loop(wrapped, self.settings.ws_heartbeat_interval)
                )

        logger.info("device_websocket_connected", client_id=client_id)
        return client_id, wrapped

    async def disconnect(self, client_id: str):
        async with self._lock:
            self._connections.pop(client_id, None)
            device_names = self._device_subscriptions.pop(client_id, set())
            heartbeat = self._heartbeat_tasks.pop(client_id, None)
            releases = []
            for device_name in device_names:
                if device_name in self._device_clients:
                    self._device_clients[device_name].discard(client_id)
                    if not self._device_clients[device_name]:
                        self._device_clients.pop(device_name)
                        for pv_name in self._device_pvs.pop(device_name, {}).values():
                            callback = self._pv_callbacks.pop(pv_name, None)
                            if callback is not None:
                                releases.append((pv_name, callback))

        if heartbeat and not heartbeat.done():
            heartbeat.cancel()

        # pv_monitor.unsubscribe does blocking CA teardown; run off-loop.
        for pv_name, callback in releases:
            await asyncio.to_thread(self.pv_monitor.unsubscribe, pv_name, callback)

        logger.info("device_websocket_disconnected", client_id=client_id)

    async def subscribe_device(
        self, client_id: str, device_name: str, require_connection: bool = False
    ) -> tuple[bool, Optional[str], list[tuple[str, str, str]]]:
        """
        Returns (ok, reason, failed_pvs).

        `reason` is one of None, 'unknown_client', 'cap_exceeded',
        'not_found', 'upstream_error', 'upstream_unreachable',
        'not_connected' so callers can surface an accurate WS error
        instead of collapsing everything into "not found".

        `failed_pvs` is a list of (component, pv_name, error) tuples for
        PVs whose CA subscribe failed but where the device subscription as
        a whole succeeded (require_connection=False, partial-success
        path). Callers should emit a per-PV error envelope so the client
        knows those signals will never arrive. Pre-S8 these failures were
        logged and silently dropped, leaving stale ``_pv_callbacks`` and
        ``_device_pvs`` entries with no live monitor.
        """
        cap = self.settings.max_subscriptions_per_client
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return False, "unknown_client", []
            current_subs = self._device_subscriptions.get(client_id, set())
            if device_name in current_subs:
                return True, None, []
            if cap > 0 and len(current_subs) + 1 > cap:
                logger.warning(
                    "device_subscribe_cap_exceeded",
                    client_id=client_id,
                    cap=cap,
                    current=len(current_subs),
                )
                return False, "cap_exceeded", []

        device_info, fetch_reason = await self._fetch_device_info(device_name)
        if device_info is None:
            return False, fetch_reason, []

        new_subscriptions: list[tuple[str, str, Callable[[PVUpdate], None]]] = []
        async with self._lock:
            self._device_subscriptions[client_id].add(device_name)

            if device_name not in self._device_clients:
                self._device_clients[device_name] = set()
                self._device_pvs[device_name] = dict(device_info.pvs)

                for component, pv_name in device_info.pvs.items():
                    callback = self._make_device_callback(device_name, component)
                    self._pv_callbacks[pv_name] = callback
                    new_subscriptions.append((component, pv_name, callback))

            self._device_clients[device_name].add(client_id)

        # Run blocking EPICS subscribes concurrently, outside the asyncio lock.
        results = await asyncio.gather(
            *(
                asyncio.to_thread(self.pv_monitor.subscribe, pv_name, callback)
                for _, pv_name, callback in new_subscriptions
            ),
            return_exceptions=True,
        )
        succeeded: list[tuple[str, str, Callable[[PVUpdate], None]]] = []
        failed: list[tuple[str, str, str]] = []
        for entry, result in zip(new_subscriptions, results):
            component, pv_name, _ = entry
            if isinstance(result, Exception):
                logger.error("device_pv_subscribe_failed", pv=pv_name, error=str(result))
                failed.append((component, pv_name, str(result)))
            else:
                logger.debug(
                    "subscribed_device_pv",
                    device=device_name,
                    component=component,
                    pv=pv_name,
                )
                succeeded.append(entry)

        if failed and require_connection:
            await self._rollback_device_subscription(client_id, device_name, succeeded)
            return False, "not_connected", []

        if failed:
            await self._purge_failed_pvs(device_name, failed)

        await self._send_current_values(client_id, device_name)

        logger.info(
            "device_subscribed",
            client_id=client_id,
            device=device_name,
            pvs=len(succeeded),
            failed=len(failed),
        )
        return True, None, failed

    async def _purge_failed_pvs(
        self, device_name: str, failed: list[tuple[str, str, str]]
    ) -> None:
        """Drop bookkeeping for PVs whose CA subscribe failed.

        The device-level subscription stays — the client gets updates for
        the PVs that DID subscribe — but the failed components are removed
        from `_device_pvs[device_name]` and `_pv_callbacks` so a later
        unsubscribe doesn't try to tear down a non-existent CA monitor and
        so a future subscribe to the same device can re-attempt them.
        """
        async with self._lock:
            device_pvs = self._device_pvs.get(device_name)
            for component, pv_name, _ in failed:
                self._pv_callbacks.pop(pv_name, None)
                if device_pvs is not None:
                    device_pvs.pop(component, None)

    async def _rollback_device_subscription(
        self,
        client_id: str,
        device_name: str,
        succeeded: list[tuple[str, str, Callable[[PVUpdate], None]]],
    ) -> None:
        """Undo a partially-completed device subscription on require_connection failure.

        Removes the client from ``_device_subscriptions`` and
        ``_device_clients``, drops ``_device_pvs[device_name]`` and the
        whole device's ``_pv_callbacks`` if no other clients hold it,
        then unsubscribes any PVs that did succeed.
        """
        teardown: list[tuple[str, Callable[[PVUpdate], None]]] = []
        async with self._lock:
            self._device_subscriptions.get(client_id, set()).discard(device_name)
            if device_name in self._device_clients:
                self._device_clients[device_name].discard(client_id)
                if not self._device_clients[device_name]:
                    self._device_clients.pop(device_name, None)
                    self._device_pvs.pop(device_name, None)
                    for _, pv_name, callback in succeeded:
                        self._pv_callbacks.pop(pv_name, None)
                        teardown.append((pv_name, callback))

        for pv_name, callback in teardown:
            await asyncio.to_thread(self.pv_monitor.unsubscribe, pv_name, callback)

    async def unsubscribe_device(self, client_id: str, device_name: str):
        released_pvs: Dict[str, str] = {}
        async with self._lock:
            if client_id not in self._device_subscriptions:
                return

            self._device_subscriptions[client_id].discard(device_name)

            if device_name in self._device_clients:
                self._device_clients[device_name].discard(client_id)
                if not self._device_clients[device_name]:
                    self._device_clients.pop(device_name)
                    released_pvs = self._device_pvs.pop(device_name, {})

        teardowns: list[tuple[str, Callable[[PVUpdate], None]]] = []
        for pv_name in released_pvs.values():
            callback = self._pv_callbacks.pop(pv_name, None)
            if callback is not None:
                teardowns.append((pv_name, callback))
        for pv_name, callback in teardowns:
            await asyncio.to_thread(self.pv_monitor.unsubscribe, pv_name, callback)

        logger.info("device_unsubscribed", client_id=client_id, device=device_name)

    def _make_device_callback(self, device_name: str, component: str) -> Callable[[PVUpdate], None]:
        def callback(update: PVUpdate) -> None:
            if self._loop is None:
                return
            device_update = DeviceUpdate(
                device=device_name,
                signal=component,
                value=update.value,
                timestamp=update.timestamp,
                connected=update.connected,
                read_access=update.read_access,
                write_access=update.write_access,
            )
            fut = asyncio.run_coroutine_threadsafe(
                self._broadcast_device_update(device_name, device_update), self._loop
            )
            fut.add_done_callback(_DEVICE_BROADCAST_DONE_CB)

        return callback

    async def _broadcast_device_update(self, device_name: str, update: DeviceUpdate):
        async with self._lock:
            client_ids = self._device_clients.get(device_name, set()).copy()
        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _send_to_client(
        self,
        client_id: str,
        update: DeviceUpdate,
        websocket: Optional[LockedWS] = None,
    ):
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)
        if not websocket:
            return

        await send_payload_or_size_error(
            websocket,
            update.model_dump(mode="json", exclude_none=True),
            log_event="device_websocket_send",
            log_fields={
                "client_id": client_id,
                "device": update.device,
                "signal": update.signal,
            },
            oversize_message="payload exceeds size limit; update dropped",
            error_envelope_fields={"device": update.device, "signal": update.signal},
        )

    async def _send_current_values(self, client_id: str, device_name: str):
        async with self._lock:
            pvs = dict(self._device_pvs.get(device_name, {}))
            websocket = self._connections.get(client_id)

        if not websocket or not pvs:
            return

        components = list(pvs.items())
        values = await asyncio.gather(
            *(asyncio.to_thread(self.pv_monitor.get_value, pv_name) for _, pv_name in components),
            return_exceptions=True,
        )
        for (component, _), value in zip(components, values):
            if isinstance(value, BaseException) or value is None:
                continue
            update = DeviceUpdate(
                device=device_name,
                signal=component,
                value=value.value,
                timestamp=value.timestamp,
                connected=value.connected,
                read_access=True,
                write_access=True,
            )
            await self._send_to_client(client_id, update, websocket=websocket)
            await self._send_meta_to_client(
                client_id, device_name, component, value, websocket=websocket
            )

    async def _send_meta_to_client(
        self,
        client_id: str,
        device_name: str,
        component: str,
        value,
        websocket: Optional[LockedWS] = None,
    ) -> None:
        """Send a finch-compatible ``sub_type: meta`` envelope on the device socket.

        Per ``finch/src/api/ophyd/ophydDeviceSocketTypes.ts:22-38`` finch's
        device hook keys on ``sub_type === 'meta'`` and reads ``message.device``
        plus units/precision/limits/enum_strs to populate device-level
        metadata. Without this message the UI's min/max/units/etc. silently
        stay empty.

        Routes through the size-cap-aware send path. Meta is one-shot at
        subscribe time, so generic-Exception failures also produce an
        error envelope — losing it silently leaves the UI metadata-blind.
        """
        if websocket is None:
            async with self._lock:
                websocket = self._connections.get(client_id)
        if not websocket:
            return

        meta_msg: dict = {
            "sub_type": SUB_TYPE_META,
            "device": device_name,
            "signal": component,
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
            log_event="device_meta_send",
            log_fields={
                "client_id": client_id,
                "device": device_name,
                "signal": component,
            },
            oversize_message="meta payload exceeds size limit; metadata dropped",
            error_envelope_fields={
                "device": device_name,
                "signal": component,
                "sub_type": SUB_TYPE_META,
            },
            notify_on_generic_exception=True,
        )

    async def handle_client(self, websocket: WebSocket):
        client_id, ws = await self.connect(websocket)

        try:
            while True:
                data = await ws.receive_json()
                action = data.get("action")

                if action == "subscribe":
                    await self._handle_subscribe(client_id, ws, data)
                elif action == "unsubscribe":
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
                    await self._handle_stop(client_id, ws, data)
                elif action == "ping":
                    await send_event(ws, "pong")
                else:
                    await send_error(
                        ws,
                        (
                            f"Unknown action: {action}. Expected: subscribe, "
                            "unsubscribe, subscribeSafely, subscribeReadOnly, "
                            "refresh, set, stop, ping"
                        ),
                    )

        except WebSocketDisconnect:
            logger.info("device_websocket_disconnect", client_id=client_id)
        except Exception as e:  # noqa: BLE001
            logger.error("device_websocket_error", client_id=client_id, error=str(e), exc_info=True)
        finally:
            await self.disconnect(client_id)

    async def _send_subscribe_error(
        self, websocket, device_name: str, reason: Optional[str]
    ) -> None:
        """Map a subscribe_device failure reason to an actionable WS error."""
        cap = self.settings.max_subscriptions_per_client
        messages = {
            "unknown_client": "Client not registered; reconnect and retry.",
            "cap_exceeded": (f"Subscribe would exceed max_subscriptions_per_client (cap={cap})."),
            "not_found": f"Device '{device_name}' not found in configuration service",
            "upstream_error": (
                f"Configuration service returned an error looking up device "
                f"'{device_name}'; cannot subscribe."
            ),
            "upstream_unreachable": (
                f"Configuration service is unreachable; cannot resolve device "
                f"'{device_name}'."
            ),
            "not_connected": f"Device {device_name} PVs are not connected",
        }
        message = messages.get(reason or "", f"Failed to subscribe to device {device_name}")
        await send_error(websocket, message, device=device_name, reason=reason)

    async def _emit_failed_pv_envelopes(
        self,
        websocket,
        device_name: str,
        failed: list[tuple[str, str, str]],
    ) -> None:
        """Emit a per-PV error envelope for PVs whose CA subscribe failed.

        Without this the client gets a "subscribed" event for the device but
        silently never sees updates for the failed components, exactly the
        no-news-is-bad-news shape S8 was meant to eliminate.
        """
        for component, pv_name, error in failed:
            await send_error(
                websocket,
                f"PV {pv_name} ({component}) failed to subscribe: {error}",
                device=device_name,
                pv=pv_name,
                component=component,
                reason="pv_subscribe_failed",
            )

    async def _handle_subscribe(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        ok, reason, failed_pvs = await self.subscribe_device(client_id, device_name)
        if ok:
            if failed_pvs:
                await self._emit_failed_pv_envelopes(websocket, device_name, failed_pvs)
            await send_event(
                websocket,
                "subscribed",
                device=device_name,
                message=f"Subscribed to device {device_name}",
            )
        else:
            await self._send_subscribe_error(websocket, device_name, reason)

    async def _handle_unsubscribe(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        await self.unsubscribe_device(client_id, device_name)
        await send_event(
            websocket,
            "unsubscribed",
            device=device_name,
            message=f"Unsubscribed from {device_name}",
        )

    async def _handle_subscribe_safely(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        ok, reason, _ = await self.subscribe_device(
            client_id, device_name, require_connection=True
        )
        # require_connection rolls back on any PV failure, so failed_pvs
        # will always be empty here (the helper returns [] alongside
        # 'not_connected'). Discard it explicitly.
        if ok:
            await send_event(websocket, "subscribed", device=device_name, connected=True)
        else:
            await self._send_subscribe_error(websocket, device_name, reason)

    async def _handle_subscribe_read_only(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        ok, reason, failed_pvs = await self.subscribe_device(client_id, device_name)
        if ok:
            if failed_pvs:
                await self._emit_failed_pv_envelopes(websocket, device_name, failed_pvs)
            await send_event(websocket, "subscribed", device=device_name, read_only=True)
        else:
            await self._send_subscribe_error(websocket, device_name, reason)

    async def _handle_refresh(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")

        async with self._lock:
            if device_name:
                devices = (
                    [device_name]
                    if device_name in self._device_subscriptions.get(client_id, set())
                    else []
                )
            else:
                devices = list(self._device_subscriptions.get(client_id, set()))

        await asyncio.gather(*(self._send_current_values(client_id, d) for d in devices))
        await send_event(websocket, "refreshed", devices=devices)

    async def _handle_set(self, client_id: str, websocket: WebSocket, data: dict):
        """Set device component via DeviceControl (inherits coordination check)."""
        device_name = data.get("device")
        value = data.get("value")
        component = data.get("component")
        timeout = data.get("timeout")
        use_put = bool(data.get("use_put", False))

        if not device_name or value is None:
            await send_error(websocket, "device and value fields required")
            return

        async with self._lock:
            if device_name not in self._device_subscriptions.get(client_id, set()):
                await send_error(
                    websocket,
                    f"Device {device_name} not subscribed. Subscribe before setting.",
                    device=device_name,
                )
                return

        try:
            device_path = f"{device_name}.{component}" if component else device_name
            method = "put" if use_put else "set"
            result = await self.device_controller.access_nested_device(
                device_path=device_path, method=method, value=value, timeout=timeout
            )
            await send_event(
                websocket,
                "set_complete",
                device=device_name,
                component=component,
                value=value,
                success=True,
                result=result,
                use_put=use_put,
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), device=device_name, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("device_set_error", device=device_name, value=value, error=str(e))
            await send_error(websocket, str(e), device=device_name)

    async def _handle_stop(self, client_id: str, websocket: WebSocket, data: dict):
        """Stop a device via DeviceControl (inherits coordination check)."""
        device_name = data.get("device")

        if not device_name:
            await send_error(websocket, "device field required for stop")
            return

        async with self._lock:
            if device_name not in self._device_subscriptions.get(client_id, set()):
                await send_error(
                    websocket,
                    f"Device {device_name} not subscribed. Subscribe before stopping.",
                    device=device_name,
                )
                return

        try:
            response = await self.device_controller.execute_device_method(
                DeviceCommandRequest(device_name=device_name, method="stop", args=[], kwargs={})
            )
            await send_event(
                websocket,
                "stop_complete",
                device=device_name,
                success=response.success,
                message=response.message or "Device stopped",
            )
        except DeviceLockedError as e:
            await send_error(websocket, str(e), device=device_name, locked=True)
        except Exception as e:  # noqa: BLE001
            logger.error("device_stop_error", device=device_name, error=str(e))
            await send_error(websocket, str(e), device=device_name)

    def get_stats(self) -> dict:
        return {
            "active_connections": len(self._connections),
            "subscribed_devices": len(self._device_clients),
            "total_device_pvs": sum(len(pvs) for pvs in self._device_pvs.values()),
        }
