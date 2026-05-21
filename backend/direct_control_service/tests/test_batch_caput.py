"""Batch caput endpoint (``POST /api/v1/pv/set/batch``).

Exercises the happy path and the fail-hard halt-on-first-failure contract.
The registry-rejection tests use ``_RejectsBadPvRegistry`` via
``dependency_overrides`` since the default ``_StubRegistry`` accepts every
PV.
"""

from __future__ import annotations

import pytest

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
    """A registry rejection on the first item stops the batch.

    Uses a per-test registry stub that raises for ``BAD:pv`` so we exercise
    the 404 mapping inside the loop. The remaining items must not be
    attempted (no state change observable on IOC:m1).
    """
    from direct_control.main import get_registry_client

    # Seed m1 with a known value so we can prove it wasn't touched.
    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 0.0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200

    app.dependency_overrides[get_registry_client] = lambda: _RejectsBadPvRegistry()
    try:
        r = client.post(
            "/api/v1/pv/set/batch",
            json={
                "caputs": [
                    {"pv_name": "BAD:pv", "value": 1.0},
                    {"pv_name": "IOC:m1", "value": 99.0, "wait": True, "timeout": 2.0},
                ]
            },
        )
    finally:
        # Restore the default stub registry so other tests aren't affected.
        from tests.conftest import _StubRegistry

        app.dependency_overrides[get_registry_client] = lambda: _StubRegistry()

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

    # The second caput must not have run.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.json()["value"] == pytest.approx(0.0)


def test_batch_set_mid_failure_halts_after_first_success(app, client):
    """Item 1 succeeds, item 2 fails registry, item 3 is never attempted.

    Proves the response carries the successful first row *and* the failing
    second row, and that the third item is absent — so callers can tell
    exactly where the batch halted.
    """
    from direct_control.main import get_registry_client

    # Seed counter so we can verify it wasn't touched.
    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:counter", "value": 0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200

    app.dependency_overrides[get_registry_client] = lambda: _RejectsBadPvRegistry()
    try:
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
    finally:
        from tests.conftest import _StubRegistry

        app.dependency_overrides[get_registry_client] = lambda: _StubRegistry()

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
