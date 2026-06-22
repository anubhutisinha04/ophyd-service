"""Monitoring-subsystem reliability regressions.

Each test pins one confirmed failure mode in the monitoring layer:

- pv_monitor: a failed signal.subscribe() left the destroyed signal in
        the caches, permanently breaking the PV until restart.
- device-socket: per-PV (not per-device) callback bookkeeping made one
        device's teardown unsubscribe a shared PV's OTHER device callback.
- pv-socket: a failed EPICS subscribe rolled back the whole PV client
        set, leaving a concurrently-joined second client with a phantom
        subscription.
- image sockets: task.exception() on a freshly-cancelled task raised
        InvalidStateError on every normal disconnect (logged as
        image_socket_error), masking real loop crashes.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from direct_control.config import Settings
from direct_control.models import PVNotFoundError, PVValue
from direct_control.protocols import MockPVMonitor


# ===== Failed subscribe must not poison the PV cache =========================


class _StubSignalBase:
    def __init__(self, pv_name, name=None):
        self.pvname = pv_name
        self.name = name or pv_name
        self.connected = True

    def wait_for_connection(self, timeout=None):
        return None

    def destroy(self):
        return None


class _FailingSubscribeSignal(_StubSignalBase):
    def subscribe(self, *args, **kwargs):
        raise RuntimeError("CA channel dropped between connect and subscribe")


class _WorkingSignal(_StubSignalBase):
    def subscribe(self, *args, **kwargs):
        return 0


def _dummy_pv_value(pv_name: str) -> PVValue:
    return PVValue(pv_name=pv_name, value=1.0, timestamp=datetime.now())


def test_pv_monitor_failed_subscribe_cleans_cache_and_retry_works(monkeypatch):
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())
    monkeypatch.setattr(
        pvm.PVMonitorManager,
        "_signal_to_pv_value",
        lambda self, pv_name, signal: _dummy_pv_value(pv_name),
    )

    monkeypatch.setattr(pvm, "EpicsSignal", _FailingSubscribeSignal)
    with pytest.raises(PVNotFoundError, match="subscription failed"):
        monitor.subscribe("X:PV")

    # No poisoned bookkeeping: pre-fix the destroyed signal stayed cached and
    # every later subscribe() silently attached callbacks to a dead object.
    assert "X:PV" not in monitor._signals
    assert "X:PV" not in monitor._latest_values
    assert "X:PV" not in monitor._buffers
    assert "X:PV" not in monitor._connection_status

    # The IOC comes back: the retry must establish a REAL subscription.
    monkeypatch.setattr(pvm, "EpicsSignal", _WorkingSignal)
    monitor.subscribe("X:PV")
    assert "X:PV" in monitor._signals
    assert isinstance(monitor._signals["X:PV"], _WorkingSignal)


# ===== Shared PV across two devices survives one device's teardown ===========


async def test_device_teardown_keeps_shared_pv_callback_of_other_device():
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    mock_monitor = MockPVMonitor()
    manager = DeviceWebSocketManager(
        pv_monitor=mock_monitor, device_controller=None, settings=Settings()
    )

    shared_pv = "BL:SHARED:CURRENT"
    cb_a = manager._make_device_callback("dev_a", "current")
    cb_b = manager._make_device_callback("dev_b", "current")

    # State as two completed subscribe_device calls would leave it: each
    # device holds its own pv_monitor callback on the shared PV.
    mock_monitor.subscribe(shared_pv, cb_a)
    mock_monitor.subscribe(shared_pv, cb_b)
    manager._pv_callbacks[("dev_a", shared_pv)] = cb_a
    manager._pv_callbacks[("dev_b", shared_pv)] = cb_b
    manager._device_clients = {"dev_a": {"client-a"}, "dev_b": {"client-b"}}
    manager._device_pvs = {
        "dev_a": {"current": shared_pv},
        "dev_b": {"current": shared_pv},
    }
    manager._device_subscriptions = {"client-a": {"dev_a"}, "client-b": {"dev_b"}}

    await manager.unsubscribe_device("client-a", "dev_a")

    # dev_a's callback is gone; dev_b's is untouched and still registered —
    # pre-fix the pv-keyed dict held only cb_b, so dev_a's teardown popped
    # and unsubscribed cb_b (silencing dev_b) while cb_a leaked forever.
    assert mock_monitor._callbacks[shared_pv] == [cb_b]
    assert ("dev_b", shared_pv) in manager._pv_callbacks
    assert ("dev_a", shared_pv) not in manager._pv_callbacks


# ===== Failed subscribe rolls back every raced-in client =====================


class _StubWS:
    async def send_json(self, *a, **k):
        return None

    async def close(self, *a, **k):
        return None


async def test_pv_socket_failed_subscribe_rolls_back_raced_second_client():
    from direct_control.monitoring.websocket_manager import WebSocketManager

    manager = WebSocketManager(
        pv_monitor=MockPVMonitor(),
        device_controller=None,
        settings=Settings(),
        registry_client=None,
    )

    pv = "BL:DOWN:PV"

    class _RacingFailingMonitor(MockPVMonitor):
        """Simulates client B piggybacking on the in-flight subscribe (it sees
        the PV already in _pv_clients and never calls EPICS itself), after
        which the EPICS subscribe fails."""

        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            manager._pv_clients[pv_name].add("client-b")
            manager._subscriptions["client-b"].add(pv_name)
            raise PVNotFoundError(f"PV {pv_name} connection timeout")

    manager.pv_monitor = _RacingFailingMonitor()
    manager._connections = {"client-a": _StubWS(), "client-b": _StubWS()}
    manager._subscriptions = {"client-a": set(), "client-b": set()}

    await manager.subscribe_pvs("client-a", [pv])

    # Pre-fix: the rollback popped _pv_clients[pv] wholesale but only cleaned
    # client-a's _subscriptions — client-b kept a phantom subscription that
    # counted toward its cap, delivered nothing, and was never torn down.
    assert pv not in manager._pv_clients
    assert pv not in manager._pv_callbacks
    assert pv not in manager._subscriptions["client-a"]
    assert pv not in manager._subscriptions["client-b"]


# ===== Image-socket disconnect must not log a loop failure ===================


def test_camera_socket_disconnect_logs_no_loop_error(client, test_ioc, monkeypatch):
    import direct_control.monitoring.image_stream_manager as ism

    errors: list[str] = []
    real_logger = ism.logger

    class _SpyLogger:
        def error(self, event, *args, **kwargs):
            errors.append(event)
            real_logger.error(event, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_logger, name)

    monkeypatch.setattr(ism, "logger", _SpyLogger())

    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "IOC:image1:ArrayData"})
        # Wait for the stream to be live (dimensions arrive first).
        while True:
            msg = ws.receive_json()
            if "x" in msg and "y" in msg:
                break

    # Normal client disconnect: give the server-side teardown a moment, then
    # assert the loop-failure paths stayed silent. Pre-fix EVERY disconnect
    # raised InvalidStateError out of _stream's finally (task.exception() on
    # a freshly-cancelled, still-pending task), logged as image_socket_error.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not getattr(client.app.state.camera_ws_manager, "_connections", {}):
            break
        time.sleep(0.05)

    loop_failures = [
        event
        for event in errors
        if event in ("image_socket_error", "image_socket_loop_failed")
    ]
    assert loop_failures == [], f"disconnect logged loop failures: {loop_failures}"
