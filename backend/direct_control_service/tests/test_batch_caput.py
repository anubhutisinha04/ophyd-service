"""Batch caput endpoint (``POST /api/v1/pv/set/batch``).

Exercises the happy path and every documented halt path (registry rejection,
device locked/disabled, coordination failure, soft failure from set_pv).
Per-test ``dependency_overrides`` are auto-cleaned by the ``app`` fixture's
teardown, so no manual restore is needed.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from direct_control.models import (
    CommandMode,
    CoordinationCheckError,
    DeviceDisabledError,
    DeviceLockedError,
    PVSetResponse,
)
from direct_control.registry_client import RegistryValidationError


class _RejectsBadPvRegistry:
    """Registry stub that rejects ``BAD:pv`` and accepts everything else."""

    async def validate_pv(self, pv_name: str):
        if pv_name == "BAD:pv":
            raise RegistryValidationError(pv_name, "PV")
        return None

    async def validate_device(self, device_name: str):
        return None

    async def get_owning_device(self, pv_name: str):
        return None

    async def cleanup(self):
        return None


class _ControllerRaises:
    """Stub device controller whose ``set_pv`` raises a fixed exception.

    Used for the device-state failure-mode tests (locked / disabled /
    coordination-error / generic) — direct_control_controller raises these
    exceptions when the coord-gate refuses or the underlying EPICS write
    fails in a typed way.
    """

    def __init__(self, exc: Exception):
        self._exc = exc

    async def set_pv(self, request):
        raise self._exc


class _ControllerSoftFails:
    """Stub controller whose ``set_pv`` returns ``PVSetResponse(success=False)``.

    Models the case where the controller didn't raise but the write was
    rejected (e.g. put-completion timeout without an exception). The batch
    loop must still halt — that's the contract.
    """

    async def set_pv(self, request):
        return PVSetResponse(
            pv_name=request.pv_name,
            success=False,
            value_set=request.value,
            timestamp=datetime.now(),
            coordination_checked=True,
            mode=CommandMode.PUT_COMPLETION,
            message="simulated soft failure",
        )


def test_batch_set_all_succeed(client):
    """Multiple caputs against IOC PVs round-trip and report ok=true."""
    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 1.5, "wait": True, "timeout": 2.0},
                {"pv_name": "IOC:counter", "value": 7, "wait": True, "timeout": 2.0},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["applied"] == 2
    assert body["requested"] == 2
    assert len(body["results"]) == 2
    assert all(item["success"] for item in body["results"])
    assert [item["pv_name"] for item in body["results"]] == ["IOC:m1", "IOC:counter"]

    # Confirm both writes actually landed.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == pytest.approx(1.5)
    r = client.get("/api/v1/pv/IOC:counter/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == 7


def test_batch_set_single_item(client):
    """A one-item batch behaves like a single /pv/set."""
    r = client.post(
        "/api/v1/pv/set/batch",
        json={"caputs": [{"pv_name": "IOC:m1", "value": 2.71, "wait": True, "timeout": 2.0}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["applied"] == 1
    assert body["requested"] == 1
    assert body["results"][0]["success"] is True


def test_batch_set_empty_rejected(client):
    """Empty caputs list is a pydantic validation error (422)."""
    r = client.post("/api/v1/pv/set/batch", json={"caputs": []})
    assert r.status_code == 422


def test_batch_set_extra_field_rejected(client):
    """extra='forbid' on the request rejects unknown keys (typo-catching)."""
    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [{"pv_name": "IOC:m1", "value": 1.0}],
            "fail_hard": True,  # not a real field
        },
    )
    assert r.status_code == 422


def test_batch_set_unknown_pv_halts_immediately(app, client):
    """A registry rejection on the first item stops the batch."""
    from direct_control.main import get_registry_client

    # Seed m1 with a known value so we can prove it wasn't touched.
    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 0.0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200

    app.dependency_overrides[get_registry_client] = lambda: _RejectsBadPvRegistry()
    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "BAD:pv", "value": 1.0},
                {"pv_name": "IOC:m1", "value": 99.0, "wait": True, "timeout": 2.0},
            ]
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 0
    assert body["requested"] == 2
    assert len(body["results"]) == 1  # halted before the second item
    failure = body["results"][0]
    assert failure["pv_name"] == "BAD:pv"
    assert failure["success"] is False
    assert failure["status_code"] == 404
    assert failure["error_type"] == "RegistryValidationError"
    # Registry rejected before coord gate runs.
    assert failure["coordination_checked"] is False

    # The second caput must not have run.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == pytest.approx(0.0)


def test_batch_set_mid_failure_halts_after_first_success(app, client):
    """Item 1 succeeds, item 2 fails registry, item 3 is never attempted."""
    from direct_control.main import get_registry_client

    # Seed counter so we can verify it wasn't touched.
    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:counter", "value": 0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200

    app.dependency_overrides[get_registry_client] = lambda: _RejectsBadPvRegistry()
    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 4.2, "wait": True, "timeout": 2.0},
                {"pv_name": "BAD:pv", "value": 1.0},
                {"pv_name": "IOC:counter", "value": 42, "wait": True, "timeout": 2.0},
            ]
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 1
    assert body["requested"] == 3
    assert len(body["results"]) == 2
    assert body["results"][0]["pv_name"] == "IOC:m1"
    assert body["results"][0]["success"] is True
    assert body["results"][0]["status_code"] == 200
    assert body["results"][1]["pv_name"] == "BAD:pv"
    assert body["results"][1]["success"] is False
    assert body["results"][1]["status_code"] == 404

    # The first caput should have landed; the third must not have.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == pytest.approx(4.2)
    r = client.get("/api/v1/pv/IOC:counter/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == 0  # untouched


def test_batch_set_device_locked_halts(app, client):
    """DeviceLockedError → 423; coord gate ran (so coordination_checked=True)."""
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerRaises(
        DeviceLockedError("IOC:m1 locked by plan_xyz")
    )

    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 1.0},
                {"pv_name": "IOC:counter", "value": 5},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 0
    assert body["requested"] == 2
    assert len(body["results"]) == 1
    failure = body["results"][0]
    assert failure["pv_name"] == "IOC:m1"
    assert failure["status_code"] == 423
    assert failure["error_type"] == "DeviceLockedError"
    assert failure["coordination_checked"] is True


def test_batch_set_device_disabled_halts(app, client):
    """DeviceDisabledError → 409."""
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerRaises(
        DeviceDisabledError("IOC:m1 administratively disabled")
    )

    r = client.post(
        "/api/v1/pv/set/batch",
        json={"caputs": [{"pv_name": "IOC:m1", "value": 1.0}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 0
    assert len(body["results"]) == 1
    failure = body["results"][0]
    assert failure["status_code"] == 409
    assert failure["error_type"] == "DeviceDisabledError"
    assert failure["coordination_checked"] is True


def test_batch_set_coordination_error_halts(app, client):
    """CoordinationCheckError → 503; coord gate attempted (so True)."""
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerRaises(
        CoordinationCheckError("configuration_service unreachable")
    )

    r = client.post(
        "/api/v1/pv/set/batch",
        json={"caputs": [{"pv_name": "IOC:m1", "value": 1.0}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert len(body["results"]) == 1
    failure = body["results"][0]
    assert failure["status_code"] == 503
    assert failure["error_type"] == "CoordinationCheckError"
    assert failure["coordination_checked"] is True


def test_batch_set_soft_failure_halts(app, client):
    """set_pv returning success=False (no exception) still halts the batch.

    The success-row gets appended with status_code=200 (the call itself
    returned 200; success=False carries the operational outcome), and the
    next item is not attempted.
    """
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerSoftFails()

    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 1.0},
                {"pv_name": "IOC:counter", "value": 2},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 0
    assert body["requested"] == 2
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["pv_name"] == "IOC:m1"
    assert row["success"] is False
    assert row["status_code"] == 200
    assert row["message"] == "simulated soft failure"


def test_batch_set_max_length_enforced(client):
    """101-item batch is rejected by the pydantic max_length guard."""
    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:counter", "value": i} for i in range(101)
            ]
        },
    )
    assert r.status_code == 422
