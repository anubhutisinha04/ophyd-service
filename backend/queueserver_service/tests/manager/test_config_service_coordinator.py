"""Unit tests for ConfigServiceCoordinator.

The config-service orchestration used to live as methods on the RE manager (a
``multiprocessing.Process`` subclass), which made it effectively untestable in
isolation. Extracted into ``ConfigServiceCoordinator`` with a ``ConfigServiceHost``
Protocol, it can now be driven directly with a fake host + fake client — these
tests pin the lock-bookkeeping invariants, the env-open/staleness flows, and the
diff/sync helpers.
"""

import asyncio

import pytest

from queueserver_service.manager import config_service_coordinator as coord_mod
from queueserver_service.manager.config_service import (
    ConfigServiceConflict,
    ConfigServiceSettings,
    ConfigServiceState,
)
from queueserver_service.manager.config_service_coordinator import (
    ConfigServiceCoordinator,
)


def _run(coro):
    return asyncio.run(coro)


class FakeHost:
    def __init__(self, existing_devices=None):
        self._existing_devices = existing_devices or {}
        self.overlay_calls = []
        self.reload_calls = 0
        self.overlay_result = (True, "")
        self.reload_result = True

    @property
    def existing_devices(self):
        return self._existing_devices

    async def worker_update_device_overlay(self, upserts, deletes, *, replace):
        self.overlay_calls.append((upserts, deletes, replace))
        return self.overlay_result

    async def reload_lists_from_worker(self):
        self.reload_calls += 1
        return self.reload_result


class FakeClient:
    def __init__(self, instantiation_specs=None):
        self.instantiation_specs = instantiation_specs or {}
        self.locked = []
        self.unlocked = []
        self.force_unlocked = []
        self.renewed = []
        self.changes_response = None
        self.lock_error_once = None
        self.unlock_error = None
        self.closed = False
        # Lease/epoch surface returned by lock_devices/renew_locks (fix #1).
        self.lock_epoch = "epoch-1"
        self.lease_ttl_seconds = 0.0
        self.renew_response = None

    def _lock_body(self):
        return {
            "lock_epoch": self.lock_epoch,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "expires_at": None,
        }

    async def get_instantiation_specs(self):
        return dict(self.instantiation_specs)

    async def get_changes_since(self, since_version):
        return self.changes_response

    async def lock_devices(self, device_names, *, item_id, plan_name):
        if self.lock_error_once is not None:
            err, self.lock_error_once = self.lock_error_once, None
            raise err
        self.locked.append((list(device_names), item_id, plan_name))
        return self._lock_body()

    async def unlock_devices(self, device_names, *, item_id):
        if self.unlock_error is not None:
            raise self.unlock_error
        self.unlocked.append((list(device_names), item_id))
        return {}

    async def renew_locks(self, device_names, *, item_id):
        self.renewed.append((list(device_names), item_id))
        if self.renew_response is not None:
            return self.renew_response
        return {
            "success": True,
            "renewed_devices": list(device_names),
            "lost_devices": [],
            "conflict_devices": [],
            "lock_epoch": self.lock_epoch,
            "expires_at": None,
        }

    async def force_unlock_devices(self, device_names, *, reason):
        self.force_unlocked.append((list(device_names), reason))
        return {}

    async def upsert_device(self, metadata, spec):
        return {}

    async def delete_device(self, name):
        return {}

    async def aclose(self):
        self.closed = True


def _coord(*, enabled=True, lock_scope="plan", host=None, client=None):
    settings = (
        ConfigServiceSettings(enabled=True, url="http://cfg", lock_scope=lock_scope)
        if enabled
        else ConfigServiceSettings()
    )
    c = ConfigServiceCoordinator(settings, host=host or FakeHost())
    if client is not None:
        c._client = client  # inject fake, bypassing the lazy real client
    return c


# --- disabled coordinator is inert -----------------------------------------


def test_disabled_prefetch_returns_empty_and_no_client():
    async def testing():
        c = _coord(enabled=False)
        assert await c.prefetch_registry() == {}
        assert c._client is None

    _run(testing())


def test_disabled_unlock_and_release_are_noops():
    async def testing():
        client = FakeClient()
        c = _coord(enabled=False, client=client)
        await c.unlock_devices()
        assert await c.release_plan_scope_lock(suppress_errors=False) is True
        await c.check_staleness_before_plan()  # returns immediately
        assert client.unlocked == []

    _run(testing())


# --- lock/unlock bookkeeping ------------------------------------------------


def test_lock_devices_for_plan_records_bookkeeping(monkeypatch):
    monkeypatch.setattr(
        coord_mod, "extract_device_names_from_plan",
        lambda item, *, existing_devices: ["det1", "det2"],
    )

    async def testing():
        client = FakeClient()
        c = _coord(lock_scope="plan", client=client)
        await c.lock_devices_for_plan(
            {"name": "count", "item_uid": "uid-1", "args": [], "kwargs": {}}
        )
        assert client.locked == [(["det1", "det2"], "uid-1", "count")]
        assert c._locked_devices == ["det1", "det2"]
        assert c._locked_item_id == "uid-1"

    _run(testing())


def test_lock_devices_for_plan_settles_prior_debt_first(monkeypatch):
    monkeypatch.setattr(
        coord_mod, "extract_device_names_from_plan",
        lambda item, *, existing_devices: ["detA"],
    )

    async def testing():
        client = FakeClient()
        c = _coord(lock_scope="plan", client=client)
        # Pretend a previous plan's lock is still on the books.
        c._locked_devices = ["old"]
        c._locked_item_id = "old-uid"
        await c.lock_devices_for_plan(
            {"name": "scan", "item_uid": "uid-2", "args": [], "kwargs": {}}
        )
        # Prior lock released before the new one is taken.
        assert client.unlocked == [(["old"], "old-uid")]
        assert client.locked == [(["detA"], "uid-2", "scan")]
        assert c._locked_item_id == "uid-2"

    _run(testing())


def test_lock_devices_for_plan_no_devices_takes_no_lock(monkeypatch):
    monkeypatch.setattr(
        coord_mod, "extract_device_names_from_plan",
        lambda item, *, existing_devices: [],
    )

    async def testing():
        client = FakeClient()
        c = _coord(lock_scope="plan", client=client)
        await c.lock_devices_for_plan({"name": "noop", "item_uid": "u", "args": []})
        assert client.locked == []
        assert c._locked_item_id == ""

    _run(testing())


def test_unlock_clears_bookkeeping_on_success():
    async def testing():
        client = FakeClient()
        c = _coord(client=client)
        c._locked_devices = ["d1"]
        c._locked_item_id = "u1"
        await c.unlock_devices()
        assert client.unlocked == [(["d1"], "u1")]
        assert c._locked_devices == []
        assert c._locked_item_id == ""

    _run(testing())


def test_unlock_keeps_bookkeeping_on_suppressed_failure():
    async def testing():
        client = FakeClient()
        client.unlock_error = RuntimeError("config-service down")
        c = _coord(client=client)
        c._locked_devices = ["d1"]
        c._locked_item_id = "u1"
        # suppress_errors -> logs, keeps the debt on the books.
        await c.unlock_devices(suppress_errors=True)
        assert c._locked_devices == ["d1"]
        assert c._locked_item_id == "u1"

    _run(testing())


def test_unlock_raises_on_unsuppressed_failure():
    async def testing():
        client = FakeClient()
        client.unlock_error = RuntimeError("boom")
        c = _coord(client=client)
        c._locked_devices = ["d1"]
        c._locked_item_id = "u1"
        with pytest.raises(RuntimeError):
            await c.unlock_devices()

    _run(testing())


def test_release_plan_scope_lock_noop_in_environment_scope():
    async def testing():
        client = FakeClient()
        c = _coord(lock_scope="environment", client=client)
        c._locked_devices = ["d1"]
        c._locked_item_id = "u1"
        assert await c.release_plan_scope_lock(suppress_errors=False) is True
        # environment scope: release is a no-op mid-queue.
        assert client.unlocked == []

    _run(testing())


# --- restart-recovery on env-scope lock conflict ---------------------------


def test_lock_with_restart_recovery_force_unlocks_then_retries():
    async def testing():
        client = FakeClient()
        client.lock_error_once = ConfigServiceConflict(409, "held by dead instance")
        c = _coord(lock_scope="environment", client=client)
        await c._lock_with_restart_recovery(
            client, ["d1", "d2"], item_id="env:new", plan_name="__environment__"
        )
        assert len(client.force_unlocked) == 1
        assert client.locked == [(["d1", "d2"], "env:new", "__environment__")]

    _run(testing())


# --- new_env_lock_item_id + device_data ------------------------------------


def test_new_env_lock_item_id_changes_id():
    c = _coord()
    first = c._lock_item_id
    c.new_env_lock_item_id()
    assert c._lock_item_id != first
    assert c._lock_item_id.startswith("env:")


def test_set_device_data_roundtrip():
    c = _coord()
    data = {"det1": {"metadata": {"name": "det1"}, "spec": {"active": True}}}
    c.set_device_data(data)
    assert c.device_data == data


# --- staleness check --------------------------------------------------------


def test_check_staleness_noop_when_no_changes():
    async def testing():
        client = FakeClient()
        client.changes_response = {
            "current_version": 5,
            "service_epoch": "e1",
            "reset_occurred": False,
            "changes": [],
        }
        host = FakeHost()
        c = _coord(client=client, host=host)
        c._state = ConfigServiceState(cursor=5, epoch="e1")
        await c.check_staleness_before_plan()
        # no changes -> worker overlay not touched, lists not reloaded.
        assert host.overlay_calls == []
        assert host.reload_calls == 0

    _run(testing())


def test_check_staleness_applies_overlay_and_reloads():
    async def testing():
        client = FakeClient()
        client.changes_response = {
            "current_version": 6,
            "service_epoch": "e1",
            "reset_occurred": False,
            "changes": [
                {"device_name": "detNew", "op": "upsert", "spec": {"active": True}}
            ],
        }
        host = FakeHost()
        c = _coord(client=client, host=host)
        c._state = ConfigServiceState(cursor=5, epoch="e1")
        await c.check_staleness_before_plan()
        assert len(host.overlay_calls) == 1
        upserts, deletes, replace = host.overlay_calls[0]
        assert "detNew" in upserts and deletes == [] and replace is False
        assert host.reload_calls == 1
        assert c._state.cursor == 6

    _run(testing())


def test_check_staleness_raises_when_worker_rejects_overlay():
    async def testing():
        client = FakeClient()
        client.changes_response = {
            "current_version": 6,
            "service_epoch": "e1",
            "reset_occurred": False,
            "changes": [
                {"device_name": "detNew", "op": "upsert", "spec": {"active": True}}
            ],
        }
        host = FakeHost()
        host.overlay_result = (False, "worker said no")
        c = _coord(client=client, host=host)
        c._state = ConfigServiceState(cursor=5, epoch="e1")
        with pytest.raises(RuntimeError, match="overlay update rejected"):
            await c.check_staleness_before_plan()

    _run(testing())


# --- diff / sync endpoints --------------------------------------------------


def test_compute_diff_against_registry_uses_device_data():
    async def testing():
        # Registry has det1 only; worker (device_data) reports det1 (changed) + det2.
        client = FakeClient(instantiation_specs={"det1": {"class": "Old"}})
        c = _coord(client=client)
        c.set_device_data(
            {
                "det1": {"metadata": {"name": "det1"}, "spec": {"class": "New"}},
                "det2": {"metadata": {"name": "det2"}, "spec": {"class": "X"}},
            }
        )
        diff = await c.compute_diff_against_registry()
        assert diff.added == ["det2"]
        assert [m["name"] for m in diff.modified] == ["det1"]

    _run(testing())


def test_apply_sync_all_upserts_added_and_returns_diff_after():
    async def testing():
        client = FakeClient(instantiation_specs={})  # empty registry
        c = _coord(client=client)
        c.set_device_data(
            {"det2": {"metadata": {"name": "det2"}, "spec": {"class": "X"}}}
        )
        result = await c.apply_sync(strategy="all", selected=None)
        assert result["applied"]["upserted"] == ["det2"]
        assert "diff_after" in result

    _run(testing())


# --- close ------------------------------------------------------------------


def test_close_closes_client():
    async def testing():
        client = FakeClient()
        c = _coord(client=client)
        await c.close()
        assert client.closed is True
        assert c._client is None

    _run(testing())


# ===========================================================================
# Migrated from test_config_service.py: lock-state reconciliation + restart
# recovery. These used to drive the RE manager's config-service methods via a
# bare-manager stub; they now drive ConfigServiceCoordinator directly.
# ===========================================================================


class _LockClientStub:
    """Records lock/unlock calls; optionally fails unlocks (per-call lock errors)."""

    def __init__(self, *, unlock_error=None, lock_errors=None, force_unlock_error=None):
        self.lock_calls = []
        self.unlock_calls = []
        self.force_unlock_calls = []
        self.unlock_error = unlock_error
        self._lock_errors = list(lock_errors) if lock_errors else []
        self.force_unlock_error = force_unlock_error

    async def lock_devices(self, device_names, *, item_id, plan_name):
        self.lock_calls.append((list(device_names), item_id, plan_name))
        if self._lock_errors:
            err = self._lock_errors.pop(0)
            if err is not None:
                raise err
        return {"success": True}

    async def unlock_devices(self, device_names, *, item_id):
        self.unlock_calls.append((list(device_names), item_id))
        if self.unlock_error is not None:
            raise self.unlock_error
        return {"success": True}

    async def force_unlock_devices(self, device_names, *, reason):
        self.force_unlock_calls.append((list(device_names), reason))
        if self.force_unlock_error is not None:
            raise self.force_unlock_error
        return {"success": True, "unlocked_devices": list(device_names)}


def _env_coord(client, *, devices=("m1", "det1")):
    """Coordinator in environment lock scope, with a fixed env lock-owner id."""
    c = _coord(
        lock_scope="environment",
        host=FakeHost(existing_devices={name: {} for name in devices}),
        client=client,
    )
    c._lock_item_id = "env:test-uid"
    return c


@pytest.fixture
def _stub_devices_sync(monkeypatch):
    """Stub the bootstrap/cursor half of sync_on_env_open so these tests exercise
    only the lock-state portion. The coordinator imported the name into its own
    module namespace, so patch it there."""
    async def _fake_sync(client, *, expected_device_names, device_data, prefetched_info):
        return ConfigServiceState(cursor=7, epoch="epoch-1")

    monkeypatch.setattr(coord_mod, "sync_devices_on_env_open", _fake_sync)


def test_get_client_returns_same_instance_across_calls():
    async def testing():
        c = _coord(enabled=True)  # no injected client -> lazy real client
        c1 = await c.get_client()
        c2 = await c.get_client()
        try:
            assert c1 is c2
        finally:
            await c.close()

    _run(testing())


def test_env_unlock_failure_keeps_bookkeeping():
    async def testing():
        client = _LockClientStub(unlock_error=RuntimeError("down"))
        c = _env_coord(client)
        c._locked_devices = ["m1", "det1"]
        c._locked_item_id = "env:test-uid"
        with pytest.raises(RuntimeError):
            await c.unlock_devices()
        assert c._locked_devices == ["m1", "det1"]
        assert c._locked_item_id == "env:test-uid"

    _run(testing())


def test_env_open_reconciles_stale_locks(_stub_devices_sync):
    async def testing():
        client = _LockClientStub()
        c = _env_coord(client, devices=("new1", "new2"))
        c._locked_devices = ["old1", "old2"]
        c._locked_item_id = "env:old-uid"
        await c.sync_on_env_open()
        assert client.unlock_calls == [(["old1", "old2"], "env:old-uid")]
        assert len(client.lock_calls) == 1
        assert sorted(client.lock_calls[0][0]) == ["new1", "new2"]
        assert client.lock_calls[0][1] == "env:test-uid"
        assert c._locked_item_id == "env:test-uid"

    _run(testing())


def test_env_open_skips_relock_within_same_env(_stub_devices_sync):
    async def testing():
        client = _LockClientStub()
        c = _env_coord(client)
        c._locked_devices = ["m1", "det1"]
        c._locked_item_id = "env:test-uid"
        await c.sync_on_env_open()
        assert client.lock_calls == []
        assert client.unlock_calls == []
        assert c._locked_devices == ["m1", "det1"]

    _run(testing())


def test_env_open_reconcile_failure_fails_loudly(_stub_devices_sync):
    async def testing():
        client = _LockClientStub(unlock_error=RuntimeError("still down"))
        c = _env_coord(client)
        c._locked_devices = ["old1"]
        c._locked_item_id = "env:old-uid"
        with pytest.raises(RuntimeError):
            await c.sync_on_env_open()
        assert client.lock_calls == []
        assert c._locked_devices == ["old1"]
        assert c._locked_item_id == "env:old-uid"

    _run(testing())


def test_restart_recovery_force_unlocks_then_retries(_stub_devices_sync):
    async def testing():
        client = _LockClientStub(
            lock_errors=[ConfigServiceConflict(409, {"detail": "locked by dead"}), None]
        )
        c = _env_coord(client, devices=("m1", "det1"))
        assert c._locked_item_id == ""
        await c.sync_on_env_open()
        assert len(client.lock_calls) == 2
        assert all(call[1] == "env:test-uid" for call in client.lock_calls)
        assert len(client.force_unlock_calls) == 1
        _, reason = client.force_unlock_calls[0]
        assert "restart recovery" in reason and "env:test-uid" in reason
        assert sorted(c._locked_devices) == ["det1", "m1"]
        assert c._locked_item_id == "env:test-uid"

    _run(testing())


def test_restart_recovery_bounded_to_one_retry(_stub_devices_sync):
    async def testing():
        conflict = ConfigServiceConflict(409, {"detail": "still locked"})
        client = _LockClientStub(lock_errors=[conflict, conflict])
        c = _env_coord(client, devices=("m1",))
        with pytest.raises(ConfigServiceConflict):
            await c.sync_on_env_open()
        assert len(client.force_unlock_calls) == 1
        assert len(client.lock_calls) == 2
        assert c._locked_devices == []
        assert c._locked_item_id == ""

    _run(testing())


def test_restart_recovery_force_unlock_failure_propagates(_stub_devices_sync):
    async def testing():
        client = _LockClientStub(
            lock_errors=[ConfigServiceConflict(409, {"detail": "locked"})],
            force_unlock_error=RuntimeError("down"),
        )
        c = _env_coord(client, devices=("m1",))
        with pytest.raises(RuntimeError):
            await c.sync_on_env_open()
        assert len(client.lock_calls) == 1
        assert len(client.force_unlock_calls) == 1
        assert c._locked_devices == []

    _run(testing())


def test_happy_path_does_not_force_unlock(_stub_devices_sync):
    async def testing():
        client = _LockClientStub()
        c = _env_coord(client, devices=("m1",))
        await c.sync_on_env_open()
        assert len(client.lock_calls) == 1
        assert client.force_unlock_calls == []
        assert c._locked_devices == ["m1"]

    _run(testing())


# --- lock lease heartbeat (fix #1) ------------------------------------------


def _held_coord(client, *, lease_ttl=30.0, epoch="epoch-1"):
    """Coordinator that already holds a per-plan lock, ready for a heartbeat."""
    c = _coord(lock_scope="plan", client=client)
    c._locked_devices = ["det1"]
    c._locked_item_id = "uid-1"
    c._locked_plan_name = "count"
    c._lock_epoch = epoch
    c._lease_ttl_seconds = lease_ttl
    return c


def test_lock_records_epoch_and_lease_and_starts_heartbeat(monkeypatch):
    monkeypatch.setattr(
        coord_mod, "extract_device_names_from_plan",
        lambda item, *, existing_devices: ["det1"],
    )

    async def testing():
        client = FakeClient()
        client.lock_epoch = "epoch-42"
        client.lease_ttl_seconds = 30.0
        c = _coord(lock_scope="plan", client=client)
        await c.lock_devices_for_plan(
            {"name": "count", "item_uid": "uid-1", "args": [], "kwargs": {}}
        )
        assert c._lock_epoch == "epoch-42"
        assert c._lease_ttl_seconds == 30.0
        # A lease > 0 starts the background heartbeat; stop it so the loop closes.
        assert c._heartbeat_task is not None
        await c._stop_heartbeat()

    _run(testing())


def test_no_heartbeat_when_lease_disabled(monkeypatch):
    monkeypatch.setattr(
        coord_mod, "extract_device_names_from_plan",
        lambda item, *, existing_devices: ["det1"],
    )

    async def testing():
        client = FakeClient()
        client.lease_ttl_seconds = 0.0  # leases disabled
        c = _coord(lock_scope="plan", client=client)
        await c.lock_devices_for_plan(
            {"name": "count", "item_uid": "uid-1", "args": [], "kwargs": {}}
        )
        assert c._heartbeat_task is None

    _run(testing())


def test_heartbeat_once_renews_and_updates_epoch():
    async def testing():
        client = FakeClient()
        client.lock_epoch = "epoch-1"
        c = _held_coord(client, epoch="epoch-1")

        await c._heartbeat_once()
        # Renewed exactly the held set under the held item id; no re-acquire.
        assert client.renewed == [(["det1"], "uid-1")]
        assert client.locked == []
        assert c._lock_epoch == "epoch-1"

    _run(testing())


def test_heartbeat_reacquires_on_lost():
    async def testing():
        client = FakeClient()
        client.renew_response = {
            "success": False,
            "renewed_devices": [],
            "lost_devices": ["det1"],
            "conflict_devices": [],
            "lock_epoch": "epoch-1",
            "expires_at": None,
        }
        c = _held_coord(client, epoch="epoch-1")

        await c._heartbeat_once()
        # A lost lease triggers a re-acquire under the same item id + plan name.
        assert client.locked == [(["det1"], "uid-1", "count")]

    _run(testing())


def test_heartbeat_reacquires_on_epoch_change():
    async def testing():
        client = FakeClient()
        # The authority restarted: renew reports a new epoch (locks were wiped).
        client.renew_response = {
            "success": True,
            "renewed_devices": ["det1"],
            "lost_devices": [],
            "conflict_devices": [],
            "lock_epoch": "epoch-2",
            "expires_at": None,
        }
        client.lock_epoch = "epoch-2"
        c = _held_coord(client, epoch="epoch-1")

        await c._heartbeat_once()
        assert client.locked == [(["det1"], "uid-1", "count")]
        assert c._lock_epoch == "epoch-2"

    _run(testing())


def test_heartbeat_once_noop_when_nothing_held():
    async def testing():
        client = FakeClient()
        c = _coord(lock_scope="plan", client=client)
        c._lease_ttl_seconds = 30.0  # leases on, but no lock held
        await c._heartbeat_once()
        assert client.renewed == []

    _run(testing())


def test_heartbeat_once_noop_when_lease_disabled():
    async def testing():
        client = FakeClient()
        c = _held_coord(client, lease_ttl=0.0)  # leases disabled
        await c._heartbeat_once()
        assert client.renewed == []

    _run(testing())


def test_heartbeat_interval_never_reaches_lease_for_small_ttl():
    """A fixed 1s floor could exceed a sub-3s lease; the interval must always
    renew strictly before the lease lapses, and approximate ttl/3 for normal
    TTLs."""
    c = _coord(lock_scope="plan")
    for ttl in (0.5, 1.0, 1.2, 3.0):
        c._lease_ttl_seconds = ttl
        assert c._heartbeat_interval() < ttl, ttl
    c._lease_ttl_seconds = 30.0
    assert 9.0 <= c._heartbeat_interval() <= 10.0  # ~ttl/3 for normal leases


def test_heartbeat_reacquires_and_clears_bookkeeping_on_conflict():
    """If renew reports the device under conflict_devices (lease lapsed and
    another owner took it), the tick attempts a re-acquire; a 409 there means
    we truly don't hold it, so the coordinator drops its stale bookkeeping."""
    async def testing():
        client = FakeClient()
        client.renew_response = {
            "success": False,
            "renewed_devices": [],
            "lost_devices": [],
            "conflict_devices": ["det1"],
            "lock_epoch": "epoch-1",
            "expires_at": None,
        }
        # The re-acquire attempt conflicts (another owner holds it).
        client.lock_error_once = ConfigServiceConflict(409, {"detail": "held by another"})
        c = _held_coord(client, epoch="epoch-1")

        await c._heartbeat_once()

        # Re-acquire was attempted, then bookkeeping cleared on the 409.
        assert client.locked == []  # lock_devices raised before recording
        assert c._locked_item_id == ""
        assert c._locked_devices == []
        assert c._locked_plan_name == ""

    _run(testing())


def test_heartbeat_conflict_then_reacquire_succeeds_keeps_lock():
    """If the conflicting owner released between renew and re-acquire, the
    re-acquire succeeds and we keep the lock."""
    async def testing():
        client = FakeClient()
        client.renew_response = {
            "success": False,
            "renewed_devices": [],
            "lost_devices": [],
            "conflict_devices": ["det1"],
            "lock_epoch": "epoch-1",
            "expires_at": None,
        }
        c = _held_coord(client, epoch="epoch-1")

        await c._heartbeat_once()

        # Re-acquire succeeded → still ours.
        assert client.locked == [(["det1"], "uid-1", "count")]
        assert c._locked_item_id == "uid-1"
    _run(testing())
