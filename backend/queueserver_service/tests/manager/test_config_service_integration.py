"""
Integration tests for queueserver_service.manager.config_service.

Unlike ``test_config_service.py`` (which drives the client with an
``httpx.MockTransport`` that the test itself shapes), this module boots a
real ``configuration_service.main.create_app`` FastAPI instance in-process
and wires ``ConfigServiceClient`` to it via ``httpx.ASGITransport``. The
client therefore speaks the actual REST contract to the real handlers,
exercising route strings, status-code semantics, Pydantic request/response
shapes, and SQLite-backed audit-log behavior that unit mocks cannot catch.

The FastAPI app is constructed with ``load_strategy="empty"`` and a
per-test SQLite file under ``tmp_path`` so each test starts with a clean
registry. Device payloads are produced by the real worker-side helper
``build_config_service_payload`` against ``FakeOphydMotor`` fixtures from
``test_device_introspection`` — that way this module also pins the
introspection → config-service payload contract.

No network sockets are opened; no external config-service process is
required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("configuration_service")
pytest.importorskip("asgi_lifespan")

from asgi_lifespan import LifespanManager  # noqa: E402
from configuration_service.config import Settings  # noqa: E402
from configuration_service.main import create_app  # noqa: E402

from queueserver_service.manager.config_service import (  # noqa: E402
    ConfigServiceClient,
    ConfigServiceConflict,
    ConfigServiceHTTPError,
    ConfigServiceSettings,
    ConfigServiceState,
    build_staleness_plan,
    fetch_staleness_plan,
    sync_devices_on_env_open,
)
from queueserver_service.manager.device_introspection import (  # noqa: E402
    build_config_service_payload,
)
from tests.manager.test_device_introspection import (  # noqa: E402
    FakeOphydMotor,
)


def _cs_settings() -> ConfigServiceSettings:
    return ConfigServiceSettings(
        enabled=True,
        url="http://testserver",
        timeout=5.0,
        max_attempts=1,
        backoff_ms=(1,),
        service_name="bluesky-queueserver-tests",
    )


def _payload(name: str, prefix: str = "XF:M1") -> Dict[str, Dict[str, Any]]:
    """``{name: {"metadata": ..., "spec": ...}}`` for a single fake device,
    produced by the real introspection pipeline so payload shapes stay in
    lockstep with what the worker sends in production."""
    return build_config_service_payload({name: FakeOphydMotor(prefix=prefix, name=name)})


def _payloads(*names_and_prefixes) -> Dict[str, Dict[str, Any]]:
    devices = {
        name: FakeOphydMotor(prefix=prefix, name=name)
        for name, prefix in names_and_prefixes
    }
    return build_config_service_payload(devices)


@pytest.fixture
def cs_app(tmp_path: Path):
    """Fresh configuration-service FastAPI app backed by tmp_path SQLite."""
    settings = Settings(
        load_strategy="empty",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'cs.db'}",
    )
    return create_app(settings)


@pytest_asyncio.fixture
async def cs_client(cs_app) -> AsyncIterator[ConfigServiceClient]:
    """ConfigServiceClient wired to the in-process FastAPI app via ASGI.

    ``LifespanManager`` is needed because ``httpx.ASGITransport`` does not
    run the lifespan protocol; without startup the app's ``get_state``
    dependency raises HTTP 503.
    """
    async with LifespanManager(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        client = ConfigServiceClient(_cs_settings(), transport=transport)
        try:
            yield client
        finally:
            await client.aclose()


@pytest_asyncio.fixture
async def raw_http(cs_app) -> AsyncIterator[httpx.AsyncClient]:
    """Raw httpx client against the same ASGI app, for calls intentionally
    outside ``ConfigServiceClient``'s wrapped API (test-only admin
    endpoints and deliberate contract probes)."""
    async with LifespanManager(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
async def test_empty_registry_reports_empty(cs_client: ConfigServiceClient):
    assert await cs_client.is_registry_empty() is True
    assert await cs_client.get_devices_info() == {}
    assert await cs_client.get_instantiation_specs() == {}


@pytest.mark.asyncio
async def test_upsert_creates_then_get_instantiation_specs_returns_it(
    cs_client: ConfigServiceClient,
):
    data = _payload("m1", prefix="XF:M1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])

    info = await cs_client.get_devices_info()
    assert "m1" in info
    assert info["m1"]["ophyd_class"] == "FakeOphydMotor"

    specs = await cs_client.get_instantiation_specs()
    assert "m1" in specs
    assert specs["m1"]["device_class"].endswith(".FakeOphydMotor")
    assert specs["m1"]["args"] == ["XF:M1"]


@pytest.mark.asyncio
async def test_upsert_conflict_falls_back_to_put(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """Second upsert of the same name must round-trip POST-409 → PUT.
    The MockTransport unit test asserts the client retries on a shaped
    409; this asserts the real server actually responds with 409 for a
    duplicate POST and accepts the PUT fallback."""
    a = _payload("m1", prefix="XF:A")["m1"]
    b = _payload("m1", prefix="XF:B")["m1"]
    await cs_client.upsert_device(a["metadata"], a["spec"])

    response = await raw_http.post(
        "/api/v1/devices",
        json={"metadata": b["metadata"], "instantiation_spec": b["spec"]},
    )
    assert response.status_code == 409

    await cs_client.upsert_device(b["metadata"], b["spec"])
    specs = await cs_client.get_instantiation_specs()
    assert specs["m1"]["args"] == ["XF:B"]


@pytest.mark.asyncio
async def test_delete_device_roundtrip(cs_client: ConfigServiceClient):
    data = _payload("m1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])
    await cs_client.delete_device("m1")

    info = await cs_client.get_devices_info()
    assert "m1" not in info


@pytest.mark.asyncio
async def test_sync_bootstraps_empty_registry_and_captures_cursor(
    cs_client: ConfigServiceClient,
):
    device_data = _payloads(("m1", "XF:M1"), ("m2", "XF:M2"))

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=device_data,
    )

    assert isinstance(state, ConfigServiceState)
    assert state.cursor > 0
    assert state.epoch

    info = await cs_client.get_devices_info()
    assert set(info.keys()) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_sync_skips_bootstrap_when_registry_populated(
    cs_client: ConfigServiceClient,
):
    # Seed a different device so sync detects non-empty and skips POST.
    seed = _payload("seed")["seed"]
    await cs_client.upsert_device(seed["metadata"], seed["spec"])

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
    )

    info = await cs_client.get_devices_info()
    assert "m1" not in info
    assert "seed" in info
    assert state.cursor > 0


@pytest.mark.asyncio
async def test_lock_and_unlock_devices(cs_client: ConfigServiceClient):
    for name, prefix in [("m1", "XF:M1"), ("m2", "XF:M2")]:
        data = _payload(name, prefix=prefix)[name]
        await cs_client.upsert_device(data["metadata"], data["spec"])

    result = await cs_client.lock_devices(
        ["m1", "m2"], item_id="env:abc", plan_name="__environment__"
    )
    assert result["success"] is True
    assert set(result["locked_devices"]) == {"m1", "m2"}

    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(
            ["m1"], item_id="env:xyz", plan_name="__environment__"
        )

    # Wrong-owner unlock surfaces as HTTP 403 — neither Conflict nor NotFound.
    with pytest.raises(ConfigServiceHTTPError) as exc_info:
        await cs_client.unlock_devices(["m1", "m2"], item_id="env:wrong")
    assert exc_info.value.status_code == 403

    unlock_result = await cs_client.unlock_devices(["m1", "m2"], item_id="env:abc")
    assert unlock_result["success"] is True
    assert set(unlock_result["unlocked_devices"]) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_changes_since_noop_after_sync(cs_client: ConfigServiceClient):
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=_payload("m1")
    )

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.is_noop
    assert plan.new_state.epoch == state.epoch
    assert plan.new_state.cursor == state.cursor


@pytest.mark.asyncio
async def test_changes_since_reports_upsert(cs_client: ConfigServiceClient):
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=_payload("m1")
    )

    m2 = _payload("m2", prefix="XF:M2")["m2"]
    await cs_client.upsert_device(m2["metadata"], m2["spec"])

    plan = await fetch_staleness_plan(cs_client, state)
    assert not plan.is_noop
    assert not plan.replace_overlay
    assert set(plan.upserts.keys()) == {"m2"}
    assert plan.upserts["m2"]["args"] == ["XF:M2"]
    assert plan.deletes == []
    assert plan.new_state.cursor > state.cursor
    assert plan.new_state.epoch == state.epoch


@pytest.mark.asyncio
async def test_changes_since_reports_delete(cs_client: ConfigServiceClient):
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=_payloads(("m1", "XF:M1"), ("m2", "XF:M2")),
    )

    await cs_client.delete_device("m2")

    plan = await fetch_staleness_plan(cs_client, state)
    assert not plan.replace_overlay
    assert plan.upserts == {}
    assert plan.deletes == ["m2"]
    assert plan.new_state.cursor > state.cursor


@pytest.mark.asyncio
async def test_changes_since_reset_triggers_full_replace(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """After POST /registry/clear, the next /devices/changes must surface
    reset_occurred=True; fetch_staleness_plan must then also pull the full
    /devices/instantiation payload so the caller can atomically replace
    its overlay with the full current registry (not just the delta)."""
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=_payloads(("m1", "XF:M1"), ("m2", "XF:M2")),
    )

    wipe = await raw_http.post("/api/v1/registry/clear")
    assert wipe.status_code == 200
    assert wipe.json()["success"] is True

    m3 = _payload("m3", prefix="XF:M3")["m3"]
    await cs_client.upsert_device(m3["metadata"], m3["spec"])

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.replace_overlay is True
    assert set(plan.upserts.keys()) == {"m3"}
    assert plan.new_state.cursor > state.cursor


@pytest.mark.asyncio
async def test_build_staleness_plan_on_real_response(
    cs_client: ConfigServiceClient,
):
    """Confirm the real server's DeviceChangesResponse shape is compatible
    with ``build_staleness_plan`` — i.e. the field names, types, and op
    values match. This catches contract drift between the two repos."""
    data = _payload("m1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])
    initial = await cs_client.get_changes_since(0)

    plan = build_staleness_plan(initial, saved_epoch=initial["service_epoch"])
    assert plan.replace_overlay is False
    assert set(plan.upserts.keys()) == {"m1"}
    assert plan.deletes == []
    assert plan.new_state.epoch == initial["service_epoch"]


@pytest.mark.asyncio
async def test_sync_with_prefetched_empty_info_bootstraps_without_probe(
    cs_client: ConfigServiceClient,
):
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
        prefetched_info={},
    )
    assert state.cursor > 0
    info = await cs_client.get_devices_info()
    assert "m1" in info


@pytest.mark.asyncio
async def test_sync_with_prefetched_populated_info_skips_bootstrap(
    cs_client: ConfigServiceClient,
):
    seed = _payload("seed")["seed"]
    await cs_client.upsert_device(seed["metadata"], seed["spec"])

    # Passing a non-empty prefetched_info tells sync to skip the POST
    # fan-out regardless of what the real /devices-info would say.
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
        prefetched_info={"seed": seed["metadata"]},
    )

    info = await cs_client.get_devices_info()
    assert "m1" not in info
    assert state.cursor > 0


# ===== Per-plan locking (lock_scope: "plan") =====
#
# Client-level tests pin the config-service lock semantics the per-plan
# lifecycle depends on; the manager-flow tests below them exercise the
# manager's lock/release helpers (as unbound methods on a minimal stub)
# against the same real app.


async def _upsert(cs_client: ConfigServiceClient, *names: str) -> None:
    for i, name in enumerate(names):
        data = _payload(name, prefix=f"XF:M{i}")[name]
        await cs_client.upsert_device(data["metadata"], data["spec"])


@pytest.mark.asyncio
async def test_per_plan_lock_lifecycle(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """Sequential plans: lock → status shows locked-by-plan → unlock →
    next plan can immediately relock an overlapping device set."""
    await _upsert(cs_client, "m1", "m2")

    result = await cs_client.lock_devices(["m1"], item_id="uid-1", plan_name="count")
    assert result["success"] is True

    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["available"] is False
    assert status["lock_status"] == "locked"
    assert status["locked_by_plan"] == "count"
    assert status["locked_by_item"] == "uid-1"

    await cs_client.unlock_devices(["m1"], item_id="uid-1")
    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["available"] is True

    # The next plan in the queue locks an overlapping set under its own uid.
    result = await cs_client.lock_devices(
        ["m1", "m2"], item_id="uid-2", plan_name="scan"
    )
    assert set(result["locked_devices"]) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_per_plan_lock_conflict_is_atomic(cs_client: ConfigServiceClient):
    """A bulk lock overlapping a foreign lock must fail as a whole —
    the non-conflicting device must NOT be left locked."""
    await _upsert(cs_client, "m1", "m2")
    await cs_client.lock_devices(["m1"], item_id="uid-1", plan_name="count")

    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(
            ["m1", "m2"], item_id="uid-2", plan_name="scan"
        )

    # m2 must still be lockable by a third owner (all-or-nothing held).
    result = await cs_client.lock_devices(["m2"], item_id="uid-3", plan_name="grid")
    assert result["locked_devices"] == ["m2"]


@pytest.mark.asyncio
async def test_relock_same_item_id_conflicts(cs_client: ConfigServiceClient):
    """The lock endpoint conflicts even when the SAME owner re-locks an
    overlapping set — this pins the semantics that force the manager to
    release-before-relock between chained plans."""
    await _upsert(cs_client, "m1")
    await cs_client.lock_devices(["m1"], item_id="uid-1", plan_name="count")
    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(["m1"], item_id="uid-1", plan_name="count")


@pytest.mark.asyncio
async def test_unlock_idempotent_for_unlocked_devices(cs_client: ConfigServiceClient):
    """Releasing devices that are not locked succeeds — the manager's
    leftover-debt release stays safe after a config-service restart
    dropped the (in-memory) locks."""
    await _upsert(cs_client, "m1")
    result = await cs_client.unlock_devices(["m1"], item_id="uid-stale")
    assert result["success"] is True


@pytest.mark.asyncio
async def test_lock_unknown_device_404s(cs_client: ConfigServiceClient):
    """Locking a name absent from the registry fails — the per-plan path
    fails the plan start rather than running with a partial lock."""
    from queueserver_service.manager.config_service import ConfigServiceNotFound

    with pytest.raises(ConfigServiceNotFound):
        await cs_client.lock_devices(
            ["ghost"], item_id="uid-1", plan_name="count"
        )


# ----- Manager-flow tests: the real helpers on a minimal stub -----


def _coordinator(client, *, lock_scope="plan", existing_devices=None):
    """Build a ConfigServiceCoordinator wired to the real in-process client.

    Post-refactor, the per-plan lock / release / restart-recovery helpers live
    on ``ConfigServiceCoordinator`` (extracted from ``RunEngineManager``). These
    integration tests drive that coordinator directly against the real
    config-service app, injecting the test's ASGI-backed client so the
    coordinator never lazily constructs one from settings."""
    import dataclasses as _dc

    from queueserver_service.manager.config_service_coordinator import (
        ConfigServiceCoordinator,
    )

    class _Host:
        def __init__(self, devices: Dict[str, Any]):
            self.existing_devices = devices or {}

        async def worker_update_device_overlay(self, *args, **kwargs):
            return None

        async def reload_lists_from_worker(self) -> bool:
            return False

    settings = _dc.replace(_cs_settings(), lock_scope=lock_scope)
    coord = ConfigServiceCoordinator(settings, host=_Host(existing_devices or {}))
    coord._client = client
    return coord


def _plan_item(uid: str, name: str, *args, **kwargs):
    return {"name": name, "args": list(args), "kwargs": kwargs, "item_uid": uid}


@pytest.mark.asyncio
async def test_manager_per_plan_lock_and_release(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    await _upsert(cs_client, "m1", "m2")
    coord = _coordinator(cs_client, existing_devices={"m1": {}, "m2": {}})

    await coord.lock_devices_for_plan(_plan_item("uid-1", "count", ["m1"], num=3))
    assert coord._locked_devices == ["m1"]
    assert coord._locked_item_id == "uid-1"
    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["lock_status"] == "locked"
    assert status["locked_by_item"] == "uid-1"
    # m2 is not referenced by the plan and must remain available.
    status = (await raw_http.get("/api/v1/devices/m2/status")).json()
    assert status["available"] is True

    assert await coord.release_plan_scope_lock(suppress_errors=False) is True
    assert coord._locked_devices == []
    assert coord._locked_item_id == ""
    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["available"] is True


@pytest.mark.asyncio
async def test_manager_per_plan_lock_settles_leftover_debt(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """A leftover lock from a previous plan whose unlock failed must be
    released before the next plan's lock — never silently skipped, never
    a permanent 409 (the lock-reconciliation regression contract, per-plan
    flavor)."""
    await _upsert(cs_client, "m1")
    coord = _coordinator(cs_client, existing_devices={"m1": {}})

    # Simulate: previous plan locked m1, its unlock failed, manager kept
    # the bookkeeping as a debt.
    await cs_client.lock_devices(["m1"], item_id="uid-old", plan_name="count")
    coord._locked_devices = ["m1"]
    coord._locked_item_id = "uid-old"

    await coord.lock_devices_for_plan(
        _plan_item("uid-new", "scan", ["m1"], "m1", -1, 1, 5)
    )
    assert coord._locked_item_id == "uid-new"
    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["locked_by_item"] == "uid-new"


@pytest.mark.asyncio
async def test_manager_heartbeat_reacquires_after_lock_loss(tmp_path):
    """With a real config-service lease, the coordinator heartbeat re-acquires a
    lock the authority dropped. A force-unlock out of band clears the in-memory
    lock exactly as a config-service restart would; the next heartbeat tick sees
    the device as lost and re-acquires it under the same item id (fix #1)."""
    settings = Settings(
        load_strategy="empty",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'cs.db'}",
        lock_lease_ttl_seconds=30.0,
    )
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        cs_client = ConfigServiceClient(_cs_settings(), transport=transport)
        raw = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        try:
            await _upsert(cs_client, "m1")
            coord = _coordinator(cs_client, existing_devices={"m1": {}})

            await coord.lock_devices_for_plan(_plan_item("uid-1", "count", ["m1"]))
            assert coord._lease_ttl_seconds == 30.0
            # Drive heartbeat ticks manually rather than via the background loop.
            await coord._stop_heartbeat()

            # Simulate the authority dropping the lock (restart / lapsed lease).
            await cs_client.force_unlock_devices(["m1"], reason="simulated restart")
            assert (await raw.get("/api/v1/devices/m1/status")).json()["available"] is True

            # One heartbeat tick: renew reports it lost → re-acquire.
            await coord._heartbeat_once()
            status = (await raw.get("/api/v1/devices/m1/status")).json()
            assert status["lock_status"] == "locked"
            assert status["locked_by_item"] == "uid-1"
        finally:
            await coord._stop_heartbeat()
            await raw.aclose()
            await cs_client.aclose()


@pytest.mark.asyncio
async def test_manager_per_plan_lock_no_registered_devices_is_noop(
    cs_client: ConfigServiceClient,
):
    await _upsert(cs_client, "m1")
    coord = _coordinator(cs_client, existing_devices={"m1": {}})

    await coord.lock_devices_for_plan(_plan_item("uid-1", "sleepy", 5.0))
    assert coord._locked_devices == []
    assert coord._locked_item_id == ""


@pytest.mark.asyncio
async def test_manager_per_plan_lock_conflict_raises_and_keeps_books_clean(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """A foreign lock (e.g. another service) must fail the plan start
    loudly; the manager must not record a lock it did not get."""
    await _upsert(cs_client, "m1")
    await cs_client.lock_devices(["m1"], item_id="foreign", plan_name="other")
    coord = _coordinator(cs_client, existing_devices={"m1": {}})

    with pytest.raises(ConfigServiceConflict):
        await coord.lock_devices_for_plan(_plan_item("uid-1", "count", ["m1"]))
    assert coord._locked_devices == []
    assert coord._locked_item_id == ""
    # The foreign lock is untouched.
    status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    assert status["locked_by_item"] == "foreign"


@pytest.mark.asyncio
async def test_manager_release_keeps_books_when_unlock_fails(cs_app):
    """If the unlock HTTP call fails, the bookkeeping must be KEPT (it is
    the manager's knowledge of an outstanding server-side lock) and the
    suppress_errors flag decides the return value — never an exception."""
    async with LifespanManager(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        client = ConfigServiceClient(_cs_settings(), transport=transport)
        try:
            coord = _coordinator(client, existing_devices={"m1": {}})
            coord._locked_devices = ["m1"]
            coord._locked_item_id = "uid-1"

            # Closing the underlying client makes every request fail.
            await client.aclose()

            assert await coord.release_plan_scope_lock(suppress_errors=False) is False
            assert coord._locked_devices == ["m1"]
            assert coord._locked_item_id == "uid-1"
            assert await coord.release_plan_scope_lock(suppress_errors=True) is True
            assert coord._locked_devices == ["m1"]
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_env_open_recovers_from_orphaned_restart_locks(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """Restart-recovery against the real config-service: simulate a manager
    restart with leftover locks under a dead previous incarnation's UID, then
    run the env-scope lock acquisition on a fresh manager. Pre-fix this 409'd
    in a watchdog crash loop; now the new manager force-unlocks the orphans
    and re-locks under its own UID.
    """
    await _upsert(cs_client, "m1", "m2")
    # Previous incarnation: lock both devices under the dead UID.
    await cs_client.lock_devices(
        ["m1", "m2"], item_id="env:dead-incarnation", plan_name="__environment__"
    )

    # Confirm baseline (and prove the bug pre-fix): plain lock_devices under
    # a new UID 409s while the orphans are held.
    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(
            ["m1", "m2"], item_id="env:fresh-incarnation", plan_name="__environment__"
        )

    # Drive the extracted coordinator's recovery helper directly.
    coord = _coordinator(cs_client)

    new_uid = "env:fresh-incarnation"
    await coord._lock_with_restart_recovery(
        cs_client, ["m1", "m2"], item_id=new_uid, plan_name="__environment__"
    )

    # New incarnation now holds the locks; dead UID is gone (force-unlock cleared it).
    m1_status = (await raw_http.get("/api/v1/devices/m1/status")).json()
    m2_status = (await raw_http.get("/api/v1/devices/m2/status")).json()
    assert m1_status["locked_by_item"] == new_uid
    assert m2_status["locked_by_item"] == new_uid

    # Audit log records the force-unlock with the manager-supplied reason.
    audit = (await raw_http.get("/api/v1/devices/history?limit=20")).json()
    force_events = [
        e for e in audit
        if e.get("operation") == "force_unlock"
    ]
    assert force_events, f"expected a force_unlock audit entry, got {audit!r}"
    assert any("restart recovery" in (e.get("details") or "") for e in force_events)
