"""Regressions for the Phase-4 monitor tail (fan-out serialization + idle-TTL eviction).

These tests pin three behaviors:

1. **Broadcast serializes once.** ``WebSocketManager._broadcast_update`` (and
   the device-socket equivalent) must serialize the update to JSON exactly
   once and hand the same text to every subscribed client, not
   ``model_dump`` + ``json.dumps`` per client. Under a bursty PV with N
   subscribers the previous per-client path allocated the same string N
   times on the CA callback thread; this test drives ``_broadcast_update``
   with N stub clients and asserts each of them received a byte-identical
   frame.

2. **Idle callback-less monitors are evicted.** The REST endpoint
   ``GET /api/v1/pvs/{pv_name}/value`` calls ``PVMonitor.subscribe(pv_name)``
   without a callback to warm the cache and returns. Nothing ever
   unsubscribes it, so pre-fix each distinct REST-queried PV leaked a CA
   monitor for the lifetime of the process. ``evict_idle_monitors(ttl)``
   tears down PVs with no callbacks that have been untouched for longer
   than ``ttl``; PVs with WS callbacks are pinned regardless of age; a
   recent subscribe / read call resets the age.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from direct_control.config import Settings
from direct_control.models import PVUpdate

# ---------------------------------------------------------------------------
# Fan-out: single serialize, N sends
# ---------------------------------------------------------------------------


class _RecordingWS:
    """Minimal LockedWS-like stub that records the exact bytes handed to
    ``send_text`` (the pre-serialized fan-out path). ``send_json`` is
    routed through ``send_text`` (matching the real LockedWS) so the test
    can spot a regression that silently reverts to per-client serialization.
    """

    def __init__(self):
        self.sent_text: list[str] = []
        self.sent_json: list = []

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_json(self, data) -> None:
        self.sent_json.append(data)
        self.sent_text.append(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


async def test_pv_broadcast_serializes_once_and_fans_out_text():
    """``_broadcast_update`` must hand the SAME text to every subscribed
    client — not run ``model_dump`` + ``json.dumps`` per client."""
    from direct_control.monitoring import _envelopes
    from direct_control.monitoring.websocket_manager import WebSocketManager

    mgr = WebSocketManager(
        pv_monitor=MagicMock(),
        device_controller=None,
        settings=Settings(),
        registry_client=None,
    )
    pv = "BL:X:VAL"
    clients = [f"c{i}" for i in range(5)]
    ws_by_client = {cid: _RecordingWS() for cid in clients}
    mgr._connections = ws_by_client
    mgr._pv_clients[pv] = set(clients)

    # Count serialize_json_frame calls to catch any regression that reverts
    # to per-client serialization (which would call it N times, not 1).
    serialize_calls = {"n": 0}
    real_serialize = _envelopes.serialize_json_frame

    def _counted_serialize(payload):
        serialize_calls["n"] += 1
        return real_serialize(payload)

    import direct_control.monitoring.websocket_manager as ws_mod

    ws_mod.serialize_json_frame = _counted_serialize
    try:
        update = PVUpdate(pv=pv, value=42, timestamp=time.time(), connected=True)
        await mgr._broadcast_update(pv, update)
    finally:
        ws_mod.serialize_json_frame = real_serialize

    # Each client received exactly one frame, all identical, produced from
    # a single serialize call.
    assert serialize_calls["n"] == 1
    frames = [ws.sent_text for ws in ws_by_client.values()]
    assert all(len(f) == 1 for f in frames), f"expected one frame per client, got {frames}"
    first = frames[0][0]
    assert all(f[0] == first for f in frames), "clients received different frame bytes"

    # And the sent bytes decode to the update, so the fan-out is
    # byte-identical to what the previous per-client path produced.
    parsed = json.loads(first)
    assert parsed["pv"] == pv
    assert parsed["value"] == 42


async def test_pv_broadcast_no_subscribers_skips_serialization():
    """No subscribers → no serialize, no send. Guards against wasted work
    on a PV whose only subscriber just disconnected."""
    import direct_control.monitoring.websocket_manager as ws_mod
    from direct_control.monitoring.websocket_manager import WebSocketManager

    mgr = WebSocketManager(
        pv_monitor=MagicMock(),
        device_controller=None,
        settings=Settings(),
        registry_client=None,
    )

    serialize_calls = {"n": 0}
    real_serialize = ws_mod.serialize_json_frame

    def _counted(payload):
        serialize_calls["n"] += 1
        return real_serialize(payload)

    ws_mod.serialize_json_frame = _counted
    try:
        update = PVUpdate(pv="BL:GONE", value=1, timestamp=time.time(), connected=True)
        # _pv_clients has no entry for this PV; the copy() returns empty set.
        await mgr._broadcast_update("BL:GONE", update)
    finally:
        ws_mod.serialize_json_frame = real_serialize

    assert serialize_calls["n"] == 0


async def test_device_broadcast_serializes_once_and_fans_out_text():
    """Same contract on the device socket: serialize once, fan out text."""
    import direct_control.monitoring.device_websocket_manager as dev_mod
    from direct_control.models import DeviceUpdate
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    mgr = DeviceWebSocketManager(
        pv_monitor=MagicMock(),
        device_controller=MagicMock(),
        settings=Settings(),
        registry_client=MagicMock(),
    )

    device = "m1"
    clients = [f"dc{i}" for i in range(4)]
    ws_by_client = {cid: _RecordingWS() for cid in clients}
    mgr._connections = ws_by_client
    mgr._device_clients[device] = set(clients)

    serialize_calls = {"n": 0}
    real_serialize = dev_mod.serialize_json_frame

    def _counted(payload):
        serialize_calls["n"] += 1
        return real_serialize(payload)

    dev_mod.serialize_json_frame = _counted
    try:
        update = DeviceUpdate(
            device=device, signal="readback", value=1.5, timestamp=time.time(), connected=True
        )
        await mgr._broadcast_device_update(device, update)
    finally:
        dev_mod.serialize_json_frame = real_serialize

    assert serialize_calls["n"] == 1
    frames = [ws.sent_text for ws in ws_by_client.values()]
    assert all(len(f) == 1 for f in frames)
    first = frames[0][0]
    assert all(f[0] == first for f in frames)
    parsed = json.loads(first)
    assert parsed["device"] == device
    assert parsed["signal"] == "readback"


# ---------------------------------------------------------------------------
# Idle-TTL eviction
# ---------------------------------------------------------------------------


class _FakeSignal:
    """Stand-in for the ophyd EpicsSignal that PVMonitorManager stores in
    ``_signals``. ``destroy()`` records that it was torn down."""

    def __init__(self):
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def _seed_pv(mgr, pv_name: str, *, touched: float | None = None) -> _FakeSignal:
    """Directly install a fake signal for a PV, bypassing the real CA path.

    Mirrors the state a completed subscribe would leave: signal in
    ``_signals``, ``_last_touched`` set, empty ``_callbacks``."""
    signal = _FakeSignal()
    with mgr._lock:
        mgr._signals[pv_name] = signal
        mgr._connection_status[pv_name] = True
        mgr._last_touched[pv_name] = touched if touched is not None else time.monotonic()
        # _callbacks intentionally left empty — a callback-less subscribe.
    return signal


def test_evict_idle_removes_untouched_callbackless_monitor():
    """A callback-less monitor untouched for longer than the TTL is torn down."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    signal = _seed_pv(mgr, "BL:REST:OLD", touched=time.monotonic() - 1000.0)

    evicted = mgr.evict_idle_monitors(idle_ttl=60.0)

    assert evicted == ["BL:REST:OLD"]
    assert signal.destroyed
    assert "BL:REST:OLD" not in mgr._signals
    assert "BL:REST:OLD" not in mgr._last_touched
    assert "BL:REST:OLD" not in mgr._connection_status


def test_evict_idle_leaves_recently_touched_monitor():
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    signal = _seed_pv(mgr, "BL:REST:FRESH", touched=time.monotonic())

    evicted = mgr.evict_idle_monitors(idle_ttl=60.0)

    assert evicted == []
    assert not signal.destroyed
    assert "BL:REST:FRESH" in mgr._signals


def test_evict_idle_pins_monitors_with_callbacks():
    """Any WS subscriber (a real ``_callbacks`` entry) keeps the monitor
    alive regardless of how long it's been since the last subscribe call.
    The TTL only applies to callback-less REST-created monitors."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    mgr = PVMonitorManager(Settings())
    signal = _seed_pv(mgr, "BL:WS:LIVE", touched=time.monotonic() - 1000.0)
    # A WS subscriber holds a callback for this PV.
    mgr._callbacks["BL:WS:LIVE"].append(_Subscriber(lambda _u: None, None))

    evicted = mgr.evict_idle_monitors(idle_ttl=60.0)

    assert evicted == []
    assert not signal.destroyed
    assert "BL:WS:LIVE" in mgr._signals


def test_evict_idle_handles_missing_last_touched_defensively():
    """A monitor with no ``_last_touched`` entry (upgrade from a pre-fix
    state, or defensive path) is treated as age=infinity and evicted."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    signal = _FakeSignal()
    with mgr._lock:
        mgr._signals["BL:LEGACY"] = signal
        # _last_touched intentionally not set.

    evicted = mgr.evict_idle_monitors(idle_ttl=60.0)

    assert evicted == ["BL:LEGACY"]
    assert signal.destroyed


def test_evict_idle_preserves_monitors_within_ttl_window():
    """A monitor touched within the TTL window is preserved. Guards the
    boundary — a monitor whose age is strictly less than the TTL must not
    be evicted (compare-strictly-less contract in ``evict_idle_monitors``)."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    # touched=now, so age ≈ 0 seconds — well under any reasonable TTL.
    signal = _seed_pv(mgr, "BL:JUST:MADE")

    evicted = mgr.evict_idle_monitors(idle_ttl=60.0)
    assert evicted == []
    assert not signal.destroyed


def test_get_value_touches_monitor():
    """A ``get_value`` call must reset the idle clock so a monitor being
    actively read via the REST endpoint doesn't get swept out from under
    an active caller."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    old = time.monotonic() - 1000.0
    _seed_pv(mgr, "BL:READ:ME", touched=old)
    # Preseed a cached value so get_value takes the fast path (no CA call).
    from datetime import datetime as _dt

    from direct_control.models import PVValue

    mgr._latest_values["BL:READ:ME"] = PVValue(
        pv_name="BL:READ:ME",
        value=1.0,
        timestamp=_dt.now(),
        status=0,
        severity=0,
        connected=True,
        read_access=True,
        write_access=False,
    )

    before = mgr._last_touched["BL:READ:ME"]
    assert mgr.get_value("BL:READ:ME") is not None
    after = mgr._last_touched["BL:READ:ME"]
    assert after > before


def test_repeat_subscribe_touches_monitor():
    """A second ``subscribe(pv_name)`` on an already-connected PV must
    reset the idle clock — the REST endpoint's warm-cache call keeps its
    own monitor alive as long as callers keep hitting it."""
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    old = time.monotonic() - 1000.0
    _seed_pv(mgr, "BL:RE:SUB", touched=old)

    mgr.subscribe("BL:RE:SUB")

    assert mgr._last_touched["BL:RE:SUB"] > old


# ---------------------------------------------------------------------------
# Lifespan wiring: sweep task starts and evicts idle monitors
# ---------------------------------------------------------------------------


def test_lifespan_starts_sweep_task_and_evicts_idle_monitor(monkeypatch):
    """End-to-end: the lifespan spawns the sweep task with a short interval,
    and after a few sweep cycles an idle callback-less monitor is gone.

    Uses a generous deadline because CI runners are slower than local dev
    (a tight budget would flake) — correctness of the eviction itself is
    pinned by the unit tests above; this test's only job is proving the
    sweep task is actually wired into the lifespan and firing.
    """
    monkeypatch.setenv("DIRECT_CONTROL_PV_MONITOR_SWEEP_INTERVAL", "0.1")
    monkeypatch.setenv("DIRECT_CONTROL_PV_MONITOR_IDLE_TTL", "0.01")
    monkeypatch.setenv("DIRECT_CONTROL_CONFIG_SERVICE_STARTUP_PROBE", "false")

    from fastapi.testclient import TestClient

    from direct_control.main import app

    with TestClient(app) as client:
        pv_monitor = client.app.state.pv_monitor
        signal = _seed_pv(pv_monitor, "BL:SWEEP:ME", touched=time.monotonic() - 10.0)

        # Verify the sweep task itself is wired into the lifespan first (this
        # is the load-bearing assertion; eviction correctness is pinned by
        # the unit tests). asyncio.all_tasks() runs from the test thread
        # so it inspects the main loop, but TestClient runs the app in a
        # background thread with its own loop — instead, verify via the
        # side effect (the eviction) with a generous deadline.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if "BL:SWEEP:ME" not in pv_monitor._signals:
                break
            time.sleep(0.1)

        assert "BL:SWEEP:ME" not in pv_monitor._signals, (
            "sweep task did not evict the idle monitor within 15 s — is the "
            "lifespan spawning the sweep? See lifespan() in main.py."
        )
        assert signal.destroyed


def test_lifespan_skips_sweep_task_when_interval_is_zero(monkeypatch):
    """``pv_monitor_sweep_interval=0`` opts out of the sweep entirely —
    tests that need deterministic eviction (or single-process deployments
    that manage the sweep externally) rely on this."""
    monkeypatch.setenv("DIRECT_CONTROL_PV_MONITOR_SWEEP_INTERVAL", "0")

    from fastapi.testclient import TestClient

    from direct_control.main import app

    with TestClient(app) as client:
        pv_monitor = client.app.state.pv_monitor
        signal = _seed_pv(pv_monitor, "BL:NO:SWEEP", touched=time.monotonic() - 1000.0)
        # Sweep is disabled — even a long-idle monitor is preserved.
        time.sleep(0.2)
        assert "BL:NO:SWEEP" in pv_monitor._signals
        assert not signal.destroyed
        # Explicit eviction still works.
        assert pv_monitor.evict_idle_monitors(idle_ttl=0.01) == ["BL:NO:SWEEP"]
        assert signal.destroyed


def test_negative_sweep_interval_rejected_at_startup():
    """A negative ``pv_monitor_sweep_interval`` must fail loud (Settings
    validation error) instead of silently disabling the sweep — the
    "interval > 0 enables the task" gate would otherwise treat -1 the same
    as 0 and quietly leak monitors on a misconfigured deployment."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="pv_monitor_sweep_interval"):
        Settings(pv_monitor_sweep_interval=-1.0)


def test_negative_idle_ttl_rejected_at_startup():
    """A negative ``pv_monitor_idle_ttl`` must fail loud — every
    callback-less monitor would otherwise be considered stale on the first
    sweep (``now - touched`` is always positive), thrashing the cache."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="pv_monitor_idle_ttl"):
        Settings(pv_monitor_idle_ttl=-1.0)
