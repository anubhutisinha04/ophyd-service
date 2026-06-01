"""
Store for standalone PV registration.

Backend-agnostic (PostgreSQL or SQLite): persists standalone PVs (not associated
with any ophyd device) so they survive service restarts and appear in the unified
PV registry. Shares the engine (and database) with the device registry store;
queries are built with SQLAlchemy Core against the shared schema in ``db.py``.

Usage:
    store = StandalonePVStore(engine)
    store.initialize()
    store.save_pv(pv_name="SR:C01:RING:CURR", description="Ring current", labels=["diagnostics"])
"""

import json
import logging
import time
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Row

from .db import metadata, standalone_pvs, upsert
from .models import StandalonePV

logger = logging.getLogger(__name__)


class StandalonePVStore:
    """
    Store for standalone PV registrations (PostgreSQL or SQLite).

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        Shared engine (created once in main.py, also used by the registry store).
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        self._initialized = False

    def initialize(self) -> None:
        """Create the standalone_pvs table if it doesn't exist. Safe to repeat."""
        if self._initialized:
            return
        metadata.create_all(self._engine)
        self._initialized = True
        logger.info(
            "Standalone PV store initialized (%s)", self._engine.dialect.name
        )

    def save_pv(
        self,
        pv_name: str,
        description: Optional[str] = None,
        protocol: str = "ca",
        access_mode: str = "read-only",
        labels: Optional[List[str]] = None,
        source: str = "runtime",
        created_by: Optional[str] = None,
    ) -> None:
        """
        Save (upsert) a standalone PV. Preserves created_at/created_by on update.
        """
        now = time.time()
        labels_json = json.dumps(labels or [])

        with self._engine.begin() as conn:
            conn.execute(
                upsert(conn, standalone_pvs)
                .values(
                    pv_name=pv_name,
                    description=description,
                    protocol=protocol,
                    access_mode=access_mode,
                    labels=labels_json,
                    source=source,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["pv_name"],
                    set_={
                        "description": description,
                        "protocol": protocol,
                        "access_mode": access_mode,
                        "labels": labels_json,
                        "source": source,
                        "updated_at": now,
                    },
                )
            )

        logger.debug(f"Saved standalone PV: {pv_name}")

    def delete_pv(self, pv_name: str) -> bool:
        """Delete a standalone PV. Returns True if it was found and deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(standalone_pvs).where(standalone_pvs.c.pv_name == pv_name)
            )
            deleted = result.rowcount > 0

        if deleted:
            logger.debug(f"Deleted standalone PV: {pv_name}")
        return deleted

    def get_pv(self, pv_name: str) -> Optional[StandalonePV]:
        """Get a single standalone PV by name."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(standalone_pvs).where(standalone_pvs.c.pv_name == pv_name)
            ).first()
        if row is None:
            return None
        return self._row_to_model(row)

    def get_all_pvs(self, labels: Optional[List[str]] = None) -> List[StandalonePV]:
        """
        Get all standalone PVs, optionally filtered to those having ALL given labels.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(standalone_pvs).order_by(standalone_pvs.c.pv_name)
            ).all()
        pvs = [self._row_to_model(row) for row in rows]

        if labels:
            pvs = [pv for pv in pvs if all(label in pv.labels for label in labels)]

        return pvs

    def get_all_labels(self) -> List[str]:
        """Get all unique labels across all standalone PVs (sorted)."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(standalone_pvs.c.labels)).all()
        all_labels: set = set()
        for row in rows:
            all_labels.update(json.loads(row[0]))
        return sorted(all_labels)

    def clear_all(self) -> int:
        """Remove all standalone PV records. Returns the number deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(delete(standalone_pvs))
            count = result.rowcount

        logger.info(f"Cleared {count} standalone PVs")
        return count

    def _row_to_model(self, row: Row) -> StandalonePV:
        """Convert a database row to a StandalonePV model."""
        m = row._mapping
        return StandalonePV(
            pv_name=m["pv_name"],
            description=m["description"],
            protocol=m["protocol"],
            access_mode=m["access_mode"],
            labels=json.loads(m["labels"]),
            source=m["source"],
            created_by=m["created_by"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
        )

    def close(self) -> None:
        """No-op: the engine is owned and disposed by main.py."""
