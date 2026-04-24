"""Tests for device locking endpoints (A4 coordination)."""

import json
import pytest
from fastapi.testclient import TestClient

from configuration_service.config import Settings
from configuration_service.main import create_app


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_locking.db")


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


class TestLockDevices:
    """POST /api/v1/devices/lock"""

    def test_lock_single_device(self, client):
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["locked_devices"] == ["sample_x"]
        assert data["lock_id"] is not None
        assert data["registry_version"] >= 1
        # Should include PVs belonging to sample_x
        assert len(data["locked_pvs"]) > 0

    def test_lock_multiple_devices(self, client):
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x", "det1"],
            "item_id": "item-001",
            "plan_name": "rel_scan",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["locked_devices"]) == {"sample_x", "det1"}

    def test_lock_conflict(self, client):
        # Lock sample_x
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Try to lock again with different item_id
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-002",
            "plan_name": "rel_scan",
        })
        assert resp.status_code == 409
        data = resp.json()
        assert data["success"] is False
        assert "locked by plan" in data["message"]
        assert len(data["conflicting_devices"]) == 1
        assert data["conflicting_devices"][0]["device_name"] == "sample_x"
        assert data["conflicting_devices"][0]["reason"] == "already_locked"

    def test_lock_nonexistent_device(self, client):
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["nonexistent_motor"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        assert resp.status_code == 404
        data = resp.json()
        assert data["success"] is False
        assert data["conflicting_devices"][0]["reason"] == "not_found"

    def test_lock_disabled_device(self, client):
        # Disable the device first
        client.patch("/api/v1/devices/sample_x/disable")
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        assert resp.status_code == 422
        data = resp.json()
        assert data["conflicting_devices"][0]["reason"] == "disabled"

    def test_lock_atomicity_partial_conflict(self, client):
        """If one device is already locked, none should be acquired."""
        # Lock det1
        client.post("/api/v1/devices/lock", json={
            "device_names": ["det1"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Try to lock both sample_x and det1
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x", "det1"],
            "item_id": "item-002",
            "plan_name": "rel_scan",
        })
        assert resp.status_code == 409
        # Verify sample_x was NOT locked (atomicity)
        status_resp = client.get("/api/v1/devices/sample_x/status")
        assert status_resp.json()["lock_status"] == "unlocked"


class TestUnlockDevices:
    """POST /api/v1/devices/unlock"""

    def test_unlock_devices(self, client):
        # Lock first
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x", "det1"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Unlock
        resp = client.post("/api/v1/devices/unlock", json={
            "device_names": ["sample_x", "det1"],
            "item_id": "item-001",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert set(data["unlocked_devices"]) == {"sample_x", "det1"}

    def test_unlock_wrong_owner(self, client):
        # Lock with item-001
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Try to unlock with item-002
        resp = client.post("/api/v1/devices/unlock", json={
            "device_names": ["sample_x"],
            "item_id": "item-002",
        })
        assert resp.status_code == 403

    def test_unlock_already_unlocked(self, client):
        """Unlocking a device that isn't locked should succeed (no-op)."""
        resp = client.post("/api/v1/devices/unlock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
        })
        assert resp.status_code == 200
        assert resp.json()["unlocked_devices"] == []

    def test_relock_after_unlock(self, client):
        """Should be able to lock again after unlocking."""
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        client.post("/api/v1/devices/unlock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
        })
        resp = client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-002",
            "plan_name": "rel_scan",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestForceUnlock:
    """POST /api/v1/devices/force-unlock"""

    def test_force_unlock(self, client):
        # Lock with item-001
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Force-unlock (different ownership doesn't matter)
        resp = client.post("/api/v1/devices/force-unlock", json={
            "device_names": ["sample_x"],
            "reason": "EE crashed, clearing stale locks",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "sample_x" in data["unlocked_devices"]
        # Verify it's actually unlocked
        status_resp = client.get("/api/v1/devices/sample_x/status")
        assert status_resp.json()["lock_status"] == "unlocked"

    def test_force_unlock_nonexistent_device(self, client):
        resp = client.post("/api/v1/devices/force-unlock", json={
            "device_names": ["nonexistent_motor"],
            "reason": "cleanup",
        })
        assert resp.status_code == 404

    def test_force_unlock_already_unlocked(self, client):
        """Force-unlocking an unlocked device should succeed."""
        resp = client.post("/api/v1/devices/force-unlock", json={
            "device_names": ["sample_x"],
            "reason": "preventive cleanup",
        })
        assert resp.status_code == 200
        assert "sample_x" in resp.json()["unlocked_devices"]


class TestDeviceStatus:
    """GET /api/v1/devices/{device_name}/status"""

    def test_status_unlocked_enabled(self, client):
        resp = client.get("/api/v1/devices/sample_x/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["device_name"] == "sample_x"
        assert data["available"] is True
        assert data["enabled"] is True
        assert data["lock_status"] == "unlocked"
        assert data["locked_by_plan"] is None

    def test_status_locked(self, client):
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        resp = client.get("/api/v1/devices/sample_x/status")
        data = resp.json()
        assert data["available"] is False
        assert data["enabled"] is True
        assert data["lock_status"] == "locked"
        assert data["locked_by_plan"] == "count"
        assert data["locked_by_item"] == "item-001"
        assert data["locked_at"] is not None

    def test_status_disabled(self, client):
        client.patch("/api/v1/devices/sample_x/disable")
        resp = client.get("/api/v1/devices/sample_x/status")
        data = resp.json()
        assert data["available"] is False
        assert data["enabled"] is False
        assert data["lock_status"] == "unlocked"

    def test_status_disabled_and_locked(self, client):
        """Disabled + locked should still be available=False."""
        # Lock first (while still enabled)
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Now disable (unlock first since disabled devices can't be locked)
        # Actually the device is already locked — force-unlock, disable, then verify
        client.post("/api/v1/devices/force-unlock", json={
            "device_names": ["sample_x"],
            "reason": "test",
        })
        client.patch("/api/v1/devices/sample_x/disable")
        resp = client.get("/api/v1/devices/sample_x/status")
        data = resp.json()
        assert data["available"] is False
        assert data["enabled"] is False

    def test_status_nonexistent_device(self, client):
        resp = client.get("/api/v1/devices/nonexistent_motor/status")
        assert resp.status_code == 404


class TestPVStatus:
    """GET /api/v1/pvs/status?pv_name=..."""

    def test_pv_status_unlocked(self, client):
        # Get a PV name from sample_x
        device_resp = client.get("/api/v1/devices/sample_x")
        pvs = device_resp.json()["pvs"]
        pv_name = list(pvs.values())[0]

        resp = client.get("/api/v1/pvs/status", params={"pv_name": pv_name})
        assert resp.status_code == 200
        data = resp.json()
        assert data["pv_name"] == pv_name
        assert data["available"] is True
        assert data["device_name"] == "sample_x"
        assert data["device_lock_status"] == "unlocked"

    def test_pv_status_locked(self, client):
        # Get a PV name
        device_resp = client.get("/api/v1/devices/sample_x")
        pvs = device_resp.json()["pvs"]
        pv_name = list(pvs.values())[0]

        # Lock the owning device
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })

        resp = client.get("/api/v1/pvs/status", params={"pv_name": pv_name})
        data = resp.json()
        assert data["available"] is False
        assert data["device_name"] == "sample_x"
        assert data["device_lock_status"] == "locked"
        assert data["locked_by_plan"] == "count"

    def test_pv_status_standalone_pv(self, client):
        # Register a standalone PV
        client.post("/api/v1/pvs", json={
            "pv_name": "BL01:RING:CURRENT",
            "description": "Ring current",
        })
        resp = client.get("/api/v1/pvs/status", params={"pv_name": "BL01:RING:CURRENT"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["device_name"] is None
        assert data["device_lock_status"] is None

    def test_pv_status_unknown_pv(self, client):
        resp = client.get("/api/v1/pvs/status", params={"pv_name": "UNKNOWN:PV"})
        assert resp.status_code == 404


class TestLockAuditLog:
    """Verify lock events appear in audit log."""

    def test_lock_unlock_in_audit_log(self, client):
        # Lock
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        # Unlock
        client.post("/api/v1/devices/unlock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
        })
        # Check audit log
        resp = client.get("/api/v1/devices/history", params={"device_name": "sample_x"})
        entries = resp.json()
        operations = [e["operation"] for e in entries]
        assert "lock" in operations
        assert "unlock" in operations

        # Verify lock details
        lock_entry = next(e for e in entries if e["operation"] == "lock")
        details = json.loads(lock_entry["details"])
        assert details["plan"] == "count"
        assert details["item_id"] == "item-001"

    def test_force_unlock_in_audit_log(self, client):
        client.post("/api/v1/devices/lock", json={
            "device_names": ["sample_x"],
            "item_id": "item-001",
            "plan_name": "count",
        })
        client.post("/api/v1/devices/force-unlock", json={
            "device_names": ["sample_x"],
            "reason": "EE crashed",
        })
        resp = client.get("/api/v1/devices/history", params={"device_name": "sample_x"})
        entries = resp.json()
        force_entry = next(e for e in entries if e["operation"] == "force_unlock")
        details = json.loads(force_entry["details"])
        assert details["reason"] == "EE crashed"
        assert details["admin"] is True
