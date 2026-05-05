"""
Coordination-gate tests: disabled-state and locked-state semantics.

direct_control reads device-lock state from configuration_service. When the
owning device is disabled (PATCH /devices/{name}/disable on config-service)
or locked (POST /devices/lock by EE/queueserver), commands must fail —
monitoring must not.

These tests stub the coordination client + registry client so the gate is
exercised without requiring a live configuration_service. The conftest
client fixture installs always-available stubs by default; here we
override the coordination client per-test to return DISABLED / LOCKED.
"""

from datetime import datetime
from typing import Optional

from direct_control.models import CoordinationStatus, DeviceLockStatus, ServiceAvailability


class _CoordStub:
    """Coordination client stub. Reports every device with the given status.

    AVAILABLE returns device_available=True; any other status returns False
    so the coord-gate fires.
    """

    def __init__(self, status: DeviceLockStatus, locked_by: Optional[str] = None):
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
    """Wire the given coordination client into the running app + controller."""
    app = client.app
    app.state.coordination_client = coord_client
    if hasattr(app.state, "device_controller"):
        app.state.device_controller.coordination = coord_client


# ─── disabled-state ─────────────────────────────────────────────────────


def test_set_pv_blocked_when_owning_device_disabled(client):
    """POST /api/v1/pv/set returns 409 with a clear DISABLED message."""
    _swap_coord(client, _CoordStub(DeviceLockStatus.DISABLED))

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 1.0, "wait": False},
    )
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"].lower()
    assert "configuration_service" in r.json()["detail"]


def test_pv_read_unaffected_by_disabled_owner(client):
    """GET /api/v1/pv/{name}/value succeeds even when owner is disabled."""
    _swap_coord(client, _CoordStub(DeviceLockStatus.DISABLED))

    # The read path doesn't go through the coord check at all; it reads
    # cached values + falls through to caget. With the test IOC up, this
    # returns 200 with a value.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.status_code == 200
    # No assertion on value — IOC defaults vary.


def test_device_method_blocked_when_disabled(client):
    """POST /api/v1/device/{name}/stop returns 409 when device is disabled."""
    _swap_coord(client, _CoordStub(DeviceLockStatus.DISABLED))

    r = client.post("/api/v1/device/some_device/stop")
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"].lower()


# ─── locked-state ───────────────────────────────────────────────────────


def test_set_pv_blocked_when_owning_device_locked(client):
    """POST /api/v1/pv/set returns 423 with the locking-plan name in the message."""
    _swap_coord(client, _CoordStub(DeviceLockStatus.LOCKED, locked_by="demo_plan"))

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 1.0, "wait": False},
    )
    assert r.status_code == 423
    assert "locked by plan demo_plan" in r.json()["detail"]


def test_disabled_distinct_from_locked_in_http_status(client):
    """Disabled is 409 (Conflict); locked is 423 (Locked). Distinct codes
    so callers can react differently (re-enable vs. wait/retry)."""
    _swap_coord(client, _CoordStub(DeviceLockStatus.DISABLED))
    r1 = client.post("/api/v1/pv/set", json={"pv_name": "IOC:m1", "value": 1.0})

    _swap_coord(client, _CoordStub(DeviceLockStatus.LOCKED, locked_by="demo_plan"))
    r2 = client.post("/api/v1/pv/set", json={"pv_name": "IOC:m1", "value": 1.0})

    assert r1.status_code == 409
    assert r2.status_code == 423
    assert r1.status_code != r2.status_code
