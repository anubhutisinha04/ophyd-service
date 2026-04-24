"""
Device Lock Manager for Configuration Service.

Manages in-memory device lock state for A4 coordination between
Experiment Execution (SVC-001) and Direct Control (SVC-003).

Lock state is ephemeral (not persisted to SQLite). On service restart,
all locks are cleared. Lock/unlock events are written to the audit log
separately by the endpoint handlers.

Design:
- Locks are stored at the device level
- PV availability is derived by resolving PV → owning device → lock state
- All-or-nothing atomic lock acquisition (no partial locks)
- Only the item_id that acquired a lock can release it (unless force-unlock)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DeviceRegistry

logger = logging.getLogger(__name__)


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
    )

    def __init__(
        self,
        device_name: str,
        locked_by_plan: str,
        locked_by_item: str,
        locked_by_service: str,
        lock_id: str,
    ):
        self.device_name = device_name
        self.locked = True
        self.locked_by_plan = locked_by_plan
        self.locked_by_item = locked_by_item
        self.locked_by_service = locked_by_service
        self.locked_at = datetime.now(timezone.utc)
        self.lock_id = lock_id

    def to_dict(self) -> dict:
        return {
            "device_name": self.device_name,
            "locked": self.locked,
            "locked_by_plan": self.locked_by_plan,
            "locked_by_item": self.locked_by_item,
            "locked_by_service": self.locked_by_service,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
            "lock_id": self.lock_id,
        }


class LockConflict:
    """Information about a device that could not be locked."""

    __slots__ = ("device_name", "reason", "locked_by_plan", "locked_at")

    def __init__(
        self,
        device_name: str,
        reason: str,
        locked_by_plan: Optional[str] = None,
        locked_at: Optional[datetime] = None,
    ):
        self.device_name = device_name
        self.reason = reason
        self.locked_by_plan = locked_by_plan
        self.locked_at = locked_at


class LockResult:
    """Result of a lock acquisition attempt."""

    def __init__(
        self,
        success: bool,
        lock_id: Optional[str] = None,
        locked_devices: Optional[List[str]] = None,
        locked_pvs: Optional[List[str]] = None,
        conflicts: Optional[List[LockConflict]] = None,
        error_code: int = 200,
    ):
        self.success = success
        self.lock_id = lock_id
        self.locked_devices = locked_devices or []
        self.locked_pvs = locked_pvs or []
        self.conflicts = conflicts or []
        self.error_code = error_code


class DeviceLockManager:
    """
    Manages in-memory device lock state.

    Thread-safe via asyncio.Lock for atomic lock acquisition/release.
    Lock state is ephemeral — cleared on service restart.
    """

    def __init__(self):
        self._locks: Dict[str, DeviceLockState] = {}
        self._lock = asyncio.Lock()
        self._version: int = 0

    @property
    def version(self) -> int:
        """Monotonic version counter, incremented on every lock/unlock."""
        return self._version

    async def acquire_locks(
        self,
        device_names: List[str],
        item_id: str,
        plan_name: str,
        locked_by_service: str,
        registry: "DeviceRegistry",
    ) -> LockResult:
        """
        Atomically acquire locks on multiple devices (all-or-nothing).

        Returns LockResult with success=True and lock details, or
        success=False with conflict information and appropriate error_code.
        """
        async with self._lock:
            conflicts: List[LockConflict] = []

            # Validate all devices before acquiring any locks
            for name in device_names:
                device = registry.get_device(name)
                if device is None:
                    conflicts.append(LockConflict(
                        device_name=name,
                        reason="not_found",
                    ))
                    continue

                spec = registry.get_instantiation_spec(name)
                if spec is not None and not spec.active:
                    conflicts.append(LockConflict(
                        device_name=name,
                        reason="disabled",
                    ))
                    continue

                existing_lock = self._locks.get(name)
                if existing_lock is not None and existing_lock.locked:
                    conflicts.append(LockConflict(
                        device_name=name,
                        reason="already_locked",
                        locked_by_plan=existing_lock.locked_by_plan,
                        locked_at=existing_lock.locked_at,
                    ))

            if conflicts:
                # Determine error code from conflict types
                reasons = {c.reason for c in conflicts}
                if "not_found" in reasons:
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
            for name in device_names:
                self._locks[name] = DeviceLockState(
                    device_name=name,
                    locked_by_plan=plan_name,
                    locked_by_item=item_id,
                    locked_by_service=locked_by_service,
                    lock_id=lock_id,
                )

            # Collect all PVs belonging to locked devices
            locked_pvs = self._get_device_pvs(device_names, registry)

            self._version += 1
            logger.info(
                "locks_acquired",
                devices=device_names,
                plan=plan_name,
                item_id=item_id,
                lock_id=lock_id,
            )

            return LockResult(
                success=True,
                lock_id=lock_id,
                locked_devices=list(device_names),
                locked_pvs=locked_pvs,
            )

    async def release_locks(
        self,
        device_names: List[str],
        item_id: str,
    ) -> Tuple[bool, List[str], Optional[str]]:
        """
        Release locks owned by item_id.

        Returns (success, unlocked_devices, error_message).
        If a device is locked by a different item_id, returns failure.
        """
        async with self._lock:
            # Verify ownership
            for name in device_names:
                lock_state = self._locks.get(name)
                if lock_state is not None and lock_state.locked:
                    if lock_state.locked_by_item != item_id:
                        return (
                            False,
                            [],
                            f"Device '{name}' is locked by item {lock_state.locked_by_item}, "
                            f"not {item_id}",
                        )

            # Release locks
            unlocked = []
            for name in device_names:
                if name in self._locks and self._locks[name].locked:
                    del self._locks[name]
                    unlocked.append(name)

            if unlocked:
                self._version += 1
                logger.info(
                    "locks_released",
                    devices=unlocked,
                    item_id=item_id,
                )

            return True, unlocked, None

    async def force_unlock(
        self,
        device_names: List[str],
        registry: "DeviceRegistry",
    ) -> Tuple[List[str], List[str]]:
        """
        Unconditionally clear locks regardless of ownership.

        Returns (unlocked_devices, not_found_devices).
        """
        async with self._lock:
            unlocked = []
            not_found = []

            for name in device_names:
                device = registry.get_device(name)
                if device is None:
                    not_found.append(name)
                    continue

                if name in self._locks and self._locks[name].locked:
                    del self._locks[name]
                    unlocked.append(name)
                else:
                    # Device exists but wasn't locked — still report as unlocked
                    unlocked.append(name)

            if unlocked:
                self._version += 1
                logger.info("locks_force_cleared", devices=unlocked)

            return unlocked, not_found

    def is_device_locked(self, device_name: str) -> bool:
        """Check if a device is currently locked."""
        lock_state = self._locks.get(device_name)
        return lock_state is not None and lock_state.locked

    def get_device_lock(self, device_name: str) -> Optional[DeviceLockState]:
        """Get the lock state for a device, or None if unlocked."""
        lock_state = self._locks.get(device_name)
        if lock_state is not None and lock_state.locked:
            return lock_state
        return None

    def get_all_locks(self) -> Dict[str, DeviceLockState]:
        """Return a copy of all active locks."""
        return {
            name: state
            for name, state in self._locks.items()
            if state.locked
        }

    def _get_device_pvs(
        self,
        device_names: List[str],
        registry: "DeviceRegistry",
    ) -> List[str]:
        """Collect all PV names belonging to the given devices."""
        pvs = []
        for name in device_names:
            device = registry.get_device(name)
            if device is not None:
                pvs.extend(device.pvs.values())
        return sorted(set(pvs))
