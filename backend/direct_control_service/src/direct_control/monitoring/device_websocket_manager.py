"""
Device WebSocket manager for ophyd-websocket compatible device monitoring.

Manages WebSocket connections for device-level subscriptions, recursively
subscribing to all PVs associated with a device from the configuration service.
Write/stop operations are routed through DeviceControl for coordination checks.
"""

import asyncio
import uuid
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
from ._envelopes import LockedWS, WebSocketResponseTooLarge, heartbeat_loop, send_error, send_event

if TYPE_CHECKING:
    from ..protocols import DeviceControl, PVMonitor


logger = structlog.get_logger(__name__)


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

    async def _fetch_device_info(self, device_name: str) -> Optional[DeviceInfo]:
        config_url = self.settings.configuration_service_url
        try:
            client = await self._get_http_client()
            response = await client.get(f"{config_url}/api/v1/devices/{device_name}")
            if response.status_code == 200:
                data = response.json()
                return DeviceInfo(
                    name=data.get("name", device_name),
                    device_type=data.get("device_type", "unknown"),
                    ophyd_class=data.get("ophyd_class"),
                    pvs=data.get("pvs", {}),
                    is_movable=data.get("is_movable", False),
                    is_readable=data.get("is_readable", True),
                )
            logger.warning(
                "device_info_fetch_failed",
                device_name=device_name,
                status=response.status_code,
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.error("device_info_fetch_error", device_name=device_name, error=str(e))
            return None

    async def connect(self, websocket: WebSocket) -> tuple[str, LockedWS]:
        """Accept the WS, wrap it for serialized sends, and register the client."""
        await websocket.accept()
        wrapped = LockedWS(
            websocket, max_message_bytes=self.settings.response_bytesize_limit
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
    ) -> tuple[bool, Optional[str]]:
        """
        Returns (ok, reason). `reason` is one of None, 'unknown_client',
        'cap_exceeded', 'not_found', 'not_connected' so callers can surface
        an accurate WS error instead of collapsing everything into "not found".
        """
        cap = self.settings.max_subscriptions_per_client
        async with self._lock:
            if client_id not in self._connections:
                logger.warning("subscribe_unknown_client", client_id=client_id)
                return False, "unknown_client"
            current_subs = self._device_subscriptions.get(client_id, set())
            if device_name in current_subs:
                return True, None
            if cap > 0 and len(current_subs) + 1 > cap:
                logger.warning(
                    "device_subscribe_cap_exceeded",
                    client_id=client_id,
                    cap=cap,
                    current=len(current_subs),
                )
                return False, "cap_exceeded"

        device_info = await self._fetch_device_info(device_name)
        if device_info is None:
            return False, "not_found"

        new_subscriptions: list[tuple[str, str, Callable[[PVUpdate], None]]] = []
        async with self._lock:
            self._device_subscriptions[client_id].add(device_name)

            if device_name not in self._device_clients:
                self._device_clients[device_name] = set()
                self._device_pvs[device_name] = device_info.pvs

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
        for (component, pv_name, _), result in zip(new_subscriptions, results):
            if isinstance(result, Exception):
                logger.error(
                    "device_pv_subscribe_failed", pv=pv_name, error=str(result)
                )
                if require_connection:
                    return False, "not_connected"
            else:
                logger.debug(
                    "subscribed_device_pv",
                    device=device_name,
                    component=component,
                    pv=pv_name,
                )

        await self._send_current_values(client_id, device_name)

        logger.info(
            "device_subscribed",
            client_id=client_id,
            device=device_name,
            pvs=len(device_info.pvs),
        )
        return True, None

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

    def _make_device_callback(
        self, device_name: str, component: str
    ) -> Callable[[PVUpdate], None]:
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
            asyncio.run_coroutine_threadsafe(
                self._broadcast_device_update(device_name, device_update), self._loop
            )

        return callback

    async def _broadcast_device_update(self, device_name: str, update: DeviceUpdate):
        async with self._lock:
            client_ids = self._device_clients.get(device_name, set()).copy()
        for client_id in client_ids:
            await self._send_to_client(client_id, update)

    async def _send_to_client(self, client_id: str, update: DeviceUpdate):
        async with self._lock:
            websocket = self._connections.get(client_id)

        if not websocket:
            return

        try:
            await websocket.send_json(update.model_dump(mode="json"))
        except WebSocketResponseTooLarge as e:
            logger.warning(
                "device_websocket_payload_too_large",
                client_id=client_id,
                device=update.device,
                signal=update.signal,
                error=str(e),
            )
            try:
                await send_error(
                    websocket,
                    "payload exceeds size limit; update dropped",
                    device=update.device,
                    signal=update.signal,
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            logger.error("device_websocket_send_error", client_id=client_id, error=str(e))

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
            try:
                await websocket.send_json(update.model_dump(mode="json"))
            except WebSocketResponseTooLarge as e:
                logger.warning(
                    "send_current_value_too_large",
                    device=device_name,
                    signal=component,
                    error=str(e),
                )
                try:
                    await send_error(
                        websocket,
                        "payload exceeds size limit; current value dropped",
                        device=device_name,
                        signal=component,
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                logger.error("send_current_value_error", error=str(e))

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
            logger.error(
                "device_websocket_error", client_id=client_id, error=str(e), exc_info=True
            )
        finally:
            await self.disconnect(client_id)

    async def _send_subscribe_error(
        self, websocket, device_name: str, reason: Optional[str]
    ) -> None:
        """Map a subscribe_device failure reason to an actionable WS error."""
        cap = self.settings.max_subscriptions_per_client
        messages = {
            "unknown_client": "Client not registered; reconnect and retry.",
            "cap_exceeded": (
                f"Subscribe would exceed max_subscriptions_per_client (cap={cap})."
            ),
            "not_found": f"Device '{device_name}' not found in configuration service",
            "not_connected": f"Device {device_name} PVs are not connected",
        }
        message = messages.get(reason or "", f"Failed to subscribe to device {device_name}")
        await send_error(websocket, message, device=device_name, reason=reason)

    async def _handle_subscribe(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        ok, reason = await self.subscribe_device(client_id, device_name)
        if ok:
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

        ok, reason = await self.subscribe_device(
            client_id, device_name, require_connection=True
        )
        if ok:
            await send_event(
                websocket, "subscribed", device=device_name, connected=True
            )
        else:
            await self._send_subscribe_error(websocket, device_name, reason)

    async def _handle_subscribe_read_only(self, client_id: str, websocket: WebSocket, data: dict):
        device_name = data.get("device")
        if not device_name:
            await send_error(websocket, "device field required")
            return

        ok, reason = await self.subscribe_device(client_id, device_name)
        if ok:
            await send_event(
                websocket, "subscribed", device=device_name, read_only=True
            )
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
                DeviceCommandRequest(
                    device_name=device_name, method="stop", args=[], kwargs={}
                )
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
