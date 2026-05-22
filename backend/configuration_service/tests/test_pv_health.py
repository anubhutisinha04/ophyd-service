"""Tests for PV health tracking — manager + endpoints + device-status rollup.

Exercises the state-machine transitions (healthy → degraded → unresponsive
→ back to healthy on success), the per-PV REST endpoints, and the
device-level health rollup that the frontend periodic-table UI reads.
"""

from __future__ import annotations

import pytest

from configuration_service.models import (
    PV_HEALTH_UNRESPONSIVE_THRESHOLD,
    PVHealthRecord,
    PVHealthState,
)
from configuration_service.pv_health_manager import PVHealthManager


# ---------------------------------------------------------------------------
# PVHealthManager unit tests — state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_pv_returns_none():
    """No record yet → caller treats as healthy."""
    mgr = PVHealthManager()
    assert await mgr.get_health("never:reported") is None


@pytest.mark.asyncio
async def test_first_failure_transitions_to_degraded():
    """One failure flips state from healthy → degraded."""
    mgr = PVHealthManager()
    record = await mgr.record_failure("IOC:m1", "timeout after 5s")
    assert record.consecutive_failures == 1
    assert record.state is PVHealthState.DEGRADED
    assert record.last_failure_message == "timeout after 5s"
    assert record.last_failure_at is not None
    assert record.last_success_at is None


@pytest.mark.asyncio
async def test_threshold_consecutive_failures_flips_to_unresponsive():
    """Reaching ``PV_HEALTH_UNRESPONSIVE_THRESHOLD`` consecutive failures
    classifies the PV as unresponsive."""
    mgr = PVHealthManager()
    for _ in range(PV_HEALTH_UNRESPONSIVE_THRESHOLD):
        record = await mgr.record_failure("IOC:m1")
    assert record.consecutive_failures == PV_HEALTH_UNRESPONSIVE_THRESHOLD
    assert record.state is PVHealthState.UNRESPONSIVE


@pytest.mark.asyncio
async def test_success_resets_state_to_healthy():
    """One success clears the counter regardless of how many failures preceded."""
    mgr = PVHealthManager()
    # Drive it deep into unresponsive territory.
    for _ in range(5):
        await mgr.record_failure("IOC:m1", "timeout")
    record = await mgr.record_success("IOC:m1")
    assert record.consecutive_failures == 0
    assert record.state is PVHealthState.HEALTHY
    # Last-failure timestamps preserved for diagnostics.
    assert record.last_failure_at is not None
    assert record.last_failure_message == "timeout"
    assert record.last_success_at is not None


@pytest.mark.asyncio
async def test_success_then_failure_starts_fresh_count():
    """After a success, the next failure resumes from 1, not from the
    pre-success count."""
    mgr = PVHealthManager()
    await mgr.record_failure("IOC:m1")
    await mgr.record_failure("IOC:m1")
    await mgr.record_success("IOC:m1")
    record = await mgr.record_failure("IOC:m1")
    assert record.consecutive_failures == 1
    assert record.state is PVHealthState.DEGRADED


@pytest.mark.asyncio
async def test_get_health_many_only_returns_pvs_with_records():
    """Bulk lookup returns only the PVs that have records; the caller
    treats absent PVs as healthy."""
    mgr = PVHealthManager()
    await mgr.record_failure("IOC:a")
    await mgr.record_success("IOC:b")
    result = await mgr.get_health_many(["IOC:a", "IOC:b", "IOC:never_reported"])
    assert set(result.keys()) == {"IOC:a", "IOC:b"}


@pytest.mark.asyncio
async def test_clear_drops_record():
    mgr = PVHealthManager()
    await mgr.record_failure("IOC:m1")
    assert await mgr.clear("IOC:m1") is True
    assert await mgr.get_health("IOC:m1") is None
    # Clear a never-reported PV — returns False, no exception.
    assert await mgr.clear("IOC:never") is False


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


def test_post_pv_failure_returns_record(client):
    r = client.post("/api/v1/pvs/IOC:m1/failure", json={"message": "timeout"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pv_name"] == "IOC:m1"
    assert body["consecutive_failures"] == 1
    assert body["state"] == "degraded"
    assert body["last_failure_message"] == "timeout"


def test_post_pv_success_resets_counter(client):
    # Drive into unresponsive territory.
    for _ in range(PV_HEALTH_UNRESPONSIVE_THRESHOLD):
        client.post("/api/v1/pvs/IOC:m1/failure", json={})
    r = client.post("/api/v1/pvs/IOC:m1/success", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["consecutive_failures"] == 0
    assert body["state"] == "healthy"


def test_post_pv_success_accepts_message_field_but_ignores_it(client):
    """Schema is symmetric so direct-control can POST the same body to
    either endpoint; success ignores the message field."""
    r = client.post("/api/v1/pvs/IOC:m1/success", json={"message": "ignored"})
    assert r.status_code == 200


def test_get_pv_health_unknown_pv_returns_404(client):
    r = client.get("/api/v1/pvs/IOC:never_reported/health")
    assert r.status_code == 404
    assert "treat as healthy" in r.json()["detail"]


def test_get_pv_health_returns_current_record(client):
    client.post("/api/v1/pvs/IOC:m1/failure", json={"message": "x"})
    client.post("/api/v1/pvs/IOC:m1/failure", json={"message": "y"})
    r = client.get("/api/v1/pvs/IOC:m1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["consecutive_failures"] == 2
    assert body["state"] == "degraded"
    # Latest failure message wins.
    assert body["last_failure_message"] == "y"


def test_extra_fields_on_report_rejected(client):
    """extra='forbid' catches typo'd request keys."""
    r = client.post(
        "/api/v1/pvs/IOC:m1/failure", json={"message": "ok", "extra": True}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Device-status response rollup
# ---------------------------------------------------------------------------


def test_device_status_includes_pv_health_for_reported_pvs(client):
    """The device-status response should roll up health for the PVs the
    device claims (via ``device.pvs``). PVs without records are absent
    from the dict — the frontend treats absence as healthy.
    """
    # sample_x's mock registry entry has user_readback, user_setpoint, velocity.
    # Report failure on one, success on another; the third stays absent.
    client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "lost CA"}
    )
    client.post("/api/v1/pvs/BL01:SAMPLE:X/success", json={})

    r = client.get("/api/v1/devices/sample_x/status")
    assert r.status_code == 200
    body = r.json()
    pv_health = body["pv_health"]
    assert set(pv_health.keys()) == {"BL01:SAMPLE:X.RBV", "BL01:SAMPLE:X"}
    assert pv_health["BL01:SAMPLE:X.RBV"]["state"] == "degraded"
    assert pv_health["BL01:SAMPLE:X"]["state"] == "healthy"
    # The velocity PV had no reports so it's omitted.
    assert "BL01:SAMPLE:X.VELO" not in pv_health


def test_device_status_with_no_health_reports_yields_empty_pv_health(client):
    """A freshly-started service has no records for anyone — the rollup
    is an empty dict, not missing or null."""
    r = client.get("/api/v1/devices/sample_x/status")
    assert r.status_code == 200
    assert r.json()["pv_health"] == {}


# ---------------------------------------------------------------------------
# Computed-field invariant on PVHealthRecord
# ---------------------------------------------------------------------------


def test_pv_health_record_state_is_computed_from_counter():
    """PVHealthRecord.state can't drift from consecutive_failures —
    it's a computed_field. Clients can't construct an inconsistent record."""
    r = PVHealthRecord(pv_name="x", consecutive_failures=0)
    assert r.state is PVHealthState.HEALTHY
    r = PVHealthRecord(pv_name="x", consecutive_failures=1)
    assert r.state is PVHealthState.DEGRADED
    r = PVHealthRecord(pv_name="x", consecutive_failures=PV_HEALTH_UNRESPONSIVE_THRESHOLD)
    assert r.state is PVHealthState.UNRESPONSIVE
