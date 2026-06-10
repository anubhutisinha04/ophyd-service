"""GLOBAL_READ_ONLY deployment gate.

When read-only, every control/write path (PV set, batch, device execute/stop,
nested write, WS set/stop) is rejected while monitoring stays open. The suite's
conftest enables control (read_only=false); these tests flip it on explicitly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from direct_control.config import Settings


@pytest.fixture
def read_only(app):
    """Flip the live Settings to read-only for the duration of a test.

    app.state.settings is the same object the WS managers and require_writable
    read, so toggling it here takes effect everywhere.
    """
    app.state.settings.global_read_only = True
    try:
        yield
    finally:
        app.state.settings.global_read_only = False


# ----- REST control endpoints: 403 in read-only -----


def test_pv_set_blocked(client, read_only):
    r = client.post("/api/v1/pv/set", json={"pv_name": "IOC:m1", "value": 1.0})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_pv_set_batch_blocked(client, read_only):
    r = client.post(
        "/api/v1/pv/set/batch",
        json={"caputs": [{"pv_name": "IOC:m1", "value": 1.0}]},
    )
    assert r.status_code == 403


def test_device_execute_blocked(client, read_only):
    r = client.post(
        "/api/v1/device/execute",
        json={"device_name": "m1", "method": "set", "args": [1], "kwargs": {}},
    )
    assert r.status_code == 403


def test_device_stop_blocked(client, read_only):
    r = client.post("/api/v1/device/m1/stop")
    assert r.status_code == 403


def test_nested_write_blocked_but_read_allowed(client, read_only):
    # A write method is blocked outright (403, before the not-implemented path)...
    w = client.post("/api/v1/device/m1.user_setpoint", json={"method": "set", "value": 1})
    assert w.status_code == 403
    # ...while a read method is NOT blocked by the read-only gate (it fails later
    # with 501 not-implemented / 404, never 403).
    rd = client.post("/api/v1/device/m1.user_readback", json={"method": "read"})
    assert rd.status_code != 403


# ----- Monitoring stays open in read-only -----


def test_monitor_paths_allowed(client, read_only):
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/stats").status_code == 200
    assert client.get("/api/v1/pvs/connected").status_code == 200


def test_read_only_reflected_in_health_and_stats(client, read_only):
    assert client.get("/health").json()["read_only"] is True
    assert client.get("/api/v1/stats").json()["read_only"] is True


# ----- WebSocket set/stop handlers: blocked without touching the controller -----


def _ws_manager_read_only(cls):
    settings = Settings(configuration_service_url="http://x", global_read_only=True)
    return cls(
        pv_monitor=Mock(),
        device_controller=AsyncMock(),
        settings=settings,
        registry_client=None,
    )


async def test_pv_socket_set_blocked_in_read_only():
    from direct_control.monitoring.websocket_manager import WebSocketManager

    mgr = _ws_manager_read_only(WebSocketManager)
    ws = AsyncMock()
    await mgr._handle_set("cid", ws, {"pv": "IOC:m1", "value": 1})
    mgr.device_controller.set_pv.assert_not_awaited()
    ws.send_json.assert_awaited()  # an error envelope was sent


async def test_pv_socket_stop_blocked_in_read_only():
    from direct_control.monitoring.websocket_manager import WebSocketManager

    mgr = _ws_manager_read_only(WebSocketManager)
    ws = AsyncMock()
    await mgr._handle_stop(ws, {"device": "m1"})
    mgr.device_controller.execute_device_method.assert_not_awaited()


async def test_device_socket_set_blocked_in_read_only():
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings(configuration_service_url="http://x", global_read_only=True)
    mgr = DeviceWebSocketManager(
        pv_monitor=Mock(), device_controller=AsyncMock(), settings=settings
    )
    ws = AsyncMock()
    await mgr._handle_set("cid", ws, {"device": "m1", "value": 1})
    mgr.device_controller.access_nested_device.assert_not_awaited()
