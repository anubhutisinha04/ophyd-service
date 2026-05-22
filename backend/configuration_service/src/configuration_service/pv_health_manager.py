"""In-memory PV health tracking for configuration_service.

Direct-control reports each caput's outcome (success or failure) back to
configuration_service via ``POST /api/v1/pvs/{pv_name}/failure`` or
``…/success``. We accumulate per-PV records — last-success / last-failure
timestamps + a consecutive-failure counter that drives the health-state
classification.

Design notes:

- **In-memory only** (mirrors ``DeviceLockManager``). Health resets on
  service restart. That's intentional for v1 because EPICS connectivity
  is itself fresh after a restart; persisting stale "unresponsive" state
  across a restart can mislead the operator.

- **No "device level" health record.** We track per-PV, with the
  device-status endpoint rolling up only the PVs that have records.
  Devices with no failed caputs ever just don't appear in the rollup,
  which the frontend treats as "healthy".

- **State classification is derived**, not stored — see
  ``PVHealthRecord.state`` in models.py. The counter is the source of
  truth.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .models import PVHealthRecord, PVHealthState

logger = logging.getLogger(__name__)


class PVHealthManager:
    """Threadsafe (via asyncio.Lock) PV health record store.

    Mirrors the shape of ``DeviceLockManager``: a per-process singleton
    with simple async methods. The lock guards multi-step updates
    (read record → mutate counter → write back) so two concurrent
    failure reports for the same PV can't lose an increment.
    """

    def __init__(self) -> None:
        self._records: Dict[str, PVHealthRecord] = {}
        self._lock = asyncio.Lock()

    async def record_failure(
        self, pv_name: str, message: Optional[str] = None
    ) -> PVHealthRecord:
        """Increment the consecutive-failure counter and stamp the failure."""
        async with self._lock:
            existing = self._records.get(pv_name)
            consecutive = (existing.consecutive_failures if existing else 0) + 1
            last_success = existing.last_success_at if existing else None
            record = PVHealthRecord(
                pv_name=pv_name,
                consecutive_failures=consecutive,
                last_failure_at=datetime.now(timezone.utc),
                last_failure_message=message,
                last_success_at=last_success,
            )
            self._records[pv_name] = record
            return record

    async def record_success(self, pv_name: str) -> PVHealthRecord:
        """Reset the counter and stamp the success.

        A single success flips the state back to ``healthy`` regardless
        of how many failures preceded it — the underlying assumption
        being that a recent success is stronger evidence than older
        failures.

        **Bounded growth:** if there's no existing record for the PV
        (i.e. it's never failed since service start), we return a
        synthetic healthy record without storing it. The dict therefore
        only retains entries for PVs that have actually failed at some
        point — *not* every PV that's ever been caput'd. For a busy
        beamline that writes to thousands of PVs without any of them
        failing, ``_records`` stays empty.
        """
        async with self._lock:
            existing = self._records.get(pv_name)
            now = datetime.now(timezone.utc)
            if existing is None:
                # Never-failed PV — return a transient record for the
                # endpoint's response without growing the dict.
                return PVHealthRecord(
                    pv_name=pv_name,
                    consecutive_failures=0,
                    last_success_at=now,
                )
            # Existing record: flip to healthy but preserve the
            # last-failure metadata so the operator UI can show
            # "recovered from <error> at <timestamp>".
            record = PVHealthRecord(
                pv_name=pv_name,
                consecutive_failures=0,
                last_failure_at=existing.last_failure_at,
                last_failure_message=existing.last_failure_message,
                last_success_at=now,
            )
            self._records[pv_name] = record
            return record

    async def get_health(self, pv_name: str) -> Optional[PVHealthRecord]:
        """Return the record for ``pv_name`` or ``None`` if no caput has
        been reported yet (the frontend treats absence as ``healthy``)."""
        async with self._lock:
            return self._records.get(pv_name)

    async def get_health_many(
        self, pv_names: List[str]
    ) -> Dict[str, PVHealthRecord]:
        """Return records only for the PVs in ``pv_names`` that have any.

        Used by the device-status endpoint to roll up health for a
        device's PVs. PVs without records are absent from the result —
        the caller's downstream UI treats absence as healthy.
        """
        async with self._lock:
            return {
                pv: self._records[pv]
                for pv in pv_names
                if pv in self._records
            }

    async def clear(self, pv_name: str) -> bool:
        """Drop the record for ``pv_name`` (admin reset). Returns True
        iff a record was actually removed."""
        async with self._lock:
            return self._records.pop(pv_name, None) is not None

    async def clear_all(self) -> int:
        """Drop everything. Returns the count of records removed."""
        async with self._lock:
            count = len(self._records)
            self._records.clear()
            return count

    def snapshot_size(self) -> int:
        """Best-effort record count (no lock). Safe to read under the GIL
        but the result is a point-in-time estimate."""
        return len(self._records)

    async def stats(self) -> Dict[str, int]:
        """Return a count of records grouped by ``PVHealthState``.

        Used by the admin stats endpoint for an at-a-glance "how many
        PVs are unhealthy?" view. Every state in :class:`PVHealthState`
        is present in the result (zero if no records match), so
        downstream UIs don't have to special-case missing keys.
        """
        async with self._lock:
            counts: Dict[str, int] = {s.value: 0 for s in PVHealthState}
            for record in self._records.values():
                counts[record.state.value] += 1
            return counts
