"""
Store for the device registry (source of truth).

Backend-agnostic: works against either PostgreSQL or SQLite via the shared
engine from ``db.py`` (the dialect-specific upsert and audit-id column are
handled there).

Two-table design:
- device_registry: current active state (one row per device)
- device_audit_log: append-only change history (its auto-incrementing ``id`` is
  the monotonic version the /changes feed exposes)

Startup flow:
1. initialize() — create tables
2. is_seeded() → if False, seed from the profile collection
3. if True, load all devices from the DB into an in-memory DeviceRegistry

All CRUD operations write to both tables in one transaction. Queries are built
with SQLAlchemy Core against the shared schema in ``db.py``; the engine is owned
by ``main.py`` and injected here.
"""

import json
import logging
import time
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine

from .db import (
    device_audit_log,
    device_registry,
    metadata,
    registry_metadata,
    upsert,
)
from .models import (
    DeviceAuditEntry,
    DeviceInstantiationSpec,
    DeviceMetadata,
    DeviceRegistry,
)

logger = logging.getLogger(__name__)


class DeviceRegistryStore:
    """
    Device registry (source of truth), backed by PostgreSQL or SQLite.

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        Shared engine (created once in main.py, also used by the PV store).
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        self._initialized = False

    def initialize(self) -> None:
        """Create tables if they don't exist. Safe to call multiple times."""
        if self._initialized:
            return

        metadata.create_all(self._engine)
        with self._engine.begin() as conn:
            # Drop the table name used by a previous schema, if present.
            conn.execute(text("DROP TABLE IF EXISTS device_change_history"))

        self._initialized = True
        logger.info("Device registry store initialized (%s)", self._engine.dialect.name)

    def is_seeded(self) -> bool:
        """Check whether the registry has been seeded from a profile."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(registry_metadata.c.value).where(registry_metadata.c.key == "seeded")
            ).first()
        return row is not None

    def seed_from_registry(self, registry: DeviceRegistry) -> None:
        """
        Seed the DB from an in-memory DeviceRegistry (loaded from profile).

        Inserts all devices and marks the DB as seeded. Each device gets a
        'seed' entry in the audit log.
        """
        now = time.time()

        with self._engine.begin() as conn:
            for name, metadata_model in registry.devices.items():
                spec = registry.instantiation_specs.get(name)
                conn.execute(
                    device_registry.insert().values(
                        name=name,
                        device_metadata=metadata_model.model_dump_json(),
                        instantiation_spec=spec.model_dump_json() if spec else None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                conn.execute(
                    device_audit_log.insert().values(
                        device_name=name,
                        operation="seed",
                        timestamp=now,
                        details=json.dumps({"source": "profile"}),
                    )
                )

            conn.execute(
                upsert(conn, registry_metadata)
                .values(key="seeded", value=str(now))
                .on_conflict_do_update(index_elements=["key"], set_={"value": str(now)})
            )

        logger.info(f"Seeded device registry with {len(registry.devices)} devices")

    def load_all_devices(self) -> DeviceRegistry:
        """Load all devices from DB into a fresh DeviceRegistry."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(select(device_registry).order_by(device_registry.c.name))
                .mappings()
                .all()
            )

        registry = DeviceRegistry()
        for row in rows:
            metadata_model = DeviceMetadata.model_validate_json(row["device_metadata"])
            spec = None
            if row["instantiation_spec"]:
                spec = DeviceInstantiationSpec.model_validate_json(row["instantiation_spec"])
            registry.add_device(metadata_model, spec)

        return registry

    def save_device(
        self,
        name: str,
        metadata: DeviceMetadata,
        spec: DeviceInstantiationSpec | None = None,
        operation: str = "add",
        details: dict | None = None,
    ) -> None:
        """
        Save or update a device in the registry.

        On update, ``created_at`` is preserved and only ``updated_at`` advances.
        Appends an audit-log entry with the given ``operation``.
        """
        now = time.time()
        metadata_json = metadata.model_dump_json()
        spec_json = spec.model_dump_json() if spec else None

        with self._engine.begin() as conn:
            conn.execute(
                upsert(conn, device_registry)
                .values(
                    name=name,
                    device_metadata=metadata_json,
                    instantiation_spec=spec_json,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["name"],
                    set_={
                        "device_metadata": metadata_json,
                        "instantiation_spec": spec_json,
                        "updated_at": now,
                    },
                )
            )
            conn.execute(
                device_audit_log.insert().values(
                    device_name=name,
                    operation=operation,
                    timestamp=now,
                    details=json.dumps(details) if details else None,
                )
            )

        logger.debug(f"Saved device: {name} (operation={operation})")

    def delete_device(self, name: str, details: dict | None = None) -> bool:
        """Delete a device. Returns True if it existed and was deleted."""
        now = time.time()

        with self._engine.begin() as conn:
            result = conn.execute(delete(device_registry).where(device_registry.c.name == name))
            deleted = result.rowcount > 0

            if deleted:
                conn.execute(
                    device_audit_log.insert().values(
                        device_name=name,
                        operation="delete",
                        timestamp=now,
                        details=json.dumps(details) if details else None,
                    )
                )

        if deleted:
            logger.debug(f"Deleted device: {name}")
        return deleted

    def get_device(self, name: str) -> dict[str, Any] | None:
        """Get a single device. Returns dict with metadata/spec/timestamps, or None."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(device_registry).where(device_registry.c.name == name))
                .mappings()
                .first()
            )
        if row is None:
            return None

        result: dict[str, Any] = {
            "metadata": DeviceMetadata.model_validate_json(row["device_metadata"]),
            "spec": None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["instantiation_spec"]:
            result["spec"] = DeviceInstantiationSpec.model_validate_json(row["instantiation_spec"])
        return result

    def get_audit_log(
        self,
        device_name: str | None = None,
        limit: int = 1000,
    ) -> list[DeviceAuditEntry]:
        """Get audit log entries, optionally filtered to a device, newest first."""
        stmt = select(device_audit_log).order_by(device_audit_log.c.id.desc()).limit(limit)
        if device_name:
            stmt = (
                select(device_audit_log)
                .where(device_audit_log.c.device_name == device_name)
                .order_by(device_audit_log.c.id.desc())
                .limit(limit)
            )

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [
            DeviceAuditEntry(
                id=row["id"],
                device_name=row["device_name"],
                operation=row["operation"],
                timestamp=row["timestamp"],
                details=row["details"],
            )
            for row in rows
        ]

    def clear_and_reseed(self, registry: DeviceRegistry) -> None:
        """Wipe all device data and re-seed. Records a 'reset' audit entry first."""
        now = time.time()

        with self._engine.begin() as conn:
            conn.execute(
                device_audit_log.insert().values(
                    device_name="*",
                    operation="reset",
                    timestamp=now,
                    details=json.dumps({"reason": "manual_reset"}),
                )
            )
            conn.execute(delete(device_registry))
            conn.execute(delete(registry_metadata).where(registry_metadata.c.key == "seeded"))

        self.seed_from_registry(registry)
        logger.info("Registry cleared and re-seeded from profile")

    def export_happi(self) -> dict[str, Any]:
        """Export the current registry in happi JSON format (keyed by device name)."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(select(device_registry).order_by(device_registry.c.name))
                .mappings()
                .all()
            )

        happi_db: dict[str, Any] = {}
        for row in rows:
            metadata_model = DeviceMetadata.model_validate_json(row["device_metadata"])
            if not row["instantiation_spec"]:
                raise RuntimeError(
                    f"Registry inconsistency: device '{metadata_model.name}' has no "
                    f"instantiation spec; cannot export"
                )
            spec = DeviceInstantiationSpec.model_validate_json(row["instantiation_spec"])

            entry: dict[str, Any] = {
                "_id": metadata_model.name,
                "name": metadata_model.name,
                "device_class": spec.device_class,
                "args": spec.args,
                "kwargs": spec.kwargs,
                "type": spec.device_class,
                "active": spec.active,
            }

            if metadata_model.beamline:
                entry["beamline"] = metadata_model.beamline
            if metadata_model.functional_group:
                entry["functional_group"] = metadata_model.functional_group
            if metadata_model.location_group:
                entry["location_group"] = metadata_model.location_group
            if metadata_model.documentation:
                entry["documentation"] = metadata_model.documentation

            if spec.args and isinstance(spec.args[0], str) and ":" in str(spec.args[0]):
                entry["prefix"] = spec.args[0]

            happi_db[metadata_model.name] = entry

        return happi_db

    def export_bits(self) -> dict[str, Any]:
        """Export the current registry in BITS (BCDA-APS guarneri) ``devices.yml`` format.

        Returns a mapping keyed by the device *callable path* — an ophyd class
        (e.g. ``ophyd.EpicsMotor``) or a module (e.g. ``ophyd.sim``) when the
        device is built by a factory function — whose value is a list of
        guarneri-style entries. Serialize the result with ``yaml.safe_dump`` to
        obtain a ``devices.yml``.

        The structure is designed to round-trip through
        :class:`configuration_service.loader.BitsProfileLoader`.

        Fidelity note: the guarneri schema only carries ``name``, ``prefix``,
        ``read_pv`` and ``labels`` per entry. Constructor arguments beyond the
        first positional (``prefix``) and kwargs beyond ``name``/``labels``
        (e.g. ``write_pv``) are not representable in this format and are dropped
        on export. Use :meth:`export_happi` (the default) when full
        instantiation fidelity is required.
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(select(device_registry).order_by(device_registry.c.name))
                .mappings()
                .all()
            )

        devices_yml: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            metadata_model = DeviceMetadata.model_validate_json(row["device_metadata"])
            if not row["instantiation_spec"]:
                raise RuntimeError(
                    f"Registry inconsistency: device '{metadata_model.name}' has no "
                    f"instantiation spec; cannot export"
                )
            spec = DeviceInstantiationSpec.model_validate_json(row["instantiation_spec"])

            group_key, entry = self._bits_entry_from_spec(spec, metadata_model)
            devices_yml.setdefault(group_key, []).append(entry)

        return devices_yml

    @staticmethod
    def _bits_entry_from_spec(
        spec: DeviceInstantiationSpec, metadata: DeviceMetadata
    ) -> tuple[str, dict[str, Any]]:
        """Map an instantiation spec to a ``(yaml_key, entry)`` BITS pair.

        Mirrors ``BitsProfileLoader._process_entry`` so exported YAML reloads
        into an equivalent registry. That loader treats a YAML key as a class
        path iff its final dotted segment starts with an uppercase letter
        (``parts[-1][0].isupper()``); this uses the same predicate:

        - Final segment starts with an uppercase letter (e.g. ``ophyd.EpicsMotor``)
          => the whole path becomes the YAML key directly; the entry carries no
          ``creator``.
        - Otherwise (e.g. ``ophyd.sim.motor``) the path is split: the module
          becomes the YAML key and the callable name becomes the entry's
          ``creator``. Using ``isupper`` rather than ``islower`` matches the
          loader for final segments that start with a non-letter (e.g. a private
          ``_Factory``), which ``islower`` would misclassify as a class path.
        """
        device_class = spec.device_class
        module_part, _, last = device_class.rpartition(".")
        if module_part and not last[:1].isupper():
            # Factory/creator function: key is the module, creator names the callable.
            group_key = module_part
            entry: dict[str, Any] = {"name": spec.name, "creator": last}
        else:
            # Class path (last segment is a capitalised class name) or a
            # single-segment fallback.
            group_key = device_class
            entry = {"name": spec.name}

        # prefix: first positional string arg. BitsProfileLoader reads
        # entry["prefix"] and rebuilds args=[prefix].
        if spec.args and isinstance(spec.args[0], str) and spec.args[0]:
            entry["prefix"] = spec.args[0]

        # read_pv: signals may carry a distinct read PV as a kwarg (best-effort).
        read_pv = spec.kwargs.get("read_pv")
        if isinstance(read_pv, str) and read_pv:
            entry["read_pv"] = read_pv

        # labels: prefer authoritative metadata labels; fall back to spec kwargs.
        labels = metadata.labels or spec.kwargs.get("labels") or []
        if labels:
            entry["labels"] = list(labels)

        return group_key, entry

    def log_lock_event(
        self,
        device_names: list[str],
        operation: str,
        details: str | None = None,
    ) -> None:
        """Write lock/unlock/force_unlock events to the audit log."""
        now = time.time()
        with self._engine.begin() as conn:
            for name in device_names:
                conn.execute(
                    device_audit_log.insert().values(
                        device_name=name,
                        operation=operation,
                        timestamp=now,
                        details=details,
                    )
                )

    def device_count(self) -> int:
        """Get the number of devices in the registry."""
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(device_registry)).scalar_one())

    # Operations exposed in the /changes feed. Lock/unlock/force_unlock don't
    # modify device state and are deliberately omitted. 'reset' is surfaced
    # through the reset_occurred flag, not as a per-device change.
    _CHANGE_FEED_OPS = ("seed", "add", "update", "delete", "enable", "disable")

    def get_changes_since(self, since_version: int) -> dict[str, Any]:
        """
        Return device-level state deltas after ``since_version``.

        Deduped per device: only the latest operation per device within the
        range is reported, along with the device's current state (or a 'delete'
        marker if it no longer exists).

        Returns a dict with keys ``current_version`` (int), ``service_epoch``
        (str), ``reset_occurred`` (bool), ``changes`` (list of dicts with keys
        ``device_name``, ``op``, ``version``, ``metadata``, ``spec``).
        """
        audit = device_audit_log
        reg = device_registry

        with self._engine.connect() as conn:
            current_version = int(
                conn.execute(select(func.coalesce(func.max(audit.c.id), 0))).scalar_one()
            )

            epoch_row = conn.execute(
                select(registry_metadata.c.value).where(registry_metadata.c.key == "seeded")
            ).first()
            service_epoch = epoch_row[0] if epoch_row else "unseeded"

            if since_version >= current_version:
                return {
                    "current_version": current_version,
                    "service_epoch": service_epoch,
                    "reset_occurred": False,
                    "changes": [],
                }

            reset_row = conn.execute(
                select(audit.c.id)
                .where(audit.c.id > since_version, audit.c.operation == "reset")
                .limit(1)
            ).first()
            reset_occurred = reset_row is not None

            # ids of the latest change-feed op per device in the range
            latest_ids = (
                select(func.max(audit.c.id))
                .where(
                    audit.c.id > since_version,
                    audit.c.operation.in_(self._CHANGE_FEED_OPS),
                    audit.c.device_name != "*",
                )
                .group_by(audit.c.device_name)
            )
            # A device whose latest op is "delete" has no registry row — the
            # LEFT JOIN yields NULL columns, which we translate into op="delete".
            stmt = (
                select(
                    audit.c.device_name,
                    audit.c.id.label("latest_id"),
                    audit.c.operation,
                    reg.c.device_metadata,
                    reg.c.instantiation_spec,
                )
                .select_from(audit.outerjoin(reg, reg.c.name == audit.c.device_name))
                .where(audit.c.id.in_(latest_ids))
                .order_by(audit.c.id)
            )
            rows = conn.execute(stmt).mappings().all()

        changes: list[dict[str, Any]] = []
        for row in rows:
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

            metadata_model = DeviceMetadata.model_validate_json(row["device_metadata"])
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
                    "metadata": metadata_model,
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
        """No-op: the engine is owned and disposed by main.py."""

    def ping(self) -> None:
        """Verify the DB is queryable. Raises on failure (used by /health).

        Sets a short timeout so a hung or slow/locked database surfaces as a fast
        /health failure instead of blocking the probe — which could otherwise
        trip a Kubernetes liveness probe and kill the pod. On PostgreSQL that's a
        transaction-local ``statement_timeout`` (never leaks back to the pool).
        On SQLite ``busy_timeout`` is CONNECTION-scoped and the connection is
        pooled, so the prior value is restored afterwards — otherwise one
        /health probe would permanently drop that connection's write-lock wait
        from the engine-configured 30s to 2s for every later checkout.
        """
        with self._engine.connect() as conn:
            if conn.dialect.name == "sqlite":
                prior = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
                conn.exec_driver_sql("PRAGMA busy_timeout = 2000")
                try:
                    conn.execute(text("SELECT 1")).first()
                finally:
                    conn.exec_driver_sql(f"PRAGMA busy_timeout = {int(prior)}")
                return
            if conn.dialect.name == "postgresql":
                conn.execute(text("SET LOCAL statement_timeout = 2000"))
            conn.execute(text("SELECT 1")).first()
