"""Registry CRUD integrity regressions.

Shared-PV ownership: ``add_device`` reassigns an already-owned PV and
``remove_device`` used to DELETE every PV the removed device owned, even when
another device (or a standalone registration) still claimed it — the
survivor's registry entry 404'd until restart. Now removal re-homes shared
PVs and restores standalone status instead of deleting.

Persistence ordering: CRUD endpoints used to mutate the in-memory
registry BEFORE the DB write, so a store failure left a phantom device that
served reads, 409'd the retry, and vanished on restart. Now persistence runs
first; on failure the registry is unchanged and the retry succeeds.
"""

from __future__ import annotations

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
    # raise_server_exceptions=False: the persist-failure tests assert the
    # HTTP 500 the client would see, not the underlying exception.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _device_body(name: str, pvs: dict) -> dict:
    return {
        "metadata": {
            "name": name,
            "device_label": "signal",
            "ophyd_class": "EpicsSignal",
            "module": "ophyd.signal",
            "pvs": pvs,
            "is_readable": True,
        },
        "instantiation_spec": {
            "name": name,
            "device_class": "ophyd.signal.EpicsSignal",
            "args": [list(pvs.values())[0]],
            "kwargs": {"name": name},
        },
    }


def _pv_status(client, pv: str):
    return client.get("/api/v1/pvs/status", params={"pv_name": pv})


# ===== Shared-PV ownership survives device removal ===========================


class TestSharedPVOwnership:
    def test_removing_one_owner_rehomes_shared_pv(self, client):
        shared = "BL:SHARED:PV"
        assert client.post("/api/v1/devices", json=_device_body("dev_a", {"val": shared})).status_code == 201
        assert client.post("/api/v1/devices", json=_device_body("dev_b", {"val": shared})).status_code == 201

        # dev_b registered last and owns the index entry; deleting it must
        # re-home the PV to dev_a, not destroy the entry.
        assert client.delete("/api/v1/devices/dev_b").status_code == 200

        resp = _pv_status(client, shared)
        assert resp.status_code == 200, resp.text
        assert resp.json()["device_name"] == "dev_a"

    def test_removing_device_restores_standalone_pv(self, client):
        pv = "BL:RING:CURRENT"
        resp = client.post(
            "/api/v1/pvs",
            json={"pv_name": pv, "description": "ring current"},
        )
        assert resp.status_code == 201, resp.text

        # A device claims the standalone PV, then is removed: the standalone
        # registration must survive (pre-fix the entry was deleted outright).
        assert client.post("/api/v1/devices", json=_device_body("claimer", {"val": pv})).status_code == 201
        assert client.delete("/api/v1/devices/claimer").status_code == 200

        resp = _pv_status(client, pv)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["device_name"] is None
        assert body["available"] is True

    def test_deleting_standalone_keeps_device_owned_entry(self, client):
        pv = "BL:DUAL:PV"
        assert client.post("/api/v1/pvs", json={"pv_name": pv}).status_code == 201
        assert client.post("/api/v1/devices", json=_device_body("owner_dev", {"val": pv})).status_code == 201

        # Deleting the standalone registration must not destroy the
        # device-owned index entry.
        assert client.delete(f"/api/v1/pvs/standalone/{pv}").status_code == 200

        resp = _pv_status(client, pv)
        assert resp.status_code == 200, resp.text
        assert resp.json()["device_name"] == "owner_dev"

    def test_sole_owner_removal_still_drops_pv(self, client):
        pv = "BL:ONLY:PV"
        assert client.post("/api/v1/devices", json=_device_body("solo", {"val": pv})).status_code == 201
        assert client.delete("/api/v1/devices/solo").status_code == 200
        assert _pv_status(client, pv).status_code == 404


# ===== Persist-before-mutate =================================================


class TestPersistBeforeMutate:
    def test_create_failure_leaves_no_phantom_and_retry_succeeds(self, client, monkeypatch):
        from configuration_service.device_registry_store import DeviceRegistryStore

        original = DeviceRegistryStore.save_device

        def _boom(self, *args, **kwargs):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(DeviceRegistryStore, "save_device", _boom)
        resp = client.post("/api/v1/devices", json=_device_body("flaky", {"val": "BL:F:PV"}))
        assert resp.status_code == 500

        # No phantom: the device must not serve reads...
        assert client.get("/api/v1/devices/flaky").status_code == 404

        # ...and the retry must succeed once the store is healthy (pre-fix
        # it 409'd on the in-memory duplicate).
        monkeypatch.setattr(DeviceRegistryStore, "save_device", original)
        resp = client.post("/api/v1/devices", json=_device_body("flaky", {"val": "BL:F:PV"}))
        assert resp.status_code == 201, resp.text
        assert client.get("/api/v1/devices/flaky").status_code == 200

    def test_delete_failure_keeps_device_fully_alive(self, client, monkeypatch):
        from configuration_service.device_registry_store import DeviceRegistryStore

        assert client.post("/api/v1/devices", json=_device_body("sticky", {"val": "BL:S:PV"})).status_code == 201

        def _boom(self, *args, **kwargs):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(DeviceRegistryStore, "delete_device", _boom)
        assert client.delete("/api/v1/devices/sticky").status_code == 500

        # Memory and DB stay consistent: the device still exists everywhere.
        assert client.get("/api/v1/devices/sticky").status_code == 200
        assert _pv_status(client, "BL:S:PV").status_code == 200

    def test_standalone_create_failure_leaves_no_phantom(self, client, monkeypatch):
        from configuration_service.standalone_pv_store import StandalonePVStore

        def _boom(self, *args, **kwargs):
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(StandalonePVStore, "save_pv", _boom)
        resp = client.post("/api/v1/pvs", json={"pv_name": "BL:P:PV"})
        assert resp.status_code == 500

        assert _pv_status(client, "BL:P:PV").status_code == 404
