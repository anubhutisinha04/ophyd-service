"""Integration tests for ``POST /api/v1/devices/resolve``.

Exercises the full endpoint path: pydantic validation, registry lookup,
prefix derivation, framework dispatch (classic ophyd + ophyd-async), and
the per-item result envelope. Uses the ``client`` fixture which seeds the
mock registry (``sample_x`` EpicsMotor, ``det1`` EpicsScaler, ``cam1``
SimDetector). An ophyd-async device is added via the CRUD POST so both
frameworks are covered end-to-end.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Classic ophyd (mock registry's EpicsMotor / EpicsScaler / SimDetector)
# ---------------------------------------------------------------------------


def test_resolve_classic_motor_setpoint(client):
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": ["sample_x.user_setpoint"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["resolved"]) == 1
    row = body["resolved"][0]
    assert row["ok"] is True
    assert row["outcome"] == "resolved"
    assert row["pv_name"] == "BL01:SAMPLE:X.VAL"


def test_resolve_classic_motor_multiple_attrs_one_batch(client):
    r = client.post(
        "/api/v1/devices/resolve",
        json={
            "addresses": [
                "sample_x.user_setpoint",
                "sample_x.user_readback",
                "sample_x.velocity",
            ]
        },
    )
    assert r.status_code == 200, r.text
    rows = r.json()["resolved"]
    assert [row["pv_name"] for row in rows] == [
        "BL01:SAMPLE:X.VAL",
        "BL01:SAMPLE:X.RBV",
        "BL01:SAMPLE:X.VELO",
    ]
    assert all(row["ok"] for row in rows)


def test_resolve_unknown_device_returns_device_not_found(client):
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": ["does_not_exist.foo"]},
    )
    assert r.status_code == 200
    row = r.json()["resolved"][0]
    assert row["ok"] is False
    assert row["outcome"] == "device_not_found"
    assert "does_not_exist" in row["message"]


def test_resolve_unknown_attr_returns_no_such_attr(client):
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": ["sample_x.does_not_exist"]},
    )
    assert r.status_code == 200
    row = r.json()["resolved"][0]
    assert row["ok"] is False
    assert row["outcome"] == "no_such_attr"
    assert "does_not_exist" in row["message"]


def test_resolve_mixed_success_and_failure_in_one_batch(client):
    """Best-effort per-item: failures don't halt the batch."""
    r = client.post(
        "/api/v1/devices/resolve",
        json={
            "addresses": [
                "sample_x.user_setpoint",  # ok
                "ghost.foo",  # device_not_found
                "sample_x.velocity",  # ok
                "sample_x.bad_attr",  # no_such_attr
            ]
        },
    )
    assert r.status_code == 200
    rows = r.json()["resolved"]
    assert [row["ok"] for row in rows] == [True, False, True, False]
    assert [row["outcome"] for row in rows] == [
        "resolved",
        "device_not_found",
        "resolved",
        "no_such_attr",
    ]


def test_resolve_empty_list_rejected(client):
    """Empty addresses list is a pydantic validation error (422)."""
    r = client.post("/api/v1/devices/resolve", json={"addresses": []})
    assert r.status_code == 422


def test_resolve_max_length_enforced(client):
    """201-address request is rejected by the pydantic max_length guard."""
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": [f"x{i}" for i in range(201)]},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ophyd-async (CRUD-add a Motor entry, then resolve through it)
# ---------------------------------------------------------------------------


@pytest.fixture
def async_motor_in_registry(client):
    """POST an ophyd-async Motor entry to the registry for the resolve tests."""
    payload = {
        "metadata": {
            "name": "async_motor",
            "device_label": "motor",
            "ophyd_class": "Motor",
            "is_movable": True,
            "is_readable": True,
            "pvs": {},  # populated by the loader for classic devices only
            "labels": ["motors"],
        },
        "instantiation_spec": {
            "name": "async_motor",
            "device_class": "ophyd_async.epics.motor.Motor",
            "args": ["BL01:ASYNC:MOT"],
            "kwargs": {"name": "async_motor"},
            "active": True,
        },
    }
    r = client.post("/api/v1/devices", json=payload)
    assert r.status_code in (200, 201), r.text
    yield "async_motor"


def test_resolve_ophyd_async_motor_setpoint(client, async_motor_in_registry):
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": ["async_motor.user_setpoint"]},
    )
    assert r.status_code == 200, r.text
    row = r.json()["resolved"][0]
    assert row["ok"] is True, row
    assert row["pv_name"] == "BL01:ASYNC:MOT.VAL"


def test_resolve_ophyd_async_motor_readback(client, async_motor_in_registry):
    r = client.post(
        "/api/v1/devices/resolve",
        json={"addresses": ["async_motor.user_readback"]},
    )
    assert r.status_code == 200, r.text
    row = r.json()["resolved"][0]
    assert row["ok"] is True, row
    assert row["pv_name"] == "BL01:ASYNC:MOT.RBV"


def test_resolve_classic_and_async_in_one_batch(client, async_motor_in_registry):
    """One batch can carry addresses from both frameworks; the resolver
    dispatches per-item based on the underlying class."""
    r = client.post(
        "/api/v1/devices/resolve",
        json={
            "addresses": [
                "sample_x.user_setpoint",  # classic ophyd
                "async_motor.user_setpoint",  # ophyd-async
            ]
        },
    )
    assert r.status_code == 200
    rows = r.json()["resolved"]
    assert all(row["ok"] for row in rows), rows
    assert rows[0]["pv_name"] == "BL01:SAMPLE:X.VAL"
    assert rows[1]["pv_name"] == "BL01:ASYNC:MOT.VAL"
