"""Monitoring-subsystem reliability regressions.

Each test pins one confirmed failure mode in the monitoring layer:

- pv_monitor: a failed signal.subscribe() left the destroyed signal in
        the caches, permanently breaking the PV until restart.
- device-socket: per-PV (not per-device) callback bookkeeping made one
        device's teardown unsubscribe a shared PV's OTHER device callback.
- pv-socket: a failed EPICS subscribe rolled back the whole PV client
        set, leaving a concurrently-joined second client with a phantom
        subscription.
- pv_monitor: the blocking CA connect + initial read ran while holding the
        global monitor lock, so one dead PV's 5 s connect stalled value/meta
        fan-out for every other monitored PV.
- pv-socket / device-socket: a client disconnecting while its first EPICS
        subscribe was still in flight left a live CA monitor that nothing
        referenced (broadcasting to an empty set, keeping the signal alive
        forever).
- image sockets: task.exception() on a freshly-cancelled task raised
        InvalidStateError on every normal disconnect (logged as
        image_socket_error), masking real loop crashes.
"""

from __future__ import annotations

import asyncio
import threading
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


def test_pv_monitor_failed_subscribe_drops_connect_lock(monkeypatch):
    """A PV that never subscribes must not leave a permanent per-PV connect
    lock: no unsubscribe path reaches a failed PV, so subscribe() itself has to
    drop it or a repeatedly-failing PV would accumulate entries forever.
    """
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())
    monkeypatch.setattr(
        pvm.PVMonitorManager,
        "_signal_to_pv_value",
        lambda self, pv_name, signal: _dummy_pv_value(pv_name),
    )

    # (a) failure while registering the CA monitors (connect succeeded)
    monkeypatch.setattr(pvm, "EpicsSignal", _FailingSubscribeSignal)
    with pytest.raises(PVNotFoundError):
        monitor.subscribe("X:PV")
    assert "X:PV" not in monitor._connect_locks

    # (b) failure to connect at all
    class _NeverConnectsSignal(_StubSignalBase):
        def __init__(self, pv_name, name=None):
            super().__init__(pv_name, name)
            self.connected = False

        def subscribe(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(pvm, "EpicsSignal", _NeverConnectsSignal)
    with pytest.raises(PVNotFoundError):
        monitor.subscribe("Y:PV")
    assert "Y:PV" not in monitor._connect_locks


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
        event for event in errors if event in ("image_socket_error", "image_socket_loop_failed")
    ]
    assert loop_failures == [], f"disconnect logged loop failures: {loop_failures}"


# ===== Blocking CA connect must not hold the global monitor lock =============


def test_pv_monitor_connect_runs_outside_lock(monkeypatch):
    """A slow/dead PV's connect must not hold ``self._lock``.

    ``_handle_value_update`` / ``_handle_meta_update`` reacquire ``self._lock``
    on the CA dispatch thread, so holding it across a 5 s ``wait_for_connection``
    (pre-fix) stalled value/meta fan-out for every other monitored PV until the
    dead PV timed out.
    """
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())
    monkeypatch.setattr(
        pvm.PVMonitorManager,
        "_signal_to_pv_value",
        lambda self, pv_name, signal: _dummy_pv_value(pv_name),
    )

    connecting = threading.Event()
    release = threading.Event()

    class _SlowSignal(_StubSignalBase):
        def wait_for_connection(self, timeout=None):
            connecting.set()
            # Block like a dead PV would, but bounded so the test can't hang.
            release.wait(timeout=5.0)
            return None

        def subscribe(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(pvm, "EpicsSignal", _SlowSignal)

    worker = threading.Thread(target=monitor.subscribe, args=("SLOW:PV",))
    worker.start()
    try:
        assert connecting.wait(timeout=2.0), "subscribe never reached wait_for_connection"
        # SLOW:PV is mid-connect: self._lock must be free. Pre-fix subscribe
        # held it across the whole connect and this acquire would time out.
        acquired = monitor._lock.acquire(timeout=1.0)
        assert acquired, "self._lock held during blocking connect (regression)"
        monitor._lock.release()
    finally:
        release.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert "SLOW:PV" in monitor._signals


# ===== Disconnect racing an in-flight subscribe must not leak a CA monitor ===


class _RealisticMockMonitor(MockPVMonitor):
    """MockPVMonitor that models the real pv_monitor teardown: removing a PV's
    last callback destroys its (mock) signal, so ``is_connected`` /
    ``get_connected_pvs`` reflect the true leak / no-leak end state.
    """

    def unsubscribe(self, pv_name, callback=None):
        super().unsubscribe(pv_name, callback)
        if callback and not self._callbacks.get(pv_name):
            self._subscribed.pop(pv_name, None)
            self._callbacks.pop(pv_name, None)
            self._values.pop(pv_name, None)


async def test_pv_socket_disconnect_during_subscribe_tears_down_ca_monitor():
    from direct_control.monitoring.websocket_manager import WebSocketManager

    manager = WebSocketManager(
        pv_monitor=MockPVMonitor(),
        device_controller=None,
        settings=Settings(),
        registry_client=None,
    )
    manager._loop = asyncio.get_running_loop()
    manager._connections = {"client-a": _StubWS()}
    manager._subscriptions = {"client-a": set()}

    pv = "BL:RACE:PV"

    class _DisconnectDuringSubscribeMonitor(_RealisticMockMonitor):
        """Runs the client's disconnect to completion while its (blocking)
        first subscribe is in flight — exactly the disconnect-during-subscribe
        window — then registers the CA monitor that nothing now references."""

        did_disconnect = False

        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            if not self.did_disconnect:
                self.did_disconnect = True
                asyncio.run_coroutine_threadsafe(
                    manager.disconnect("client-a"), manager._loop
                ).result(timeout=5.0)
            super().subscribe(pv_name, callback, read_only=read_only, on_error=on_error)

    manager.pv_monitor = _DisconnectDuringSubscribeMonitor()

    await manager.subscribe_pvs("client-a", [pv])

    # Pre-fix the orphaned monitor stayed live (broadcasting to nobody) and,
    # because teardown matches callbacks by identity, kept the signal forever.
    assert manager.pv_monitor.get_connected_pvs() == [], "leaked CA monitor"
    assert not manager.pv_monitor.is_connected(pv)
    assert pv not in manager._pv_clients
    assert pv not in manager._pv_callbacks


async def test_device_socket_disconnect_during_subscribe_tears_down_ca_monitors():
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    manager = DeviceWebSocketManager(
        pv_monitor=MockPVMonitor(), device_controller=None, settings=Settings()
    )
    manager._loop = asyncio.get_running_loop()
    manager._connections = {"client-a": _StubWS()}
    manager._device_subscriptions = {"client-a": set()}

    device = "dev_a"
    device_info = DeviceInfo(
        name=device,
        device_type="motor",
        pvs={"readback": "BL:DEV:RBV", "setpoint": "BL:DEV:VAL"},
    )

    async def _fake_fetch(name):
        return device_info, None

    manager._fetch_device_info = _fake_fetch

    class _DisconnectDuringGatherMonitor(_RealisticMockMonitor):
        """The only client disconnects while the device's PV subscribes are
        gathering. The per-device lock doesn't block disconnect (it takes only
        self._lock), so the CA monitors register for a device with zero clients.
        """

        _guard = threading.Lock()
        did_disconnect = False

        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            with self._guard:
                first = not self.did_disconnect
                self.did_disconnect = True
            if first:
                asyncio.run_coroutine_threadsafe(
                    manager.disconnect("client-a"), manager._loop
                ).result(timeout=5.0)
            super().subscribe(pv_name, callback, read_only=read_only, on_error=on_error)

    manager.pv_monitor = _DisconnectDuringGatherMonitor()

    outcome = await manager.subscribe_device("client-a", device)

    assert outcome.ok is False
    # Every gathered CA subscription for the now-clientless device was torn
    # down; pre-fix they leaked (a later subscriber would then overwrite the
    # per-(device, pv) callback slot, orphaning these forever).
    assert manager.pv_monitor.get_connected_pvs() == [], "leaked device CA monitors"
    assert device not in manager._device_clients
    assert device not in manager._device_pvs
    assert not any(key[0] == device for key in manager._pv_callbacks)


async def test_device_socket_disconnect_during_failed_subscribe_returns_cleanly():
    """Device-gone branch must return cleanly even when every PV subscribe also
    failed, i.e. there are no succeeded teardowns to run.
    """
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    manager = DeviceWebSocketManager(
        pv_monitor=MockPVMonitor(), device_controller=None, settings=Settings()
    )
    manager._loop = asyncio.get_running_loop()
    manager._connections = {"client-a": _StubWS()}
    manager._device_subscriptions = {"client-a": set()}

    device = "dev_b"
    device_info = DeviceInfo(name=device, device_type="motor", pvs={"readback": "BL:DEV2:RBV"})

    async def _fake_fetch(name):
        return device_info, None

    manager._fetch_device_info = _fake_fetch

    class _DisconnectThenFailMonitor(_RealisticMockMonitor):
        _guard = threading.Lock()
        did_disconnect = False

        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            with self._guard:
                first = not self.did_disconnect
                self.did_disconnect = True
            if first:
                asyncio.run_coroutine_threadsafe(
                    manager.disconnect("client-a"), manager._loop
                ).result(timeout=5.0)
            raise PVNotFoundError(f"PV {pv_name} connection timeout")

    manager.pv_monitor = _DisconnectThenFailMonitor()

    outcome = await manager.subscribe_device("client-a", device)

    assert outcome.ok is False
    assert device not in manager._device_clients
    assert device not in manager._device_pvs
    assert not any(key[0] == device for key in manager._pv_callbacks)
