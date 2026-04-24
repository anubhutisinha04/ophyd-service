"""
Tests for Standalone PV Registration endpoints and StandalonePVStore.

Tests standalone PV creation, update, deletion, listing, and label filtering
through the REST API, as well as direct store persistence.
"""

import pytest
from fastapi.testclient import TestClient

from configuration_service.main import create_app
from configuration_service.config import Settings
from configuration_service.standalone_pv_store import StandalonePVStore


# ===== Fixtures =====


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return tmp_path / "test_standalone_pv.db"


@pytest.fixture
def client(tmp_db):
    """Create test client with mock data, change history enabled."""
    settings = Settings(
        use_mock_data=True,
        db_path=tmp_db,
        device_change_history_enabled=True,

    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_pv_payload():
    """Sample standalone PV creation payload."""
    return {
        "pv_name": "SR:C01:RING:CURR",
        "description": "Storage ring current",
        "protocol": "ca",
        "access_mode": "read-only",
        "labels": ["diagnostics", "ring"],
    }


# ===== StandalonePVStore Unit Tests =====


class TestStandalonePVStore:
    """Test StandalonePVStore persistence layer."""

    def test_initialize(self, tmp_db):
        """Test store initialization creates table."""
        store = StandalonePVStore(tmp_db)
        store.initialize()
        assert store._initialized is True
        # Safe to call again
        store.initialize()
        store.close()

    def test_save_and_get_pv(self, tmp_db):
        """Test saving and retrieving a standalone PV."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        store.save_pv(
            pv_name="TEST:PV:1",
            description="Test PV",
            protocol="ca",
            access_mode="read-only",
            labels=["test", "diagnostics"],
            source="runtime",
            created_by="test_user",
        )

        pv = store.get_pv("TEST:PV:1")
        assert pv is not None
        assert pv.pv_name == "TEST:PV:1"
        assert pv.description == "Test PV"
        assert pv.protocol == "ca"
        assert pv.access_mode == "read-only"
        assert pv.labels == ["test", "diagnostics"]
        assert pv.source == "runtime"
        assert pv.created_by == "test_user"
        assert pv.created_at is not None
        assert pv.updated_at is not None

        store.close()

    def test_save_pv_update_preserves_created_at(self, tmp_db):
        """Test that updating a PV preserves created_at."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        store.save_pv(
            pv_name="TEST:PV:1",
            description="Original",
            created_by="user1",
        )
        first = store.get_pv("TEST:PV:1")
        created_at = first.created_at

        # Update the PV
        store.save_pv(
            pv_name="TEST:PV:1",
            description="Updated",
        )
        second = store.get_pv("TEST:PV:1")

        assert second.created_at == created_at  # preserved
        assert second.description == "Updated"
        assert second.updated_at >= second.created_at

        store.close()

    def test_delete_pv(self, tmp_db):
        """Test deleting a standalone PV."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        store.save_pv(pv_name="TEST:PV:1", description="Test")

        assert store.delete_pv("TEST:PV:1") is True
        assert store.get_pv("TEST:PV:1") is None
        # Deleting again returns False
        assert store.delete_pv("TEST:PV:1") is False

        store.close()

    def test_get_all_pvs(self, tmp_db):
        """Test listing all standalone PVs."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        for i in range(3):
            store.save_pv(pv_name=f"TEST:PV:{i}", description=f"PV {i}")

        pvs = store.get_all_pvs()
        assert len(pvs) == 3

        store.close()

    def test_get_all_pvs_with_label_filter(self, tmp_db):
        """Test listing PVs filtered by labels."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        store.save_pv(pv_name="PV:A", labels=["diagnostics", "ring"])
        store.save_pv(pv_name="PV:B", labels=["diagnostics"])
        store.save_pv(pv_name="PV:C", labels=["vacuum"])

        # Filter by single label
        pvs = store.get_all_pvs(labels=["diagnostics"])
        assert len(pvs) == 2
        names = [pv.pv_name for pv in pvs]
        assert "PV:A" in names
        assert "PV:B" in names

        # Filter by multiple labels (AND)
        pvs = store.get_all_pvs(labels=["diagnostics", "ring"])
        assert len(pvs) == 1
        assert pvs[0].pv_name == "PV:A"

        store.close()

    def test_get_all_labels(self, tmp_db):
        """Test listing unique labels across all PVs."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        store.save_pv(pv_name="PV:A", labels=["diagnostics", "ring"])
        store.save_pv(pv_name="PV:B", labels=["diagnostics", "vacuum"])

        labels = store.get_all_labels()
        assert labels == ["diagnostics", "ring", "vacuum"]

        store.close()

    def test_get_all_labels_empty(self, tmp_db):
        """Test listing labels when no PVs exist."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        labels = store.get_all_labels()
        assert labels == []

        store.close()

    def test_clear_all(self, tmp_db):
        """Test clearing all standalone PVs."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        for i in range(3):
            store.save_pv(pv_name=f"TEST:PV:{i}")

        count = store.clear_all()
        assert count == 3
        assert store.get_all_pvs() == []

        store.close()

    def test_pv_persists_across_reopen(self, tmp_db):
        """Test that PVs persist across store reopens (simulated restart)."""
        store1 = StandalonePVStore(tmp_db)
        store1.initialize()
        store1.save_pv(
            pv_name="PERSIST:PV:1",
            description="Persistent PV",
            protocol="pva",
            access_mode="read-write",
            labels=["persistent"],
        )
        store1.close()

        # Reopen and verify
        store2 = StandalonePVStore(tmp_db)
        store2.initialize()
        pv = store2.get_pv("PERSIST:PV:1")
        assert pv is not None
        assert pv.description == "Persistent PV"
        assert pv.protocol == "pva"
        assert pv.access_mode == "read-write"
        assert pv.labels == ["persistent"]
        store2.close()

    def test_get_nonexistent_pv(self, tmp_db):
        """Test getting a PV that doesn't exist returns None."""
        store = StandalonePVStore(tmp_db)
        store.initialize()

        assert store.get_pv("NONEXISTENT") is None

        store.close()


# ===== API Endpoint Tests =====


class TestCreateStandalonePVEndpoint:
    """Test POST /api/v1/pvs."""

    def test_create_standalone_pv(self, client, sample_pv_payload):
        """Test successful standalone PV registration."""
        response = client.post("/api/v1/pvs", json=sample_pv_payload)
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True
        assert data["pv_name"] == "SR:C01:RING:CURR"
        assert data["operation"] == "create"

    def test_created_pv_appears_in_standalone_list(self, client, sample_pv_payload):
        """Test that a created PV shows up in GET /api/v1/pvs/standalone."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        response = client.get("/api/v1/pvs/standalone")
        assert response.status_code == 200
        pvs = response.json()
        names = [pv["pv_name"] for pv in pvs]
        assert "SR:C01:RING:CURR" in names

    def test_created_pv_appears_in_main_pv_list(self, client, sample_pv_payload):
        """Test that a created PV shows up in GET /api/v1/pvs."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        response = client.get("/api/v1/pvs")
        assert response.status_code == 200
        data = response.json()
        assert "SR:C01:RING:CURR" in data["pvs"]

    def test_create_pv_conflict_with_device_pv(self, client):
        """Test 409 when registering a PV that already exists as a device PV."""
        # First, find an existing device PV from the mock data
        response = client.get("/api/v1/pvs")
        assert response.status_code == 200
        existing_pvs = response.json()["pvs"]

        if existing_pvs:
            # Try to register a PV that already exists
            payload = {
                "pv_name": existing_pvs[0],
                "description": "Conflicting PV",
            }
            response = client.post("/api/v1/pvs", json=payload)
            assert response.status_code == 409

    def test_create_pv_duplicate(self, client, sample_pv_payload):
        """Test 409 when registering a duplicate standalone PV."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        response = client.post("/api/v1/pvs", json=sample_pv_payload)
        assert response.status_code == 409

    def test_create_pv_minimal_payload(self, client):
        """Test creating a standalone PV with minimal fields."""
        payload = {"pv_name": "MINIMAL:PV"}
        response = client.post("/api/v1/pvs", json=payload)
        assert response.status_code == 201

    def test_create_pv_with_pva_protocol(self, client):
        """Test creating a standalone PV with pvAccess protocol."""
        payload = {
            "pv_name": "PVA:CHANNEL",
            "protocol": "pva",
            "access_mode": "read-write",
        }
        response = client.post("/api/v1/pvs", json=payload)
        assert response.status_code == 201


class TestUpdateStandalonePVEndpoint:
    """Test PUT /api/v1/pvs/standalone/{pv_name}."""

    def test_update_standalone_pv(self, client, sample_pv_payload):
        """Test successful standalone PV update."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        update_payload = {
            "description": "Updated ring current",
            "labels": ["diagnostics", "ring", "important"],
        }
        response = client.put(
            "/api/v1/pvs/standalone/SR:C01:RING:CURR",
            json=update_payload,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "update"

        # Verify update took effect
        pvs = client.get("/api/v1/pvs/standalone").json()
        pv = next(p for p in pvs if p["pv_name"] == "SR:C01:RING:CURR")
        assert pv["description"] == "Updated ring current"
        assert "important" in pv["labels"]

    def test_update_standalone_pv_partial(self, client, sample_pv_payload):
        """Test partial update preserves non-updated fields."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        # Only update description
        update_payload = {"description": "New description"}
        client.put("/api/v1/pvs/standalone/SR:C01:RING:CURR", json=update_payload)

        pvs = client.get("/api/v1/pvs/standalone").json()
        pv = next(p for p in pvs if p["pv_name"] == "SR:C01:RING:CURR")
        assert pv["description"] == "New description"
        # Other fields preserved
        assert pv["protocol"] == "ca"
        assert pv["labels"] == ["diagnostics", "ring"]

    def test_update_standalone_pv_not_found(self, client):
        """Test updating a nonexistent PV returns 404."""
        response = client.put(
            "/api/v1/pvs/standalone/NONEXISTENT:PV",
            json={"description": "Won't work"},
        )
        assert response.status_code == 404


class TestDeleteStandalonePVEndpoint:
    """Test DELETE /api/v1/pvs/standalone/{pv_name}."""

    def test_delete_standalone_pv(self, client, sample_pv_payload):
        """Test successful standalone PV deletion."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        response = client.delete("/api/v1/pvs/standalone/SR:C01:RING:CURR")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "delete"

        # Verify it's gone from standalone list
        pvs = client.get("/api/v1/pvs/standalone").json()
        names = [pv["pv_name"] for pv in pvs]
        assert "SR:C01:RING:CURR" not in names

    def test_delete_standalone_pv_not_found(self, client):
        """Test deleting a nonexistent PV returns 404."""
        response = client.delete("/api/v1/pvs/standalone/NONEXISTENT:PV")
        assert response.status_code == 404

    def test_deleted_pv_removed_from_main_pv_list(self, client, sample_pv_payload):
        """Test that a deleted PV is removed from GET /api/v1/pvs."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        # Verify it's in the main list
        pvs_before = client.get("/api/v1/pvs").json()["pvs"]
        assert "SR:C01:RING:CURR" in pvs_before

        client.delete("/api/v1/pvs/standalone/SR:C01:RING:CURR")

        # Verify it's gone from the main list
        pvs_after = client.get("/api/v1/pvs").json()["pvs"]
        assert "SR:C01:RING:CURR" not in pvs_after


class TestListStandalonePVEndpoints:
    """Test GET /api/v1/pvs/standalone and GET /api/v1/pvs/labels."""

    def test_list_empty(self, client):
        """Test listing standalone PVs when none exist."""
        response = client.get("/api/v1/pvs/standalone")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_with_data(self, client, sample_pv_payload):
        """Test listing standalone PVs with data."""
        client.post("/api/v1/pvs", json=sample_pv_payload)
        client.post("/api/v1/pvs", json={
            "pv_name": "VAC:GAUGE:1",
            "description": "Vacuum gauge",
            "labels": ["vacuum"],
        })

        response = client.get("/api/v1/pvs/standalone")
        assert response.status_code == 200
        pvs = response.json()
        assert len(pvs) == 2

    def test_list_with_label_filter(self, client):
        """Test listing standalone PVs filtered by labels."""
        client.post("/api/v1/pvs", json={
            "pv_name": "PV:A",
            "labels": ["diagnostics", "ring"],
        })
        client.post("/api/v1/pvs", json={
            "pv_name": "PV:B",
            "labels": ["vacuum"],
        })

        response = client.get("/api/v1/pvs/standalone?labels=diagnostics")
        assert response.status_code == 200
        pvs = response.json()
        assert len(pvs) == 1
        assert pvs[0]["pv_name"] == "PV:A"

    def test_list_labels(self, client):
        """Test listing unique labels."""
        client.post("/api/v1/pvs", json={
            "pv_name": "PV:A",
            "labels": ["diagnostics", "ring"],
        })
        client.post("/api/v1/pvs", json={
            "pv_name": "PV:B",
            "labels": ["diagnostics", "vacuum"],
        })

        response = client.get("/api/v1/pvs/labels")
        assert response.status_code == 200
        labels = response.json()
        assert labels == ["diagnostics", "ring", "vacuum"]

    def test_list_labels_empty(self, client):
        """Test listing labels when no PVs exist."""
        response = client.get("/api/v1/pvs/labels")
        assert response.status_code == 200
        assert response.json() == []


class TestStandalonePVExistingEndpointIntegration:
    """Test that standalone PVs integrate with existing PV endpoints."""

    def test_standalone_pv_in_main_pv_list(self, client, sample_pv_payload):
        """Test that standalone PVs appear in GET /api/v1/pvs."""
        # Get PVs before
        before = client.get("/api/v1/pvs").json()["pvs"]
        assert "SR:C01:RING:CURR" not in before

        # Add standalone PV
        client.post("/api/v1/pvs", json=sample_pv_payload)

        # Verify it appears
        after = client.get("/api/v1/pvs").json()["pvs"]
        assert "SR:C01:RING:CURR" in after

    def test_standalone_pv_resolvable(self, client, sample_pv_payload):
        """Test that standalone PVs are resolvable via GET /api/v1/pvs/{name}."""
        client.post("/api/v1/pvs", json=sample_pv_payload)

        response = client.get("/api/v1/pvs/SR:C01:RING:CURR")
        assert response.status_code == 200
        data = response.json()
        assert data["pv"] == "SR:C01:RING:CURR"
        assert data["device_name"] is None


class TestStandalonePVPersistence:
    """Test that standalone PVs survive simulated service restarts."""

    def test_pv_survives_restart(self, tmp_path):
        """Test that a registered standalone PV persists across app restarts."""
        db_path = tmp_path / "persist_test.db"

        payload = {
            "pv_name": "PERSIST:PV:RING",
            "description": "Persistent ring current",
            "labels": ["persistent", "diagnostics"],
        }

        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
    
        )

        # First "session"
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            resp = c1.post("/api/v1/pvs", json=payload)
            assert resp.status_code == 201
            assert "PERSIST:PV:RING" in c1.get("/api/v1/pvs").json()["pvs"]

        # Second "session" (new app, same DB)
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            # Should be in standalone list
            pvs = c2.get("/api/v1/pvs/standalone").json()
            names = [pv["pv_name"] for pv in pvs]
            assert "PERSIST:PV:RING" in names

            # Should be in main PV list
            all_pvs = c2.get("/api/v1/pvs").json()["pvs"]
            assert "PERSIST:PV:RING" in all_pvs

            # Should be resolvable
            pv = c2.get("/api/v1/pvs/PERSIST:PV:RING").json()
            assert pv["pv"] == "PERSIST:PV:RING"
            assert pv["device_name"] is None

    def test_deleted_pv_stays_deleted_after_restart(self, tmp_path):
        """Test that a deleted standalone PV stays deleted after restart."""
        db_path = tmp_path / "delete_persist.db"

        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
    
        )

        # First session: create and delete a PV
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            c1.post("/api/v1/pvs", json={
                "pv_name": "TEMP:PV",
                "description": "Temporary",
            })
            assert "TEMP:PV" in c1.get("/api/v1/pvs").json()["pvs"]

            resp = c1.delete("/api/v1/pvs/standalone/TEMP:PV")
            assert resp.status_code == 200
            assert "TEMP:PV" not in c1.get("/api/v1/pvs").json()["pvs"]

        # Second session: verify it's still gone
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            pvs = c2.get("/api/v1/pvs/standalone").json()
            names = [pv["pv_name"] for pv in pvs]
            assert "TEMP:PV" not in names

            all_pvs = c2.get("/api/v1/pvs").json()["pvs"]
            assert "TEMP:PV" not in all_pvs
