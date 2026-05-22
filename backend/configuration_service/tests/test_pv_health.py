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
    record = await mgr.record_failure("BL01:SAMPLE:X.RBV", "timeout after 5s")
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
        record = await mgr.record_failure("BL01:SAMPLE:X.RBV")
    assert record.consecutive_failures == PV_HEALTH_UNRESPONSIVE_THRESHOLD
    assert record.state is PVHealthState.UNRESPONSIVE


@pytest.mark.asyncio
async def test_success_resets_state_to_healthy():
    """One success clears the counter regardless of how many failures preceded."""
    mgr = PVHealthManager()
    # Drive it deep into unresponsive territory.
    for _ in range(5):
        await mgr.record_failure("BL01:SAMPLE:X.RBV", "timeout")
    record = await mgr.record_success("BL01:SAMPLE:X.RBV")
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
    await mgr.record_failure("BL01:SAMPLE:X.RBV")
    await mgr.record_failure("BL01:SAMPLE:X.RBV")
    await mgr.record_success("BL01:SAMPLE:X.RBV")
    record = await mgr.record_failure("BL01:SAMPLE:X.RBV")
    assert record.consecutive_failures == 1
    assert record.state is PVHealthState.DEGRADED


@pytest.mark.asyncio
async def test_record_success_on_never_failed_pv_does_not_grow_dict():
    """A success on a PV that's never failed returns a synthetic healthy
    record but does NOT add to the internal dict. Otherwise ``_records``
    would grow to every PV ever caput'd, not just the ones that have
    failed.
    """
    mgr = PVHealthManager()
    record = await mgr.record_success("IOC:never_failed")
    assert record.state is PVHealthState.HEALTHY
    assert record.consecutive_failures == 0
    assert record.last_success_at is not None
    # No record is stored — the bulk lookup confirms it's not there.
    assert await mgr.get_health("IOC:never_failed") is None
    rollup = await mgr.get_health_many(["IOC:never_failed"])
    assert rollup == {}


@pytest.mark.asyncio
async def test_record_success_after_failure_is_stored():
    """The first success on a PV that HAS failed must persist so the
    operator UI can still see 'recovered at <time>' diagnostics."""
    mgr = PVHealthManager()
    await mgr.record_failure("BL01:SAMPLE:X.RBV", "timeout")
    record = await mgr.record_success("BL01:SAMPLE:X.RBV")
    assert record.state is PVHealthState.HEALTHY
    # The record IS stored — last-failure metadata preserved.
    persisted = await mgr.get_health("BL01:SAMPLE:X.RBV")
    assert persisted is not None
    assert persisted.last_failure_message == "timeout"
    assert persisted.last_success_at is not None


@pytest.mark.asyncio
async def test_get_health_many_only_returns_pvs_with_records():
    """Bulk lookup returns only the PVs with stored records. Successes
    on never-failed PVs are intentionally not stored (see
    ``record_success`` docstring), so ``IOC:b`` below is absent."""
    mgr = PVHealthManager()
    await mgr.record_failure("IOC:a")
    await mgr.record_success("IOC:b")  # synthetic; not stored
    await mgr.record_failure("IOC:c")
    await mgr.record_success("IOC:c")  # updates the existing record
    result = await mgr.get_health_many(
        ["IOC:a", "IOC:b", "IOC:c", "IOC:never_reported"]
    )
    assert set(result.keys()) == {"IOC:a", "IOC:c"}


@pytest.mark.asyncio
async def test_clear_drops_record():
    mgr = PVHealthManager()
    await mgr.record_failure("BL01:SAMPLE:X.RBV")
    assert await mgr.clear("BL01:SAMPLE:X.RBV") is True
    assert await mgr.get_health("BL01:SAMPLE:X.RBV") is None
    # Clear a never-reported PV — returns False, no exception.
    assert await mgr.clear("IOC:never") is False


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


def test_post_pv_failure_returns_record(client):
    r = client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "timeout"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pv_name"] == "BL01:SAMPLE:X.RBV"
    assert body["consecutive_failures"] == 1
    assert body["state"] == "degraded"
    assert body["last_failure_message"] == "timeout"


def test_post_pv_success_resets_counter(client):
    # Drive into unresponsive territory.
    for _ in range(PV_HEALTH_UNRESPONSIVE_THRESHOLD):
        client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={})
    r = client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/success", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["consecutive_failures"] == 0
    assert body["state"] == "healthy"


def test_post_pv_success_accepts_message_field_but_ignores_it(client):
    """Schema is symmetric so direct-control can POST the same body to
    either endpoint; success ignores the message field."""
    r = client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/success", json={"message": "ignored"})
    assert r.status_code == 200


def test_get_pv_health_unknown_pv_returns_404(client):
    r = client.get("/api/v1/pvs/IOC:never_reported/health")
    assert r.status_code == 404
    assert "treat as healthy" in r.json()["detail"]


def test_get_pv_health_returns_current_record(client):
    client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "x"})
    client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "y"})
    r = client.get("/api/v1/pvs/BL01:SAMPLE:X.RBV/health")
    assert r.status_code == 200
    body = r.json()
    assert body["consecutive_failures"] == 2
    assert body["state"] == "degraded"
    # Latest failure message wins.
    assert body["last_failure_message"] == "y"


def test_extra_fields_on_report_rejected(client):
    """extra='forbid' catches typo'd request keys."""
    r = client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "ok", "extra": True}
    )
    assert r.status_code == 422


def test_post_failure_unknown_pv_returns_404(client):
    """Health-report endpoints reject PVs not in the registry — otherwise
    a typo'd report (or an untrusted caller) could grow the health store
    unbounded with garbage entries. Matches direct-control's caput path,
    which validates pv_name against the registry before writing."""
    r = client.post(
        "/api/v1/pvs/IOC:not_registered/failure", json={"message": "nope"}
    )
    assert r.status_code == 404
    assert "not registered" in r.json()["detail"]


def test_post_success_unknown_pv_returns_404(client):
    r = client.post("/api/v1/pvs/IOC:not_registered/success", json={})
    assert r.status_code == 404
    assert "not registered" in r.json()["detail"]


def test_dotted_pv_names_route_correctly(client):
    """EpicsMotor's ``.RBV`` / ``.VAL`` / ``.VELO`` PVs all contain dots,
    and most beamline PVs use colons and braces as well. Starlette's
    default ``{param}`` converter actually does match dots (only slashes
    require ``:path``), so this test isn't strictly required to defend
    against a converter change — but it pins the end-to-end routing +
    record-roundtrip for a realistic motor-record PV name shape so the
    next person who touches the URL grammar gets an immediate failure
    instead of a Postman-only discovery."""
    r = client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure",
        json={"message": "lost CA"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pv_name"] == "BL01:SAMPLE:X.RBV"
    assert body["consecutive_failures"] == 1

    # Round-trip through the GET endpoint too.
    r = client.get("/api/v1/pvs/BL01:SAMPLE:X.RBV/health")
    assert r.status_code == 200
    assert r.json()["pv_name"] == "BL01:SAMPLE:X.RBV"

    # And success after failure.
    r = client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/success", json={})
    assert r.status_code == 200
    assert r.json()["state"] == "healthy"


# ---------------------------------------------------------------------------
# Device-status response rollup
# ---------------------------------------------------------------------------


def test_device_status_includes_pv_health_for_reported_pvs(client):
    """The device-status response should roll up health for the PVs the
    device claims (via ``device.pvs``). The rollup only includes PVs
    that have failed at some point — successes on never-failed PVs are
    not stored, by design (see ``PVHealthManager.record_success``).
    """
    # sample_x's mock registry entry has user_readback, user_setpoint, velocity.
    # Report failure on one (creates a record), then a success on the
    # *same* PV (updates the existing record back to healthy). Also report
    # a success on a never-failed PV — that one should stay absent from
    # the rollup.
    client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "lost CA"}
    )
    # A success on a never-failed PV is intentionally not persisted.
    client.post("/api/v1/pvs/BL01:SAMPLE:X/success", json={})

    r = client.get("/api/v1/devices/sample_x/status")
    assert r.status_code == 200
    body = r.json()
    pv_health = body["pv_health"]
    # Only the failed-then-not-yet-recovered PV appears.
    assert set(pv_health.keys()) == {"BL01:SAMPLE:X.RBV"}
    assert pv_health["BL01:SAMPLE:X.RBV"]["state"] == "degraded"


# ---------------------------------------------------------------------------
# Admin endpoints (clear single, clear all, stats)
# ---------------------------------------------------------------------------


def test_delete_pv_health_removes_record(client):
    """Clearing a PV with a record returns cleared=1; the next GET 404s."""
    client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "ca timeout"}
    )
    r = client.delete("/api/v1/pvs/BL01:SAMPLE:X.RBV/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"cleared": 1}

    # Record is gone.
    r = client.get("/api/v1/pvs/BL01:SAMPLE:X.RBV/health")
    assert r.status_code == 404


def test_delete_pv_health_is_idempotent(client):
    """Clearing a PV with no record returns cleared=0 (not 404). Lets the
    operator UI fire-and-forget without checking existence first."""
    r = client.delete("/api/v1/pvs/BL01:SAMPLE:X.RBV/health")
    assert r.status_code == 200
    assert r.json() == {"cleared": 0}


def test_delete_pv_health_dotted_pv_name(client):
    """Same routing concern as the POST endpoints — make sure the
    delete route accepts realistic motor-record PV names."""
    client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.VELO/failure", json={"message": "x"}
    )
    r = client.delete("/api/v1/pvs/BL01:SAMPLE:X.VELO/health")
    assert r.status_code == 200
    assert r.json() == {"cleared": 1}


def test_delete_all_pv_health_returns_count(client):
    """Bulk clear returns the count of removed records."""
    client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "a"})
    client.post("/api/v1/pvs/BL01:SAMPLE:X/failure", json={"message": "b"})
    client.post("/api/v1/pvs/BL01:DET1:CNT/failure", json={"message": "c"})

    r = client.delete("/api/v1/admin/pv-health")
    assert r.status_code == 200, r.text
    assert r.json() == {"cleared": 3}

    # All gone — a follow-up clear-all returns 0.
    r = client.delete("/api/v1/admin/pv-health")
    assert r.json() == {"cleared": 0}


def test_pv_health_stats_empty(client):
    """Empty registry returns 0 counts for every state, not missing keys.

    Frontends rely on the per-state keys always being present so they
    don't have to special-case healthy=missing vs. healthy=0.
    """
    r = client.get("/api/v1/admin/pv-health/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracked_pvs"] == 0
    assert body["by_state"] == {
        "healthy": 0,
        "degraded": 0,
        "unresponsive": 0,
    }


def test_pv_health_stats_counts_by_state(client):
    """Stats include all three states and the total matches the sum.

    Uses ``PV_HEALTH_UNRESPONSIVE_THRESHOLD`` rather than hardcoding 3
    so a future tuning of the threshold doesn't silently shift this
    test into verifying ``degraded`` instead of ``unresponsive``.
    """
    # One degraded (1 failure).
    client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={})
    # One unresponsive (threshold-many consecutive failures).
    for _ in range(PV_HEALTH_UNRESPONSIVE_THRESHOLD):
        client.post("/api/v1/pvs/BL01:SAMPLE:X/failure", json={})
    # One previously-failed but now recovered (state=healthy).
    client.post("/api/v1/pvs/BL01:DET1:CNT/failure", json={})
    client.post("/api/v1/pvs/BL01:DET1:CNT/success", json={})

    r = client.get("/api/v1/admin/pv-health/stats")
    body = r.json()
    assert body["tracked_pvs"] == 3
    assert body["by_state"] == {
        "healthy": 1,
        "degraded": 1,
        "unresponsive": 1,
    }


def test_delete_pv_health_unregistered_pv_returns_cleared_zero(client):
    """Unlike POST /failure and POST /success, the DELETE endpoint does
    NOT gate on registry membership — it's idempotent for any string.
    Confirms unregistered PVs return 200 with cleared=0 (not 404), so
    operator UIs don't have to check existence first."""
    r = client.delete("/api/v1/pvs/IOC:not_registered/health")
    assert r.status_code == 200
    assert r.json() == {"cleared": 0}


def test_device_status_pv_health_after_failure_then_recovery(client):
    """A failure then a success on the same PV keeps the record (so the
    UI can show 'recovered at <time>') with state flipped to healthy."""
    client.post(
        "/api/v1/pvs/BL01:SAMPLE:X.RBV/failure", json={"message": "lost CA"}
    )
    client.post("/api/v1/pvs/BL01:SAMPLE:X.RBV/success", json={})

    r = client.get("/api/v1/devices/sample_x/status")
    pv_health = r.json()["pv_health"]
    assert "BL01:SAMPLE:X.RBV" in pv_health
    row = pv_health["BL01:SAMPLE:X.RBV"]
    assert row["state"] == "healthy"
    assert row["last_failure_message"] == "lost CA"
    assert row["last_success_at"] is not None


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
