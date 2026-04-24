"""
SQLite Store for Standalone PV Registration.

Persists standalone PVs (not associated with any ophyd device) so they
survive service restarts and appear in the unified PV registry.

Design:
- SQLite with WAL mode for concurrent read/write access (same pattern as device_change_history.py)
- Shares the same database file as device_change_history
- Thread-local connections with 30s timeout

Usage:
    store = StandalonePVStore("/var/lib/bluesky/config_service.db")
    store.initialize()
    store.save_pv(pv_name="SR:C01:RING:CURR", description="Ring current", labels=["diagnostics"])
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import StandalonePV

logger = logging.getLogger(__name__)


class StandalonePVStore:
    """
    SQLite-based store for standalone PV registrations.

    Parameters
    ----------
    db_path : str or Path
        Path to SQLite database file (shared with device_change_history)
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._initialized = False

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,
            )

            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row

            self._local.conn = conn

        return self._local.conn

    @contextmanager
    def _transaction(self):
        """Context manager for database transactions."""
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def initialize(self) -> None:
        """
        Initialize database schema.

        Creates the standalone_pvs table if it doesn't exist.
        Safe to call multiple times.
        """
        if self._initialized:
            return

        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS standalone_pvs (
                    pv_name TEXT PRIMARY KEY,
                    description TEXT,
                    protocol TEXT NOT NULL DEFAULT 'ca',
                    access_mode TEXT NOT NULL DEFAULT 'read-only',
                    labels TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'runtime',
                    created_by TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

        self._initialized = True
        logger.info(f"Standalone PV store initialized: {self.db_path}")

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
        Save (upsert) a standalone PV. Preserves created_at on update.

        Parameters
        ----------
        pv_name : str
            EPICS PV name
        description : str, optional
            Human-readable description
        protocol : str
            EPICS protocol ("ca" or "pva")
        access_mode : str
            Access mode ("read-only" or "read-write")
        labels : list of str, optional
            Labels for RBAC grouping
        source : str
            Source identifier (default: "runtime")
        created_by : str, optional
            User who registered this PV
        """
        now = time.time()
        labels_json = json.dumps(labels or [])

        with self._transaction() as conn:
            # Check if PV already exists to preserve created_at
            cursor = conn.execute(
                "SELECT created_at, created_by FROM standalone_pvs WHERE pv_name = ?",
                (pv_name,),
            )
            existing = cursor.fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE standalone_pvs
                    SET description = ?, protocol = ?, access_mode = ?,
                        labels = ?, source = ?, updated_at = ?
                    WHERE pv_name = ?
                    """,
                    (description, protocol, access_mode,
                     labels_json, source, now, pv_name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO standalone_pvs
                        (pv_name, description, protocol, access_mode,
                         labels, source, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (pv_name, description, protocol, access_mode,
                     labels_json, source, created_by, now, now),
                )

        logger.debug(f"Saved standalone PV: {pv_name}")

    def delete_pv(self, pv_name: str) -> bool:
        """
        Delete a standalone PV.

        Returns
        -------
        bool
            True if PV was found and deleted
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM standalone_pvs WHERE pv_name = ?", (pv_name,)
            )
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug(f"Deleted standalone PV: {pv_name}")
        return deleted

    def get_pv(self, pv_name: str) -> Optional[StandalonePV]:
        """
        Get a single standalone PV by name.

        Returns
        -------
        StandalonePV or None
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM standalone_pvs WHERE pv_name = ?", (pv_name,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_model(row)

    def get_all_pvs(self, labels: Optional[List[str]] = None) -> List[StandalonePV]:
        """
        Get all standalone PVs, optionally filtered by labels.

        Parameters
        ----------
        labels : list of str, optional
            If provided, only return PVs that have ALL specified labels

        Returns
        -------
        list of StandalonePV
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM standalone_pvs ORDER BY pv_name"
        )
        pvs = [self._row_to_model(row) for row in cursor.fetchall()]

        if labels:
            pvs = [
                pv for pv in pvs
                if all(label in pv.labels for label in labels)
            ]

        return pvs

    def get_all_labels(self) -> List[str]:
        """
        Get all unique labels across all standalone PVs.

        Returns
        -------
        list of str
            Sorted list of unique labels
        """
        conn = self._get_connection()
        cursor = conn.execute("SELECT labels FROM standalone_pvs")
        all_labels: set = set()
        for row in cursor.fetchall():
            labels = json.loads(row["labels"])
            all_labels.update(labels)
        return sorted(all_labels)

    def clear_all(self) -> int:
        """
        Remove all standalone PV records.

        Returns
        -------
        int
            Number of records deleted
        """
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM standalone_pvs")
            count = cursor.rowcount

        logger.info(f"Cleared {count} standalone PVs")
        return count

    def _row_to_model(self, row: sqlite3.Row) -> StandalonePV:
        """Convert database row to StandalonePV model."""
        return StandalonePV(
            pv_name=row["pv_name"],
            description=row["description"],
            protocol=row["protocol"],
            access_mode=row["access_mode"],
            labels=json.loads(row["labels"]),
            source=row["source"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
