"""Phase 3 regressions: lock-aware mutation, registry serialization, input
validation, the device_class import allowlist, and off-loop resolution.

Covers:
- delete/update of a locked device is rejected (409), and force-unlock /
  registry reset|clear clear the lock table (so a deleted-then-orphaned lock
  can't report the whole beamline locked forever).
- one process-wide lock serializes registry mutations, so concurrent
  same-name creates yield exactly one winner instead of a double-write.
- device names and device-owned PV names are validated like standalone PVs
  (no empty/whitespace/``/``/NUL entries that can't be addressed again).
- when an import allowlist is configured, an out-of-allowlist ``device_class``
  is rejected at create/update and never imported by the resolver.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

from configuration_service.config import Settings
from configuration_service.lock_manager import DeviceLockManager
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


def _device_body(name: str, pvs: dict, device_class: str = "ophyd.signal.EpicsSignal") -> dict:
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
            "device_class": device_class,
            "args": [list(pvs.values())[0]],
            "kwargs": {"name": name},
        },
    }


def _lock(client, names, item_id="item-1", plan="count"):
    return client.post(
        "/api/v1/devices/lock",
        json={"device_names": names, "item_id": item_id, "plan_name": plan},
    )


# ===== 3.2 — lock-aware mutation =============================================


class TestLockedDeviceMutation:
    def test_delete_locked_device_is_rejected(self, client):
        assert _lock(client, ["sample_x"]).status_code == 200

        resp = client.delete("/api/v1/devices/sample_x")
        assert resp.status_code == 409
        assert "locked" in resp.json()["detail"].lower()
        # Still present.
        assert client.get("/api/v1/devices/sample_x").status_code == 200

    def test_update_locked_device_is_rejected(self, client):
        assert _lock(client, ["sample_x"]).status_code == 200

        resp = client.put(
            "/api/v1/devices/sample_x",
            json={"metadata": {"documentation": "changed"}},
        )
        assert resp.status_code == 409
        assert "locked" in resp.json()["detail"].lower()

    def test_unlock_then_delete_succeeds(self, client):
        assert _lock(client, ["sample_x"]).status_code == 200
        assert client.delete("/api/v1/devices/sample_x").status_code == 409

        unlock = client.post(
            "/api/v1/devices/unlock",
            json={"device_names": ["sample_x"], "item_id": "item-1"},
        )
        assert unlock.status_code == 200
        assert client.delete("/api/v1/devices/sample_x").status_code == 200

    def test_reset_clears_locks(self, client):
        assert _lock(client, ["sample_x"], item_id="item-1").status_code == 200

        assert client.post("/api/v1/registry/reset").status_code == 200

        # Reset re-seeds the same mock devices; the lock must be gone, so a
        # fresh acquisition by a different holder succeeds (would 409 if the
        # stale lock survived).
        assert _lock(client, ["sample_x"], item_id="item-2").status_code == 200

    def test_clear_drops_lock_table(self, client):
        assert _lock(client, ["sample_x"]).status_code == 200
        manager = client.app.state.lock_manager_container["manager"]
        assert manager.get_all_locks() != {}

        assert client.post("/api/v1/registry/clear").status_code == 200
        assert manager.get_all_locks() == {}


class TestForceUnlockOrphan:
    """force_unlock clears a lock whose device left the registry, but a name
    that is neither a device nor a held lock is still 'not found'."""

    async def test_force_unlock_clears_orphaned_lock(self):
        class _Reg:
            def get_device(self, name):
                return None  # "ghost" is not (or no longer) registered

            def get_instantiation_spec(self, name):
                return None

        mgr = DeviceLockManager()
        # Simulate an orphan: a held lock for a device no longer in the registry.
        mgr._locks["ghost"] = _make_lock_state("ghost")

        unlocked, not_found = await mgr.force_unlock(["ghost"], _Reg())
        assert unlocked == ["ghost"]
        assert not_found == []
        assert mgr.get_all_locks() == {}

    async def test_force_unlock_unknown_name_still_not_found(self):
        class _Reg:
            def get_device(self, name):
                return None

            def get_instantiation_spec(self, name):
                return None

        mgr = DeviceLockManager()
        unlocked, not_found = await mgr.force_unlock(["never_existed"], _Reg())
        assert unlocked == []
        assert not_found == ["never_existed"]

    async def test_clear_all_drops_every_lock(self):
        class _Dev:
            pvs = {"val": "BL:PV"}

        class _Reg:
            def get_device(self, name):
                return _Dev()

            def get_instantiation_spec(self, name):
                class _S:
                    active = True

                return _S()

        mgr = DeviceLockManager()
        await mgr.acquire_locks(["a", "b"], "item-1", "count", "ee", _Reg())
        before = mgr.version
        cleared = await mgr.clear_all()
        assert set(cleared) == {"a", "b"}
        assert mgr.get_all_locks() == {}
        assert mgr.version == before + 1


def _make_lock_state(name: str):
    from configuration_service.lock_manager import DeviceLockState

    return DeviceLockState(
        device_name=name,
        locked_by_plan="count",
        locked_by_item="item-1",
        locked_by_service="ee",
        lock_id="lid",
    )


# ===== 3.5 — registry mutation serialization ================================


class TestRegistryMutationSerialization:
    async def test_concurrent_same_name_creates_yield_one_winner(self, db_url):
        settings = Settings(
            use_mock_data=True,
            database_url=db_url,
            device_change_history_enabled=True,
        )
        app = create_app(settings)
        body = _device_body("racer", {"val": "BL:RACER:PV"})
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                results = await asyncio.gather(
                    *[ac.post("/api/v1/devices", json=body) for _ in range(8)]
                )
        codes = [r.status_code for r in results]
        # Exactly one create wins; every other request sees the conflict
        # instead of silently double-writing (the pre-lock behavior).
        assert codes.count(201) == 1
        assert codes.count(409) == 7

    async def test_concurrent_distinct_creates_all_succeed(self, db_url):
        settings = Settings(
            use_mock_data=True,
            database_url=db_url,
            device_change_history_enabled=True,
        )
        app = create_app(settings)
        names = [f"dev_{i}" for i in range(8)]
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                results = await asyncio.gather(
                    *[
                        ac.post(
                            "/api/v1/devices",
                            json=_device_body(n, {"val": f"BL:{n}:PV"}),
                        )
                        for n in names
                    ]
                )
                assert all(r.status_code == 201 for r in results)
                listed = (await ac.get("/api/v1/devices")).json()
        registered = set(listed)
        assert set(names) <= registered

    async def test_reset_racing_creates_leave_memory_and_db_consistent(self, db_url):
        # Creates that race a reset must never leave the in-memory registry and
        # the database disagreeing: every mutation re-reads the published state
        # under the lock, so a create can't land on a registry the reset just
        # discarded (which would strand it in the DB but not in memory).
        settings = Settings(
            use_mock_data=True,
            database_url=db_url,
            device_change_history_enabled=True,
        )
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                tasks = [ac.post("/api/v1/registry/reset")]
                tasks += [
                    ac.post(
                        "/api/v1/devices",
                        json=_device_body(f"racer_{i}", {"val": f"BL:R{i}:PV"}),
                    )
                    for i in range(6)
                ]
                await asyncio.gather(*tasks)

                in_memory = set((await ac.get("/api/v1/devices")).json())
                store = app.state.registry_store_container["store"]
                in_db = set((await asyncio.to_thread(store.load_all_devices)).devices.keys())
        assert in_memory == in_db


# ===== 3.6 — name / PV validation ===========================================


class TestDeviceNameAndPVValidation:
    @pytest.mark.parametrize("bad_name", ["", " ", "with space", "a/b", "tab\tname"])
    def test_invalid_device_name_rejected(self, client, bad_name):
        resp = client.post(
            "/api/v1/devices",
            json=_device_body(bad_name, {"val": "BL:OK:PV"}),
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("bad_pv", ["", "pv with space", "pv\x00nul"])
    def test_invalid_pv_value_rejected_on_create(self, client, bad_pv):
        resp = client.post(
            "/api/v1/devices",
            json=_device_body("okdev", {"val": bad_pv}),
        )
        assert resp.status_code == 422

    def test_invalid_pv_value_rejected_on_update(self, client):
        assert (
            client.post(
                "/api/v1/devices", json=_device_body("puttable", {"val": "BL:OK:PV"})
            ).status_code
            == 201
        )
        resp = client.put(
            "/api/v1/devices/puttable",
            json={"metadata": {"pvs": {"val": "bad pv"}}},
        )
        assert resp.status_code == 422

    def test_valid_name_and_pv_accepted(self, client):
        resp = client.post(
            "/api/v1/devices",
            json=_device_body("XF:23ID2-OP", {"val": "XF:23ID2-OP{Mono}Enrgy-SP"}),
        )
        assert resp.status_code == 201


# ===== 3.6 — device_class import allowlist ==================================


class TestDeviceClassAllowlist:
    @pytest.fixture
    def allow_client(self, db_url):
        settings = Settings(
            use_mock_data=True,
            database_url=db_url,
            device_change_history_enabled=True,
            device_class_allowlist=["ophyd."],
        )
        app = create_app(settings)
        with TestClient(app) as c:
            yield c

    def test_allowed_class_accepted(self, allow_client):
        resp = allow_client.post(
            "/api/v1/devices",
            json=_device_body("okdev", {"val": "BL:OK:PV"}, device_class="ophyd.EpicsSignal"),
        )
        assert resp.status_code == 201

    def test_out_of_allowlist_class_rejected_on_create(self, allow_client):
        resp = allow_client.post(
            "/api/v1/devices",
            json=_device_body("evil", {"val": "BL:OK:PV"}, device_class="os.system"),
        )
        assert resp.status_code == 422
        assert "allowlist" in resp.json()["detail"].lower()

    def test_out_of_allowlist_class_rejected_on_update(self, allow_client):
        assert (
            allow_client.post(
                "/api/v1/devices",
                json=_device_body("dev", {"val": "BL:OK:PV"}, device_class="ophyd.EpicsSignal"),
            ).status_code
            == 201
        )
        resp = allow_client.put(
            "/api/v1/devices/dev",
            json={"instantiation_spec": {"device_class": "subprocess.Popen"}},
        )
        assert resp.status_code == 422

    def test_resolver_refuses_out_of_allowlist_class(self, allow_client):
        # Seed a device whose class is outside the allowlist by writing it
        # straight into the in-memory registry (bypassing CRUD validation),
        # then confirm the resolver still refuses to import it.
        from configuration_service.models import DeviceInstantiationSpec, DeviceMetadata

        state = allow_client.app.state.state_container["state"]
        state.registry.add_device(
            DeviceMetadata(
                name="sneaky",
                device_label="signal",
                ophyd_class="X",
                pvs={"val": "BL:SNEAKY:PV"},
            ),
            DeviceInstantiationSpec(
                name="sneaky",
                device_class="os.system",
                args=["BL:SNEAKY:PV"],
                kwargs={"name": "sneaky"},
            ),
        )
        resp = allow_client.post("/api/v1/devices/resolve", json={"addresses": ["sneaky.val"]})
        assert resp.status_code == 200
        row = resp.json()["resolved"][0]
        assert row["ok"] is False
        assert row["outcome"] == "import_failed"
        assert "allowlist" in row["message"].lower()

    def test_resolver_allows_mock_devices_under_allowlist(self, allow_client):
        # All mock devices use ophyd.* classes, so they still resolve.
        resp = allow_client.post(
            "/api/v1/devices/resolve", json={"addresses": ["sample_x.user_setpoint"]}
        )
        assert resp.status_code == 200
        assert resp.json()["resolved"][0]["ok"] is True
