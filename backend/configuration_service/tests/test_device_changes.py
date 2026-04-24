"""
Tests for GET /api/v1/devices/changes (registry delta endpoint).

Covers the cursor advance story that bluesky-queueserver relies on to keep
its local device instances in sync with the service without a full refetch.
"""

import pytest
from fastapi.testclient import TestClient

from configuration_service.main import create_app
from configuration_service.config import Settings


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "changes.db"


@pytest.fixture
def client(tmp_db):
    settings = Settings(
        use_mock_data=True,
        db_path=tmp_db,
        device_change_history_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _create_device(client: TestClient, name: str, prefix: str = "XF:01-Mtr{M1}") -> None:
    resp = client.post(
        "/api/v1/devices",
        json={
            "metadata": {
                "name": name,
                "device_label": "motor",
                "ophyd_class": "EpicsMotor",
            },
            "instantiation_spec": {
                "name": name,
                "device_class": "ophyd.EpicsMotor",
                "args": [prefix],
                "kwargs": {"name": name},
            },
        },
    )
    assert resp.status_code == 201, resp.text


def _changes(client: TestClient, since: int):
    resp = client.get("/api/v1/devices/changes", params={"since_version": since})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_initial_fetch_returns_seeded_devices(client):
    body = _changes(client, since=0)
    assert body["current_version"] > 0
    assert body["service_epoch"] != "unseeded"
    assert body["reset_occurred"] is False
    assert len(body["changes"]) > 0
    assert all(c["op"] == "upsert" for c in body["changes"])
    assert all(c["metadata"] is not None for c in body["changes"])


def test_cursor_at_current_version_is_empty(client):
    first = _changes(client, since=0)
    cursor = first["current_version"]
    follow_up = _changes(client, since=cursor)
    assert follow_up["changes"] == []
    assert follow_up["current_version"] == cursor
    assert follow_up["reset_occurred"] is False


def test_cursor_beyond_current_is_empty(client):
    first = _changes(client, since=0)
    way_ahead = _changes(client, since=first["current_version"] + 1000)
    assert way_ahead["changes"] == []


def test_device_create_surfaces_as_upsert(client):
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    _create_device(client, "new_motor_1")

    delta = _changes(client, since=cursor)
    assert len(delta["changes"]) == 1
    ch = delta["changes"][0]
    assert ch["device_name"] == "new_motor_1"
    assert ch["op"] == "upsert"
    assert ch["metadata"]["name"] == "new_motor_1"
    assert ch["spec"]["args"] == ["XF:01-Mtr{M1}"]
    assert ch["version"] > cursor
    assert delta["current_version"] == ch["version"]


def test_device_update_surfaces_as_upsert_with_new_spec(client):
    _create_device(client, "upd_motor", prefix="XF:01-Mtr{Old}")
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    resp = client.put(
        "/api/v1/devices/upd_motor",
        json={"instantiation_spec": {"args": ["XF:01-Mtr{New}"]}},
    )
    assert resp.status_code == 200, resp.text

    delta = _changes(client, since=cursor)
    motor_changes = [c for c in delta["changes"] if c["device_name"] == "upd_motor"]
    assert len(motor_changes) == 1
    ch = motor_changes[0]
    assert ch["op"] == "upsert"
    assert ch["spec"]["args"] == ["XF:01-Mtr{New}"]


def test_device_delete_surfaces_as_delete(client):
    _create_device(client, "del_motor")
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    resp = client.delete("/api/v1/devices/del_motor")
    assert resp.status_code == 200, resp.text

    delta = _changes(client, since=cursor)
    del_changes = [c for c in delta["changes"] if c["device_name"] == "del_motor"]
    assert len(del_changes) == 1
    assert del_changes[0]["op"] == "delete"
    assert del_changes[0]["metadata"] is None
    assert del_changes[0]["spec"] is None


def test_dedupe_multiple_updates_per_device(client):
    _create_device(client, "rapid_motor", prefix="XF:01-Mtr{A}")
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    for prefix in ["XF:01-Mtr{B}", "XF:01-Mtr{C}", "XF:01-Mtr{D}"]:
        resp = client.put(
            "/api/v1/devices/rapid_motor",
            json={"instantiation_spec": {"args": [prefix]}},
        )
        assert resp.status_code == 200

    delta = _changes(client, since=cursor)
    motor_changes = [c for c in delta["changes"] if c["device_name"] == "rapid_motor"]
    assert len(motor_changes) == 1
    assert motor_changes[0]["op"] == "upsert"
    assert motor_changes[0]["spec"]["args"] == ["XF:01-Mtr{D}"]


def test_create_then_delete_collapses_to_delete(client):
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    _create_device(client, "ephemeral")
    resp = client.delete("/api/v1/devices/ephemeral")
    assert resp.status_code == 200

    delta = _changes(client, since=cursor)
    eph = [c for c in delta["changes"] if c["device_name"] == "ephemeral"]
    assert len(eph) == 1
    assert eph[0]["op"] == "delete"


def test_lock_unlock_not_in_changes_feed(client):
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]
    device_name = baseline["changes"][0]["device_name"]

    resp = client.post(
        "/api/v1/devices/lock",
        json={
            "device_names": [device_name],
            "item_id": "test-item",
            "plan_name": "test_plan",
        },
    )
    assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/v1/devices/unlock",
        json={"device_names": [device_name], "item_id": "test-item"},
    )
    assert resp.status_code == 200, resp.text

    delta = _changes(client, since=cursor)
    assert delta["changes"] == []


def test_registry_clear_sets_reset_occurred(client):
    baseline = _changes(client, since=0)
    cursor = baseline["current_version"]

    resp = client.post("/api/v1/registry/clear", json={"confirm": True})
    assert resp.status_code in (200, 204), resp.text

    delta = _changes(client, since=cursor)
    assert delta["reset_occurred"] is True


def test_service_epoch_stable_across_calls(client):
    first = _changes(client, since=0)
    second = _changes(client, since=first["current_version"])
    assert first["service_epoch"] == second["service_epoch"]
