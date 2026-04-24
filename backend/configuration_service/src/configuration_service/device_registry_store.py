"""
SQLite Store for Device Registry (source of truth).

Two-table design:
- device_registry: Current active state (one row per device)
- device_audit_log: Append-only change history

Startup flow:
1. Initialize tables
2. Check is_seeded() → if False, seed from profile collection
3. If True, load all devices from DB into in-memory DeviceRegistry

All CRUD operations write to both tables atomically.
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    DeviceAuditEntry,
    DeviceInstantiationSpec,
    DeviceMetadata,
    DeviceRegistry,
)

logger = logging.getLogger(__name__)


class DeviceRegistryStore:
    """
    SQLite-backed device registry (source of truth).

    Parameters
    ----------
    db_path : str or Path
        Path to SQLite database file (shared with standalone_pv_store)
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
        """Create tables if they don't exist. Safe to call multiple times."""
        if self._initialized:
            return

        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_registry (
                    name TEXT PRIMARY KEY,
                    device_metadata TEXT NOT NULL,
                    instantiation_spec TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_name TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    details TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS registry_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # Drop old table from previous schema if present
            conn.execute("DROP TABLE IF EXISTS device_change_history")

        self._initialized = True
        logger.info(f"Device registry store initialized: {self.db_path}")

    def is_seeded(self) -> bool:
        """Check whether the registry has been seeded from a profile."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT value FROM registry_metadata WHERE key = 'seeded'"
        )
        row = cursor.fetchone()
        return row is not None

    def seed_from_registry(self, registry: DeviceRegistry) -> None:
        """
        Seed the DB from an in-memory DeviceRegistry (loaded from profile).

        Inserts all devices and marks the DB as seeded. Each device gets
        a 'seed' entry in the audit log.
        """
        now = time.time()

        with self._transaction() as conn:
            for name, metadata in registry.devices.items():
                spec = registry.instantiation_specs.get(name)
                metadata_json = metadata.model_dump_json()
                spec_json = spec.model_dump_json() if spec else None

                conn.execute(
                    """
                    INSERT INTO device_registry
                        (name, device_metadata, instantiation_spec, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, metadata_json, spec_json, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO device_audit_log
                        (device_name, operation, timestamp, details)
                    VALUES (?, 'seed', ?, ?)
                    """,
                    (name, now, json.dumps({"source": "profile"})),
                )

            conn.execute(
                "INSERT OR REPLACE INTO registry_metadata (key, value) VALUES ('seeded', ?)",
                (str(now),),
            )

        logger.info(f"Seeded device registry with {len(registry.devices)} devices")

    def load_all_devices(self) -> DeviceRegistry:
        """
        Load all devices from DB into a fresh DeviceRegistry.

        Returns
        -------
        DeviceRegistry
            In-memory registry built from DB state
        """
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM device_registry ORDER BY name")

        registry = DeviceRegistry()
        for row in cursor.fetchall():
            metadata = DeviceMetadata.model_validate_json(row["device_metadata"])
            spec = None
            if row["instantiation_spec"]:
                spec = DeviceInstantiationSpec.model_validate_json(
                    row["instantiation_spec"]
                )
            registry.add_device(metadata, spec)

        return registry

    def save_device(
        self,
        name: str,
        metadata: DeviceMetadata,
        spec: Optional[DeviceInstantiationSpec] = None,
        operation: str = "add",
        details: Optional[dict] = None,
    ) -> None:
        """
        Save or update a device in the registry.

        Parameters
        ----------
        name : str
            Device name
        metadata : DeviceMetadata
            Device metadata
        spec : DeviceInstantiationSpec, optional
            Instantiation spec
        operation : str
            Audit log operation (add, update, enable, disable)
        details : dict, optional
            Details about what changed (serialized to JSON for storage)
        """
        now = time.time()
        metadata_json = metadata.model_dump_json()
        spec_json = spec.model_dump_json() if spec else None

        with self._transaction() as conn:
            cursor = conn.execute(
                "SELECT created_at FROM device_registry WHERE name = ?", (name,)
            )
            existing = cursor.fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE device_registry
                    SET device_metadata = ?, instantiation_spec = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (metadata_json, spec_json, now, name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO device_registry
                        (name, device_metadata, instantiation_spec, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, metadata_json, spec_json, now, now),
                )

            details_json = json.dumps(details) if details else None
            conn.execute(
                """
                INSERT INTO device_audit_log (device_name, operation, timestamp, details)
                VALUES (?, ?, ?, ?)
                """,
                (name, operation, now, details_json),
            )

        logger.debug(f"Saved device: {name} (operation={operation})")

    def delete_device(self, name: str, details: Optional[dict] = None) -> bool:
        """
        Delete a device from the registry.

        Returns True if the device existed and was deleted.
        """
        now = time.time()

        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM device_registry WHERE name = ?", (name,)
            )
            deleted = cursor.rowcount > 0

            if deleted:
                details_json = json.dumps(details) if details else None
                conn.execute(
                    """
                    INSERT INTO device_audit_log (device_name, operation, timestamp, details)
                    VALUES (?, 'delete', ?, ?)
                    """,
                    (name, now, details_json),
                )

        if deleted:
            logger.debug(f"Deleted device: {name}")
        return deleted

    def get_device(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get a single device from the DB.

        Returns dict with 'metadata' and 'spec' keys, or None.
        """
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT * FROM device_registry WHERE name = ?", (name,)
        )
        row = cursor.fetchone()
        if row is None:
            return None

        result: Dict[str, Any] = {
            "metadata": DeviceMetadata.model_validate_json(row["device_metadata"]),
            "spec": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["instantiation_spec"]:
            result["spec"] = DeviceInstantiationSpec.model_validate_json(
                row["instantiation_spec"]
            )
        return result

    def get_audit_log(
        self,
        device_name: Optional[str] = None,
        limit: int = 1000,
    ) -> List[DeviceAuditEntry]:
        """
        Get audit log entries.

        Parameters
        ----------
        device_name : str, optional
            Filter to a specific device
        limit : int
            Max entries to return (default 1000)
        """
        conn = self._get_connection()
        if device_name:
            cursor = conn.execute(
                "SELECT * FROM device_audit_log WHERE device_name = ? ORDER BY id DESC LIMIT ?",
                (device_name, limit),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM device_audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )

        return [
            DeviceAuditEntry(
                id=row["id"],
                device_name=row["device_name"],
                operation=row["operation"],
                timestamp=row["timestamp"],
                details=row["details"],
            )
            for row in cursor.fetchall()
        ]

    def clear_and_reseed(self, registry: DeviceRegistry) -> None:
        """
        Wipe all device data and re-seed from a fresh profile-loaded registry.

        Records a 'reset' audit entry before wiping.
        """
        now = time.time()

        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO device_audit_log (device_name, operation, timestamp, details)
                VALUES ('*', 'reset', ?, ?)
                """,
                (now, json.dumps({"reason": "manual_reset"})),
            )
            conn.execute("DELETE FROM device_registry")
            conn.execute("DELETE FROM registry_metadata WHERE key = 'seeded'")

        self.seed_from_registry(registry)
        logger.info("Registry cleared and re-seeded from profile")

    def export_happi(self) -> Dict[str, Any]:
        """
        Export the current registry in happi JSON format.

        Returns a dict keyed by device name, compatible with happi_db.json.
        """
        conn = self._get_connection()
        cursor = conn.execute("SELECT * FROM device_registry ORDER BY name")

        happi_db: Dict[str, Any] = {}
        for row in cursor.fetchall():
            metadata = DeviceMetadata.model_validate_json(row["device_metadata"])
            spec = None
            if row["instantiation_spec"]:
                spec = DeviceInstantiationSpec.model_validate_json(
                    row["instantiation_spec"]
                )

            device_class = (
                spec.device_class
                if spec
                else (
                    f"{metadata.module}.{metadata.ophyd_class}"
                    if metadata.module
                    else metadata.ophyd_class
                )
            )

            entry: Dict[str, Any] = {
                "_id": metadata.name,
                "name": metadata.name,
                "device_class": device_class,
                "args": spec.args if spec else [],
                "kwargs": spec.kwargs if spec else {"name": metadata.name},
                "type": device_class,
                "active": spec.active if spec else True,
            }

            if metadata.beamline:
                entry["beamline"] = metadata.beamline
            if metadata.functional_group:
                entry["functional_group"] = metadata.functional_group
            if metadata.location_group:
                entry["location_group"] = metadata.location_group
            if metadata.documentation:
                entry["documentation"] = metadata.documentation

            # Add prefix from first arg if it looks like a PV prefix
            if spec and spec.args and isinstance(spec.args[0], str) and ":" in str(spec.args[0]):
                entry["prefix"] = spec.args[0]

            happi_db[metadata.name] = entry

        return happi_db

    def log_lock_event(
        self,
        device_names: List[str],
        operation: str,
        details: Optional[str] = None,
    ) -> None:
        """
        Write lock/unlock/force_unlock events to the audit log.

        Parameters
        ----------
        device_names : list of str
            Devices involved in the lock event
        operation : str
            One of: "lock", "unlock", "force_unlock"
        details : str, optional
            JSON string with event details (plan name, item_id, reason, etc.)
        """
        now = time.time()
        with self._transaction() as conn:
            for name in device_names:
                conn.execute(
                    """
                    INSERT INTO device_audit_log (device_name, operation, timestamp, details)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, operation, now, details),
                )

    def device_count(self) -> int:
        """Get the number of devices in the registry."""
        conn = self._get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM device_registry")
        return cursor.fetchone()[0]

    # Operations exposed in the /changes feed. Lock/unlock/force_unlock don't
    # modify device state and are deliberately omitted. 'reset' is surfaced
    # through the reset_occurred flag, not as a per-device change.
    _CHANGE_FEED_OPS = ("seed", "add", "update", "delete", "enable", "disable")

    def get_changes_since(self, since_version: int) -> Dict[str, Any]:
        """
        Return device-level state deltas after ``since_version``.

        The result is deduped per device: only the latest operation per
        device within the range is reported, along with the device's current
        state (or a 'delete' marker if it no longer exists).

        Returns a dict with keys: ``current_version`` (int), ``service_epoch``
        (str), ``reset_occurred`` (bool), ``changes`` (list of dicts with
        keys ``device_name``, ``op``, ``version``, ``metadata``, ``spec``).
        """
        conn = self._get_connection()

        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS v FROM device_audit_log").fetchone()
        current_version = int(row["v"])

        row = conn.execute(
            "SELECT value FROM registry_metadata WHERE key = 'seeded'"
        ).fetchone()
        service_epoch = row["value"] if row else "unseeded"

        if since_version >= current_version:
            return {
                "current_version": current_version,
                "service_epoch": service_epoch,
                "reset_occurred": False,
                "changes": [],
            }

        reset_row = conn.execute(
            "SELECT 1 FROM device_audit_log WHERE id > ? AND operation = 'reset' LIMIT 1",
            (since_version,),
        ).fetchone()
        reset_occurred = reset_row is not None

        placeholders = ",".join("?" * len(self._CHANGE_FEED_OPS))
        # Single query joining audit log (for latest op per device in range)
        # with device_registry (for current state). A device whose latest op
        # is "delete" won't have a matching registry row — the LEFT JOIN
        # yields NULL columns, which we translate into op="delete".
        cursor = conn.execute(
            f"""
            SELECT a.device_name, a.id AS latest_id, a.operation,
                   r.device_metadata, r.instantiation_spec
            FROM device_audit_log a
            LEFT JOIN device_registry r ON r.name = a.device_name
            WHERE a.id IN (
                SELECT MAX(id) FROM device_audit_log
                WHERE id > ?
                  AND operation IN ({placeholders})
                  AND device_name != '*'
                GROUP BY device_name
            )
            ORDER BY a.id
            """,
            (since_version, *self._CHANGE_FEED_OPS),
        )

        changes: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            latest_id = int(row["latest_id"])
            if row["operation"] == "delete" or row["device_metadata"] is None:
                changes.append(
                    {
                        "device_name": row["device_name"],
                        "op": "delete",
                        "version": latest_id,
                        "metadata": None,
                        "spec": None,
                    }
                )
                continue

            metadata = DeviceMetadata.model_validate_json(row["device_metadata"])
            spec = (
                DeviceInstantiationSpec.model_validate_json(row["instantiation_spec"])
                if row["instantiation_spec"]
                else None
            )
            changes.append(
                {
                    "device_name": row["device_name"],
                    "op": "upsert",
                    "version": latest_id,
                    "metadata": metadata,
                    "spec": spec,
                }
            )

        return {
            "current_version": current_version,
            "service_epoch": service_epoch,
            "reset_occurred": reset_occurred,
            "changes": changes,
        }

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
