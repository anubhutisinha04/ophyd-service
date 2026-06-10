"""End-to-end device-control tests against the caproto test IOC.

Exercises the full HTTP path — registry spec lookup → DeviceManager
instantiate + connect → framework driver → marshalled response — for BOTH
frameworks: ``tests.device_classes.ClassicPair`` (classic ophyd / pyepics)
and ``tests.device_classes.AsyncPair`` (ophyd-async / aioca), driven from a
file registry exactly as a standalone deployment would be.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def control_client(app, client, tmp_path):
    """TestClient whose controller reads specs from a real file registry.

    The conftest ``client`` fixture stubs the registry with a spec-less stub;
    here we swap in a FileRegistryProvider carrying instantiation specs for
    the IOC-backed test device classes.
    """
    from direct_control.registry_file import FileRegistryProvider

    registry = {
        "devices": [
            {
                "name": "classic_pair",
                "pvs": ["IOC:m1", "IOC:counter"],
                "device_class": "tests.device_classes.ClassicPair",
                "args": ["IOC:"],
                "framework": "ophyd-sync",
            },
            {
                "name": "async_pair",
                # PVs are unique per file entry; the classic device already
                # claims the IOC PVs and the async device shares them on the
                # wire via its ctor prefix.
                "pvs": [],
                "device_class": "tests.device_classes.AsyncPair",
                "args": ["IOC:"],
                "framework": "ophyd-async",
            },
            {
                "name": "mistagged",
                "pvs": [],
                "device_class": "tests.device_classes.AsyncPair",
                "args": ["IOC:"],
                "framework": "ophyd-sync",
            },
            {"name": "pv_only", "pvs": ["IOC:wf1"]},
        ]
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    app.state.device_controller.registry_client = FileRegistryProvider(str(path))
    return client


# ===== classic ophyd over CA =====


def test_execute_read_classic_device(control_client):
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "classic_pair", "method": "read"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "ophyd-sync" in body["message"]
    assert "classic_pair_m1" in body["result"]


def test_nested_set_then_read_classic(control_client):
    r = control_client.post(
        "/api/v1/device/classic_pair.m1",
        json={"method": "set", "value": 4.25, "timeout": 5.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    r = control_client.get("/api/v1/device/classic_pair.m1/value")
    assert r.status_code == 200, r.text
    value = r.json()["value"]
    assert value["classic_pair_m1"]["value"] == 4.25


def test_stop_classic_device(control_client):
    r = control_client.post("/api/v1/device/classic_pair/stop")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


# ===== ophyd-async over CA =====


def test_execute_read_async_device(control_client):
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "async_pair", "method": "read"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "ophyd-async" in body["message"]
    assert any(key.endswith("m1") for key in body["result"])


def test_nested_set_then_get_async(control_client):
    r = control_client.post(
        "/api/v1/device/async_pair.m1",
        json={"method": "set", "value": 7.75, "timeout": 5.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    r = control_client.post(
        "/api/v1/device/async_pair.m1",
        json={"method": "get", "timeout": 5.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == 7.75


def test_cross_framework_visibility(control_client):
    """A write through the async driver is visible through the classic one —
    both stacks talk to the same IOC PV."""
    r = control_client.post(
        "/api/v1/device/async_pair.m1",
        json={"method": "set", "value": 1.5, "timeout": 5.0},
    )
    assert r.status_code == 200, r.text

    r = control_client.post(
        "/api/v1/device/classic_pair.m1",
        json={"method": "get", "timeout": 5.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == 1.5


# ===== refusal paths =====


def test_execute_method_outside_allowlist_400(control_client):
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "classic_pair", "method": "destroy"},
    )
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"]


def test_execute_unsupported_method_on_device_400(control_client):
    # 'set' is an allowlisted verb but a plain compound Device doesn't move.
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "classic_pair", "method": "set", "args": [1.0]},
    )
    assert r.status_code == 400
    assert "does not support" in r.json()["detail"]


def test_stop_unsupported_on_async_pair_400(control_client):
    r = control_client.post("/api/v1/device/async_pair/stop")
    assert r.status_code == 400
    assert "does not support" in r.json()["detail"]


def test_execute_without_spec_422(control_client):
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "pv_only", "method": "read"},
    )
    assert r.status_code == 422
    assert "no instantiation spec" in r.json()["detail"]


def test_execute_mistagged_framework_500(control_client):
    r = control_client.post(
        "/api/v1/device/execute",
        json={"device_name": "mistagged", "method": "read"},
    )
    assert r.status_code == 500
    assert "Fix the registry" in r.json()["detail"]


def test_nested_unknown_component_404(control_client):
    r = control_client.post(
        "/api/v1/device/classic_pair.no_such_signal",
        json={"method": "read"},
    )
    assert r.status_code == 404
    assert "no component" in r.json()["detail"]
