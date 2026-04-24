"""
SQLite Store for Generic Metadata Key-Value Entries.

Persists arbitrary JSON dictionaries keyed by string keys. The service
is agnostic to the content — it stores and returns whatever the user provides.

Shares the same database file as device_registry_store and standalone_pv_store.
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MetadataStore:
    """
    SQLite-backed generic metadata key-value store.

    Parameters
    ----------
    db_path : str or Path
        Path to SQLite database file (shared with other stores)
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._initialized = False

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
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
        """Create table if it doesn't exist. Safe to call multiple times."""
        if self._initialized:
            return

        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata_entries (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

        self._initialized = True
        logger.info(f"Metadata store initialized: {self.db_path}")

    def save(self, key: str, value: Dict[str, Any]) -> None:
        """
        Save (upsert) a metadata entry. Preserves created_at on update.

        Parameters
        ----------
        key : str
            Unique string key
        value : dict
            Arbitrary JSON-serializable dictionary
        """
        now = time.time()
        value_json = json.dumps(value)

        with self._transaction() as conn:
            cursor = conn.execute(
                "SELECT created_at FROM metadata_entries WHERE key = ?",
                (key,),
            )
            existing = cursor.fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE metadata_entries
                    SET value = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (value_json, now, key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO metadata_entries (key, value, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, value_json, now, now),
                )

        logger.debug(f"Saved metadata: {key}")

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Get a single metadata entry by key.

        Returns
        -------
        dict or None
            {"key": str, "value": dict, "created_at": float, "updated_at": float}
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM metadata_entries WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_all(self) -> List[Dict[str, Any]]:
        """
        Get all metadata entries.

        Returns
        -------
        list of dict
            Sorted by key
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM metadata_entries ORDER BY key"
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def delete(self, key: str) -> bool:
        """
        Delete a metadata entry.

        Returns
        -------
        bool
            True if the key existed and was deleted
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM metadata_entries WHERE key = ?", (key,)
            )
            deleted = cursor.rowcount > 0

        if deleted:
            logger.debug(f"Deleted metadata: {key}")
        return deleted

    def clear_all(self) -> int:
        """
        Remove all metadata entries.

        Returns
        -------
        int
            Number of entries deleted
        """
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM metadata_entries")
            count = cursor.rowcount

        logger.info(f"Cleared {count} metadata entries")
        return count

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert database row to dictionary."""
        return {
            "key": row["key"],
            "value": json.loads(row["value"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
