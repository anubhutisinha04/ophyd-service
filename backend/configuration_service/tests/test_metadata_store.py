"""
Tests for generic metadata key-value store.

Tests both the MetadataStore persistence layer and the REST API endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from configuration_service.main import create_app
from configuration_service.config import Settings
from configuration_service.metadata_store import MetadataStore


# ===== Fixtures =====


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test_metadata.db"


@pytest.fixture
def client(tmp_db):
    settings = Settings(
        use_mock_data=True,
        db_path=tmp_db,
        device_change_history_enabled=True,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_value():
    return {
        "value": {
            "sample_id": "NaCl-042",
            "composition": "NaCl",
            "holder": "capillary-1mm",
            "temperature_setpoint": 300,
            "notes": "pre-annealed at 500K for 2hr",
        }
    }


# ===== MetadataStore Unit Tests =====


class TestMetadataStore:

    def test_initialize(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()
        assert store._initialized is True
        store.initialize()  # safe to call again
        store.close()

    def test_save_and_get(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        store.save("sample_info", {"sample_id": "ABC-123", "operator": "Jane"})

        result = store.get("sample_info")
        assert result is not None
        assert result["key"] == "sample_info"
        assert result["value"]["sample_id"] == "ABC-123"
        assert result["value"]["operator"] == "Jane"
        assert result["created_at"] is not None
        assert result["updated_at"] is not None

        store.close()

    def test_save_update_preserves_created_at(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        store.save("key1", {"v": 1})
        first = store.get("key1")
        created_at = first["created_at"]

        store.save("key1", {"v": 2})
        second = store.get("key1")

        assert second["created_at"] == created_at
        assert second["value"]["v"] == 2
        assert second["updated_at"] >= second["created_at"]

        store.close()

    def test_get_nonexistent(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()
        assert store.get("nonexistent") is None
        store.close()

    def test_delete(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        store.save("key1", {"v": 1})
        assert store.delete("key1") is True
        assert store.get("key1") is None
        assert store.delete("key1") is False

        store.close()

    def test_get_all(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        store.save("b_key", {"v": 2})
        store.save("a_key", {"v": 1})
        store.save("c_key", {"v": 3})

        results = store.get_all()
        assert len(results) == 3
        assert results[0]["key"] == "a_key"  # sorted
        assert results[1]["key"] == "b_key"
        assert results[2]["key"] == "c_key"

        store.close()

    def test_get_all_empty(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()
        assert store.get_all() == []
        store.close()

    def test_clear_all(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        store.save("a", {"v": 1})
        store.save("b", {"v": 2})

        count = store.clear_all()
        assert count == 2
        assert store.get_all() == []

        store.close()

    def test_persists_across_reopen(self, tmp_db):
        store1 = MetadataStore(tmp_db)
        store1.initialize()
        store1.save("persistent", {"data": "survives"})
        store1.close()

        store2 = MetadataStore(tmp_db)
        store2.initialize()
        result = store2.get("persistent")
        assert result is not None
        assert result["value"]["data"] == "survives"
        store2.close()

    def test_value_is_arbitrary_json(self, tmp_db):
        store = MetadataStore(tmp_db)
        store.initialize()

        complex_value = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "nested": {"a": {"b": {"c": "deep"}}},
        }
        store.save("complex", complex_value)

        result = store.get("complex")
        assert result["value"] == complex_value

        store.close()


# ===== API Endpoint Tests =====


class TestCreateMetadataEndpoint:

    def test_create_metadata(self, client, sample_value):
        response = client.post("/api/v1/metadata/sample_info", json=sample_value)
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True
        assert data["key"] == "sample_info"
        assert data["operation"] == "create"

    def test_create_duplicate_key_409(self, client, sample_value):
        client.post("/api/v1/metadata/sample_info", json=sample_value)
        response = client.post("/api/v1/metadata/sample_info", json=sample_value)
        assert response.status_code == 409

    def test_create_with_nested_value(self, client):
        payload = {
            "value": {
                "experiment": {
                    "type": "XRD",
                    "parameters": {"wavelength": 0.7749, "exposure": 60},
                },
                "tags": ["commissioning", "standard"],
            }
        }
        response = client.post("/api/v1/metadata/experiment_config", json=payload)
        assert response.status_code == 201


class TestGetMetadataEndpoint:

    def test_get_existing(self, client, sample_value):
        client.post("/api/v1/metadata/sample_info", json=sample_value)

        response = client.get("/api/v1/metadata/sample_info")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "sample_info"
        assert data["value"]["sample_id"] == "NaCl-042"
        assert data["created_at"] is not None

    def test_get_nonexistent_404(self, client):
        response = client.get("/api/v1/metadata/nonexistent")
        assert response.status_code == 404


class TestListMetadataEndpoint:

    def test_list_empty(self, client):
        response = client.get("/api/v1/metadata")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_with_data(self, client):
        client.post("/api/v1/metadata/key_a", json={"value": {"v": 1}})
        client.post("/api/v1/metadata/key_b", json={"value": {"v": 2}})

        response = client.get("/api/v1/metadata")
        assert response.status_code == 200
        entries = response.json()
        assert len(entries) == 2
        assert entries[0]["key"] == "key_a"
        assert entries[1]["key"] == "key_b"


class TestUpsertMetadataEndpoint:

    def test_upsert_creates_new(self, client):
        response = client.put("/api/v1/metadata/new_key", json={"value": {"v": 1}})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "create"

    def test_upsert_updates_existing(self, client):
        client.post("/api/v1/metadata/key1", json={"value": {"v": 1}})

        response = client.put("/api/v1/metadata/key1", json={"value": {"v": 2}})
        assert response.status_code == 200
        data = response.json()
        assert data["operation"] == "update"

        # Verify value changed
        entry = client.get("/api/v1/metadata/key1").json()
        assert entry["value"]["v"] == 2


class TestDeleteMetadataEndpoint:

    def test_delete_existing(self, client, sample_value):
        client.post("/api/v1/metadata/sample_info", json=sample_value)

        response = client.delete("/api/v1/metadata/sample_info")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["operation"] == "delete"

        # Verify it's gone
        assert client.get("/api/v1/metadata/sample_info").status_code == 404

    def test_delete_nonexistent_404(self, client):
        response = client.delete("/api/v1/metadata/nonexistent")
        assert response.status_code == 404


class TestMetadataPersistence:

    def test_survives_restart(self, tmp_path):
        db_path = tmp_path / "persist_test.db"
        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
        )

        # First session
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            resp = c1.post(
                "/api/v1/metadata/sample_info",
                json={"value": {"sample_id": "ABC-123"}},
            )
            assert resp.status_code == 201

        # Second session (new app, same DB)
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            resp = c2.get("/api/v1/metadata/sample_info")
            assert resp.status_code == 200
            assert resp.json()["value"]["sample_id"] == "ABC-123"

    def test_deleted_stays_deleted_after_restart(self, tmp_path):
        db_path = tmp_path / "delete_persist.db"
        settings = Settings(
            use_mock_data=True,
            db_path=db_path,
            device_change_history_enabled=True,
        )

        # First session: create and delete
        app1 = create_app(settings)
        with TestClient(app1) as c1:
            c1.post("/api/v1/metadata/temp", json={"value": {"v": 1}})
            c1.delete("/api/v1/metadata/temp")

        # Second session: verify still gone
        app2 = create_app(settings)
        with TestClient(app2) as c2:
            assert c2.get("/api/v1/metadata/temp").status_code == 404
