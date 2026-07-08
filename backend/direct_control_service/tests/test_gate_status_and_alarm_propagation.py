"""Coordination-gate status handling and PV monitoring metadata propagation.

Covers two remediation items:

- A device whose coordination status is UNKNOWN (configuration_service
  returned a state we don't model) must be refused with 409 and, like the
  other gate refusals, must NEVER be reported as PV health — it's an
  orchestration-policy outcome, not an EPICS failure.
- The PV monitor must propagate EPICS alarm status/severity into PVUpdate,
  the pv-socket must actually subscribe read-only when asked, and a failed
  EPICS subscribe must produce a per-PV error envelope (with the client told
  ``subscribed`` only for the PVs that actually stuck) instead of a silent
  never-updating subscription.
"""

from __future__ import annotations

from datetime import datetime

from direct_control.config import Settings
from direct_control.models import (
    CoordinationStatus,
    DeviceLockStatus,
    PVNotFoundError,
    ServiceAvailability,
)
from direct_control.monitoring.websocket_manager import WebSocketManager
from direct_control.protocols import MockPVMonitor

# ===== Shared stubs =========================================================


class _CoordStub:
    """Coordination client stub reporting a fixed status for every device."""

    def __init__(self, status: DeviceLockStatus, locked_by: str | None = None):
        self.status = status
        self.locked_by = locked_by

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        return CoordinationStatus(
            device_available=(self.status == DeviceLockStatus.AVAILABLE),
            locked_by=self.locked_by,
            status=self.status,
            timestamp=datetime.now(),
        )

    async def is_service_available(self) -> ServiceAvailability:
        return ServiceAvailability(available=True)

    async def cleanup(self) -> None:
        return None


def _swap_coord(client, coord_client) -> None:
    app = client.app
    app.state.coordination_client = coord_client
    if hasattr(app.state, "device_controller"):
        app.state.device_controller.coordination = coord_client


class _HealthSpy:
    """Records every PV-health report() call so a test can assert none happen."""

    def __init__(self):
        self.calls: list[tuple[str, bool, str | None]] = []

    def report(self, pv_name: str, success: bool, message: str | None = None):
        self.calls.append((pv_name, success, message))
        return None

    async def drain(self, timeout: float = 5.0) -> None:
        return None


class _StubWS:
    async def send_json(self, *a, **k):
        return None

    async def close(self, *a, **k):
        return None


class _RecordingWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, *a, **k):
        return None


def _ws_manager(pv_monitor) -> WebSocketManager:
    return WebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=None,
        settings=Settings(),
        registry_client=None,
    )


# ===== UNKNOWN coordination status: 409, never a PV-health event ============


def test_set_pv_unknown_status_returns_409_not_pv_health(client):
    _swap_coord(client, _CoordStub(DeviceLockStatus.UNKNOWN))
    spy = _HealthSpy()
    client.app.state.pv_health_reporter = spy

    r = client.post("/api/v1/pv/set", json={"pv_name": "IOC:m1", "value": 1.0, "wait": False})

    # A device-unavailable refusal, not a service outage (503) and not an
    # EPICS execution failure (500) — and not reported as PV health.
    assert r.status_code == 409, r.text
    assert spy.calls == []


def test_batch_unknown_status_returns_409_row_not_pv_health(client):
    _swap_coord(client, _CoordStub(DeviceLockStatus.UNKNOWN))
    spy = _HealthSpy()
    client.app.state.pv_health_reporter = spy

    r = client.post(
        "/api/v1/pv/set/batch",
        json={"caputs": [{"pv_name": "IOC:m1", "value": 1.0}]},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["results"][0]["status_code"] == 409
    assert spy.calls == []


def test_device_method_unknown_status_returns_409(client):
    _swap_coord(client, _CoordStub(DeviceLockStatus.UNKNOWN))

    r = client.post("/api/v1/device/some_device/stop")
    assert r.status_code == 409, r.text


# ===== Alarm status/severity propagation ====================================


def _make_signal(status: int, severity: int, *, read: bool = True, write: bool = True):
    class _PV:
        read_access = read
        write_access = write

    _PV.status = status
    _PV.severity = severity

    class _Signal:
        _read_pv = _PV()
        timestamp = None

    return _Signal()


def test_value_update_propagates_alarm_severity_and_status():
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())
    monitor._signals["X:PV"] = _make_signal(status=3, severity=1)  # HIHI / MINOR

    captured: list = []
    monitor._callbacks["X:PV"].append(pvm._Subscriber(lambda u: captured.append(u), None))

    monitor._handle_value_update("X:PV", 1.23, None)

    assert len(captured) == 1
    upd = captured[0]
    assert upd.status == 3
    assert upd.severity == 1
    assert upd.alarm_severity == 1
    assert upd.alarm_severity_name == "MINOR"
    assert upd.alarm_status == "HIHI"
    # The cached PVValue carries the raw ints too (pre-fix: always 0/0).
    cached = monitor._latest_values["X:PV"]
    assert cached.status == 3
    assert cached.severity == 1


def test_value_update_no_alarm_reports_no_alarm():
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())
    monitor._signals["Y:PV"] = _make_signal(status=0, severity=0)

    captured: list = []
    monitor._callbacks["Y:PV"].append(pvm._Subscriber(lambda u: captured.append(u), None))
    monitor._handle_value_update("Y:PV", 5, None)

    upd = captured[0]
    assert upd.severity == 0
    assert upd.alarm_severity_name == "NO_ALARM"
    assert upd.alarm_status == "NO_ALARM"


def test_pvupdate_from_value_carries_alarm_names():
    from direct_control.models import PVUpdate, PVValue

    v = PVValue(pv_name="Z:PV", value=1, timestamp=datetime.now(), status=5, severity=2)
    upd = PVUpdate.from_value(v)

    # The initial-subscribe snapshot must carry the same friendly alarm fields
    # the streaming updates do (status 5 == LOLO, severity 2 == MAJOR).
    assert upd.severity == 2
    assert upd.alarm_severity == 2
    assert upd.alarm_severity_name == "MAJOR"
    assert upd.alarm_status == "LOLO"


def test_signal_to_pv_value_extracts_alarm_from_cached_pv():
    import direct_control.monitoring.pv_monitor as pvm

    monitor = pvm.PVMonitorManager(Settings())

    class _PV:
        read_access = True
        write_access = True
        units = None
        precision = None
        enum_strs = None
        lower_ctrl_limit = None
        upper_ctrl_limit = None
        lower_disp_limit = None
        upper_disp_limit = None
        status = 3  # HIHI
        severity = 2  # MAJOR

    class _Signal:
        _read_pv = _PV()
        connected = True
        timestamp = None

        def get(self):
            return 4.2

    # The initial snapshot cached at subscribe time must reflect the PV's real
    # alarm state, not a hardcoded 0/0 that would report NO_ALARM until the
    # first monitor callback.
    pv_value = monitor._signal_to_pv_value("W:PV", _Signal())
    assert pv_value.status == 3
    assert pv_value.severity == 2


# ===== read-only subscription actually subscribes read-only =================


async def test_subscribe_read_only_plumbs_flag_to_pv_monitor():
    calls: list[tuple[str, bool]] = []

    class _RecordingMonitor(MockPVMonitor):
        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            calls.append((pv_name, read_only))
            return super().subscribe(pv_name, callback, read_only, on_error)

    manager = _ws_manager(_RecordingMonitor())
    manager._connections = {"c1": _StubWS()}
    manager._subscriptions = {"c1": set()}

    failed = await manager.subscribe_pvs("c1", ["X:PV"], read_only=True)

    assert failed == []
    assert calls == [("X:PV", True)]


async def test_subscribe_default_is_not_read_only():
    calls: list[tuple[str, bool]] = []

    class _RecordingMonitor(MockPVMonitor):
        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            calls.append((pv_name, read_only))
            return super().subscribe(pv_name, callback, read_only, on_error)

    manager = _ws_manager(_RecordingMonitor())
    manager._connections = {"c1": _StubWS()}
    manager._subscriptions = {"c1": set()}

    await manager.subscribe_pvs("c1", ["X:PV"])
    assert calls == [("X:PV", False)]


# ===== failed subscribe → per-PV error envelope, partial "subscribed" =======


async def test_subscribe_pvs_returns_per_pv_failures():
    class _PartialFailMonitor(MockPVMonitor):
        def subscribe(self, pv_name, callback=None, read_only=False, on_error=None):
            if pv_name == "BAD:PV":
                raise PVNotFoundError(f"PV {pv_name} connection timeout")
            return super().subscribe(pv_name, callback, read_only, on_error)

    manager = _ws_manager(_PartialFailMonitor())
    manager._connections = {"c1": _StubWS()}
    manager._subscriptions = {"c1": set()}

    failed = await manager.subscribe_pvs("c1", ["GOOD:PV", "BAD:PV"])

    assert [pv for pv, _ in failed] == ["BAD:PV"]
    # The good PV stuck; the failed one was rolled back everywhere.
    assert "GOOD:PV" in manager._subscriptions["c1"]
    assert "BAD:PV" not in manager._subscriptions["c1"]
    assert "BAD:PV" not in manager._pv_clients


async def test_confirm_pv_subscribe_errors_failed_and_confirms_only_stuck():
    manager = _ws_manager(MockPVMonitor())
    ws = _RecordingWS()

    await manager._confirm_pv_subscribe(ws, ["A:PV", "B:PV"], [("B:PV", "boom")], read_only=True)

    errors = [m for m in ws.sent if m["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["pv"] == "B:PV"
    assert errors[0]["reason"] == "pv_subscribe_failed"

    subscribed = [m for m in ws.sent if m["type"] == "subscribed"]
    assert len(subscribed) == 1
    assert subscribed[0]["pv_names"] == ["A:PV"]
    assert subscribed[0]["read_only"] is True


async def test_confirm_pv_subscribe_all_failed_sends_no_subscribed():
    manager = _ws_manager(MockPVMonitor())
    ws = _RecordingWS()

    await manager._confirm_pv_subscribe(ws, ["A:PV"], [("A:PV", "boom")])

    assert not any(m["type"] == "subscribed" for m in ws.sent)
    assert any(m["type"] == "error" and m["pv"] == "A:PV" for m in ws.sent)


async def test_subscribe_pvs_unknown_client_reports_all_failed():
    # Client gone (e.g. disconnected at shutdown) before subscribe_pvs took the
    # lock: every requested PV must come back as failed so the caller never
    # confirms `subscribed` for a connection that no longer exists.
    manager = _ws_manager(MockPVMonitor())
    manager._connections = {}
    manager._subscriptions = {}

    failed = await manager.subscribe_pvs("gone", ["A:PV", "B:PV"])

    assert [pv for pv, _ in failed] == ["A:PV", "B:PV"]
