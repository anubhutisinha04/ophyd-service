"""Tests for device locking endpoints (A4 coordination)."""

import json
import pytest
from fastapi.testclient import TestClient

from configuration_service.config import Settings
from configuration_service.main import create_app


@pytest.fixture
def client(db_url):
    settings = Settings(
        use_mock_data=True,
        database_url=db_url,
        device_change_history_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


class TestLockDevices:
    """POST /api/v1/devices/lock"""

    def test_lock_single_device(self, client):
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["locked_devices"] == ["sample_x"]
        assert data["lock_id"] is not None
        assert data["registry_version"] >= 1
        # Should include PVs belonging to sample_x
        assert len(data["locked_pvs"]) > 0

    def test_lock_multiple_devices(self, client):
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x", "det1"],
                "item_id": "item-001",
                "plan_name": "rel_scan",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["locked_devices"]) == {"sample_x", "det1"}

    def test_lock_conflict(self, client):
        # Lock sample_x
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Try to lock again with different item_id
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-002",
                "plan_name": "rel_scan",
            },
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["success"] is False
        assert "locked by plan" in data["message"]
        assert len(data["conflicting_devices"]) == 1
        assert data["conflicting_devices"][0]["device_name"] == "sample_x"
        assert data["conflicting_devices"][0]["reason"] == "already_locked"

    def test_lock_nonexistent_device(self, client):
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["nonexistent_motor"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["success"] is False
        assert data["conflicting_devices"][0]["reason"] == "not_found"

    def test_lock_disabled_device(self, client):
        # Disable the device first
        client.patch("/api/v1/devices/sample_x/disable")
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["conflicting_devices"][0]["reason"] == "disabled"

    def test_lock_atomicity_partial_conflict(self, client):
        """If one device is already locked, none should be acquired."""
        # Lock det1
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["det1"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Try to lock both sample_x and det1
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x", "det1"],
                "item_id": "item-002",
                "plan_name": "rel_scan",
            },
        )
        assert resp.status_code == 409
        # Verify sample_x was NOT locked (atomicity)
        status_resp = client.get("/api/v1/devices/sample_x/status")
        assert status_resp.json()["lock_status"] == "unlocked"


class TestUnlockDevices:
    """POST /api/v1/devices/unlock"""

    def test_unlock_devices(self, client):
        # Lock first
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x", "det1"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Unlock
        resp = client.post(
            "/api/v1/devices/unlock",
            json={
                "device_names": ["sample_x", "det1"],
                "item_id": "item-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert set(data["unlocked_devices"]) == {"sample_x", "det1"}

    def test_unlock_wrong_owner(self, client):
        # Lock with item-001
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Try to unlock with item-002
        resp = client.post(
            "/api/v1/devices/unlock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-002",
            },
        )
        assert resp.status_code == 403

    def test_unlock_already_unlocked(self, client):
        """Unlocking a device that isn't locked should succeed (no-op)."""
        resp = client.post(
            "/api/v1/devices/unlock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["unlocked_devices"] == []

    def test_relock_after_unlock(self, client):
        """Should be able to lock again after unlocking."""
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        client.post(
            "/api/v1/devices/unlock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
            },
        )
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-002",
                "plan_name": "rel_scan",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestForceUnlock:
    """POST /api/v1/devices/force-unlock"""

    def test_force_unlock(self, client):
        # Lock with item-001
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Force-unlock (different ownership doesn't matter)
        resp = client.post(
            "/api/v1/devices/force-unlock",
            json={
                "device_names": ["sample_x"],
                "reason": "EE crashed, clearing stale locks",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "sample_x" in data["unlocked_devices"]
        # Verify it's actually unlocked
        status_resp = client.get("/api/v1/devices/sample_x/status")
        assert status_resp.json()["lock_status"] == "unlocked"

    def test_force_unlock_nonexistent_device(self, client):
        resp = client.post(
            "/api/v1/devices/force-unlock",
            json={
                "device_names": ["nonexistent_motor"],
                "reason": "cleanup",
            },
        )
        assert resp.status_code == 404

    def test_force_unlock_already_unlocked(self, client):
        """Force-unlocking an unlocked device should succeed."""
        resp = client.post(
            "/api/v1/devices/force-unlock",
            json={
                "device_names": ["sample_x"],
                "reason": "preventive cleanup",
            },
        )
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
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
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
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Now disable (unlock first since disabled devices can't be locked)
        # Actually the device is already locked — force-unlock, disable, then verify
        client.post(
            "/api/v1/devices/force-unlock",
            json={
                "device_names": ["sample_x"],
                "reason": "test",
            },
        )
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
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )

        resp = client.get("/api/v1/pvs/status", params={"pv_name": pv_name})
        data = resp.json()
        assert data["available"] is False
        assert data["device_name"] == "sample_x"
        assert data["device_lock_status"] == "locked"
        assert data["locked_by_plan"] == "count"

    def test_pv_status_standalone_pv(self, client):
        # Register a standalone PV
        client.post(
            "/api/v1/pvs",
            json={
                "pv_name": "BL01:RING:CURRENT",
                "description": "Ring current",
            },
        )
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
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        # Unlock
        client.post(
            "/api/v1/devices/unlock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
            },
        )
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
        client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        client.post(
            "/api/v1/devices/force-unlock",
            json={
                "device_names": ["sample_x"],
                "reason": "EE crashed",
            },
        )
        resp = client.get("/api/v1/devices/history", params={"device_name": "sample_x"})
        entries = resp.json()
        force_entry = next(e for e in entries if e["operation"] == "force_unlock")
        details = json.loads(force_entry["details"])
        assert details["reason"] == "EE crashed"
        assert details["admin"] is True


class TestSpecMissingFailsHard:
    """Regression: missing instantiation spec on a known device must fail loudly.

    Pre-fix, the three sites listed below silently coerced "no spec" into
    "available/enabled/lockable", so a corrupted registry would advertise a
    device as commandable. Per the no-silent-fallbacks rule (S3 from the
    2026-05-01 silent-failure audit), all three now surface 500 / spec_missing.
    """

    @staticmethod
    def _corrupt_spec(client, device_name: str) -> None:
        state = client.app.state.state_container["state"]
        assert device_name in state.registry.devices, "precondition: device must exist"
        state.registry.instantiation_specs.pop(device_name, None)

    def test_device_status_spec_missing_returns_500(self, client):
        self._corrupt_spec(client, "sample_x")
        resp = client.get("/api/v1/devices/sample_x/status")
        assert resp.status_code == 500
        assert "instantiation spec" in resp.json()["detail"]
        assert "sample_x" in resp.json()["detail"]

    def test_pv_status_spec_missing_returns_500(self, client):
        # Pick a PV owned by sample_x
        device_resp = client.get("/api/v1/devices/sample_x")
        pv_name = list(device_resp.json()["pvs"].values())[0]

        self._corrupt_spec(client, "sample_x")
        resp = client.get("/api/v1/pvs/status", params={"pv_name": pv_name})
        assert resp.status_code == 500
        assert "instantiation spec" in resp.json()["detail"]
        assert "sample_x" in resp.json()["detail"]

    def test_lock_devices_spec_missing_returns_500(self, client):
        self._corrupt_spec(client, "sample_x")
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        assert resp.status_code == 500
        data = resp.json()
        assert data["success"] is False
        assert data["conflicting_devices"][0]["reason"] == "spec_missing"
        assert "instantiation spec" in data["message"]

    def test_lock_atomicity_when_one_device_spec_missing(self, client):
        """500 from corruption must not leave partial locks behind."""
        self._corrupt_spec(client, "sample_x")
        resp = client.post(
            "/api/v1/devices/lock",
            json={
                "device_names": ["det1", "sample_x"],
                "item_id": "item-001",
                "plan_name": "count",
            },
        )
        assert resp.status_code == 500
        # det1 must remain unlocked — atomicity preserved
        det_status = client.get("/api/v1/devices/det1/status").json()
        assert det_status["lock_status"] == "unlocked"


class TestLockAllPolicy:
    """lock_all availability policy: while ANY lock is held, every registered
    device reports locked — not just the devices the plan named. Acquisition
    and release semantics stay untouched."""

    def _lock_sample_x(self, client, plan="count", item="item-001"):
        resp = client.post(
            "/api/v1/devices/lock",
            json={"device_names": ["sample_x"], "item_id": item, "plan_name": plan},
        )
        assert resp.status_code == 200

    def test_policy_defaults_off_and_locking_stays_scoped(self, client):
        assert client.get("/api/v1/devices/lock/policy").json() == {"lock_all": False}

        self._lock_sample_x(client)
        status = client.get("/api/v1/devices/det1/status").json()
        assert status["available"] is True
        assert status["lock_status"] == "unlocked"

    def test_lock_all_locks_unnamed_devices(self, client):
        resp = client.put("/api/v1/devices/lock/policy", json={"lock_all": True})
        assert resp.status_code == 200
        assert resp.json() == {"lock_all": True}

        self._lock_sample_x(client)

        # det1 was never named in the lock request, yet reports locked,
        # attributed to the plan that holds the (global) lock.
        status = client.get("/api/v1/devices/det1/status").json()
        assert status["available"] is False
        assert status["lock_status"] == "locked"
        assert status["locked_by_plan"] == "count"

        # PV-keyed availability derives identically (det1's own PV).
        pvs = client.get("/api/v1/devices/det1").json()["pvs"]
        pv_name = list(pvs.values())[0]
        pv = client.get("/api/v1/pvs/status", params={"pv_name": pv_name}).json()
        assert pv["available"] is False
        assert pv["device_lock_status"] == "locked"
        assert pv["locked_by_plan"] == "count"

    def test_lock_all_releases_with_last_lock(self, client):
        client.put("/api/v1/devices/lock/policy", json={"lock_all": True})
        self._lock_sample_x(client)
        assert client.get("/api/v1/devices/det1/status").json()["available"] is False

        resp = client.post(
            "/api/v1/devices/unlock",
            json={"device_names": ["sample_x"], "item_id": "item-001"},
        )
        assert resp.status_code == 200

        status = client.get("/api/v1/devices/det1/status").json()
        assert status["available"] is True
        assert status["lock_status"] == "unlocked"

    def test_lock_all_does_not_change_acquisition(self, client):
        """The policy only widens AVAILABILITY derivation; a second plan can
        still acquire a lock on an un-locked device (acquisition conflicts
        remain per-device, all-or-nothing)."""
        client.put("/api/v1/devices/lock/policy", json={"lock_all": True})
        self._lock_sample_x(client)

        resp = client.post(
            "/api/v1/devices/lock",
            json={"device_names": ["det1"], "item_id": "item-002", "plan_name": "scan"},
        )
        assert resp.status_code == 200

        # det1 now carries its OWN lock and is attributed to its own plan.
        status = client.get("/api/v1/devices/det1/status").json()
        assert status["locked_by_plan"] == "scan"

    def test_policy_can_be_turned_off_again(self, client):
        client.put("/api/v1/devices/lock/policy", json={"lock_all": True})
        self._lock_sample_x(client)
        assert client.get("/api/v1/devices/det1/status").json()["available"] is False

        client.put("/api/v1/devices/lock/policy", json={"lock_all": False})
        assert client.get("/api/v1/devices/det1/status").json()["available"] is True

    def test_lock_all_boot_default_from_settings(self, db_url):
        settings = Settings(
            use_mock_data=True,
            database_url=db_url,
            device_change_history_enabled=True,
            lock_all=True,
        )
        app = create_app(settings)
        with TestClient(app) as c:
            assert c.get("/api/v1/devices/lock/policy").json() == {"lock_all": True}


class TestLockLoggingAtInfoLevel:
    """The lock manager logs through the stdlib logger; INFO-level deployments
    used to hit TypeError from structlog-style kwargs AFTER the lock state was
    mutated — the endpoint 500'd while the devices stayed locked (review P2.1).
    Latent under the default WARNING level, so the cycle is exercised with the
    lock_manager logger explicitly raised to INFO."""

    def test_full_lock_cycle_logs_cleanly_at_info(self, client, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="configuration_service.lock_manager"):
            resp = client.post(
                "/api/v1/devices/lock",
                json={
                    "device_names": ["sample_x"],
                    "item_id": "item-001",
                    "plan_name": "count",
                },
            )
            assert resp.status_code == 200, resp.text

            resp = client.post(
                "/api/v1/devices/unlock",
                json={"device_names": ["sample_x"], "item_id": "item-001"},
            )
            assert resp.status_code == 200, resp.text

            client.post(
                "/api/v1/devices/lock",
                json={
                    "device_names": ["sample_x"],
                    "item_id": "item-002",
                    "plan_name": "scan",
                },
            )
            resp = client.post(
                "/api/v1/devices/force-unlock",
                json={"device_names": ["sample_x"], "reason": "test cleanup"},
            )
            assert resp.status_code == 200, resp.text

        messages = [record.getMessage() for record in caplog.records]
        assert any("locks_acquired" in m for m in messages)
        assert any("locks_released" in m for m in messages)
        assert any("locks_force_cleared" in m for m in messages)
