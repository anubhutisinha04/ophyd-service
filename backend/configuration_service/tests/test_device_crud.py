"""
Tests for Device CRUD endpoints and DeviceRegistryStore.

Tests runtime device creation, update, and deletion through the REST API,
as well as direct registry store persistence, audit log, reset, and export.
"""

import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from configuration_service.main import create_app
from configuration_service.config import Settings
from configuration_service.models import (
    DeviceMetadata,
    DeviceInstantiationSpec,
    DeviceRegistry,
    DeviceLabel,
)
from configuration_service.device_registry_store import DeviceRegistryStore


# ===== Fixtures =====


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return tmp_path / "test_cache.db"


@pytest.fixture
def client(tmp_db):
    """Create test client with mock data, DB persistence enabled."""
    settings = Settings(
        use_mock_data=True,
        db_path=tmp_db,
        device_change_history_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_device_payload():
    """Sample device creation payload."""
    return {
        "metadata": {
            "name": "new_motor",
            "device_label": "motor",
            "ophyd_class": "EpicsMotor",
            "is_movable": True,
            "is_flyable": False,
            "is_readable": True,
            "pvs": {
                "user_readback": "TEST:NEW_MOTOR.RBV",
                "user_setpoint": "TEST:NEW_MOTOR",
            },
            "read_attrs": ["user_readback", "user_setpoint"],
            "configuration_attrs": ["velocity"],
            "labels": ["motors"],
        },
        "instantiation_spec": {
            "name": "new_motor",
            "device_class": "ophyd.EpicsMotor",
            "args": ["TEST:NEW_MOTOR"],
            "kwargs": {"name": "new_motor"},
            "active": True,
        },
    }


# ===== DeviceRegistryStore Unit Tests =====


class TestDeviceRegistryStore:
    """Test DeviceRegistryStore persistence layer."""

    def test_initialize(self, tmp_db):
        """Test store initialization creates tables."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()
        assert store._initialized is True
        # Safe to call again
        store.initialize()
        store.close()

    def test_not_seeded_initially(self, tmp_db):
        """Test that a fresh DB is not seeded."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()
        assert store.is_seeded() is False
        store.close()

    def test_seed_from_registry(self, tmp_db):
        """Test seeding the DB from a DeviceRegistry."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        registry = DeviceRegistry()
        metadata = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            is_movable=True,
        )
        spec = DeviceInstantiationSpec(
            name="motor1",
            device_class="ophyd.EpicsMotor",
            args=["M1:"],
            kwargs={"name": "motor1"},
        )
        registry.add_device(metadata, spec)

        store.seed_from_registry(registry)
        assert store.is_seeded() is True
        assert store.device_count() == 1

        # Verify device is loadable
        device = store.get_device("motor1")
        assert device is not None
        assert device["metadata"].name == "motor1"
        assert device["spec"].device_class == "ophyd.EpicsMotor"

        # Verify audit log has seed entry
        log = store.get_audit_log()
        assert len(log) == 1
        assert log[0].operation == "seed"
        assert log[0].device_name == "motor1"

        store.close()

    def test_load_all_devices(self, tmp_db):
        """Test loading all devices from DB into a DeviceRegistry."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        registry = DeviceRegistry()
        for i in range(3):
            metadata = DeviceMetadata(
                name=f"dev_{i}",
                device_label=DeviceLabel.MOTOR,
                ophyd_class="EpicsMotor",
            )
            registry.add_device(metadata)

        store.seed_from_registry(registry)

        # Load back
        loaded = store.load_all_devices()
        assert len(loaded.devices) == 3
        assert "dev_0" in loaded.devices
        assert "dev_1" in loaded.devices
        assert "dev_2" in loaded.devices

        store.close()

    def test_save_and_get_device(self, tmp_db):
        """Test saving and retrieving a single device."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        metadata = DeviceMetadata(
            name="test_motor",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            is_movable=True,
        )
        spec = DeviceInstantiationSpec(
            name="test_motor",
            device_class="ophyd.EpicsMotor",
            args=["TEST:MOTOR"],
            kwargs={"name": "test_motor"},
        )

        store.save_device(
            name="test_motor",
            metadata=metadata,
            spec=spec,
            operation="add",
        )

        device = store.get_device("test_motor")
        assert device is not None
        assert device["metadata"].name == "test_motor"
        assert device["spec"].device_class == "ophyd.EpicsMotor"

        # Verify audit log
        log = store.get_audit_log()
        assert len(log) == 1
        assert log[0].operation == "add"

        store.close()

    def test_save_device_update(self, tmp_db):
        """Test that updating a device preserves created_at."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        metadata = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        )
        store.save_device(name="motor1", metadata=metadata, operation="add")

        first = store.get_device("motor1")
        created_at = first["created_at"]

        # Update
        updated = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="CustomMotor",
        )
        store.save_device(name="motor1", metadata=updated, operation="update")

        second = store.get_device("motor1")
        assert second["created_at"] == created_at  # preserved
        assert second["updated_at"] >= second["created_at"]
        assert second["metadata"].ophyd_class == "CustomMotor"

        # Audit log has both entries
        log = store.get_audit_log()
        assert len(log) == 2
        assert log[0].operation == "update"  # newest first
        assert log[1].operation == "add"

        store.close()

    def test_delete_device(self, tmp_db):
        """Test deleting a device."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        metadata = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        )
        store.save_device(name="motor1", metadata=metadata, operation="add")

        assert store.delete_device("motor1") is True
        assert store.get_device("motor1") is None
        assert store.device_count() == 0

        # Deleting again returns False
        assert store.delete_device("motor1") is False

        # Audit log has both add and delete
        log = store.get_audit_log()
        assert len(log) == 2
        assert log[0].operation == "delete"
        assert log[1].operation == "add"

        store.close()

    def test_audit_log_filter_by_device(self, tmp_db):
        """Test filtering audit log by device name."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        for name in ["dev_a", "dev_b", "dev_a"]:
            metadata = DeviceMetadata(
                name=name,
                device_label=DeviceLabel.MOTOR,
                ophyd_class="EpicsMotor",
            )
            store.save_device(name=name, metadata=metadata, operation="add")

        log_a = store.get_audit_log(device_name="dev_a")
        assert len(log_a) == 2  # add + update (second save is an update)
        for entry in log_a:
            assert entry.device_name == "dev_a"

        log_b = store.get_audit_log(device_name="dev_b")
        assert len(log_b) == 1

        store.close()

    def test_clear_and_reseed(self, tmp_db):
        """Test clearing DB and re-seeding from profile."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        # Initial seed
        registry1 = DeviceRegistry()
        registry1.add_device(DeviceMetadata(
            name="old_device",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        store.seed_from_registry(registry1)

        # Add a runtime device
        store.save_device(
            name="runtime_dev",
            metadata=DeviceMetadata(
                name="runtime_dev",
                device_label=DeviceLabel.DETECTOR,
                ophyd_class="EpicsScaler",
            ),
            operation="add",
        )
        assert store.device_count() == 2

        # Reset with a new registry
        registry2 = DeviceRegistry()
        registry2.add_device(DeviceMetadata(
            name="fresh_device",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        store.clear_and_reseed(registry2)

        assert store.device_count() == 1
        assert store.get_device("old_device") is None
        assert store.get_device("runtime_dev") is None
        assert store.get_device("fresh_device") is not None

        # Audit log contains reset entry
        log = store.get_audit_log()
        reset_entries = [e for e in log if e.operation == "reset"]
        assert len(reset_entries) == 1

        store.close()

    def test_export_happi(self, tmp_db):
        """Test exporting registry in happi format."""
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        registry = DeviceRegistry()
        metadata = DeviceMetadata(
            name="test_motor",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            beamline="BL01",
            documentation="Test motor",
        )
        spec = DeviceInstantiationSpec(
            name="test_motor",
            device_class="ophyd.EpicsMotor",
            args=["BL01:MOTOR:"],
            kwargs={"name": "test_motor"},
            active=True,
        )
        registry.add_device(metadata, spec)
        store.seed_from_registry(registry)

        happi = store.export_happi()
        assert "test_motor" in happi
        entry = happi["test_motor"]
        assert entry["_id"] == "test_motor"
        assert entry["device_class"] == "ophyd.EpicsMotor"
        assert entry["args"] == ["BL01:MOTOR:"]
        assert entry["beamline"] == "BL01"
        assert entry["documentation"] == "Test motor"
        assert entry["prefix"] == "BL01:MOTOR:"
        assert entry["active"] is True

        store.close()

    def test_data_survives_reopen(self, tmp_db):
        """Test that data persists across store reopens (simulated restart)."""
        store1 = DeviceRegistryStore(tmp_db)
        store1.initialize()

        registry = DeviceRegistry()
        registry.add_device(DeviceMetadata(
            name="persistent_motor",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        store1.seed_from_registry(registry)
        store1.close()

        # Reopen and verify
        store2 = DeviceRegistryStore(tmp_db)
        store2.initialize()
        assert store2.is_seeded() is True
        device = store2.get_device("persistent_motor")
        assert device is not None
        assert device["metadata"].name == "persistent_motor"
        store2.close()

    def test_drops_old_change_history_table(self, tmp_db):
        """Test that old device_change_history table is dropped on init."""
        import sqlite3

        # Create old table
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("""
            CREATE TABLE device_change_history (
                name TEXT PRIMARY KEY,
                operation TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO device_change_history VALUES ('old', 'add')")
        conn.commit()
        conn.close()

        # Initialize new store — should drop old table
        store = DeviceRegistryStore(tmp_db)
        store.initialize()

        # Old table should be gone
        conn = sqlite3.connect(str(tmp_db))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='device_change_history'"
        )
        assert cursor.fetchone() is None
        conn.close()

        store.close()


# ===== DeviceRegistry Method Tests =====


class TestDeviceRegistryMethods:
    """Test remove_device and update_device methods on DeviceRegistry."""

    def test_remove_device(self):
        """Test removing a device removes metadata, spec, and PV indexes."""
        registry = DeviceRegistry()
        metadata = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={"readback": "M1:RBV", "setpoint": "M1:SP"},
        )
        spec = DeviceInstantiationSpec(
            name="motor1",
            device_class="ophyd.EpicsMotor",
            args=["M1"],
            kwargs={"name": "motor1"},
        )
        registry.add_device(metadata, spec)

        assert "motor1" in registry.devices
        assert "motor1" in registry.instantiation_specs
        assert "M1:RBV" in registry.pvs

        result = registry.remove_device("motor1")
        assert result is True
        assert "motor1" not in registry.devices
        assert "motor1" not in registry.instantiation_specs
        assert "M1:RBV" not in registry.pvs
        assert "M1:SP" not in registry.pvs

    def test_remove_device_not_found(self):
        """Test removing a nonexistent device returns False."""
        registry = DeviceRegistry()
        assert registry.remove_device("nonexistent") is False

    def test_update_device(self):
        """Test updating a device replaces metadata and re-indexes PVs."""
        registry = DeviceRegistry()
        metadata = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={"readback": "M1:RBV"},
        )
        registry.add_device(metadata)

        # Update with new PVs
        updated = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={"readback": "M1:NEW_RBV"},
        )
        result = registry.update_device(updated)
        assert result is True
        assert "M1:RBV" not in registry.pvs
        assert "M1:NEW_RBV" in registry.pvs

    def test_update_device_not_found(self):
        """Test updating a nonexistent device returns False."""
        registry = DeviceRegistry()
        metadata = DeviceMetadata(
            name="nonexistent",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        )
        assert registry.update_device(metadata) is False


# ===== API Endpoint Tests =====


class TestCreateDeviceEndpoint:
    """Test POST /api/v1/devices."""

    def test_create_device(self, client, sample_device_payload):
        """Test successful device creation."""
        response = client.post("/api/v1/devices", json=sample_device_payload)
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True
        assert data["device_name"] == "new_motor"
        assert data["operation"] == "create"

    def test_created_device_appears_in_list(self, client, sample_device_payload):
        """Test that a created device shows up in GET /api/v1/devices."""
        client.post("/api/v1/devices", json=sample_device_payload)

        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        devices = response.json()
        assert "new_motor" in devices

    def test_created_device_get_metadata(self, client, sample_device_payload):
        """Test that created device metadata is retrievable."""
        client.post("/api/v1/devices", json=sample_device_payload)

        response = client.get("/api/v1/devices/new_motor")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "new_motor"
        assert data["device_label"] == "motor"
        assert data["ophyd_class"] == "EpicsMotor"

    def test_create_device_conflict(self, client, sample_device_payload):
        """Test creating a device with an existing name returns 409."""
        # sample_x is a mock device that already exists
        payload = sample_device_payload.copy()
        payload["metadata"]["name"] = "sample_x"
        payload["instantiation_spec"]["name"] = "sample_x"

        response = client.post("/api/v1/devices", json=payload)
        assert response.status_code == 409

    def test_create_device_name_mismatch(self, client, sample_device_payload):
        """Test creating with mismatched names returns 400."""
        payload = sample_device_payload.copy()
        payload["instantiation_spec"] = dict(payload["instantiation_spec"])
        payload["instantiation_spec"]["name"] = "different_name"

        response = client.post("/api/v1/devices", json=payload)
        assert response.status_code == 400


class TestUpdateDeviceEndpoint:
    """Test PUT /api/v1/devices/{device_name}."""

    def test_update_device(self, client, sample_device_payload):
        """Test updating an existing device."""
        # Create device first
        client.post("/api/v1/devices", json=sample_device_payload)

        # Update it
        update_payload = {
            "metadata": {
                "name": "new_motor",
                "device_label": "motor",
                "ophyd_class": "CustomMotor",
                "is_movable": True,
                "is_readable": True,
                "pvs": {"readback": "TEST:UPDATED.RBV"},
                "labels": ["motors", "updated"],
            },
        }
        response = client.put("/api/v1/devices/new_motor", json=update_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "update"

        # Verify update took effect
        response = client.get("/api/v1/devices/new_motor")
        assert response.status_code == 200
        device = response.json()
        assert device["ophyd_class"] == "CustomMotor"

    def test_update_profile_device(self, client):
        """Test updating a profile-loaded (seeded) device."""
        update_payload = {
            "metadata": {
                "name": "sample_x",
                "device_label": "motor",
                "ophyd_class": "UpdatedEpicsMotor",
                "is_movable": True,
                "is_readable": True,
                "pvs": {"readback": "UPDATED:X.RBV"},
            },
        }
        response = client.put("/api/v1/devices/sample_x", json=update_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify
        response = client.get("/api/v1/devices/sample_x")
        device = response.json()
        assert device["ophyd_class"] == "UpdatedEpicsMotor"

    def test_update_device_not_found(self, client):
        """Test updating a nonexistent device returns 404."""
        update_payload = {
            "metadata": {
                "name": "nonexistent",
                "device_label": "motor",
                "ophyd_class": "EpicsMotor",
            },
        }
        response = client.put("/api/v1/devices/nonexistent", json=update_payload)
        assert response.status_code == 404

    def test_update_device_name_mismatch(self, client):
        """Test updating with mismatched name returns 400."""
        update_payload = {
            "metadata": {
                "name": "wrong_name",
                "device_label": "motor",
                "ophyd_class": "EpicsMotor",
            },
        }
        response = client.put("/api/v1/devices/sample_x", json=update_payload)
        assert response.status_code == 400


class TestPartialUpdateDevice:
    """Test field-level partial updates via PUT /api/v1/devices/{device_name}."""

    def test_partial_update_documentation_only(self, client):
        """Sending just documentation preserves all other fields."""
        # Get original device state
        original = client.get("/api/v1/devices/sample_x").json()
        assert original["documentation"] is None

        # Update only documentation
        response = client.put("/api/v1/devices/sample_x", json={
            "metadata": {"documentation": "Updated description"}
        })
        assert response.status_code == 200

        # Verify documentation changed and everything else preserved
        updated = client.get("/api/v1/devices/sample_x").json()
        assert updated["documentation"] == "Updated description"
        assert updated["name"] == original["name"]
        assert updated["device_label"] == original["device_label"]
        assert updated["ophyd_class"] == original["ophyd_class"]
        assert updated["is_movable"] == original["is_movable"]
        assert updated["pvs"] == original["pvs"]

    def test_partial_update_labels_only(self, client):
        """Sending just labels preserves all other fields."""
        original = client.get("/api/v1/devices/sample_x").json()

        response = client.put("/api/v1/devices/sample_x", json={
            "metadata": {"labels": ["motors", "sample-stage"]}
        })
        assert response.status_code == 200

        updated = client.get("/api/v1/devices/sample_x").json()
        assert updated["labels"] == ["motors", "sample-stage"]
        assert updated["ophyd_class"] == original["ophyd_class"]
        assert updated["pvs"] == original["pvs"]

    def test_partial_update_spec_active_only(self, client):
        """Sending just active flag on spec preserves device_class/args/kwargs."""
        original_spec = client.get("/api/v1/devices/sample_x/instantiation").json()
        assert original_spec["active"] is True

        response = client.put("/api/v1/devices/sample_x", json={
            "instantiation_spec": {"active": False}
        })
        assert response.status_code == 200

        updated_spec = client.get("/api/v1/devices/sample_x/instantiation").json()
        assert updated_spec["active"] is False
        assert updated_spec["device_class"] == original_spec["device_class"]
        assert updated_spec["args"] == original_spec["args"]
        assert updated_spec["kwargs"] == original_spec["kwargs"]

        # Restore
        client.put("/api/v1/devices/sample_x", json={
            "instantiation_spec": {"active": True}
        })

    def test_partial_update_invalid_device_label(self, client):
        """Invalid enum value in partial update returns 422."""
        response = client.put("/api/v1/devices/sample_x", json={
            "metadata": {"device_label": "not_a_real_label"}
        })
        assert response.status_code == 422

    def test_partial_update_multiple_fields(self, client):
        """Multiple fields in one request all apply."""
        response = client.put("/api/v1/devices/sample_x", json={
            "metadata": {
                "documentation": "Multi-field update",
                "is_stoppable": True,
                "labels": ["updated"],
            }
        })
        assert response.status_code == 200

        updated = client.get("/api/v1/devices/sample_x").json()
        assert updated["documentation"] == "Multi-field update"
        assert updated["is_stoppable"] is True
        assert updated["labels"] == ["updated"]
        # Required fields preserved
        assert updated["name"] == "sample_x"
        assert updated["ophyd_class"] == "EpicsMotor"

    def test_partial_update_empty_body(self, client):
        """Empty metadata/spec body is a no-op, not an error."""
        original = client.get("/api/v1/devices/sample_x").json()

        response = client.put("/api/v1/devices/sample_x", json={})
        assert response.status_code == 200

        unchanged = client.get("/api/v1/devices/sample_x").json()
        assert unchanged == original

    def test_partial_update_null_required_field(self, client):
        """Setting a required field to null exercises the model_validate catch.

        device_label is Optional in the update model (so null passes
        deserialization), but required in DeviceMetadata (so model_validate
        rejects it with 422).
        """
        response = client.put("/api/v1/devices/sample_x", json={
            "metadata": {"device_label": None}
        })
        assert response.status_code == 422
        assert "Invalid metadata update" in response.json()["detail"]

    def test_partial_update_null_required_spec_field(self, client):
        """Setting device_class to null fails model_validate on the spec."""
        response = client.put("/api/v1/devices/sample_x", json={
            "instantiation_spec": {"device_class": None}
        })
        assert response.status_code == 422
        assert "Invalid instantiation spec update" in response.json()["detail"]


class TestDeleteDeviceEndpoint:
    """Test DELETE /api/v1/devices/{device_name}."""

    def test_delete_runtime_device(self, client, sample_device_payload):
        """Test deleting a runtime-added device."""
        client.post("/api/v1/devices", json=sample_device_payload)

        response = client.delete("/api/v1/devices/new_motor")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "delete"

        # Verify it's gone
        response = client.get("/api/v1/devices/new_motor")
        assert response.status_code == 404

    def test_delete_seeded_device(self, client):
        """Test deleting a seeded (profile-loaded) device."""
        response = client.delete("/api/v1/devices/sample_x")
        assert response.status_code == 200

        # Verify it's gone from device list
        response = client.get("/api/v1/devices/sample_x")
        assert response.status_code == 404

        devices = client.get("/api/v1/devices").json()
        assert "sample_x" not in devices

    def test_delete_device_not_found(self, client):
        """Test deleting a nonexistent device returns 404."""
        response = client.delete("/api/v1/devices/nonexistent")
        assert response.status_code == 404


class TestEnableDisableEndpoint:
    """Test PATCH /api/v1/devices/{device_name}/enable and /disable."""

    def test_disable_device(self, client, sample_device_payload):
        """Test disabling an active device."""
        client.post("/api/v1/devices", json=sample_device_payload)

        response = client.patch("/api/v1/devices/new_motor/disable")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "disable"
        assert data["device_name"] == "new_motor"

        # Verify spec shows active=False
        spec = client.get("/api/v1/devices/new_motor/instantiation").json()
        assert spec["active"] is False

    def test_enable_device(self, client, sample_device_payload):
        """Test enabling a disabled device."""
        client.post("/api/v1/devices", json=sample_device_payload)
        client.patch("/api/v1/devices/new_motor/disable")

        response = client.patch("/api/v1/devices/new_motor/enable")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "enable"

        # Verify spec shows active=True
        spec = client.get("/api/v1/devices/new_motor/instantiation").json()
        assert spec["active"] is True

    def test_disable_already_disabled(self, client, sample_device_payload):
        """Test disabling an already disabled device returns success (idempotent)."""
        client.post("/api/v1/devices", json=sample_device_payload)
        client.patch("/api/v1/devices/new_motor/disable")

        response = client.patch("/api/v1/devices/new_motor/disable")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "already disabled" in data["message"]

    def test_enable_already_enabled(self, client, sample_device_payload):
        """Test enabling an already enabled device returns success (idempotent)."""
        client.post("/api/v1/devices", json=sample_device_payload)

        response = client.patch("/api/v1/devices/new_motor/enable")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "already enabled" in data["message"]

    def test_disable_nonexistent_device(self, client):
        """Test disabling a nonexistent device returns 404."""
        response = client.patch("/api/v1/devices/nonexistent/disable")
        assert response.status_code == 404

    def test_enable_nonexistent_device(self, client):
        """Test enabling a nonexistent device returns 404."""
        response = client.patch("/api/v1/devices/nonexistent/enable")
        assert response.status_code == 404

    def test_disabled_device_excluded_from_active_specs(self, client, sample_device_payload):
        """Test that disabled devices are excluded from active_only spec listing."""
        client.post("/api/v1/devices", json=sample_device_payload)
        client.patch("/api/v1/devices/new_motor/disable")

        # active_only=True (default) should exclude disabled device
        specs = client.get("/api/v1/devices/instantiation").json()
        assert "new_motor" not in specs

        # active_only=False should include it
        specs = client.get("/api/v1/devices/instantiation?active_only=false").json()
        assert "new_motor" in specs
        assert specs["new_motor"]["active"] is False

    def test_disable_persists_to_audit_log(self, client, sample_device_payload):
        """Test that enable/disable operations appear in audit log with details."""
        client.post("/api/v1/devices", json=sample_device_payload)
        client.patch("/api/v1/devices/new_motor/disable")

        entries = client.get("/api/v1/devices/history?device_name=new_motor").json()
        operations = [e["operation"] for e in entries]
        assert "add" in operations
        assert "disable" in operations

        # Verify details are recorded
        disable_entry = next(e for e in entries if e["operation"] == "disable")
        assert disable_entry["details"] is not None
        details = json.loads(disable_entry["details"])
        assert details["field"] == "active"
        assert details["new"] is False

    def test_disable_seeded_device(self, client):
        """Test disabling a profile-seeded device works correctly."""
        response = client.patch("/api/v1/devices/sample_x/disable")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "disable"

        # Verify spec shows active=False
        spec = client.get("/api/v1/devices/sample_x/instantiation").json()
        assert spec["active"] is False


class TestAuditLogEndpoint:
    """Test GET /api/v1/devices/history (audit log)."""

    def test_audit_log_has_seed_entries(self, client):
        """Test that seeding from profile creates audit log entries."""
        response = client.get("/api/v1/devices/history")
        assert response.status_code == 200
        entries = response.json()
        # Mock profile has 3 devices (sample_x, det1, cam1)
        seed_entries = [e for e in entries if e["operation"] == "seed"]
        assert len(seed_entries) == 3

    def test_audit_log_after_crud(self, client, sample_device_payload):
        """Test that CRUD operations appear in the audit log."""
        # Create
        client.post("/api/v1/devices", json=sample_device_payload)

        entries = client.get("/api/v1/devices/history").json()
        add_entries = [e for e in entries if e["operation"] == "add"]
        assert len(add_entries) == 1
        assert add_entries[0]["device_name"] == "new_motor"

    def test_audit_log_filter_by_device(self, client, sample_device_payload):
        """Test filtering audit log by device name."""
        client.post("/api/v1/devices", json=sample_device_payload)

        # Filter to new_motor only
        entries = client.get("/api/v1/devices/history?device_name=new_motor").json()
        assert len(entries) == 1
        assert entries[0]["device_name"] == "new_motor"
        assert entries[0]["operation"] == "add"

    def test_audit_log_limit(self, client):
        """Test limiting audit log entries."""
        entries = client.get("/api/v1/devices/history?limit=2").json()
        assert len(entries) == 2


# ===== Registry Admin Endpoint Tests =====


class TestResetEndpoint:
    """Test POST /api/v1/registry/reset."""

    def test_reset_restores_profile_devices(self, client, sample_device_payload):
        """Test that reset erases CRUD changes and re-seeds from profile."""
        # Create a device and delete a profile device
        client.post("/api/v1/devices", json=sample_device_payload)
        client.delete("/api/v1/devices/sample_x")

        devices = client.get("/api/v1/devices").json()
        assert "new_motor" in devices
        assert "sample_x" not in devices

        # Reset
        response = client.post("/api/v1/registry/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "reset"

        # After reset: profile devices restored, runtime device gone
        devices = client.get("/api/v1/devices").json()
        assert "sample_x" in devices
        assert "new_motor" not in devices


class TestExportEndpoint:
    """Test GET /api/v1/registry/export."""

    def test_export_happi(self, client):
        """Test exporting registry in happi format."""
        response = client.get("/api/v1/registry/export?format=happi")
        assert response.status_code == 200
        data = response.json()

        # Mock profile has sample_x, det1, cam1
        assert "sample_x" in data
        assert "det1" in data
        assert "cam1" in data

        entry = data["sample_x"]
        assert entry["_id"] == "sample_x"
        assert entry["name"] == "sample_x"
        assert "device_class" in entry
        assert entry["active"] is True

    def test_export_unsupported_format(self, client):
        """Test that unsupported format returns 400."""
        response = client.get("/api/v1/registry/export?format=xml")
        assert response.status_code == 400


# ===== Persistence Tests =====


class TestPersistenceAcrossRestarts:
    """Test that device state survives simulated service restarts."""

    def test_created_device_survives_restart(self, tmp_path):
        """Test that a runtime-created device persists across app restarts."""
        db_path = tmp_path / "persist_test.db"

        payload = {
            "metadata": {
                "name": "persistent_dev",
                "device_label": "detector",
                "ophyd_class": "EpicsScaler",
                "pvs": {"count": "PERSIST:CNT"},
            },
            "instantiation_spec": {
                "name": "persistent_dev",
                "device_class": "ophyd.EpicsScaler",
                "args": ["PERSIST:"],
                "kwargs": {"name": "persistent_dev"},
                "active": True,
            },
        }

        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
        )

        # First "session"
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            resp = c1.post("/api/v1/devices", json=payload)
            assert resp.status_code == 201
            assert "persistent_dev" in c1.get("/api/v1/devices").json()

        # Second "session" (new app, same DB) — should load from DB
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            devices = c2.get("/api/v1/devices").json()
            assert "persistent_dev" in devices

            device = c2.get("/api/v1/devices/persistent_dev").json()
            assert device["ophyd_class"] == "EpicsScaler"

    def test_deleted_device_stays_gone(self, tmp_path):
        """Test that a deleted device stays gone after restart."""
        db_path = tmp_path / "delete_persist.db"

        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
        )

        # First session: delete a seeded device
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            assert "sample_x" in c1.get("/api/v1/devices").json()
            resp = c1.delete("/api/v1/devices/sample_x")
            assert resp.status_code == 200
            assert "sample_x" not in c1.get("/api/v1/devices").json()

        # Second session: sample_x should still be gone (loaded from DB, not re-seeded)
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            devices = c2.get("/api/v1/devices").json()
            assert "sample_x" not in devices

    def test_profile_not_reread_after_seed(self, tmp_path):
        """Test that profile is NOT re-read after initial seeding."""
        db_path = tmp_path / "no_reseed.db"

        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
        )

        # First session: seeds from mock profile (sample_x, det1, cam1)
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            assert "sample_x" in c1.get("/api/v1/devices").json()
            # Delete all seeded devices
            c1.delete("/api/v1/devices/sample_x")
            c1.delete("/api/v1/devices/det1")
            c1.delete("/api/v1/devices/cam1")
            assert c1.get("/api/v1/devices").json() == []

        # Second session: DB is seeded (even though empty), should NOT re-seed from profile
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            devices = c2.get("/api/v1/devices").json()
            # If profile were re-read, these would be back
            assert "sample_x" not in devices
            assert devices == []
