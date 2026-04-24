# Architecture

## What the service does

The Configuration Service is a device registry. It answers the question: "What devices exist on this beamline, and how do I instantiate them?"

Other services — Experiment Execution, Direct Control, Device Monitoring — query this registry to discover devices, get their PV mappings, fetch constructor arguments, and check availability. The Configuration Service does not connect to EPICS or control hardware. It is a metadata service.

## Source files

```
src/configuration_service/
├── main.py                  FastAPI app factory, all endpoints, lifespan management
├── models.py                Pydantic domain models (DeviceMetadata, DeviceRegistry, etc.)
├── config.py                Pydantic Settings (CONFIG_ env vars)
├── protocols.py             Protocol interfaces (ProfileLoader, DeviceRegistryProtocol)
├── cli.py                   CLI entry point (argparse + uvicorn)
├── loader.py                Profile loaders (HappiProfileLoader, BitsProfileLoader, MockProfileLoader, EmptyProfileLoader)
├── class_capabilities.py    Static lookup table: ophyd class name → capability flags
├── device_registry_store.py SQLite persistence for device registry + audit log
├── standalone_pv_store.py   SQLite persistence for standalone PVs
├── lock_manager.py          In-memory device lock state (ephemeral)
└── __init__.py              Package exports
```

## Startup flow

The `create_app()` factory in `main.py` creates a FastAPI application with a lifespan context manager. This is the full startup sequence:

```
create_app(settings)
    │
    ▼
lifespan(app)
    │
    ├── device_change_history_enabled?
    │       │
    │       ├── yes → DeviceRegistryStore.initialize()
    │       │           │
    │       │           ├── is_seeded()? → yes → load_all_devices() from DB
    │       │           │
    │       │           └── is_seeded()? → no  → create_loader(settings)
    │       │                                      → loader.load_registry()
    │       │                                      → store.seed_from_registry()
    │       │
    │       └── no  → create_loader(settings)
    │                   → loader.load_registry()
    │                   (no persistence, reload from profile every startup)
    │
    ├── ConfigurationState(registry) → state_container
    │
    ├── StandalonePVStore.initialize() → apply saved standalone PVs
    │
    └── DeviceLockManager() → lock_manager_container
```

## DB-as-source-of-truth

When `device_change_history_enabled=true` (the default), the SQLite database is the source of truth for the device registry.

**First startup**: The loader reads from the profile collection and seeds the database. The DB is marked as seeded.

**Subsequent startups**: The service loads directly from the database. The profile collection is not re-read. This means runtime CRUD changes (creates, updates, deletes) persist across restarts.

**Reset**: `POST /api/v1/registry/reset` wipes the device tables and re-seeds from the profile. All runtime changes are lost.

### SQLite schema

Three tables in the same database file:

**device_registry** — current state, one row per device:
- `name` (PK), `device_metadata` (JSON), `instantiation_spec` (JSON), `created_at`, `updated_at`

**device_audit_log** — append-only history:
- `id` (autoincrement), `device_name`, `operation`, `timestamp`, `details` (JSON)

**registry_metadata** — tracks seeding status:
- `key` (PK), `value`

**standalone_pvs** — standalone PV registrations:
- `pv_name` (PK), `description`, `protocol`, `access_mode`, `labels` (JSON), `source`, `created_by`, `created_at`, `updated_at`

WAL mode is enabled for concurrent read/write access. Connections are thread-local.

## Loader design

Each profile format has an isolated loader class that implements the `ProfileLoader` protocol:

```python
class ProfileLoader(Protocol):
    def load_registry(self) -> DeviceRegistry: ...
```

- **EmptyProfileLoader** — starts with zero devices (populated via CRUD API by the EE service)
- **MockProfileLoader** — returns three hardcoded devices
- **HappiProfileLoader** — parses `happi_db.json`
- **BitsProfileLoader** — parses `configs/devices.yml` + `configs/iconfig.yml`

The `create_loader(settings)` factory selects the right loader based on `load_strategy`. When `auto`, it calls `detect_profile_type()` which checks for marker files in order: happi → bits.

Each loader is independent. Removing one is a single class deletion.

## Dependency injection

The app uses FastAPI's `Depends()` with closure-scoped containers:

```python
state_container: Dict[str, ConfigurationState] = {}
registry_store_container: Dict[str, DeviceRegistryStore] = {}
standalone_pv_container: Dict[str, StandalonePVStore] = {}
lock_manager_container: Dict[str, DeviceLockManager] = {}
```

These are populated during lifespan startup and accessed by dependency functions (`get_state()`, `get_registry_store()`, etc.) injected into route handlers via `Annotated[Type, Depends(...)]`.

## In-memory registry

`DeviceRegistry` is a Pydantic model holding three dictionaries:

- `devices: Dict[str, DeviceMetadata]` — the full metadata for each device
- `pvs: Dict[str, PVMetadata]` — PV index built from device PV mappings
- `instantiation_specs: Dict[str, DeviceInstantiationSpec]` — constructor info

When a device is added, its PVs are automatically indexed in the `pvs` dict. When a device is removed, its PV entries are cleaned up. This allows O(1) PV-to-device lookups.

## Route ordering

FastAPI matches routes in definition order. Several endpoint groups have both fixed paths (`/api/v1/devices/classes`) and wildcard paths (`/api/v1/devices/{device_name}`). The fixed paths must be defined first, or the wildcard swallows them. The comments in `main.py` mark these ordering constraints.

## Class capabilities

`class_capabilities.py` contains a static lookup table mapping ophyd class names to protocol capability flags (`is_movable`, `is_readable`, etc.). The happi and BITS loaders use this instead of importing ophyd — the service does not need ophyd installed.
