"""
Device Lock Manager for Configuration Service.

Manages in-memory device lock state for A4 coordination between
Experiment Execution (SVC-001) and Direct Control (SVC-003).

Lock state is ephemeral (not persisted to the database). On service restart,
all locks are cleared. Lock/unlock events are written to the audit log
separately by the endpoint handlers.

Design:
- Locks are stored at the device level
- PV availability is derived by resolving PV → owning device → lock state
- All-or-nothing atomic lock acquisition (no partial locks)
- Only the item_id that acquired a lock can release it (unless force-unlock)

Lease / heartbeat (opt-in via ``lease_ttl`` > 0):
- Each lock carries an ``expires_at``. A holder must renew (heartbeat) via
  ``renew_locks`` before expiry, otherwise the lock lapses and the device
  becomes available again. This bounds the blast radius of a lock holder
  that crashes without releasing (no orphaned locks held forever).
- ``lease_ttl == 0`` disables expiry — locks are held until explicitly
  released or force-unlocked (the historical behavior).

Lock-authority epoch:
- ``epoch`` is a fresh id generated every time the manager is constructed
  (i.e. every configuration_service process start). Because lock state is
  in-memory, a restart silently drops every lock; the registry
  ``service_epoch`` does NOT change on a plain restart (it is DB-backed), so
  it cannot signal lock loss. ``epoch`` is that signal: it is surfaced on
  lock/renew/status responses so a holder (queueserver) can detect that the
  authority reset and re-acquire, and so readers (direct-control) can tell
  the lock table was rebuilt.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .models import DeviceRegistry

logger = logging.getLogger(__name__)


LockConflictReason = Literal["not_found", "spec_missing", "disabled", "already_locked"]


class DeviceLockState:
    """Per-device lock state (in-memory only)."""

    __slots__ = (
        "device_name",
        "locked",
        "locked_by_plan",
        "locked_by_item",
        "locked_by_service",
        "locked_at",
        "lock_id",
        "expires_at",
    )

    def __init__(
        self,
        device_name: str,
        locked_by_plan: str,
        locked_by_item: str,
        locked_by_service: str,
        lock_id: str,
        expires_at: datetime | None = None,
    ):
        self.device_name = device_name
        self.locked = True
        self.locked_by_plan = locked_by_plan
        self.locked_by_item = locked_by_item
        self.locked_by_service = locked_by_service
        self.locked_at = datetime.now(UTC)
        self.lock_id = lock_id
        # None means "no lease" (lease_ttl disabled) — the lock never
        # expires on its own and must be released explicitly.
        self.expires_at = expires_at

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when this lock has a lease that has elapsed."""
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "device_name": self.device_name,
            "locked": self.locked,
            "locked_by_plan": self.locked_by_plan,
            "locked_by_item": self.locked_by_item,
            "locked_by_service": self.locked_by_service,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
            "lock_id": self.lock_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class LockConflict:
    """Information about a device that could not be locked."""

    __slots__ = ("device_name", "reason", "locked_by_plan", "locked_at")

    def __init__(
        self,
        device_name: str,
        reason: LockConflictReason,
        locked_by_plan: str | None = None,
        locked_at: datetime | None = None,
    ):
        self.device_name = device_name
        self.reason: LockConflictReason = reason
        self.locked_by_plan = locked_by_plan
        self.locked_at = locked_at


class LockResult:
    """Result of a lock acquisition attempt."""

    def __init__(
        self,
        success: bool,
        lock_id: str | None = None,
        locked_devices: list[str] | None = None,
        locked_pvs: list[str] | None = None,
        conflicts: list[LockConflict] | None = None,
        error_code: int = 200,
        expires_at: datetime | None = None,
    ):
        self.success = success
        self.lock_id = lock_id
        self.locked_devices = locked_devices or []
        self.locked_pvs = locked_pvs or []
        self.conflicts = conflicts or []
        self.error_code = error_code
        self.expires_at = expires_at


class RenewResult:
    """Result of a lock-renewal (heartbeat) attempt.

    ``renewed`` are devices whose lease this ``item_id`` successfully
    extended. ``lost`` are devices the ``item_id`` expected to own but that
    are no longer held here (expired, released, or wiped by a restart) —
    the caller should treat these as "re-acquire needed". ``conflicts`` are
    devices currently held by a *different* item_id.
    """

    def __init__(
        self,
        success: bool,
        renewed: list[str] | None = None,
        lost: list[str] | None = None,
        conflicts: list[str] | None = None,
        expires_at: datetime | None = None,
    ):
        self.success = success
        self.renewed = renewed or []
        self.lost = lost or []
        self.conflicts = conflicts or []
        self.expires_at = expires_at


class DeviceLockManager:
    """
    Manages in-memory device lock state.

    Thread-safe via asyncio.Lock for atomic lock acquisition/release.
    Lock state is ephemeral — cleared on service restart.
    """

    def __init__(self, lock_all: bool = False, lease_ttl: float = 0.0):
        self._locks: dict[str, DeviceLockState] = {}
        self._lock = asyncio.Lock()
        self._version: int = 0
        # "lock_all" availability policy: when True and any lock is held,
        # effective_lock() reports every device as locked. Boot default comes
        # from settings (CONFIG_LOCK_ALL); mutable at runtime via the
        # lock-policy endpoint.
        self.lock_all_enabled = lock_all
        # Lease TTL in seconds. 0 disables expiry (locks held until released).
        # When > 0, acquired locks carry an expires_at and holders must renew
        # before it elapses (see renew_locks).
        self._lease_ttl = lease_ttl
        # Lock-authority generation id: new on every construction (process
        # start). Surfaced on lock/renew/status responses so holders can
        # detect that the in-memory lock table was rebuilt and re-acquire.
        self._epoch = uuid.uuid4().hex

    @property
    def version(self) -> int:
        """Monotonic version counter, incremented on every lock/unlock."""
        return self._version

    @property
    def epoch(self) -> str:
        """Lock-authority generation id (stable for this process' lifetime)."""
        return self._epoch

    @property
    def lease_ttl(self) -> float:
        """Configured lease TTL in seconds (0 = leases disabled)."""
        return self._lease_ttl

    def _next_expiry(self, now: datetime | None = None) -> datetime | None:
        """Compute a fresh lease expiry, or None when leases are disabled."""
        if self._lease_ttl <= 0:
            return None
        return (now or datetime.now(UTC)) + timedelta(seconds=self._lease_ttl)

    def _active_lock(self, device_name: str, now: datetime | None = None) -> DeviceLockState | None:
        """The device's lock iff present, held, and not lapsed by its lease."""
        state = self._locks.get(device_name)
        if state is None or not state.locked:
            return None
        if state.is_expired(now):
            return None
        return state

    async def acquire_locks(
        self,
        device_names: list[str],
        item_id: str,
        plan_name: str,
        locked_by_service: str,
        registry: DeviceRegistry,
    ) -> LockResult:
        """
        Atomically acquire locks on multiple devices (all-or-nothing).

        Returns LockResult with success=True and lock details, or
        success=False with conflict information and appropriate error_code.
        """
        async with self._lock:
            conflicts: list[LockConflict] = []

            # Validate all devices before acquiring any locks
            for name in device_names:
                device = registry.get_device(name)
                if device is None:
                    conflicts.append(
                        LockConflict(
                            device_name=name,
                            reason="not_found",
                        )
                    )
                    continue

                spec = registry.get_instantiation_spec(name)
                if spec is None:
                    conflicts.append(
                        LockConflict(
                            device_name=name,
                            reason="spec_missing",
                        )
                    )
                    continue
                if not spec.active:
                    conflicts.append(
                        LockConflict(
                            device_name=name,
                            reason="disabled",
                        )
                    )
                    continue

                existing_lock = self._active_lock(name)
                if existing_lock is not None:
                    conflicts.append(
                        LockConflict(
                            device_name=name,
                            reason="already_locked",
                            locked_by_plan=existing_lock.locked_by_plan,
                            locked_at=existing_lock.locked_at,
                        )
                    )

            if conflicts:
                # Determine error code from conflict types
                reasons = {c.reason for c in conflicts}
                if "spec_missing" in reasons:
                    error_code = 500
                elif "not_found" in reasons:
                    error_code = 404
                elif "disabled" in reasons:
                    error_code = 422
                else:
                    error_code = 409
                return LockResult(
                    success=False,
                    conflicts=conflicts,
                    error_code=error_code,
                )

            # All devices valid and available — acquire locks
            lock_id = str(uuid.uuid4())
            expires_at = self._next_expiry()
            for name in device_names:
                self._locks[name] = DeviceLockState(
                    device_name=name,
                    locked_by_plan=plan_name,
                    locked_by_item=item_id,
                    locked_by_service=locked_by_service,
                    lock_id=lock_id,
                    expires_at=expires_at,
                )

            # Collect all PVs belonging to locked devices
            locked_pvs = self._get_device_pvs(device_names, registry)

            self._version += 1
            # stdlib logger: %-style only. structlog-style kwargs raise
            # TypeError at INFO level — after the lock state was mutated.
            logger.info(
                "locks_acquired devices=%s plan=%s item_id=%s lock_id=%s expires_at=%s",
                device_names,
                plan_name,
                item_id,
                lock_id,
                expires_at.isoformat() if expires_at else None,
            )

            return LockResult(
                success=True,
                lock_id=lock_id,
                locked_devices=list(device_names),
                locked_pvs=locked_pvs,
                expires_at=expires_at,
            )

    async def renew_locks(
        self,
        device_names: list[str],
        item_id: str,
    ) -> RenewResult:
        """Extend the lease on locks held by ``item_id`` (heartbeat).

        For each requested device:
        - held by ``item_id`` and not lapsed → lease extended (``renewed``);
        - held by a different item_id → ``conflicts``;
        - not held / lapsed / never existed here → ``lost`` (the caller must
          re-acquire; this is what a config-service restart looks like to a
          holder that still thinks it owns the lock).

        ``success`` is True only when every requested device was renewed.
        A no-op when leases are disabled is still reported as renewed so a
        holder's heartbeat doesn't spuriously trigger re-acquisition.
        """
        async with self._lock:
            now = datetime.now(UTC)
            renewed: list[str] = []
            lost: list[str] = []
            conflicts: list[str] = []
            expires_at = self._next_expiry(now)

            for name in device_names:
                state = self._locks.get(name)
                if state is None or not state.locked or state.is_expired(now):
                    lost.append(name)
                    continue
                if state.locked_by_item != item_id:
                    conflicts.append(name)
                    continue
                state.expires_at = expires_at
                renewed.append(name)

            if lost or conflicts:
                logger.info(
                    "lock_renew_partial item_id=%s renewed=%s lost=%s conflicts=%s",
                    item_id,
                    renewed,
                    lost,
                    conflicts,
                )

            return RenewResult(
                success=not lost and not conflicts,
                renewed=renewed,
                lost=lost,
                conflicts=conflicts,
                expires_at=expires_at,
            )

    async def release_locks(
        self,
        device_names: list[str],
        item_id: str,
    ) -> tuple[bool, list[str], str | None]:
        """
        Release locks owned by item_id.

        Returns (success, unlocked_devices, error_message).
        If a device is locked by a different item_id, returns failure.
        """
        async with self._lock:
            now = datetime.now(UTC)
            # Verify ownership. An expired (lapsed-lease) lock is effectively
            # free, so it must NOT block a non-owner's unlock with a 403 —
            # only a live lock held by a different item_id is a conflict.
            for name in device_names:
                active = self._active_lock(name, now)
                if active is not None and active.locked_by_item != item_id:
                    return (
                        False,
                        [],
                        f"Device '{name}' is locked by item {active.locked_by_item}, not {item_id}",
                    )

            # Release locks
            unlocked = []
            for name in device_names:
                if name in self._locks and self._locks[name].locked:
                    del self._locks[name]
                    unlocked.append(name)

            if unlocked:
                self._version += 1
                logger.info("locks_released devices=%s item_id=%s", unlocked, item_id)

            return True, unlocked, None

    async def force_unlock(
        self,
        device_names: list[str],
        registry: DeviceRegistry,
    ) -> tuple[list[str], list[str]]:
        """
        Unconditionally clear locks regardless of ownership.

        Returns (unlocked_devices, not_found_devices).
        """
        async with self._lock:
            # Validate first: if any requested device is unknown, change nothing
            # and report them all. This keeps force-unlock all-or-nothing, so the
            # caller never gets a "not found" error after some locks were already
            # cleared (and the audit log skipped).
            names = list(dict.fromkeys(device_names))  # de-duplicate, preserve order
            not_found = [name for name in names if registry.get_device(name) is None]
            if not_found:
                return [], not_found

            unlocked = []
            for name in names:
                if name in self._locks and self._locks[name].locked:
                    del self._locks[name]
                # Device exists (validated above); report it as unlocked whether
                # or not it currently held a lock.
                unlocked.append(name)

            if unlocked:
                self._version += 1
                logger.info("locks_force_cleared devices=%s", unlocked)

            return unlocked, not_found

    def is_device_locked(self, device_name: str) -> bool:
        """Check if a device is currently locked (and lease not lapsed)."""
        return self._active_lock(device_name) is not None

    def get_device_lock(self, device_name: str) -> DeviceLockState | None:
        """Get the lock state for a device, or None if unlocked/lapsed."""
        return self._active_lock(device_name)

    def get_all_locks(self) -> dict[str, DeviceLockState]:
        """Return a copy of all active (held, non-lapsed) locks."""
        now = datetime.now(UTC)
        return {
            name: state
            for name, state in self._locks.items()
            if state.locked and not state.is_expired(now)
        }

    def set_lock_all(self, enabled: bool) -> None:
        """Flip the lock_all availability policy at runtime."""
        if enabled != self.lock_all_enabled:
            self.lock_all_enabled = enabled
            self._version += 1
            logger.info("lock_all policy set to %s", enabled)

    def global_lock_holder(self) -> DeviceLockState | None:
        """The lock representing "a plan is running" under lock_all.

        The earliest-acquired active lock — stable for as long as that plan
        holds it, so locked_by_plan in status responses doesn't flap when
        additional locks come and go.
        """
        now = datetime.now(UTC)
        active = [
            state for state in self._locks.values() if state.locked and not state.is_expired(now)
        ]
        if not active:
            return None
        return min(active, key=lambda state: state.locked_at)

    def effective_lock(self, device_name: str) -> DeviceLockState | None:
        """The lock state governing this device's AVAILABILITY.

        The device's own lock when present; otherwise, when the lock_all
        policy is on and any lock is held anywhere, the global holder's
        lock — every registered device is unavailable while a plan runs.
        Lock acquisition/release is not affected by the policy; use
        get_device_lock() for the device's literal lock state.
        """
        own = self.get_device_lock(device_name)
        if own is not None:
            return own
        if self.lock_all_enabled:
            return self.global_lock_holder()
        return None

    def _get_device_pvs(
        self,
        device_names: list[str],
        registry: DeviceRegistry,
    ) -> list[str]:
        """Collect all PV names belonging to the given devices."""
        pvs = []
        for name in device_names:
            device = registry.get_device(name)
            if device is not None:
                pvs.extend(device.pvs.values())
        return sorted(set(pvs))
