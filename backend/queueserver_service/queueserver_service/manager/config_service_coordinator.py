"""
Config-service integration for the RE manager, extracted into one collaborator.

``RunEngineManager`` owns worker lifecycle, the 0MQ server, and the plan queue.
The configuration-service integration — device-registry bootstrap/sync, device
locking, the pre-plan staleness check, and the device diff/sync endpoints — used
to live as ~11 methods and ~9 state fields scattered through ``manager.py``.
``ConfigServiceCoordinator`` gathers them into one cohesive, injectable object.

Dependency injection: the coordinator owns its own state (settings, HTTP client,
version cursor, lock bookkeeping, the worker device-data snapshot) and calls back
into the manager for the few worker-side operations it needs through the
``ConfigServiceHost`` Protocol. That keeps it decoupled from the manager
god-object and unit-testable with a fake host + fake client — no ``RunEngineManager``
(a ``multiprocessing.Process`` subclass) required.

Lazy initialization: the HTTP client and the sync ``asyncio.Lock`` are created on
first use, not in ``__init__``, because ``httpx.AsyncClient`` and ``asyncio.Lock``
bind to the running event loop and the coordinator is constructed in the parent
process before the manager's loop starts.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .config_service import (
    ConfigServiceConflict,
    ConfigServiceSettings,
    ConfigServiceState,
    DeviceDiff,
    apply_diff,
    compute_diff,
    fetch_staleness_plan,
    sync_devices_on_env_open,
)
from .profile_ops import extract_device_names_from_plan

logger = logging.getLogger(__name__)


class ConfigServiceHost(Protocol):
    """The manager-side surface the coordinator depends on.

    Implemented by ``RunEngineManager``; a lightweight fake satisfies it in
    tests.
    """

    @property
    def existing_devices(self) -> Dict[str, Any]:
        """The manager's current ``{name: device_info}`` allowed-devices dict."""
        ...

    async def worker_update_device_overlay(
        self, upserts: Dict[str, Any], deletes: List[str], *, replace: bool
    ) -> Tuple[bool, str]:
        """Apply a device overlay to the running worker; return (success, err_msg)."""
        ...

    async def reload_lists_from_worker(self) -> bool:
        """Re-download the worker's plan/device lists; return success."""
        ...


class ConfigServiceCoordinator:
    """Owns all config-service state + orchestration for one manager instance."""

    def __init__(self, settings: ConfigServiceSettings, *, host: ConfigServiceHost) -> None:
        self._settings = settings
        self._host = host
        self._state = ConfigServiceState()
        # UID that identifies this manager as the lock owner in config-service
        # for environment-scope locks. Regenerated on every env-open (see
        # ``new_env_lock_item_id``) so a leftover lock from a previous
        # environment (failed unlock at close) is distinguishable from
        # "already locked for this environment".
        self._lock_item_id = self._new_lock_item_id()
        # Best knowledge of server-side lock state: the device list and the
        # item_id actually on the wire ("" == no lock known). Set together on
        # successful lock, cleared together only after successful unlock. NEVER
        # used as a precondition to skip locking — only as a debt to settle
        # (release-before-relock), so an unlock failure cannot latch locking off.
        self._locked_devices: List[str] = []
        self._locked_item_id: str = ""
        # Registry snapshot (``{name: spec}``) fetched at the start of env-open.
        # ``None`` means "not fetched this env-cycle" and is distinct from the
        # known-empty ``{}`` case.
        self._prefetched_info: Optional[Dict[str, Any]] = None
        # Long-lived ConfigServiceClient shared across prefetch / env-open sync /
        # staleness check / unlock; lazy (loop affinity). Closed in ``close``.
        self._client = None
        # asyncio.Lock serializing config-service sync runs (awaited env-open
        # call vs. the manual-sync endpoint). Lazy for the same loop reason.
        self._sync_alock: Optional[asyncio.Lock] = None
        # The worker's introspected device payloads
        # (``{name: {"metadata": ..., "spec": ...}}``), pushed by the manager
        # whenever it downloads the worker's lists.
        self._device_data: Dict[str, Any] = {}

    @staticmethod
    def _new_lock_item_id() -> str:
        return f"env:{uuid.uuid4()}"

    # -- state the manager reads/writes -------------------------------------

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def lock_scope(self) -> str:
        return self._settings.lock_scope

    @property
    def device_data(self) -> Dict[str, Any]:
        return self._device_data

    def set_device_data(self, device_data: Dict[str, Any]) -> None:
        """Manager pushes the latest worker device-data snapshot here."""
        self._device_data = device_data

    def new_env_lock_item_id(self) -> None:
        """Refresh the environment lock-owner id at the start of env-open.

        A leftover lock recorded under a previous id (unlock failed at the last
        env-close) is then recognizable as a debt and released before any new
        lock is taken.
        """
        self._lock_item_id = self._new_lock_item_id()

    # -- lazy client + sync lock --------------------------------------------

    async def get_client(self):
        """Return the long-lived ConfigServiceClient (lazy-initialized).

        Callers must gate on ``self.enabled`` (ConfigServiceClient's constructor
        rejects disabled settings on purpose).
        """
        if self._client is None:
            from .config_service import ConfigServiceClient

            self._client = ConfigServiceClient(self._settings)
        return self._client

    def _get_sync_alock(self) -> asyncio.Lock:
        if self._sync_alock is None:
            self._sync_alock = asyncio.Lock()
        return self._sync_alock

    async def close(self) -> None:
        """Close the long-lived client (called on manager shutdown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- env-open: prefetch + sync ------------------------------------------

    async def prefetch_registry(self) -> dict:
        """Fetch the config-service registry before spawning the worker.

        Returns the ``{name: spec}`` dict to forward to the worker (empty dict
        if consume-mode does not apply). Stores the same dict so the post-spawn
        sync can skip its redundant emptiness probe. Any failure propagates —
        env-open fails loudly when config-service is enabled.
        """
        self._prefetched_info = None
        if not self._settings.enabled:
            return {}

        client = await self.get_client()
        specs = await client.get_instantiation_specs()
        self._prefetched_info = specs
        if not specs:
            return {}
        logger.info(
            "config-service consume-mode: prefetched %d device spec(s) for worker injection",
            len(specs),
        )
        return dict(specs)

    async def sync_on_env_open(self) -> None:
        """Bootstrap-if-empty, capture the version cursor, and (environment lock
        scope only) lock the environment's devices.

        The environment lock is acquired once per env (owner id regenerated on
        every env-open). A leftover lock under a different owner id is released
        first, loudly — never silently skipped. Errors propagate so env-open
        fails loudly. Serialized via the sync lock because the awaited env-open
        call and the periodic poll's update task can otherwise run concurrently.
        """
        async with self._get_sync_alock():
            device_names = list(self._host.existing_devices.keys())
            client = await self.get_client()
            state = await sync_devices_on_env_open(
                client,
                expected_device_names=device_names,
                device_data=self._device_data,
                prefetched_info=self._prefetched_info,
            )
            if self._settings.lock_scope == "environment":
                if self._locked_item_id == self._lock_item_id:
                    pass  # already locked for THIS environment (sync re-runs on list updates)
                else:
                    if self._locked_item_id:
                        # Leftover from a previous environment or crashed plan.
                        # Release loudly before taking the new lock; raising here
                        # fails the env-open sync visibly rather than locking on
                        # top of (or skipping because of) stale state.
                        await self.unlock_devices()
                    if device_names:
                        await self._lock_with_restart_recovery(
                            client,
                            device_names,
                            item_id=self._lock_item_id,
                            plan_name="__environment__",
                        )
                        self._locked_devices = list(device_names)
                        self._locked_item_id = self._lock_item_id
                        logger.info(
                            "config-service locked %d device(s) under item_id=%s",
                            len(device_names), self._lock_item_id,
                        )
            else:
                # "plan" scope: no env lock. Leftover debts are settled at the
                # next per-plan acquisition (release-before-relock) and at
                # env-close — NOT here: this sync re-runs whenever the worker
                # lists update, which happens mid-queue (e.g. after an overlay
                # refresh), and releasing here would drop a RUNNING plan's lock.
                pass
            self._state = state
            logger.info(
                "config-service cursor=%d epoch=%s", state.cursor, state.epoch
            )

    async def check_staleness_before_plan(self) -> None:
        """Pre-plan staleness check.

        No-op when disabled. When enabled, calls /devices/changes with the saved
        cursor; on reset/epoch mismatch fetches the full registry; applies the
        resulting upserts + deletes to the worker's overlay; commits the advanced
        cursor. Raises if config-service is unreachable or the worker rejects the
        overlay update — plan start aborts loudly (no silent fallback).
        """
        if not self._settings.enabled:
            return

        client = await self.get_client()
        plan = await fetch_staleness_plan(client, self._state)

        if plan.is_noop:
            return

        logger.info(
            "config-service staleness check: replace=%s upserts=%d deletes=%d",
            plan.replace_overlay,
            len(plan.upserts),
            len(plan.deletes),
        )
        success, err_msg = await self._host.worker_update_device_overlay(
            plan.upserts, plan.deletes, replace=plan.replace_overlay
        )
        if not success:
            raise RuntimeError(
                f"config-service overlay update rejected by worker: {err_msg}"
            )
        self._state = plan.new_state
        # The worker recomputed its plan/device lists as part of the overlay
        # update; pull them now (instead of waiting for the periodic poll) so
        # this plan's per-plan device extraction and permission filtering see
        # the devices that just arrived from the registry.
        if not await self._host.reload_lists_from_worker():
            raise RuntimeError(
                "worker accepted the device overlay but the updated lists of "
                "plans and devices could not be downloaded"
            )

    # -- device locking ------------------------------------------------------

    async def unlock_devices(self, *, suppress_errors: bool = False) -> None:
        """Release the lock this manager knows it holds (environment or per-plan).

        On success the bookkeeping is cleared. On failure it is KEPT — it is the
        manager's knowledge of an outstanding server-side lock, settled by the
        next lock attempt (release-before-relock); never consulted to skip
        locking, so a failed unlock cannot latch locking off.

        ``suppress_errors=True`` (recovery paths) logs at ERROR instead of
        raising, so a dead config-service can't stop queueserver killing a hung
        worker. These are the only exceptions to the hard-fail rule.
        """
        if not self._settings.enabled:
            return
        devices = self._locked_devices
        item_id = self._locked_item_id
        if not devices:
            return

        try:
            client = await self.get_client()
            await client.unlock_devices(devices, item_id=item_id)
        except Exception:
            if not suppress_errors:
                raise
            logger.exception(
                "config-service unlock failed; locks for item_id=%s are kept on "
                "the books and will be released before the next lock acquisition",
                item_id,
            )
            return
        self._locked_devices = []
        self._locked_item_id = ""

    async def lock_devices_for_plan(self, item: dict) -> None:
        """Acquire per-plan locks for exactly the registered devices the plan
        references (lock scope "plan" only). Raises on ANY failure — the caller
        aborts the plan start. No silent fallback.
        """
        # Settle outstanding debt first: a previous plan whose unlock failed, or
        # an env-scope leftover. Also required for correctness — the lock
        # endpoint conflicts even when the same owner re-locks an overlapping set.
        if self._locked_item_id:
            await self.unlock_devices()

        device_names = extract_device_names_from_plan(
            item, existing_devices=self._host.existing_devices
        )
        if not device_names:
            logger.info(
                "Plan %r references no registered devices; no config-service locks taken",
                item.get("name"),
            )
            return

        client = await self.get_client()
        await client.lock_devices(
            device_names, item_id=item["item_uid"], plan_name=item["name"]
        )
        self._locked_devices = list(device_names)
        self._locked_item_id = item["item_uid"]
        logger.info(
            "config-service locked %d device(s) for plan %r (item_uid=%s): %s",
            len(device_names), item["name"], item["item_uid"], device_names,
        )

    async def _lock_with_restart_recovery(
        self,
        client,
        device_names: list,
        *,
        item_id: str,
        plan_name: str,
    ) -> None:
        """Env-scope lock with restart-orphan recovery: force-unlock + retry once
        on a 409, since a single-manager deployment can only 409 on a previous
        incarnation's leftover locks. Bounded retry; a second 409 raises so
        env-open fails loudly. Intentionally NOT used by the per-plan path, where
        a 409 can legitimately mean another plan in this env still holds the lock.
        """
        try:
            await client.lock_devices(
                device_names, item_id=item_id, plan_name=plan_name
            )
            return
        except ConfigServiceConflict as conflict:
            logger.warning(
                "config-service lock conflict for item_id=%s plan=%r "
                "(likely orphaned locks from a previous manager incarnation); "
                "force-unlocking %d device(s) and retrying once: %s",
                item_id, plan_name, len(device_names), conflict,
            )
            await client.force_unlock_devices(
                device_names,
                reason=(
                    f"queueserver manager restart recovery "
                    f"(new item_id={item_id}, plan={plan_name})"
                ),
            )
        await client.lock_devices(
            device_names, item_id=item_id, plan_name=plan_name
        )

    async def release_plan_scope_lock(self, *, suppress_errors: bool) -> bool:
        """Release the per-plan lock if one is held (lock scope "plan" only;
        no-op otherwise). Returns True on success or no-op. With
        ``suppress_errors=False`` a failure returns False instead of raising, so
        the plan-report path can decide what to do without an exception escaping.
        """
        if not self._settings.enabled:
            return True
        if self._settings.lock_scope != "plan":
            return True
        try:
            await self.unlock_devices()
            return True
        except Exception as ex:
            logger.error(
                "config-service per-plan unlock failed (item_id=%s): %s",
                self._locked_item_id, ex,
            )
            return suppress_errors

    # -- device diff / sync endpoints (core; the manager handler wraps these) --

    async def compute_diff_against_registry(self) -> DeviceDiff:
        """Diff the worker's introspected devices against the registry (pure read)."""
        client = await self.get_client()
        registry_specs = await client.get_instantiation_specs()
        return compute_diff(self._device_data, registry_specs)

    async def apply_sync(self, *, strategy: str, selected) -> dict:
        """Apply the profile-vs-registry diff per ``strategy`` and return
        ``{"applied": ..., "diff_after": ...}``. Acquires the same sync lock as
        env-open so a concurrent env-open + manual sync don't race the registry.
        Assumes ``strategy``/``selected`` were already validated by the caller.
        """
        async with self._get_sync_alock():
            client = await self.get_client()
            registry_specs = await client.get_instantiation_specs()
            diff_before = compute_diff(self._device_data, registry_specs)
            applied = await apply_diff(
                client,
                diff_before,
                self._device_data,
                strategy=strategy,
                selected=selected,
            )
            registry_after = await client.get_instantiation_specs()
            diff_after = compute_diff(self._device_data, registry_after)
        return {"applied": applied, "diff_after": diff_after.to_dict()}
