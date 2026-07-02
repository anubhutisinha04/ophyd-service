"""
Configuration Service (SVC-004) - FastAPI Application

Implements ProvidesDeviceRegistry protocol.
Provides REST API for device/PV registry access.

Note: Plan catalog is NOT maintained here. Plans are the responsibility
of the Experiment Execution Service (SVC-001), which is the single source
of truth for available plans. Plans cannot be serialized over HTTP since
they are Python generator functions.

Architecture:
- DB is the source of truth for devices (seeded from profile on first startup)
- Profile collections are only used to seed the DB when empty
- All CRUD changes are persisted to DB and tracked in an audit log
- On restart: DB populated → load from DB; DB empty → seed from profile
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, NamedTuple, TypeVar

import structlog
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import Engine

from .config import Settings
from .db import make_engine
from .device_registry_store import DeviceRegistryStore
from .direct_control_client import (
    DirectControlClient,
    DirectControlUnavailable,
    EnrichmentSpec,
)
from .loader import create_loader
from .lock_manager import DeviceLockManager
from .models import (
    DeviceAuditEntry,
    DeviceChangesResponse,
    DeviceCreateRequest,
    DeviceCRUDResponse,
    DeviceForceUnlockRequest,
    DeviceInstantiationSpec,
    DeviceLabel,
    DeviceLockConflict,
    DeviceLockConflictResponse,
    DeviceLockRenewRequest,
    DeviceLockRenewResponse,
    DeviceLockRequest,
    DeviceLockResponse,
    DeviceMetadata,
    DeviceRegistry,
    DeviceStatusResponse,
    DeviceUnlockRequest,
    DeviceUnlockResponse,
    DeviceUpdateRequest,
    LockPolicy,
    NestedDeviceComponent,
    PathResolveRequest,
    PathResolveResponse,
    PathResolveResultItem,
    PVHealthClearResponse,
    PVHealthRecord,
    PVHealthReport,
    PVHealthStats,
    PVMetadata,
    PVStatusResponse,
    StandalonePV,
    StandalonePVCreateRequest,
    StandalonePVCRUDResponse,
    StandalonePVUpdateRequest,
)
from .path_resolver import Outcome
from .path_resolver import resolve as resolve_path
from .protocols import ConfigurationState
from .pv_health_manager import PVHealthManager
from .standalone_pv_store import StandalonePVStore

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()


_OPENAPI_EXPORT_PATH_ENV = "OPHYD_SERVICE_OPENAPI_EXPORT_PATH"


def _maybe_export_openapi(app: FastAPI) -> None:
    """If ``_OPENAPI_EXPORT_PATH_ENV`` is set, dump the schema there.

    Used by docker-compose to publish the schema onto the shared-schema volume
    for the frontend's codegen watcher. A no-op in local dev unless the env var is set.

    Setting the env var is an explicit "I expect this to work" signal — if the
    write fails (volume unwritable, parent missing, etc.) we fail startup rather
    than let the frontend codegen silently consume a stale schema.
    """
    path = os.environ.get(_OPENAPI_EXPORT_PATH_ENV)
    if not path:
        return
    try:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
        logger.info("openapi_schema_exported", path=str(out))
    except Exception as exc:
        logger.error("openapi_schema_export_failed", path=path, error=str(exc), exc_info=True)
        raise RuntimeError(
            f"OpenAPI schema export to {path} failed: {exc}. "
            f"Unset {_OPENAPI_EXPORT_PATH_ENV} to skip export."
        ) from exc


_M = TypeVar("_M", bound=BaseModel)


def _apply_partial_update(
    existing: _M,
    update: BaseModel,
    target_cls: type[_M],
    label: str,
) -> _M:
    """Merge only the fields the caller sent onto an existing model.

    Uses ``model_dump(exclude_unset=True)`` on the update to distinguish
    "not sent" from "sent as None", then validates the merged result
    against *target_cls*.  Returns 422 on validation failure.
    """
    merged = existing.model_dump()
    merged.update(update.model_dump(exclude_unset=True))
    try:
        return target_cls.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid {label}: {exc}",
        ) from exc


def _get_device_prefix(device: "DeviceMetadata", registry: "DeviceRegistry") -> str | None:
    """Derive the EPICS PV prefix for a device.

    Checks three sources in order:
    1. Explicit ``prefix`` key in device.pvs (set by happi/BITS loaders)
    2. First arg of the instantiation spec (standard ophyd pattern)
    3. Longest common prefix computed from all PV names
    """
    # 1. Explicit prefix in PV mapping
    if "prefix" in device.pvs:
        return device.pvs["prefix"]

    # 2. Instantiation spec first arg (typically the EPICS prefix)
    spec = registry.get_instantiation_spec(device.name)
    if spec and spec.args:
        first_arg = spec.args[0]
        if isinstance(first_arg, str) and ":" in first_arg:
            return first_arg

    # 3. Compute longest common prefix from PV names
    pv_names = list(device.pvs.values())
    if not pv_names:
        return None
    if len(pv_names) == 1:
        return pv_names[0]

    prefix = pv_names[0]
    for pv in pv_names[1:]:
        while not pv.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return None
    return prefix if prefix else None


class _DeferredEnrichment(NamedTuple):
    """One row in the resolve endpoint's deferred-enrichment queue.

    ``result_idx`` is the index of the placeholder slot in ``results``;
    ``address`` is the original frontend-facing string echoed back in
    the response; ``cache_key`` is the ``(device_class, prefix, sub_path)``
    triple used both as the direct-control request and the in-process
    enrichment cache key.
    """

    result_idx: int
    address: str
    cache_key: tuple[str, str, str]


def _apply_standalone_pvs(registry, pv_store: StandalonePVStore, log) -> None:
    """
    Load saved standalone PVs into the registry.

    Adds each standalone PV to registry.pvs as PVMetadata with device_name=None.
    Called at startup and after registry reset.
    """
    pvs = pv_store.get_all_pvs()
    if not pvs:
        return

    applied = 0
    for pv in pvs:
        registry.add_standalone_pv(pv.pv_name)
        applied += 1

    log.info("standalone_pvs_applied", count=applied)


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Create FastAPI application instance.

    Args:
        settings: Optional settings override (for testing)

    Returns:
        Configured FastAPI app with dependency injection
    """
    if settings is None:
        settings = Settings()

    # Container for injected state - populated at startup
    state_container: dict[str, ConfigurationState] = {}

    # Container for device registry store (DB)
    registry_store_container: dict[str, DeviceRegistryStore] = {}

    # Container for standalone PV store
    standalone_pv_container: dict[str, StandalonePVStore] = {}

    # Container for device lock manager (in-memory, ephemeral)
    lock_manager_container: dict[str, DeviceLockManager] = {}

    # Container for the PV health manager (in-memory, ephemeral).
    # Receives caput outcome reports from direct-control and exposes the
    # state on /api/v1/pvs/{pv_name}/health + the device-status response.
    pv_health_container: dict[str, PVHealthManager] = {}

    # Container for the optional direct-control client used by the path
    # resolver's live-enrichment fallback. None if CONFIG_DIRECT_CONTROL_URL
    # isn't set — needs_enrichment outcomes then remain unenriched.
    direct_control_container: dict[str, DirectControlClient] = {}

    # In-process cache for live-enrichment results, keyed by
    # (device_class_path, prefix, sub_path). Survives across requests
    # so a warm-cache resolve never re-calls direct-control.
    enrichment_cache_container: dict[str, dict] = {}

    # The single SQLAlchemy engine shared by both persistent stores. Created in
    # the lifespan, disposed at shutdown.
    engine_container: dict[str, Engine] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Manage application lifecycle - load configuration at startup."""
        _maybe_export_openapi(app)
        logger.info(
            "configuration_service_startup",
            profile_path=str(settings.profile_path),
            load_strategy=settings.effective_strategy,
        )

        if settings.device_change_history_enabled:
            # DB-as-source-of-truth mode (PostgreSQL or SQLite).
            if not settings.database_url:
                raise RuntimeError(
                    "device_change_history_enabled is True but CONFIG_DATABASE_URL is not set. "
                    "Provide a PostgreSQL DSN (postgresql+psycopg://...) or a SQLite DSN "
                    "(sqlite+pysqlite:///...), or set "
                    "CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=false to run without persistence."
                )
            engine = make_engine(settings.database_url)
            engine_container["engine"] = engine

            store = DeviceRegistryStore(engine)
            store.initialize()
            registry_store_container["store"] = store

            if store.is_seeded():
                # Load from DB (normal restart path)
                registry = store.load_all_devices()
                logger.info(
                    "loaded_from_database",
                    devices=len(registry.devices),
                    database_url=settings.database_url,
                )
            else:
                # First startup: seed from profile collection
                loader = create_loader(settings)
                registry = loader.load_registry()
                store.seed_from_registry(registry)
                logger.info(
                    "seeded_from_profile",
                    devices=len(registry.devices),
                    strategy=settings.effective_strategy,
                )
        else:
            # Legacy mode: load from profile every time, no persistence
            loader = create_loader(settings)
            registry = loader.load_registry()
            logger.info(
                "loaded_from_profile",
                devices=len(registry.devices),
                strategy=settings.effective_strategy,
            )

        # Create state container for dependency injection
        state = ConfigurationState(registry=registry)
        state_container["state"] = state

        # Initialize standalone PV store (uses same gate as registry store).
        # Init failure must crash startup — pre-fix behavior was to log+continue,
        # leaving the service "healthy" but every /standalone-pvs/* endpoint
        # returning 501 with the misleading "Set CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=true"
        # message even though the flag was set.
        if settings.device_change_history_enabled:
            pv_store = StandalonePVStore(engine_container["engine"])
            pv_store.initialize()
            standalone_pv_container["store"] = pv_store
            _apply_standalone_pvs(registry, pv_store, logger)
            logger.info(
                "standalone_pv_store_enabled",
                database_url=settings.database_url,
            )

        # Initialize device lock manager (in-memory, ephemeral). lock_all is
        # the boot default for the availability policy; runtime-changeable
        # via PUT /api/v1/devices/lock/policy. lease_ttl bounds orphaned locks
        # (0 = disabled, historical behavior).
        lock_manager_container["manager"] = DeviceLockManager(
            lock_all=settings.lock_all,
            lease_ttl=settings.lock_lease_ttl_seconds,
        )
        logger.info(
            "device_lock_manager_initialized",
            lock_all=settings.lock_all,
            lease_ttl_seconds=settings.lock_lease_ttl_seconds,
            lock_epoch=lock_manager_container["manager"].epoch,
        )

        # Initialize PV health manager (in-memory, ephemeral).
        pv_health_container["manager"] = PVHealthManager()
        logger.info("pv_health_manager_initialized")

        # Initialize direct-control client for resolver enrichment fallback.
        # Opt-in: requires CONFIG_DIRECT_CONTROL_URL to be set.
        if settings.direct_control_url:
            direct_control_container["client"] = DirectControlClient(
                base_url=settings.direct_control_url,
                timeout=settings.direct_control_timeout,
            )
            logger.info(
                "direct_control_client_initialized",
                base_url=settings.direct_control_url,
            )

        # Empty enrichment cache. Lives for the lifetime of the process;
        # explicit invalidation isn't wired up yet (a device-class deploy
        # change would require a service restart to clear stale entries).
        enrichment_cache_container["cache"] = {}

        yield

        # Cleanup
        if "store" in standalone_pv_container:
            standalone_pv_container["store"].close()
        if "store" in registry_store_container:
            registry_store_container["store"].close()
        if "engine" in engine_container:
            engine_container["engine"].dispose()
        if "client" in direct_control_container:
            await direct_control_container["client"].aclose()
        logger.info("configuration_service_shutdown")
        state_container.clear()
        registry_store_container.clear()
        standalone_pv_container.clear()
        engine_container.clear()
        lock_manager_container.clear()
        pv_health_container.clear()
        direct_control_container.clear()
        enrichment_cache_container.clear()

    openapi_tags = [
        {"name": "Health", "description": "Service health and readiness checks"},
        {"name": "Device Registry", "description": "Query registered devices and their metadata"},
        {
            "name": "Device Instantiation",
            "description": "Device instantiation specifications for remote creation",
        },
        {
            "name": "Device Management",
            "description": "Runtime device CRUD operations (add/update/delete)",
        },
        {"name": "Registry Admin", "description": "Administrative operations (reset, export)"},
        {
            "name": "Standalone PVs",
            "description": "Register and manage standalone PVs not tied to ophyd devices",
        },
        {"name": "PV Registry", "description": "Query registered PVs from loaded devices"},
        {"name": "Device Components", "description": "Nested device component lookup and listing"},
        {
            "name": "Device Locking",
            "description": "Device lock management for experiment coordination (A4)",
        },
    ]

    app = FastAPI(
        title="Configuration Service",
        description="Device/PV registry for Bluesky Remote Architecture (SVC-004). Plans are managed by Experiment Execution Service.",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=openapi_tags,
        lifespan=lifespan,
    )

    # Add CORS middleware to allow UI access. Never combine a wildcard origin
    # with credentials: Starlette would then reflect the request Origin and set
    # Access-Control-Allow-Credentials, letting any site issue credentialed
    # cross-origin calls. Auth is enforced upstream via bearer headers (not
    # cookies), so credentials stay off unless an explicit origin allowlist is
    # configured.
    allow_credentials = settings.cors_allow_credentials
    if allow_credentials and "*" in settings.cors_origins:
        logger.warning(
            "cors_credentials_disabled_with_wildcard_origin",
            detail="cors_allow_credentials=True is ignored while cors_origins contains '*'; "
            "set an explicit origin allowlist to enable credentials",
        )
        allow_credentials = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Expose DI containers on app.state so tests can introspect / mutate
    # them (registry mutation for S3, store swap for S5). Production code
    # goes through Depends().
    app.state.state_container = state_container
    app.state.registry_store_container = registry_store_container
    app.state.direct_control_container = direct_control_container
    app.state.enrichment_cache_container = enrichment_cache_container

    # Dependency injection function
    def get_state() -> ConfigurationState:
        """Get configuration state for dependency injection."""
        if "state" not in state_container:
            raise HTTPException(status_code=503, detail="Configuration not loaded")
        return state_container["state"]

    # Type alias for dependency injection
    StateDep = Annotated[ConfigurationState, Depends(get_state)]

    # Registry store dependency
    def get_registry_store() -> DeviceRegistryStore:
        """Get device registry store for dependency injection."""
        if "store" not in registry_store_container:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Device registry persistence not enabled. Set CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=true.",
            )
        return registry_store_container["store"]

    RegistryStoreDep = Annotated[DeviceRegistryStore, Depends(get_registry_store)]

    # Standalone PV store dependency
    def get_standalone_pv_store() -> StandalonePVStore:
        """Get standalone PV store for dependency injection."""
        if "store" not in standalone_pv_container:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Standalone PV registration not enabled. Set CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=true.",
            )
        return standalone_pv_container["store"]

    StandalonePVStoreDep = Annotated[StandalonePVStore, Depends(get_standalone_pv_store)]

    # Lock manager dependency
    def get_lock_manager() -> DeviceLockManager:
        """Get device lock manager for dependency injection."""
        if "manager" not in lock_manager_container:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Device lock manager not initialized",
            )
        return lock_manager_container["manager"]

    LockManagerDep = Annotated[DeviceLockManager, Depends(get_lock_manager)]

    # PV health manager dependency
    def get_pv_health_manager() -> PVHealthManager:
        """Get the PV health manager for dependency injection."""
        if "manager" not in pv_health_container:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PV health manager not initialized",
            )
        return pv_health_container["manager"]

    PVHealthDep = Annotated[PVHealthManager, Depends(get_pv_health_manager)]

    # ===== Health Endpoints =====

    @app.get("/health", tags=["Health"])
    async def health_check(state: StateDep):
        """Health check: state loaded AND registry DB queryable.

        Runs ``SELECT 1`` against the registry store (when DB mode is on)
        and returns 503 with the failure detail when it can't. Without
        the DB ping, a mount-gone or permissions-revoked store would
        still pass health while every CRUD call 500'd.
        """
        store = registry_store_container.get("store")
        if store is not None:
            try:
                await asyncio.to_thread(store.ping)
            except Exception as exc:
                logger.error("health_db_ping_failed", error=str(exc))
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "unhealthy",
                        "service": "configuration_service",
                        "detail": f"registry store unreachable: {exc}",
                    },
                )
        return {
            "status": "healthy",
            "service": "configuration_service",
            "devices_loaded": len(state.registry.devices),
        }

    @app.get("/ready", tags=["Health"])
    async def readiness_check():
        """Readiness check endpoint."""
        if "state" not in state_container:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "reason": "registry not loaded"}
            )
        return {"status": "ready"}

    # ===== Device Endpoints =====

    @app.get(
        "/api/v1/devices",
        response_model=list[str],
        summary="List Devices",
        description="Query available devices from registry",
        tags=["Device Registry"],
    )
    async def list_devices(
        state: StateDep,
        device_label: DeviceLabel | None = Query(None, description="Filter by device type"),
        pattern: str | None = Query(None, description="Glob pattern for name matching"),
        ophyd_class: str | None = Query(None, description="Filter by ophyd device class name"),
        readable: bool | None = Query(None, description="Filter by the Readable protocol flag"),
        movable: bool | None = Query(None, description="Filter by the Movable protocol flag"),
        flyable: bool | None = Query(None, description="Filter by the Flyable protocol flag"),
    ) -> list[str]:
        """
        List available devices.

        Implements interface: "List Devices" from service_architecture.json
        Protocol: ProvidesDeviceRegistry.list_devices()
        """
        logger.info(
            "list_devices",
            device_label=device_label,
            pattern=pattern,
            ophyd_class=ophyd_class,
            readable=readable,
            movable=movable,
            flyable=flyable,
        )
        return state.registry.list_devices(
            device_label=device_label,
            pattern=pattern,
            ophyd_class=ophyd_class,
            readable=readable,
            movable=movable,
            flyable=flyable,
        )

    @app.get(
        "/api/v1/devices-info",
        response_model=dict[str, DeviceMetadata],
        summary="Get All Devices Info",
        description="Get detailed metadata for all devices (ophyd-websocket compatible)",
        tags=["Device Registry"],
    )
    async def get_all_devices_info(state: StateDep) -> dict[str, DeviceMetadata]:
        """
        Get metadata for all registered devices.

        Implements interface: ophyd-websocket /devices-info endpoint
        Returns a dictionary mapping device names to their full metadata.
        """
        logger.info("get_all_devices_info")
        return dict(state.registry.devices)

    @app.get(
        "/api/v1/devices/classes",
        response_model=list[str],
        summary="List Device Classes",
        description="Get list of unique ophyd device classes (as-ophyd-api compatible)",
        tags=["Device Registry"],
    )
    async def get_device_classes(state: StateDep) -> list[str]:
        """
        Get list of unique device classes.

        Implements interface: as-ophyd-api /devices/classes endpoint
        Returns sorted list of unique ophyd_class values from all devices.
        """
        logger.info("get_device_classes")
        classes = sorted(
            {device.ophyd_class for device in state.registry.devices.values() if device.ophyd_class}
        )
        return classes

    @app.get(
        "/api/v1/devices/types",
        response_model=list[str],
        summary="List Device Types",
        description="Get list of device type categories",
        tags=["Device Registry"],
    )
    async def get_device_labels(state: StateDep) -> list[str]:
        """
        Get list of device type categories.

        Returns sorted list of unique device_label values (motor, detector, etc.).
        """
        logger.info("get_device_labels")
        types = sorted({device.device_label.value for device in state.registry.devices.values()})
        return types

    # ===== Device Instantiation Endpoints =====
    # NOTE: These must be defined BEFORE /api/v1/devices/{device_name}
    # to avoid the wildcard matching "instantiation" as a device name

    @app.get(
        "/api/v1/devices/instantiation",
        response_model=dict[str, DeviceInstantiationSpec],
        summary="List Device Instantiation Specs",
        description="Get all device instantiation specifications for remote device creation",
        tags=["Device Instantiation"],
    )
    async def list_device_instantiations(
        state: StateDep,
        active_only: bool = Query(True, description="Only return active devices"),
    ) -> dict[str, DeviceInstantiationSpec]:
        """
        Get all device instantiation specifications.

        Returns specifications needed to recreate devices in other services
        (e.g., Experiment Execution Service). This ensures PV names and
        configurations are consistent across all services.

        The instantiation spec includes:
        - device_class: Fully qualified class path
        - args: Positional constructor arguments
        - kwargs: Keyword constructor arguments
        """
        logger.info("list_device_instantiations", active_only=active_only)
        return state.registry.list_instantiation_specs(active_only=active_only)

    @app.get(
        "/api/v1/devices/history",
        response_model=list[DeviceAuditEntry],
        summary="Device Audit Log",
        description="List device change history (audit log of all mutations)",
        tags=["Device Management"],
    )
    async def list_device_history(
        registry_store: RegistryStoreDep,
        device_name: str | None = Query(None, description="Filter to a specific device"),
        limit: int = Query(1000, ge=1, le=10000, description="Max entries to return"),
    ) -> list[DeviceAuditEntry]:
        """
        Get the device audit log.

        Returns append-only history of all device mutations (seed, add,
        update, delete, reset). Use device_name to filter to a specific device.
        """
        # PostgreSQL text fields cannot contain NUL — without this guard a
        # \x00 in the filter reaches the driver and 500s (backend-dependent:
        # SQLite tolerates it). Reject loudly instead.
        if device_name is not None and "\x00" in device_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="device_name must not contain NUL (0x00) characters",
            )
        return await asyncio.to_thread(
            registry_store.get_audit_log, device_name=device_name, limit=limit
        )

    @app.get(
        "/api/v1/devices/changes",
        response_model=DeviceChangesResponse,
        summary="Device Registry Delta",
        description=(
            "Return device-state changes after a given audit cursor. Intended "
            "for clients (like bluesky-queueserver) that cache device state "
            "and need to keep it fresh without re-fetching the full registry. "
            "Each device is reported at most once with its current state. If "
            "reset_occurred is true, the caller should discard local state "
            "and re-fetch /devices-info; likewise if service_epoch differs "
            "from a previously-observed value."
        ),
        tags=["Device Management"],
    )
    async def get_device_changes(
        registry_store: RegistryStoreDep,
        since_version: int = Query(
            0, ge=0, description="Return changes with audit id greater than this value"
        ),
    ) -> DeviceChangesResponse:
        result = await asyncio.to_thread(
            registry_store.get_changes_since, since_version=since_version
        )
        return DeviceChangesResponse(**result)

    # ===== Device Locking Endpoints =====
    # NOTE: These must be defined BEFORE /api/v1/devices/{device_name}
    # to avoid the wildcard matching "lock" / "unlock" / "force-unlock" as device names.

    @app.post(
        "/api/v1/devices/lock",
        response_model=DeviceLockResponse,
        responses={
            409: {
                "model": DeviceLockConflictResponse,
                "description": "One or more devices already locked",
            },
            404: {
                "model": DeviceLockConflictResponse,
                "description": "One or more devices not found",
            },
            422: {
                "model": DeviceLockConflictResponse,
                "description": "One or more devices are disabled",
            },
        },
        summary="Lock Devices (Bulk Atomic)",
        tags=["Device Locking"],
    )
    async def lock_devices(
        request: DeviceLockRequest,
        state: StateDep,
        lock_manager: LockManagerDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceLockResponse:
        """
        Acquire locks on multiple devices atomically (all-or-nothing).

        When a device is locked, all PVs belonging to that device are implicitly
        locked. Direct Control checks PV/device status before every write operation
        and will refuse to command locked PVs.

        Locks are held for the duration of a Bluesky plan (minutes to hours).
        EE sends unlock when the plan completes, fails, or is aborted.
        """
        result = await lock_manager.acquire_locks(
            device_names=request.device_names,
            item_id=request.item_id,
            plan_name=request.plan_name,
            locked_by_service=request.locked_by_service,
            registry=state.registry,
        )

        if not result.success:
            # Build conflict response
            conflicts = [
                DeviceLockConflict(
                    device_name=c.device_name,
                    reason=c.reason,
                    locked_by_plan=c.locked_by_plan,
                    locked_at=c.locked_at.isoformat() if c.locked_at else None,
                )
                for c in result.conflicts
            ]
            first = result.conflicts[0]
            if first.reason == "already_locked":
                message = f"Device '{first.device_name}' is locked by plan '{first.locked_by_plan}'"
            elif first.reason == "not_found":
                message = f"Device not found: {first.device_name}"
            elif first.reason == "spec_missing":
                message = (
                    f"Registry inconsistency: device '{first.device_name}' "
                    f"has no instantiation spec"
                )
            else:
                message = f"Device '{first.device_name}' is disabled"

            return JSONResponse(
                status_code=result.error_code,
                content=DeviceLockConflictResponse(
                    success=False,
                    message=message,
                    conflicting_devices=conflicts,
                ).model_dump(),
            )

        # Write audit log
        await asyncio.to_thread(
            registry_store.log_lock_event,
            device_names=result.locked_devices,
            operation="lock",
            details=json.dumps(
                {
                    "plan": request.plan_name,
                    "item_id": request.item_id,
                    "service": request.locked_by_service,
                    "lock_id": result.lock_id,
                }
            ),
        )

        logger.info(
            "devices_locked",
            devices=result.locked_devices,
            plan=request.plan_name,
            item_id=request.item_id,
        )

        return DeviceLockResponse(
            success=True,
            locked_devices=result.locked_devices,
            locked_pvs=result.locked_pvs,
            lock_id=result.lock_id,
            registry_version=lock_manager.version,
            lock_epoch=lock_manager.epoch,
            expires_at=result.expires_at.isoformat() if result.expires_at else None,
            lease_ttl_seconds=lock_manager.lease_ttl,
        )

    @app.post(
        "/api/v1/devices/unlock",
        response_model=DeviceUnlockResponse,
        summary="Unlock Devices",
        tags=["Device Locking"],
    )
    async def unlock_devices(
        request: DeviceUnlockRequest,
        state: StateDep,
        lock_manager: LockManagerDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceUnlockResponse:
        """
        Release locks on devices. Only the item_id that acquired the lock can
        release it. Use force-unlock for admin override.
        """
        success, unlocked, error_msg = await lock_manager.release_locks(
            device_names=request.device_names,
            item_id=request.item_id,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_msg,
            )

        if unlocked:
            await asyncio.to_thread(
                registry_store.log_lock_event,
                device_names=unlocked,
                operation="unlock",
                details=json.dumps(
                    {
                        "item_id": request.item_id,
                        "reason": "plan_completed",
                    }
                ),
            )

        logger.info("devices_unlocked", devices=unlocked, item_id=request.item_id)

        return DeviceUnlockResponse(
            success=True,
            unlocked_devices=unlocked,
            registry_version=lock_manager.version,
            lock_epoch=lock_manager.epoch,
        )

    @app.post(
        "/api/v1/devices/lock/renew",
        response_model=DeviceLockRenewResponse,
        summary="Renew Device Locks (Heartbeat)",
        tags=["Device Locking"],
    )
    async def renew_device_locks(
        request: DeviceLockRenewRequest,
        lock_manager: LockManagerDep,
    ) -> DeviceLockRenewResponse:
        """Extend the lease on locks held by ``item_id`` (heartbeat).

        Called periodically by the lock holder while it still needs the
        devices. Only meaningful when leases are enabled
        (CONFIG_LOCK_LEASE_TTL_SECONDS > 0); with leases disabled every held
        lock is renewed as a no-op. ``lost_devices`` tells the holder which
        locks it must re-acquire (expired, released, or dropped by a restart),
        and ``lock_epoch`` confirms whether the authority itself reset.

        This route is registered before the ``{device_name}`` wildcard so
        ``renew`` is never matched as a device name.
        """
        result = await lock_manager.renew_locks(
            device_names=request.device_names,
            item_id=request.item_id,
        )
        return DeviceLockRenewResponse(
            success=result.success,
            renewed_devices=result.renewed,
            lost_devices=result.lost,
            conflict_devices=result.conflicts,
            lock_epoch=lock_manager.epoch,
            expires_at=result.expires_at.isoformat() if result.expires_at else None,
        )

    @app.post(
        "/api/v1/devices/force-unlock",
        response_model=DeviceUnlockResponse,
        summary="Force Unlock Devices (Admin)",
        tags=["Device Locking"],
    )
    async def force_unlock_devices(
        request: DeviceForceUnlockRequest,
        state: StateDep,
        lock_manager: LockManagerDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceUnlockResponse:
        """
        Emergency endpoint to clear stale locks regardless of ownership.

        Use when EE crashes mid-plan and locks are orphaned. Requires ADMIN
        role when AuthZ middleware is enabled.
        """
        unlocked, not_found = await lock_manager.force_unlock(
            device_names=request.device_names,
            registry=state.registry,
        )

        if not_found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Devices not found: {', '.join(not_found)}",
            )

        if unlocked:
            await asyncio.to_thread(
                registry_store.log_lock_event,
                device_names=unlocked,
                operation="force_unlock",
                details=json.dumps(
                    {
                        "reason": request.reason,
                        "admin": True,
                    }
                ),
            )

        logger.info("devices_force_unlocked", devices=unlocked, reason=request.reason)

        return DeviceUnlockResponse(
            success=True,
            unlocked_devices=unlocked,
            registry_version=lock_manager.version,
            lock_epoch=lock_manager.epoch,
        )

    # ===== Lock Policy =====
    # Like the lock routes above, must precede the {device_name} wildcard.

    @app.get(
        "/api/v1/devices/lock/policy",
        response_model=LockPolicy,
        summary="Get Lock Policy",
        tags=["Device Locking"],
    )
    async def get_lock_policy(lock_manager: LockManagerDep) -> LockPolicy:
        """Current lock_all availability policy (boot default: CONFIG_LOCK_ALL)."""
        return LockPolicy(lock_all=lock_manager.lock_all_enabled)

    @app.put(
        "/api/v1/devices/lock/policy",
        response_model=LockPolicy,
        summary="Set Lock Policy",
        tags=["Device Locking"],
    )
    async def set_lock_policy(policy: LockPolicy, lock_manager: LockManagerDep) -> LockPolicy:
        """
        Set the lock_all availability policy at runtime.

        Takes effect immediately for every subsequent availability read
        (device status, PV status). In-memory like the locks themselves —
        a restart returns to the CONFIG_LOCK_ALL boot default.
        """
        lock_manager.set_lock_all(policy.lock_all)
        logger.info("lock_policy_set", lock_all=policy.lock_all)
        return LockPolicy(lock_all=lock_manager.lock_all_enabled)

    # ===== Device Status Endpoint =====
    # Must be defined before the {device_name} wildcard GET route.

    @app.get(
        "/api/v1/devices/{device_name}/status",
        response_model=DeviceStatusResponse,
        summary="Get Device Availability",
        tags=["Device Locking"],
    )
    async def get_device_status(
        device_name: str,
        state: StateDep,
        lock_manager: LockManagerDep,
        pv_health: PVHealthDep,
    ) -> DeviceStatusResponse:
        """
        Combined availability check: lock state + enabled/disabled + PV health.

        A device is available only when it is both enabled and unlocked.
        ``pv_health`` is a dict keyed by PV name; only PVs with reported
        caput outcomes appear, so an empty dict means "no failures
        observed yet for any of this device's PVs".
        """
        device = state.registry.get_device(device_name)
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device not found: {device_name}",
            )

        spec = state.registry.get_instantiation_spec(device_name)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Registry inconsistency: device '{device_name}' has no instantiation spec",
            )
        enabled = spec.active
        lock_state = lock_manager.effective_lock(device_name)
        locked = lock_state is not None

        # Roll up health for this device's PVs. ``device.pvs`` maps
        # component-name → PV-name; we want the PV-name values.
        device_pv_names = list(device.pvs.values())
        pv_health_rollup = await pv_health.get_health_many(device_pv_names)

        return DeviceStatusResponse(
            device_name=device_name,
            available=enabled and not locked,
            enabled=enabled,
            lock_status="locked" if locked else "unlocked",
            locked_by_plan=lock_state.locked_by_plan if lock_state else None,
            locked_by_item=lock_state.locked_by_item if lock_state else None,
            locked_at=lock_state.locked_at.isoformat() if lock_state else None,
            locked_until=(
                lock_state.expires_at.isoformat() if lock_state and lock_state.expires_at else None
            ),
            lock_epoch=lock_manager.epoch,
            pv_health=pv_health_rollup,
        )

    # ===== PV Health Endpoints =====
    #
    # These are the receiving side of direct-control's caput-outcome
    # reports. The /failure and /success endpoints are intended to be
    # called by direct-control fire-and-forget; the /health GET is for
    # ad-hoc operator lookups (the device-status endpoint above provides
    # the device-rollup view the frontend periodic table needs).

    def _ensure_pv_registered(state: ConfigurationState, pv_name: str) -> None:
        """Reject health reports / lookups for PVs not in the registry.

        Without this gate, any caller could POST to /failure with arbitrary
        strings and grow PVHealthManager's records dict unbounded with
        garbage entries. Direct-control already validates pv_name against
        the registry before caputting (RegistryClient → /api/v1/pvs/lookup),
        so this matches the existing trust model: only registered PVs
        flow through the caput→report pipeline.
        """
        if state.registry.get_pv(pv_name) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PV not registered: {pv_name}",
            )

    @app.post(
        "/api/v1/pvs/{pv_name:path}/failure",
        response_model=PVHealthRecord,
        summary="Report Failed Caput",
        description=(
            "Direct-control calls this after a caput fails (EPICS timeout, "
            "put-rejection, IOC unreachable, etc.). Increments the PV's "
            "consecutive-failure counter and updates last_failure_at. The "
            "``message`` body field carries the diagnostic the operator UI "
            "shows next to the unhealthy PV. Returns 404 if ``pv_name`` is "
            "not registered."
        ),
        tags=["PV Health"],
    )
    async def report_pv_failure(
        pv_name: str,
        report: PVHealthReport,
        state: StateDep,
        pv_health: PVHealthDep,
    ) -> PVHealthRecord:
        _ensure_pv_registered(state, pv_name)
        return await pv_health.record_failure(pv_name, report.message)

    @app.post(
        "/api/v1/pvs/{pv_name:path}/success",
        response_model=PVHealthRecord,
        summary="Report Successful Caput",
        description=(
            "Direct-control calls this after a caput succeeds. If a record "
            "exists for the PV (i.e. it had previously failed), resets the "
            "consecutive-failure counter to zero and flips the state back "
            "to ``healthy`` — a recent success is stronger evidence than "
            "older failures. For PVs that have never failed since service "
            "start, the response carries a synthetic healthy record but "
            "no record is persisted (keeps the health store bounded by the "
            "PVs that have actually failed, not every PV ever caput'd). "
            "Returns 404 if ``pv_name`` is not registered."
        ),
        tags=["PV Health"],
    )
    async def report_pv_success(
        pv_name: str,
        report: PVHealthReport,
        state: StateDep,
        pv_health: PVHealthDep,
    ) -> PVHealthRecord:
        # ``report.message`` is allowed but ignored for success reports —
        # the schema is symmetric with /failure so direct-control can
        # POST the same request shape to either endpoint.
        _ensure_pv_registered(state, pv_name)
        return await pv_health.record_success(pv_name)

    @app.get(
        "/api/v1/pvs/{pv_name:path}/health",
        response_model=PVHealthRecord,
        summary="Get PV Health",
        description=(
            "Returns the current health record for ``pv_name``. 404 if no "
            "failures have been recorded for this PV (i.e. either it has "
            "only succeeded since service start, or never been written to "
            "at all). Successes on never-failed PVs are intentionally not "
            "persisted, so a 404 here does not mean 'never touched' — "
            "frontends should treat it as 'no failures observed, assume "
            "healthy'."
        ),
        tags=["PV Health"],
    )
    async def get_pv_health(
        pv_name: str,
        pv_health: PVHealthDep,
    ) -> PVHealthRecord:
        record = await pv_health.get_health(pv_name)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No health record for PV '{pv_name}'. No failures recorded; treat as healthy."
                ),
            )
        return record

    @app.delete(
        "/api/v1/pvs/{pv_name:path}/health",
        response_model=PVHealthClearResponse,
        summary="Clear PV Health Record",
        description=(
            "Idempotently drops the in-memory health record for "
            "``pv_name``. Useful when an operator wants to ack a PV "
            "that's been marked ``unresponsive`` by past failures so "
            "the UI no longer shows the warning. Returns "
            "``cleared: 1`` if a record was actually removed, "
            "``cleared: 0`` if no record existed (still 200, idempotent)."
        ),
        tags=["PV Health"],
    )
    async def clear_pv_health(
        pv_name: str,
        pv_health: PVHealthDep,
    ) -> PVHealthClearResponse:
        removed = await pv_health.clear(pv_name)
        return PVHealthClearResponse(cleared=1 if removed else 0)

    @app.delete(
        "/api/v1/admin/pv-health",
        response_model=PVHealthClearResponse,
        summary="Clear All PV Health Records",
        description=(
            "Wipes every in-memory health record. Returns the count of "
            "records removed. Intended for ops use (e.g. after IOC "
            "maintenance) — there's no per-PV gate, so call deliberately."
        ),
        tags=["PV Health"],
    )
    async def clear_all_pv_health(
        pv_health: PVHealthDep,
    ) -> PVHealthClearResponse:
        return PVHealthClearResponse(cleared=await pv_health.clear_all())

    @app.get(
        "/api/v1/admin/pv-health/stats",
        response_model=PVHealthStats,
        summary="Get PV Health Aggregate Stats",
        description=(
            "Returns the total tracked PV count plus a per-state count "
            "(healthy / degraded / unresponsive). Every state appears as "
            "a key in ``by_state`` even if its count is zero, so callers "
            "never have to special-case missing keys."
        ),
        tags=["PV Health"],
    )
    async def get_pv_health_stats(
        pv_health: PVHealthDep,
    ) -> PVHealthStats:
        # ``tracked_pvs`` is a computed_field on the model — Pydantic
        # derives it from ``by_state`` so the two can never drift.
        return PVHealthStats(by_state=await pv_health.stats())

    # ===== Device Enable/Disable Endpoints =====
    # These must be defined before the {device_name} wildcard routes.

    @app.patch(
        "/api/v1/devices/{device_name}/enable",
        response_model=DeviceCRUDResponse,
        summary="Enable Device",
        description="Enable a device so it will be instantiated by remote services",
        tags=["Device Management"],
    )
    async def enable_device(
        device_name: str,
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Enable a device in the registry.

        Sets the device's instantiation spec `active` flag to True.
        Enabled devices will be included when remote services (e.g.,
        Experiment Execution) pull the device list.
        """
        existing = state.registry.get_device(device_name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device not found: {device_name}",
            )

        spec = state.registry.get_instantiation_spec(device_name)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No instantiation spec for device: {device_name}",
            )

        if spec.active:
            return DeviceCRUDResponse(
                success=True,
                device_name=device_name,
                operation="enable",
                message=f"Device '{device_name}' is already enabled",
            )

        spec.active = True
        state.registry.update_device(existing, spec)
        await asyncio.to_thread(
            registry_store.save_device,
            name=device_name,
            metadata=existing,
            spec=spec,
            operation="enable",
            details={"field": "active", "old": False, "new": True},
        )

        logger.info("device_enabled", device_name=device_name)

        return DeviceCRUDResponse(
            success=True,
            device_name=device_name,
            operation="enable",
            message=f"Device '{device_name}' enabled",
        )

    @app.patch(
        "/api/v1/devices/{device_name}/disable",
        response_model=DeviceCRUDResponse,
        summary="Disable Device",
        description="Disable a device so it will not be instantiated by remote services",
        tags=["Device Management"],
    )
    async def disable_device(
        device_name: str,
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Disable a device in the registry.

        Sets the device's instantiation spec `active` flag to False.
        Disabled devices remain in the registry but are excluded when
        remote services pull the active device list.
        """
        existing = state.registry.get_device(device_name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device not found: {device_name}",
            )

        spec = state.registry.get_instantiation_spec(device_name)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No instantiation spec for device: {device_name}",
            )

        if not spec.active:
            return DeviceCRUDResponse(
                success=True,
                device_name=device_name,
                operation="disable",
                message=f"Device '{device_name}' is already disabled",
            )

        spec.active = False
        state.registry.update_device(existing, spec)
        await asyncio.to_thread(
            registry_store.save_device,
            name=device_name,
            metadata=existing,
            spec=spec,
            operation="disable",
            details={"field": "active", "old": True, "new": False},
        )

        logger.info("device_disabled", device_name=device_name)

        return DeviceCRUDResponse(
            success=True,
            device_name=device_name,
            operation="disable",
            message=f"Device '{device_name}' disabled",
        )

    @app.get(
        "/api/v1/devices/{device_name}",
        response_model=DeviceMetadata,
        summary="Get Device Metadata",
        description="Get detailed metadata for specific device including PV mappings",
        tags=["Device Registry"],
    )
    async def get_device(state: StateDep, device_name: str) -> DeviceMetadata:
        """
        Get device metadata.

        Implements interface: "Get Device Metadata" from service_architecture.json
        Protocol: ProvidesDeviceRegistry.get_device()
        """
        logger.info("get_device", device_name=device_name)
        device = state.registry.get_device(device_name)

        if device is None:
            logger.warning("device_not_found", device_name=device_name)
            raise HTTPException(status_code=404, detail=f"Device not found: {device_name}")

        return device

    @app.get(
        "/api/v1/devices/{device_name}/pvs",
        summary="Get Device PVs",
        description="Get all PVs owned by a device, mapped by component name",
        tags=["PV Registry"],
    )
    async def get_device_pvs(state: StateDep, device_name: str) -> dict:
        """
        Get PVs associated with a device.

        Returns the component-name → PV-name mapping for the device,
        plus ownership metadata for each PV from the PV index.
        """
        device = state.registry.get_device(device_name)
        if device is None:
            raise HTTPException(status_code=404, detail=f"Device not found: {device_name}")

        pv_details = {}
        for component_name, pv_name in device.pvs.items():
            pv_meta = state.registry.get_pv(pv_name)
            pv_details[component_name] = {
                "pv_name": pv_name,
                "connected": pv_meta.connected if pv_meta else None,
                "dtype": pv_meta.dtype if pv_meta else None,
            }

        return {
            "device_name": device_name,
            "device_label": device.device_label,
            "pvs": pv_details,
            "count": len(pv_details),
        }

    @app.get(
        "/api/v1/devices/{device_name}/instantiation",
        response_model=DeviceInstantiationSpec,
        summary="Get Device Instantiation Spec",
        description="Get instantiation specification for a specific device",
        tags=["Device Instantiation"],
    )
    async def get_device_instantiation(
        state: StateDep, device_name: str
    ) -> DeviceInstantiationSpec:
        """
        Get device instantiation specification.

        Returns the specification needed to recreate this device in another
        service. If the device exists but has no instantiation spec, returns 404.
        """
        logger.info("get_device_instantiation", device_name=device_name)

        # First check if device exists
        device = state.registry.get_device(device_name)
        if device is None:
            logger.warning("device_not_found", device_name=device_name)
            raise HTTPException(status_code=404, detail=f"Device not found: {device_name}")

        # Get instantiation spec
        spec = state.registry.get_instantiation_spec(device_name)
        if spec is None:
            logger.warning("instantiation_spec_not_found", device_name=device_name)
            raise HTTPException(
                status_code=404, detail=f"Instantiation spec not found for device: {device_name}"
            )

        return spec

    # ===== Device CRUD Endpoints =====

    @app.post(
        "/api/v1/devices",
        response_model=DeviceCRUDResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create Runtime Device",
        description="Add a new device to the registry at runtime",
        tags=["Device Management"],
    )
    async def create_device(
        request: DeviceCreateRequest,
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Create a new runtime device.

        Adds the device to the in-memory registry and persists to DB.
        """
        device_name = request.metadata.name

        # Validate name consistency
        if request.metadata.name != request.instantiation_spec.name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Name mismatch: metadata.name='{request.metadata.name}' != instantiation_spec.name='{request.instantiation_spec.name}'",
            )

        # Check for conflict
        if state.registry.get_device(device_name) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Device already exists: {device_name}",
            )

        # Persist FIRST, then mutate memory: if the DB write fails the
        # request 500s with the registry unchanged, so the client's retry
        # succeeds instead of 409ing on a phantom device that would vanish
        # at the next restart.
        await asyncio.to_thread(
            registry_store.save_device,
            name=device_name,
            metadata=request.metadata,
            spec=request.instantiation_spec,
            operation="add",
            details={
                "device_label": request.metadata.device_label,
                "ophyd_class": request.metadata.ophyd_class,
            },
        )
        state.registry.add_device(request.metadata, request.instantiation_spec)

        logger.info("device_created", device_name=device_name)

        return DeviceCRUDResponse(
            success=True,
            device_name=device_name,
            operation="create",
            message=f"Device '{device_name}' created successfully",
        )

    @app.put(
        "/api/v1/devices/{device_name}",
        response_model=DeviceCRUDResponse,
        summary="Update Device",
        description="Update an existing device's metadata and/or instantiation spec",
        tags=["Device Management"],
    )
    async def update_device(
        device_name: str,
        request: DeviceUpdateRequest,
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Update a device's metadata and/or instantiation spec.

        Supports field-level partial updates: only the fields included
        in the request body are changed.  Omitted fields keep their
        current values.
        """
        # Check device exists
        existing_metadata = state.registry.get_device(device_name)
        if existing_metadata is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device not found: {device_name}",
            )

        # Validate name in body matches path param (if provided)
        if request.metadata and request.metadata.name is not None:
            if request.metadata.name != device_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Name in body '{request.metadata.name}' does not match path '{device_name}'",
                )
        if request.instantiation_spec and request.instantiation_spec.name is not None:
            if request.instantiation_spec.name != device_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Spec name '{request.instantiation_spec.name}' does not match path '{device_name}'",
                )

        # Field-level merge: overlay only the fields the caller sent
        merged_metadata = (
            _apply_partial_update(
                existing_metadata, request.metadata, DeviceMetadata, "metadata update"
            )
            if request.metadata
            else existing_metadata
        )

        existing_spec = state.registry.get_instantiation_spec(device_name)
        if request.instantiation_spec:
            if existing_spec:
                merged_spec = _apply_partial_update(
                    existing_spec,
                    request.instantiation_spec,
                    DeviceInstantiationSpec,
                    "instantiation spec update",
                )
            else:
                # No existing spec — treat as creation from the partial fields
                try:
                    merged_spec = DeviceInstantiationSpec.model_validate(
                        request.instantiation_spec.model_dump(exclude_unset=True)
                    )
                except ValidationError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid instantiation spec: {exc}",
                    ) from exc
        else:
            merged_spec = existing_spec

        # Track what changed for audit details
        changed_fields = []
        if request.metadata:
            changed_fields.extend(list(request.metadata.model_dump(exclude_unset=True).keys()))
        if request.instantiation_spec:
            changed_fields.extend(
                [
                    f"spec.{k}"
                    for k in request.instantiation_spec.model_dump(exclude_unset=True).keys()
                ]
            )

        # Persist FIRST, then mutate memory (see create_device).
        await asyncio.to_thread(
            registry_store.save_device,
            name=device_name,
            metadata=merged_metadata,
            spec=merged_spec,
            operation="update",
            details={"changed_fields": changed_fields} if changed_fields else None,
        )
        state.registry.update_device(merged_metadata, merged_spec)

        logger.info("device_updated", device_name=device_name)

        return DeviceCRUDResponse(
            success=True,
            device_name=device_name,
            operation="update",
            message=f"Device '{device_name}' updated successfully",
        )

    @app.delete(
        "/api/v1/devices/{device_name}",
        response_model=DeviceCRUDResponse,
        summary="Delete Device",
        description="Remove a device from the registry",
        tags=["Device Management"],
    )
    async def delete_device(
        device_name: str,
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Delete a device from the registry.

        Removes the device from both the in-memory registry and the DB.
        The deletion is recorded in the audit log.
        """
        # Check device exists
        existing_device = state.registry.get_device(device_name)
        if existing_device is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device not found: {device_name}",
            )

        # Persist FIRST, then mutate memory (see create_device): a failed
        # DB delete leaves the device fully present instead of a memory/DB
        # split that resurrects it at the next restart.
        await asyncio.to_thread(
            registry_store.delete_device,
            device_name,
            details={
                "ophyd_class": existing_device.ophyd_class,
                "device_label": existing_device.device_label,
            },
        )
        state.registry.remove_device(device_name)

        logger.info("device_deleted", device_name=device_name)

        return DeviceCRUDResponse(
            success=True,
            device_name=device_name,
            operation="delete",
            message=f"Device '{device_name}' deleted successfully",
        )

    # ===== Registry Admin Endpoints =====

    @app.post(
        "/api/v1/registry/reset",
        response_model=DeviceCRUDResponse,
        summary="Reset Registry",
        description="Wipe the device DB and re-seed from profile collection",
        tags=["Registry Admin"],
    )
    async def reset_registry(
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Reset the device registry.

        Erases all devices from the DB and re-seeds from the profile
        collection. Standalone PVs are re-applied on top. The reset
        is recorded in the audit log.
        """
        # Re-load from profile
        loader = create_loader(settings)
        registry = loader.load_registry()

        # Wipe DB and re-seed
        await asyncio.to_thread(registry_store.clear_and_reseed, registry)

        # Re-apply standalone PVs
        if "store" in standalone_pv_container:
            _apply_standalone_pvs(registry, standalone_pv_container["store"], logger)

        # Replace in-memory state
        state_container["state"] = ConfigurationState(registry=registry)

        logger.info(
            "registry_reset",
            devices=len(registry.devices),
        )

        return DeviceCRUDResponse(
            success=True,
            device_name="*",
            operation="reset",
            message=f"Registry reset and re-seeded with {len(registry.devices)} devices from profile",
        )

    @app.post(
        "/api/v1/registry/clear",
        response_model=DeviceCRUDResponse,
        summary="Clear Registry",
        description="Wipe all devices without re-seeding. The next restart will re-seed from the profile.",
        tags=["Registry Admin"],
    )
    async def clear_registry(
        state: StateDep,
        registry_store: RegistryStoreDep,
    ) -> DeviceCRUDResponse:
        """
        Clear the device registry to empty.

        Unlike reset, this does NOT re-seed from the profile collection.
        The registry remains empty until devices are added via CRUD,
        the EE service syncs, or the service is restarted.
        """
        empty_registry = DeviceRegistry()

        await asyncio.to_thread(registry_store.clear_and_reseed, empty_registry)

        # Re-apply standalone PVs (they are preserved)
        if "store" in standalone_pv_container:
            _apply_standalone_pvs(empty_registry, standalone_pv_container["store"], logger)

        state_container["state"] = ConfigurationState(registry=empty_registry)

        logger.info("registry_cleared")

        return DeviceCRUDResponse(
            success=True,
            device_name="*",
            operation="clear",
            message="Registry cleared. 0 devices. Use CRUD or EE sync to populate.",
        )

    @app.get(
        "/api/v1/registry/export",
        summary="Export Registry",
        description="Export the device registry in a portable format (happi JSON or BITS devices.yml)",
        tags=["Registry Admin"],
        responses={
            200: {
                "description": (
                    "Registry export. `format=happi` (default) returns happi "
                    "JSON (`application/json`); `format=bits` returns a BITS "
                    "`devices.yml` document (`application/x-yaml`)."
                ),
                "content": {
                    "application/json": {},
                    "application/x-yaml": {},
                },
            },
            400: {"description": "Unsupported export format."},
        },
    )
    async def export_registry(
        registry_store: RegistryStoreDep,
        format: str = Query(
            "happi",
            description="Export format: 'happi' (default, JSON) or 'bits' (guarneri devices.yml)",
        ),
    ):
        """
        Export the device registry.

        Returns the full device registry in happi JSON format (default) or, when
        ``format=bits``, as a BITS (BCDA-APS guarneri) ``devices.yml``. Either is
        suitable for importing on another VM or as a backup. The BITS format is
        lossy for constructor arguments the guarneri schema cannot express
        (see ``DeviceRegistryStore.export_bits``); prefer happi for full fidelity.
        """
        if format == "happi":
            happi_data = await asyncio.to_thread(registry_store.export_happi)
            return JSONResponse(content=happi_data)

        if format == "bits":
            bits_data = await asyncio.to_thread(registry_store.export_bits)
            yaml_text = yaml.safe_dump(bits_data, default_flow_style=False, sort_keys=True)
            return Response(content=yaml_text, media_type="application/x-yaml")

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported export format: '{format}'. Supported: happi, bits",
        )

    # ===== Standalone PV Endpoints =====
    # NOTE: These must be defined BEFORE /api/v1/pvs/{pv_name:path}
    # to avoid the path parameter from swallowing these routes.

    @app.post(
        "/api/v1/pvs",
        response_model=StandalonePVCRUDResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Register Standalone PV",
        description="Register a standalone PV not associated with any ophyd device",
        tags=["Standalone PVs"],
    )
    async def create_standalone_pv(
        request: StandalonePVCreateRequest,
        state: StateDep,
        pv_store: StandalonePVStoreDep,
    ) -> StandalonePVCRUDResponse:
        """
        Register a standalone PV.

        Adds the PV to the in-memory registry and persists it to PostgreSQL.
        Returns 409 if the PV name already exists (device-bound or standalone).
        """
        pv_name = request.pv_name

        # Check for conflict with existing registry PVs (device-bound)
        if state.registry.get_pv(pv_name) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"PV already exists in registry: {pv_name}",
            )

        # Check for conflict with existing standalone PVs
        if await asyncio.to_thread(pv_store.get_pv, pv_name) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Standalone PV already registered: {pv_name}",
            )

        # Persist FIRST, then mutate memory (see create_device).
        await asyncio.to_thread(
            pv_store.save_pv,
            pv_name=pv_name,
            description=request.description,
            protocol=request.protocol.value,
            access_mode=request.access_mode.value,
            labels=request.labels,
            source="runtime",
            created_by=None,
        )
        state.registry.add_standalone_pv(pv_name)

        logger.info("standalone_pv_created", pv_name=pv_name)

        return StandalonePVCRUDResponse(
            success=True,
            pv_name=pv_name,
            operation="create",
            message=f"Standalone PV '{pv_name}' registered successfully",
        )

    @app.get(
        "/api/v1/pvs/standalone",
        response_model=list[StandalonePV],
        summary="List Standalone PVs",
        description="List all registered standalone PVs with optional label filtering",
        tags=["Standalone PVs"],
    )
    async def list_standalone_pvs(
        pv_store: StandalonePVStoreDep,
        labels: str | None = Query(None, description="Comma-separated labels to filter by"),
    ) -> list[StandalonePV]:
        """
        List all standalone PVs.

        Optional labels query parameter filters to PVs having ALL specified labels.
        """
        label_list = None
        if labels:
            label_list = [s.strip() for s in labels.split(",") if s.strip()]

        return await asyncio.to_thread(pv_store.get_all_pvs, labels=label_list)

    @app.get(
        "/api/v1/pvs/labels",
        response_model=list[str],
        summary="List Standalone PV Labels",
        description="Get all unique labels across registered standalone PVs",
        tags=["Standalone PVs"],
    )
    async def list_standalone_pv_labels(
        pv_store: StandalonePVStoreDep,
    ) -> list[str]:
        """Get all unique labels from standalone PVs."""
        return await asyncio.to_thread(pv_store.get_all_labels)

    @app.put(
        "/api/v1/pvs/standalone/{pv_name:path}",
        response_model=StandalonePVCRUDResponse,
        summary="Update Standalone PV",
        description="Update a registered standalone PV",
        tags=["Standalone PVs"],
    )
    async def update_standalone_pv(
        pv_name: str,
        request: StandalonePVUpdateRequest,
        pv_store: StandalonePVStoreDep,
    ) -> StandalonePVCRUDResponse:
        """
        Update a standalone PV's metadata.

        Supports field-level partial updates. Returns 404 if not found.
        """
        existing = await asyncio.to_thread(pv_store.get_pv, pv_name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Standalone PV not found: {pv_name}",
            )

        # Merge only the fields the caller sent
        updates = request.model_dump(mode="json", exclude_unset=True)
        merged = {
            "description": existing.description,
            "protocol": existing.protocol,
            "access_mode": existing.access_mode,
            "labels": existing.labels,
            "source": existing.source,
        }
        merged.update(updates)

        await asyncio.to_thread(pv_store.save_pv, pv_name=pv_name, **merged)

        logger.info("standalone_pv_updated", pv_name=pv_name)

        return StandalonePVCRUDResponse(
            success=True,
            pv_name=pv_name,
            operation="update",
            message=f"Standalone PV '{pv_name}' updated successfully",
        )

    @app.delete(
        "/api/v1/pvs/standalone/{pv_name:path}",
        response_model=StandalonePVCRUDResponse,
        summary="Delete Standalone PV",
        description="Remove a registered standalone PV",
        tags=["Standalone PVs"],
    )
    async def delete_standalone_pv(
        pv_name: str,
        state: StateDep,
        pv_store: StandalonePVStoreDep,
    ) -> StandalonePVCRUDResponse:
        """
        Delete a standalone PV.

        Removes from both the persistent store and the in-memory registry.
        Returns 404 if not found.
        """
        existing = await asyncio.to_thread(pv_store.get_pv, pv_name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Standalone PV not found: {pv_name}",
            )

        # Remove from persistent store
        await asyncio.to_thread(pv_store.delete_pv, pv_name)

        # Remove from in-memory registry (keeps the entry if a device owns
        # the PV — deleting it would destroy the device's registration).
        state.registry.remove_standalone_pv(pv_name)

        logger.info("standalone_pv_deleted", pv_name=pv_name)

        return StandalonePVCRUDResponse(
            success=True,
            pv_name=pv_name,
            operation="delete",
            message=f"Standalone PV '{pv_name}' deleted successfully",
        )

    # ===== PV Endpoints =====

    @app.get(
        "/api/v1/pvs",
        summary="List PVs",
        description="Query available PVs from loaded devices",
        tags=["PV Registry"],
    )
    async def list_pvs(
        state: StateDep,
        pattern: str | None = Query(None, description="Glob pattern for PV name matching"),
    ) -> dict:
        """
        List available PVs.

        PVs are extracted from the device registry.

        Returns a response compatible with the UI's fetchAvailablePVs().
        """
        logger.info("list_pvs", pattern=pattern)

        pv_list = state.get_pv_list()

        # Apply pattern filter if specified
        if pattern:
            import fnmatch

            pv_list = [pv for pv in pv_list if fnmatch.fnmatch(pv, pattern)]

        return {
            "success": True,
            "pvs": pv_list,
            "count": len(pv_list),
        }

    @app.get(
        "/api/v1/pvs/detailed",
        summary="Get Detailed PV Information",
        description="Get PVs organized by device with signal path information",
        tags=["PV Registry"],
    )
    async def get_pvs_detailed(state: StateDep) -> dict:
        """
        Get detailed PV information organized by device.

        Returns:
            Dict with devices mapping: {device_name: {signal_path: pv_name}}
        """
        logger.info("get_pvs_detailed")

        all_pvs = state.get_all_pvs()

        return {
            "success": True,
            "devices": all_pvs,
            "device_count": len(all_pvs),
            "pv_count": sum(len(pvs) for pvs in all_pvs.values()),
        }

    @app.get(
        "/api/v1/pvs/lookup",
        summary="Lookup Device PVs by PV Name",
        description="Given a PV, find the owning device and return all PVs for that device",
        tags=["PV Registry"],
    )
    async def lookup_device_pvs_by_pv(
        state: StateDep,
        pv_name: str = Query(..., description="PV name to look up"),
    ) -> dict:
        """
        Given a PV name, find which device owns it and return all sibling PVs.

        Useful when you know one PV and want to discover the full device context.
        """
        pv_meta = state.registry.get_pv(pv_name)
        if pv_meta is None:
            raise HTTPException(status_code=404, detail=f"PV not found: {pv_name}")

        device_name = pv_meta.device_name
        if device_name is None:
            # Standalone PV — no owning device
            return {
                "pv_name": pv_name,
                "device_name": None,
                "device_label": None,
                "prefix": None,
                "sibling_pvs": {},
                "count": 0,
            }

        device = state.registry.get_device(device_name)
        if device is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Registry inconsistency: PV '{pv_name}' references device "
                    f"'{device_name}' which has no metadata"
                ),
            )

        prefix = _get_device_prefix(device, state.registry)

        return {
            "pv_name": pv_name,
            "device_name": device_name,
            "device_label": device.device_label,
            "prefix": prefix,
            "sibling_pvs": device.pvs,
            "count": len(device.pvs),
        }

    # ===== PV Status Endpoint =====
    # Must be defined BEFORE /api/v1/pvs/{pv_name:path} wildcard.

    @app.get(
        "/api/v1/pvs/status",
        response_model=PVStatusResponse,
        summary="Get PV Availability",
        tags=["Device Locking"],
    )
    async def get_pv_status(
        state: StateDep,
        lock_manager: LockManagerDep,
        pv_name: str = Query(..., description="EPICS PV name to check"),
    ) -> PVStatusResponse:
        """
        Check whether a PV can be commanded (caput).

        Resolves PV to its owning device and returns the device's lock and
        enabled state. Standalone PVs (not bound to a device) are always
        available. This is the primary endpoint Direct Control calls before
        every write operation.
        """
        pv_meta = state.registry.get_pv(pv_name)
        if pv_meta is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PV not found: {pv_name}",
            )

        device_name = pv_meta.device_name

        # Standalone PV — no owning device, always available
        if device_name is None:
            return PVStatusResponse(
                pv_name=pv_name,
                available=True,
                device_name=None,
                device_enabled=None,
                device_lock_status=None,
                locked_by_plan=None,
                locked_by_item=None,
                locked_at=None,
            )

        # Device-bound PV — check device lock and enabled state
        spec = state.registry.get_instantiation_spec(device_name)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Registry inconsistency: PV '{pv_name}' references device "
                    f"'{device_name}' which has no instantiation spec"
                ),
            )
        enabled = spec.active
        lock_state = lock_manager.effective_lock(device_name)
        locked = lock_state is not None

        return PVStatusResponse(
            pv_name=pv_name,
            available=enabled and not locked,
            device_name=device_name,
            device_enabled=enabled,
            device_lock_status="locked" if locked else "unlocked",
            locked_by_plan=lock_state.locked_by_plan if lock_state else None,
            locked_by_item=lock_state.locked_by_item if lock_state else None,
            locked_at=lock_state.locked_at.isoformat() if lock_state else None,
        )

    @app.get(
        "/api/v1/pvs/{pv_name:path}",
        response_model=PVMetadata,
        summary="Get PV Metadata",
        description="Get detailed metadata for specific PV",
        tags=["PV Registry"],
    )
    async def get_pv(state: StateDep, pv_name: str) -> PVMetadata:
        """
        Get PV metadata.

        Protocol: ProvidesDeviceRegistry.get_pv_metadata()
        """
        logger.info("get_pv", pv_name=pv_name)
        pv = state.registry.get_pv(pv_name)

        if pv is None:
            logger.warning("pv_not_found", pv_name=pv_name)
            raise HTTPException(status_code=404, detail=f"PV not found: {pv_name}")

        return pv

    # ===== Nested Device Component Endpoints =====

    @app.get(
        "/api/v1/devices/{device_path:path}/component",
        response_model=NestedDeviceComponent,
        summary="Get Nested Device Component",
        description="Look up nested device component by dot-separated path",
        tags=["Device Components"],
    )
    async def get_nested_device_component(
        state: StateDep, device_path: str
    ) -> NestedDeviceComponent:
        """
        Get information about a nested device component.

        Protocol: ophyd-websocket compatible nested device lookup

        Supports paths like:
        - motor1
        - motor1.user_readback
        - detector.image.array_size

        Returns component metadata including associated PV.
        """
        logger.info("get_nested_component", device_path=device_path)

        # Parse device path
        parts = device_path.split(".")
        device_name = parts[0]
        component_path = parts[1:] if len(parts) > 1 else []

        # Get base device
        device = state.registry.get_device(device_name)
        if device is None:
            logger.warning("device_not_found", device_name=device_name)
            raise HTTPException(status_code=404, detail=f"Device not found: {device_name}")

        # If no component path, return top-level device info
        if not component_path:
            return NestedDeviceComponent(
                name=device_name,
                device_path=device_path,
                parent_device=device_name,
                component_type=device.ophyd_class,
                pv=None,
                is_readable=device.is_readable,
                is_settable=device.is_movable,
            )

        # Look up component in device's PV mapping
        component_name = ".".join(component_path)

        # Try exact match first
        pv = device.pvs.get(component_name)

        # Try just the first component if nested path doesn't match
        if pv is None and len(component_path) == 1:
            pv = device.pvs.get(component_path[0])

        # Determine if settable (heuristic: setpoints are usually settable)
        is_settable = False
        if component_name in ("user_setpoint", "setpoint", "val"):
            is_settable = True
        elif pv and not any(ro in pv.upper() for ro in ["RBV", "READBACK", "STAT"]):
            is_settable = True

        return NestedDeviceComponent(
            name=component_name,
            device_path=device_path,
            parent_device=device_name,
            component_type="Signal",  # Default to Signal for components
            pv=pv,
            is_readable=True,
            is_settable=is_settable,
        )

    @app.post(
        "/api/v1/devices/resolve",
        response_model=PathResolveResponse,
        summary="Resolve Dotted Device Addresses to PV Names",
        description=(
            "Walk the ophyd / ophyd-async device class for each address "
            "and return the underlying EPICS PV. Read-only, best-effort "
            "per-item — no halt-on-error. Used by the frontend to translate "
            "friendly addresses like 'vortex.mca.rois.roi2.lo_chan' into "
            "the PV strings needed by direct-control's batch caput."
        ),
        tags=["Device Components"],
    )
    async def resolve_device_paths(
        state: StateDep, request: PathResolveRequest
    ) -> PathResolveResponse:
        """Resolve a batch of dotted device addresses to PV names.

        Two-pass resolution. First pass:

        1. Split off the head segment as the device name and look it up
           in the registry. Missing device → ``device_not_found``.
        2. Pull the device's instantiation spec (which holds the
           ``device_class`` import path) and resolve the prefix via the
           shared ``_get_device_prefix`` helper.
        3. Hand off to ``path_resolver.resolve`` which dispatches to the
           classic-ophyd class walker or the ophyd-async
           instantiate-then-walk path based on the class hierarchy.

        First-pass resolution never opens EPICS connections. ophyd-async
        classes are instantiated locally (no ``.connect()``) so their
        Signal.source URIs can be read; classic-ophyd classes are walked
        at class level.

        Second pass (only if ``CONFIG_DIRECT_CONTROL_URL`` is set): any
        ``needs_enrichment`` outcomes from the first pass are batched and
        sent to direct-control's ``/api/v1/devices/enrich`` endpoint,
        which instantiates the device against the live IOC and reads the
        leaf signal's PV name. Successful enrichments are cached in-
        process so subsequent identical addresses skip the round-trip.

        Top-level addresses (no sub-attribute) are framework-dependent:
        for classic ophyd they resolve to the device's prefix (the happi
        entry IS the leaf, e.g. a standalone ``EpicsSignal``); for
        ophyd-async they return ``no_such_attr`` since async devices have
        many signals and no canonical "the PV". Use
        ``<device>.<attr>`` instead for async devices.
        """
        # Per-request cache for ophyd-async device instances. A batch that
        # addresses the same device multiple times (e.g. motor.user_setpoint
        # + motor.velocity) instantiates the class once and reuses it.
        # Classic-ophyd resolution is purely static and ignores this cache.
        device_cache: dict = {}

        dc_client = direct_control_container.get("client")
        enrich_cache = enrichment_cache_container.get("cache", {})

        # First pass: static resolution. Slots that need enrichment get a
        # placeholder + an entry in `deferred`.
        results: list[PathResolveResultItem | None] = []
        deferred: list[_DeferredEnrichment] = []

        for address in request.addresses:
            head, _, sub_path = address.partition(".")
            device = state.registry.get_device(head)
            if device is None:
                results.append(
                    PathResolveResultItem(
                        address=address,
                        outcome=Outcome.DEVICE_NOT_FOUND,
                        message=f"no device named '{head}' in registry",
                    )
                )
                continue

            spec = state.registry.get_instantiation_spec(head)
            if spec is None or not spec.device_class:
                results.append(
                    PathResolveResultItem(
                        address=address,
                        outcome=Outcome.IMPORT_FAILED,
                        message=(
                            f"device '{head}' has no instantiation spec "
                            f"(can't resolve class to walk)"
                        ),
                    )
                )
                continue

            prefix = _get_device_prefix(device, state.registry)
            if prefix is None:
                results.append(
                    PathResolveResultItem(
                        address=address,
                        outcome=Outcome.IMPORT_FAILED,
                        message=(
                            f"device '{head}' has no derivable prefix "
                            f"(checked pvs['prefix'], spec.args[0], "
                            f"longest common PV prefix)"
                        ),
                    )
                )
                continue

            resolution = resolve_path(
                address,
                device_class_path=spec.device_class,
                prefix=prefix,
                device_cache=device_cache,
            )

            # Enrichment path: needs_enrichment + client configured.
            if resolution.outcome is Outcome.NEEDS_ENRICHMENT and dc_client is not None:
                cache_key = (spec.device_class, prefix, sub_path)
                cached_pv = enrich_cache.get(cache_key)
                if cached_pv is not None:
                    results.append(
                        PathResolveResultItem(
                            address=address,
                            outcome=Outcome.RESOLVED,
                            pv_name=cached_pv,
                        )
                    )
                    continue
                results.append(None)  # placeholder filled in by second pass
                deferred.append(
                    _DeferredEnrichment(
                        result_idx=len(results) - 1,
                        address=address,
                        cache_key=cache_key,
                    )
                )
                continue

            results.append(
                PathResolveResultItem(
                    address=address,
                    outcome=resolution.outcome,
                    pv_name=resolution.pv_name,
                    message=resolution.message,
                )
            )

        # Second pass: call direct-control to enrich deferred items.
        if deferred:
            specs = [
                EnrichmentSpec(
                    device_class_path=d.cache_key[0],
                    prefix=d.cache_key[1],
                    sub_path=d.cache_key[2],
                )
                for d in deferred
            ]
            try:
                enrichments = await dc_client.enrich(specs)  # type: ignore[union-attr]
            except DirectControlUnavailable as e:
                # Mark every deferred slot as enrichment_unavailable.
                for d in deferred:
                    results[d.result_idx] = PathResolveResultItem(
                        address=d.address,
                        outcome=Outcome.ENRICHMENT_UNAVAILABLE,
                        message=str(e),
                    )
            else:
                for d, result in zip(deferred, enrichments, strict=True):
                    if result.ok and result.pv_name:
                        # Cache success; failures are not cached because
                        # they may be transient (IOC down, etc.) and we
                        # want them re-attempted on the next request.
                        enrich_cache[d.cache_key] = result.pv_name
                        results[d.result_idx] = PathResolveResultItem(
                            address=d.address,
                            outcome=Outcome.RESOLVED,
                            pv_name=result.pv_name,
                        )
                    else:
                        results[d.result_idx] = PathResolveResultItem(
                            address=d.address,
                            outcome=Outcome.ENRICHMENT_UNAVAILABLE,
                            message=(
                                f"direct-control enrichment failed: "
                                f"{result.error_type}: {result.message}"
                            ),
                        )

        # Every slot is filled by now; the cast keeps the response model happy.
        return PathResolveResponse(resolved=[r for r in results if r is not None])

    @app.get(
        "/api/v1/devices/{device_name}/components",
        response_model=list[NestedDeviceComponent],
        summary="List Device Components",
        description="List all components of a device",
        tags=["Device Components"],
    )
    async def list_device_components(
        state: StateDep,
        device_name: str,
        max_depth: int | None = Query(
            None, ge=0, description="Maximum component depth (0 = all, 1 = top-level only)"
        ),
    ) -> list[NestedDeviceComponent]:
        """
        List all components of a device.

        Protocol: ophyd-websocket compatible device component listing

        Returns list of all readable/settable components with their PV mappings.
        Use max_depth to limit traversal into nested sub-components.
        """
        logger.info("list_device_components", device_name=device_name, max_depth=max_depth)

        device = state.registry.get_device(device_name)
        if device is None:
            logger.warning("device_not_found", device_name=device_name)
            raise HTTPException(status_code=404, detail=f"Device not found: {device_name}")

        components = []

        # Add components from PV mapping
        for component_name, pv in device.pvs.items():
            # Apply depth filter: depth = number of dots + 1
            if max_depth is not None and max_depth > 0:
                component_depth = component_name.count(".") + 1
                if component_depth > max_depth:
                    continue

            # Determine if settable
            is_settable = False
            if component_name in ("user_setpoint", "setpoint", "val"):
                is_settable = True
            elif not any(ro in pv.upper() for ro in ["RBV", "READBACK", "STAT"]):
                is_settable = True

            components.append(
                NestedDeviceComponent(
                    name=component_name,
                    device_path=f"{device_name}.{component_name}",
                    parent_device=device_name,
                    component_type="Signal",
                    pv=pv,
                    is_readable=True,
                    is_settable=is_settable,
                )
            )

        return components

    return app


# App instance for direct imports and testing
# Note: CLI uses factory=True with create_app() to ensure env vars are set first
app = create_app()
