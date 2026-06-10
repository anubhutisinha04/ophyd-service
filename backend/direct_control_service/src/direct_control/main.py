"""
FastAPI application for the merged Direct Device Control + Monitoring Service.

Combines A4-coordinated device commanding with EPICS PV monitoring and
WebSocket streaming on a single port. Authorization is handled by upstream
middleware — no auth enforcement in this service.

Uses lifespan pattern to defer Settings() creation until after CLI sets
environment variables (pyepics reads EPICS_CA_* env vars at import time).
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
import numpy as np
import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ._array_metadata import describe_array
from .config import READ_ONLY_MESSAGE, Settings
from .coordination_client import CoordinationClient
from .device_controller import DeviceController
from .models import (
    CoordinationCheckError,
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDisabledError,
    DeviceLockedError,
    EnrichmentRequest,
    EnrichmentResponse,
    EnrichmentResultItem,
    HealthResponse,
    NestedDeviceRequest,
    NestedDeviceResponse,
    PVNotFoundError,
    PVReadError,
    PVSetBatchItemResult,
    PVSetBatchRequest,
    PVSetBatchResponse,
    PVSetRequest,
    PVSetResponse,
)
from .ophyd_cache import OphydDeviceCache
from .pv_health_reporter import PVHealthReporter
from .protocols import CoordinationService, DeviceControl, PVMonitor, RegistryProvider
from .registry_client import RegistryClient, RegistryValidationError
from .registry_file import FileRegistryProvider

logger = structlog.get_logger(__name__)


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
        import json
        from pathlib import Path as _Path

        out = _Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
        logger.info("openapi_schema_exported", path=str(out))
    except Exception as exc:
        logger.error("openapi_schema_export_failed", path=path, error=str(exc), exc_info=True)
        raise RuntimeError(
            f"OpenAPI schema export to {path} failed: {exc}. "
            f"Unset {_OPENAPI_EXPORT_PATH_ENV} to skip export."
        ) from exc


async def _check_config_health(config_http: httpx.AsyncClient) -> Optional[str]:
    """One config-service /health poll. Returns None if healthy, else a reason.

    Distinguishes timeout / connection error / non-2xx so a failed probe says
    why, rather than a bare "unreachable".
    """
    try:
        resp = await config_http.get("/health", timeout=2.0)
    except httpx.TimeoutException as exc:
        return f"timeout reaching /health: {exc}"
    except httpx.RequestError as exc:
        return f"cannot reach /health: {exc}"
    if resp.status_code == 200:
        return None
    return f"/health returned HTTP {resp.status_code}"


async def _await_config_service(
    settings: Settings, config_http: httpx.AsyncClient
) -> Optional[str]:
    """Poll config-service /health until ready or the startup timeout elapses.

    Returns None once /health answers 200, else the last failure detail.
    Retries for config_service_startup_timeout seconds so compose/k8s start
    ordering doesn't cause a spurious failure.
    """
    deadline = time.monotonic() + settings.config_service_startup_timeout
    attempt = 0
    last_detail = "unreachable"
    while True:
        attempt += 1
        detail = await _check_config_health(config_http)
        if detail is None:
            logger.info(
                "configuration_service_ready",
                url=settings.configuration_service_url,
                attempts=attempt,
            )
            return None
        last_detail = detail
        if time.monotonic() >= deadline:
            return last_detail
        logger.warning(
            "waiting_for_configuration_service",
            url=settings.configuration_service_url,
            attempt=attempt,
            detail=last_detail,
            retry_in_s=settings.config_service_startup_probe_interval,
        )
        await asyncio.sleep(settings.config_service_startup_probe_interval)


async def _probe_configuration_service(settings: Settings, config_http: httpx.AsyncClient) -> None:
    """Block startup until configuration_service is reachable, or fail hard.

    For the http/file backends. configuration_service is required whenever the
    registry is HTTP-backed (registry validation) OR coordination is enabled
    (device-lock state). Without this probe a misconfigured or not-yet-started
    config-service is invisible at boot and only surfaces later as per-request
    503s — we fail loudly at startup instead.

    Skipped entirely when config-service isn't a dependency (file-backed
    registry with coordination disabled), or when the probe is opted out.
    The auto backend does its own probe-and-fallback in
    ``_resolve_registry_backend`` and does not call this.
    """
    config_service_required = (
        settings.registry_backend == "http" or settings.coordination_check_enabled
    )
    if not config_service_required:
        logger.info(
            "config_service_not_required",
            registry_backend=settings.registry_backend,
            coordination_check_enabled=settings.coordination_check_enabled,
            note="file-backed registry with coordination disabled — not probing",
        )
        return

    if not settings.config_service_startup_probe:
        logger.warning(
            "config_service_startup_probe_disabled",
            note="Not verifying configuration_service reachability at startup",
        )
        return

    detail = await _await_config_service(settings, config_http)
    if detail is None:
        return

    raise RuntimeError(
        f"configuration_service at {settings.configuration_service_url} not "
        f"reachable after {settings.config_service_startup_timeout:.0f}s "
        f"({detail}). direct_control requires it for registry validation and/or "
        f"device-lock coordination. Set "
        f"DIRECT_CONTROL_CONFIG_SERVICE_STARTUP_PROBE=false to start without it, "
        f"or use a file-backed registry (DIRECT_CONTROL_REGISTRY_BACKEND=file) "
        f"for monitoring-only deployments."
    )


class RegistryResolution(NamedTuple):
    """Outcome of choosing the registry backend at startup.

    ``effective_backend`` is the resolved choice ("http" or "file"); for auto it
    reflects which one actually got picked. ``coordination_enabled`` is the
    effective device-lock coordination decision — off in file/standalone mode,
    where there is no configuration_service to read lock state from. Both are
    surfaced via /health and /api/v1/stats so the running mode is always visible.
    """

    provider: RegistryProvider
    coordination_enabled: bool
    effective_backend: str


async def _resolve_registry_backend(
    settings: Settings, config_http: httpx.AsyncClient
) -> RegistryResolution:
    """Pick the registry backend, gating startup on config-service as needed.

    - http: probe config-service (raises if down), use the HTTP registry with
      coordination as configured.
    - file: fully-featured standalone mode on the local registry file. No
      config-service, so the lock-coordination check is turned off (access is
      governed by global_read_only instead).
    - auto: prefer config-service; if unreachable, run the same fully-featured
      standalone mode on the file registry, or raise if no file is configured.

    Returns the decision explicitly (effective backend + coordination flag)
    rather than mutating settings, so the caller applies it at one visible point.
    """
    backend = settings.registry_backend

    if backend == "http":
        await _probe_configuration_service(settings, config_http)
        return RegistryResolution(
            RegistryClient(settings), settings.coordination_check_enabled, "http"
        )

    if backend == "file":
        logger.info(
            "registry_backend_file",
            path=settings.registry_file_path,
            note="standalone mode: file registry, configuration_service not used",
        )
        return RegistryResolution(FileRegistryProvider(settings.registry_file_path), False, "file")

    # auto
    logger.info("registry_backend_auto_probing", url=settings.configuration_service_url)
    if settings.config_service_startup_probe:
        detail = await _await_config_service(settings, config_http)
    else:
        # Opt-out honored: decide from a single /health check instead of
        # blocking for the full startup window.
        logger.warning(
            "config_service_startup_probe_disabled",
            note="auto backend deciding from a single /health check, no wait",
        )
        detail = await _check_config_health(config_http)

    if detail is None:
        logger.info("registry_backend_auto_resolved", choice="http")
        return RegistryResolution(
            RegistryClient(settings), settings.coordination_check_enabled, "http"
        )

    if settings.registry_file_path:
        logger.warning(
            "registry_backend_auto_using_file",
            path=settings.registry_file_path,
            config_service_detail=detail,
            note=(
                "configuration_service unreachable; running fully-featured "
                "standalone on the file registry (lock coordination off, "
                "access governed by global_read_only)"
            ),
        )
        return RegistryResolution(FileRegistryProvider(settings.registry_file_path), False, "file")

    raise RuntimeError(
        f"registry_backend=auto: configuration_service at "
        f"{settings.configuration_service_url} unreachable after "
        f"{settings.config_service_startup_timeout:.0f}s ({detail}) and no "
        f"DIRECT_CONTROL_REGISTRY_FILE_PATH configured to fall back to. "
        f"Cannot start."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize clients and managers on startup, clean up on shutdown."""
    logger.info("Starting Direct Device Control + Monitoring Service")

    _maybe_export_openapi(app)

    settings = Settings()

    # Import pyepics-dependent managers after env vars are in place.
    from .monitoring.device_websocket_manager import DeviceWebSocketManager
    from .monitoring.image_stream_manager import ImageStreamManager
    from .monitoring.pv_monitor import PVMonitorManager
    from .monitoring.websocket_manager import WebSocketManager

    coordination_client = CoordinationClient(settings)
    config_http = httpx.AsyncClient(base_url=settings.configuration_service_url, timeout=10.0)
    # Resolve the registry backend. This gates startup on config-service where
    # required (http, or file+coordination), and for backend=auto probes
    # config-service and may fall back to the file registry (disabling
    # coordination). Fails hard if a required config-service is unreachable —
    # close config_http first so the aborting boot doesn't leak it.
    try:
        resolution = await _resolve_registry_backend(settings, config_http)
    except BaseException:
        await config_http.aclose()
        raise
    registry_client = resolution.provider
    # Apply the effective coordination decision at one explicit point. The
    # coordination client reads settings.coordination_check_enabled live, so
    # this takes effect for the gate even though the client predates this line.
    settings.coordination_check_enabled = resolution.coordination_enabled
    device_controller = DeviceController(settings, coordination_client, registry_client)
    pv_monitor = PVMonitorManager(settings)
    ws_manager = WebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=device_controller,
        settings=settings,
        registry_client=registry_client,
    )
    device_ws_manager = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=device_controller,
        settings=settings,
    )
    # Image-streaming sockets (finch camera-socket / tiff-socket). Read-only
    # EPICS monitors with their own raw-numpy path — see ImageStreamManager.
    # registry_client gives them the same PV-existence gate as the other sockets.
    camera_ws_manager = ImageStreamManager(
        settings=settings, kind="camera", registry_client=registry_client
    )
    tiff_ws_manager = ImageStreamManager(
        settings=settings, kind="tiff", registry_client=registry_client
    )

    app.state.settings = settings
    app.state.coordination_client = coordination_client
    app.state.device_controller = device_controller
    app.state.registry_client = registry_client
    app.state.config_http = config_http
    app.state.pv_monitor = pv_monitor
    app.state.ws_manager = ws_manager
    app.state.device_ws_manager = device_ws_manager
    app.state.camera_ws_manager = camera_ws_manager
    app.state.tiff_ws_manager = tiff_ws_manager
    app.state.ophyd_cache = OphydDeviceCache()
    app.state.pv_health_reporter = PVHealthReporter(config_http)
    # The resolved registry backend ("http" | "file"). Surfaced by /health and
    # /api/v1/stats so the running mode (incl. auto's choice) is always visible.
    app.state.effective_registry_backend = resolution.effective_backend

    logger.info(
        "Service initialized",
        port=settings.port,
        configuration_service_url=settings.configuration_service_url,
        coordination_enabled=settings.coordination_check_enabled,
    )

    try:
        yield
    finally:
        logger.info("Shutting down service")
        # Drain in-flight PV-health reports before closing the httpx
        # client they need. 5s cap so a hung config-service can't block
        # shutdown indefinitely.
        await app.state.pv_health_reporter.drain(timeout=5.0)
        await ws_manager.close_all()
        await camera_ws_manager.close_all()
        await tiff_ws_manager.close_all()
        await device_ws_manager.cleanup()
        await coordination_client.cleanup()
        await registry_client.cleanup()
        await config_http.aclose()
        await pv_monitor.cleanup()
        # ophyd_cache is created on startup but, unlike the resources above, was
        # the one with no shutdown teardown — drop its cached devices so their
        # EPICS CA channels are released (pyepics tears them down on the next GC
        # pass) instead of lingering past a graceful shutdown / reload.
        app.state.ophyd_cache.clear()
        logger.info("Service shut down")


app = FastAPI(
    title="Bluesky Direct Device Control + Monitoring",
    description=(
        "Device commanding with A4 coordination checks, plus real-time "
        "EPICS PV monitoring via WebSocket."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_settings() -> Settings:
    return app.state.settings


def get_coordination_client() -> CoordinationService:
    return app.state.coordination_client


def get_device_controller() -> DeviceControl:
    return app.state.device_controller


def get_registry_client() -> RegistryClient:
    return app.state.registry_client


def get_pv_monitor() -> PVMonitor:
    return app.state.pv_monitor


def get_ws_manager():
    return app.state.ws_manager


def get_device_ws_manager():
    return app.state.device_ws_manager


def get_ophyd_cache() -> OphydDeviceCache:
    return app.state.ophyd_cache


def get_pv_health_reporter() -> PVHealthReporter:
    return app.state.pv_health_reporter


def get_effective_registry_backend() -> str:
    """The resolved registry backend ("http" | "file"). ``getattr`` default
    covers fixtures that don't run lifespan."""
    return getattr(app.state, "effective_registry_backend", "http")


def require_writable(settings: Settings = Depends(get_settings)) -> None:
    """Reject the request with 403 when the deployment is read-only.

    Applied to every control/write REST endpoint. The WebSocket set/stop
    handlers enforce the same gate inline."""
    if settings.global_read_only:
        raise HTTPException(status_code=403, detail=READ_ONLY_MESSAGE)


# ----- PV value response builder (tiled-style content negotiation) -----

_JSON_MEDIA = "application/json"
_BINARY_MEDIA = "application/octet-stream"
_FORMAT_ALIASES = {
    "json": _JSON_MEDIA,
    "binary": _BINARY_MEDIA,
    "octet-stream": _BINARY_MEDIA,
}


def _negotiate_format(request: Request, format_param: Optional[str]) -> str:
    """Pick a supported media type from ?format= or the Accept header.

    Returns 406 if Accept lists only media types we don't serve, matching
    tiled's contract rather than silently defaulting to JSON.
    """
    if format_param:
        resolved = _FORMAT_ALIASES.get(format_param.lower(), format_param)
        if resolved not in (_JSON_MEDIA, _BINARY_MEDIA):
            raise HTTPException(
                status_code=406,
                detail=f"Unsupported format: {format_param}. Supported: json, binary.",
            )
        return resolved

    accept = request.headers.get("accept")
    if not accept:
        return _JSON_MEDIA
    for chunk in accept.split(","):
        media = chunk.split(";")[0].strip()
        if media == "*/*":
            return _JSON_MEDIA
        if media in (_JSON_MEDIA, _BINARY_MEDIA):
            return media
    raise HTTPException(
        status_code=406,
        detail=(
            f"No supported media types in Accept: {accept}. "
            f"Supported: {_JSON_MEDIA}, {_BINARY_MEDIA}."
        ),
    )


def _build_value_response(
    request: Request,
    *,
    pv_name: str,
    value: Any,
    timestamp_iso: str,
    size_limit: int,
    format_param: Optional[str],
    # Pre-computed metadata overrides (used when value was already converted
    # to JSON-native form and shape/dtype/ndim/nbytes are known from capture).
    shape: Optional[List[int]] = None,
    dtype: Optional[str] = None,
    ndim: Optional[int] = None,
    nbytes: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Response:
    """
    Build a tiled-style PV value response.

    JSON mode returns `{pv_name, value, timestamp, shape, dtype, ndim, nbytes, **extra}`.
    Binary mode returns raw bytes; shape/dtype live in `X-PV-*` headers so
    clients can reshape.
    """
    if shape is None or dtype is None or ndim is None or nbytes is None:
        shape, dtype, ndim, nbytes = describe_array(value)
    assert shape is not None and ndim is not None and nbytes is not None

    if nbytes > size_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Response would be {nbytes} bytes, exceeds "
                f"DIRECT_CONTROL_RESPONSE_BYTESIZE_LIMIT ({size_limit}). "
                "Slice the value or raise the limit."
            ),
        )

    media = _negotiate_format(request, format_param)

    if media == _BINARY_MEDIA:
        # Reconstruct a contiguous numpy array. If `value` is already an
        # ndarray we use it directly; if it was converted to a list upstream
        # (monitored endpoint path) we rebuild via dtype.
        if isinstance(value, np.ndarray):
            arr = value
        elif dtype:
            try:
                arr = np.asarray(value, dtype=np.dtype(dtype))
            except (TypeError, ValueError) as e:
                raise HTTPException(
                    status_code=406,
                    detail=f"Cannot serve as binary ({e}); request application/json.",
                )
        else:
            raise HTTPException(
                status_code=406,
                detail=(
                    "Value is not a numeric array/scalar with known dtype; "
                    "cannot serve as binary. Request application/json."
                ),
            )
        if arr.dtype.kind not in "iufbc":
            raise HTTPException(
                status_code=406,
                detail=(
                    f"dtype {arr.dtype} is not numeric; "
                    "cannot serve as binary. Request application/json."
                ),
            )
        body = np.ascontiguousarray(arr).tobytes()
        headers = {
            "X-PV-Name": pv_name,
            "X-PV-Shape": ",".join(str(s) for s in shape),
            "X-PV-Dtype": dtype or "",
            "X-PV-Ndim": str(ndim),
            "X-PV-Nbytes": str(nbytes),
            "X-PV-Timestamp": timestamp_iso,
        }
        return Response(body, media_type=_BINARY_MEDIA, headers=headers)

    tolist = getattr(value, "tolist", None)
    payload: Dict[str, Any] = {
        "pv_name": pv_name,
        "value": tolist() if callable(tolist) else value,
        "timestamp": timestamp_iso,
        "shape": shape,
        "dtype": dtype,
        "ndim": ndim,
        "nbytes": nbytes,
    }
    if extra:
        payload.update(extra)
    return JSONResponse(payload)


def _raise_http_for_device_unavailable(
    exc: "DeviceDisabledError | DeviceLockedError",
    event_prefix: str,
    **log_fields: Any,
) -> None:
    """Translate a coord-gate exception into the right HTTP status + log line.

    Disabled and locked are distinct enough that the frontend should branch
    on the status code (re-enable in config-service vs wait/retry), so they
    map to different codes (409 / 423).
    """
    if isinstance(exc, DeviceDisabledError):
        logger.warning(f"{event_prefix}_disabled", error=str(exc), **log_fields)
        raise HTTPException(status_code=409, detail=str(exc))
    logger.warning(f"{event_prefix}_locked", error=str(exc), **log_fields)
    raise HTTPException(status_code=423, detail=str(exc))


def _raise_http_501_not_implemented(
    exc: NotImplementedError,
    event: str,
    **log_fields: Any,
) -> None:
    """Translate a placeholder ``NotImplementedError`` into a clear 501.

    Per the no-silent-fallbacks audit, device-method endpoints whose
    underlying ophyd integration isn't done must surface 501 with the
    exception's message rather than 200 OK with ``success=False``.
    """
    logger.warning(event, error=str(exc), **log_fields)
    raise HTTPException(status_code=501, detail=str(exc))


@app.get("/health", response_model=HealthResponse)
async def health_check(
    settings: Settings = Depends(get_settings),
    coordination_client: CoordinationService = Depends(get_coordination_client),
    pv_monitor: PVMonitor = Depends(get_pv_monitor),
    ws_manager=Depends(get_ws_manager),
    registry_backend: str = Depends(get_effective_registry_backend),
):
    """Combined health check: coordination availability and monitoring stats.

    Returns 503 when a REQUIRED configuration_service (http backend) is
    unreachable so LB readiness probes can route away;
    ``coordination_service_detail`` carries the structured reason. In
    file/standalone mode there is no config-service requirement, so the node is
    healthy. ``registry_backend`` and ``read_only`` report the running mode so
    a file-backed or read-only deployment is always visible, never silent.
    """
    coord = await coordination_client.is_service_available()
    stats = ws_manager.get_stats()

    body = HealthResponse(
        status="healthy" if coord.available else "unhealthy",
        timestamp=datetime.now(),
        coordination_service_available=coord.available,
        coordination_service_detail=coord.detail,
        registry_backend=registry_backend,
        read_only=settings.global_read_only,
        active_subscriptions=len(pv_monitor.get_connected_pvs()),
        connected_pvs=stats["connected_pvs"],
        websocket_connections=stats["active_connections"],
    )
    if not coord.available:
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
    return body


@app.get("/api/v1/stats")
async def get_stats(
    settings: Settings = Depends(get_settings),
    coordination_client: CoordinationService = Depends(get_coordination_client),
    ws_manager=Depends(get_ws_manager),
    device_ws_manager=Depends(get_device_ws_manager),
    registry_backend: str = Depends(get_effective_registry_backend),
):
    coord = await coordination_client.is_service_available()
    pv_stats = ws_manager.get_stats()
    device_stats = device_ws_manager.get_stats()

    return {
        "service": "direct_control",
        "timestamp": datetime.now().isoformat(),
        "registry_backend": registry_backend,
        "read_only": settings.global_read_only,
        "coordination_enabled": settings.coordination_check_enabled,
        "coordination_service_available": coord.available,
        "coordination_service_detail": coord.detail,
        "command_timeout": settings.command_timeout,
        "pv_socket": {
            "websocket_connections": pv_stats["active_connections"],
            "total_pvs": pv_stats["total_pvs"],
            "connected_pvs": pv_stats["connected_pvs"],
        },
        "device_socket": {
            "websocket_connections": device_stats["active_connections"],
            "subscribed_devices": device_stats["subscribed_devices"],
            "total_device_pvs": device_stats["total_device_pvs"],
        },
        "buffer_size": settings.pv_buffer_size,
        "max_connections": settings.ws_max_connections,
    }


@app.post(
    "/api/v1/pv/set",
    response_model=PVSetResponse,
    dependencies=[Depends(require_writable)],
)
async def set_pv(
    request: PVSetRequest,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
    pv_health_reporter: PVHealthReporter = Depends(get_pv_health_reporter),
):
    """
    Set EPICS PV value with coordination check (Low Fidelity Channel).

    Two modes:
    - wait=False (fire-and-forget, default): Issues write, returns immediately.
    - wait=True (put-completion): Waits for EPICS put-completion callback.

    Raises 404 if PV not in registry, 423 if device locked, 503 if
    coordination service unavailable.

    After the caput, fires a background report to configuration_service's
    PV-health endpoint (``/api/v1/pvs/{pv_name}/{success|failure}``) so
    the operator UI can see degraded/unresponsive PVs. The report runs
    in a fire-and-forget task; the response returns without waiting on it.
    Gate failures (locked / disabled / coordination) are NOT reported —
    those reflect orchestration policy, not PV health.
    """
    try:
        await registry_client.validate_pv(request.pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        resp = await device_controller.set_pv(request)
    except (DeviceDisabledError, DeviceLockedError) as e:
        # Gate refusal — not a PV-health event.
        _raise_http_for_device_unavailable(e, "pv", pv_name=request.pv_name)
    except CoordinationCheckError as e:
        # Coordination unavailability — not a PV-health event.
        logger.error("coordination_check_failed", pv_name=request.pv_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")
    except Exception as e:
        # An unexpected error after we got past the gates almost always
        # means a pyepics CA failure (timeout, put-rejected, etc.) —
        # that's a PV-health event.
        pv_health_reporter.report(request.pv_name, success=False, message=str(e))
        logger.error("set_pv_error", pv_name=request.pv_name, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    pv_health_reporter.report(
        request.pv_name,
        success=resp.success,
        message=None if resp.success else resp.message,
    )
    return resp


def _batch_failure_result(
    pv_name: str,
    exc: Exception,
    status_code: int,
    *,
    coordination_checked: bool,
) -> PVSetBatchItemResult:
    """Build a failure row for one item in a batch caput.

    ``coordination_checked`` reflects whether the coord-gate ran before the
    failure: False for failures that happen during registry validation
    (we never get to the gate), True for failures raised by
    ``device_controller.set_pv`` (registry passed, the call entered the
    controller where the gate runs).
    """
    return PVSetBatchItemResult(
        pv_name=pv_name,
        success=False,
        timestamp=datetime.now(),
        coordination_checked=coordination_checked,
        error_type=type(exc).__name__,
        message=str(exc),
        status_code=status_code,
    )


@app.post(
    "/api/v1/pv/set/batch",
    response_model=PVSetBatchResponse,
    dependencies=[Depends(require_writable)],
)
async def set_pv_batch(
    request: PVSetBatchRequest,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
    pv_health_reporter: PVHealthReporter = Depends(get_pv_health_reporter),
):
    """Apply a sequence of caputs with fail-hard semantics.

    Caputs are run in the order given. On the first failure, the loop
    halts and the response is returned with ``ok=false`` — remaining items
    are NOT attempted. Items already applied are NOT rolled back (the IOC
    has no notion of a transaction). The HTTP status of the batch call
    itself is always 200 on a well-formed request; per-item HTTP-equivalent
    codes are in each result's ``status_code`` field so the caller can
    branch the same way it would on a single ``/pv/set`` call.

    Designed for "configure beamline for edge X" flows where the frontend
    bundles ~10–20 caputs from a preset table. If the half-applied state
    is unacceptable for your use case, do not catch ``ok=false`` and move
    on — surface the failure to the operator.
    """
    results: List[PVSetBatchItemResult] = []
    applied = 0

    for item in request.caputs:
        # Mirror the single /pv/set validation chain so a batch behaves
        # exactly like a sequence of individual calls would (modulo the
        # short-circuit on first failure).
        try:
            await registry_client.validate_pv(item.pv_name)
        except RegistryValidationError as e:
            results.append(_batch_failure_result(item.pv_name, e, 404, coordination_checked=False))
            logger.warning(
                "pv_set_batch_registry_invalid",
                pv_name=item.pv_name,
                applied_before_halt=applied,
            )
            break
        except RuntimeError as e:
            results.append(_batch_failure_result(item.pv_name, e, 503, coordination_checked=False))
            logger.warning(
                "pv_set_batch_registry_unavailable",
                pv_name=item.pv_name,
                applied_before_halt=applied,
            )
            break

        try:
            resp = await device_controller.set_pv(item)
        except DeviceDisabledError as e:
            results.append(_batch_failure_result(item.pv_name, e, 409, coordination_checked=True))
            logger.warning(
                "pv_set_batch_device_disabled",
                pv_name=item.pv_name,
                applied_before_halt=applied,
            )
            break
        except DeviceLockedError as e:
            results.append(_batch_failure_result(item.pv_name, e, 423, coordination_checked=True))
            logger.warning(
                "pv_set_batch_device_locked",
                pv_name=item.pv_name,
                applied_before_halt=applied,
            )
            break
        except CoordinationCheckError as e:
            results.append(_batch_failure_result(item.pv_name, e, 503, coordination_checked=True))
            logger.error(
                "pv_set_batch_coordination_failed",
                pv_name=item.pv_name,
                applied_before_halt=applied,
                error=str(e),
            )
            break
        except Exception as e:
            # Past the coord gate — this is a real PV-health event
            # (typically a pyepics CA timeout or rejected put).
            pv_health_reporter.report(item.pv_name, success=False, message=str(e))
            results.append(_batch_failure_result(item.pv_name, e, 500, coordination_checked=True))
            logger.error(
                "pv_set_batch_item_error",
                pv_name=item.pv_name,
                applied_before_halt=applied,
                error=str(e),
                exc_info=True,
            )
            break

        pv_health_reporter.report(
            item.pv_name,
            success=resp.success,
            message=None if resp.success else resp.message,
        )
        results.append(
            PVSetBatchItemResult(
                pv_name=resp.pv_name,
                success=resp.success,
                value_set=resp.value_set,
                timestamp=resp.timestamp,
                coordination_checked=resp.coordination_checked,
                mode=resp.mode,
                message=resp.message,
                status_code=200,
            )
        )
        if not resp.success:
            # set_pv() returned success=False without raising — surface as
            # a halt rather than continuing into items that may depend on it.
            logger.warning(
                "pv_set_batch_item_returned_failure",
                pv_name=item.pv_name,
                applied_before_halt=applied,
                message=resp.message,
            )
            break
        applied += 1

    ok = applied == len(request.caputs)
    return PVSetBatchResponse(
        ok=ok,
        applied=applied,
        requested=len(request.caputs),
        results=results,
    )


# ophyd-async Signal.source URIs carry a backend scheme prefix; the
# downstream PV-write path expects bare PV strings. Listed schemes are
# the ones ophyd-async ships today; any new transport would need to be
# added here. (configuration_service uses a regex for the same purpose
# at a different boundary; the explicit list here makes the supported
# set visible at direct-control's edge.)
_SIGNAL_SOURCE_SCHEMES = ("ca://", "pva://", "mock://", "soft://")


def _extract_pv_name(leaf) -> Optional[str]:
    """Read the PV name off a leaf signal, framework-agnostic.

    Returns ``pvname`` for classic-ophyd ``EpicsSignal``-style leaves,
    the scheme-stripped ``source`` for ophyd-async ``Signal`` leaves, or
    ``None`` if the object exposes neither (i.e. not a PV-bearing leaf).
    """
    pvname = getattr(leaf, "pvname", None)
    if pvname:
        return pvname
    source = getattr(leaf, "source", None)
    if source:
        pv = str(source)
        for scheme in _SIGNAL_SOURCE_SCHEMES:
            if pv.startswith(scheme):
                return pv[len(scheme) :]
        return pv
    return None


def _enrich_one(
    cache: OphydDeviceCache,
    device_class_path: str,
    prefix: str,
    sub_path: str,
) -> EnrichmentResultItem:
    """Walk one ``(class, prefix, sub_path)`` to a leaf PV name.

    Live ophyd-cache access opens EPICS connections under the hood (the
    Component descriptor's lazy ``create_component`` calls
    ``wait_for_connection``). For an ophyd-async device the source URI
    appears in ``.source`` once the signal is constructed — no connection
    needed.
    """
    import operator

    cache_entry = cache.get_or_instantiate(device_class_path, prefix)
    if cache_entry.device is None:
        return EnrichmentResultItem(
            ok=False,
            error_type="InstantiationFailed",
            message=cache_entry.error,
        )

    device = cache_entry.device
    try:
        leaf = operator.attrgetter(sub_path)(device) if sub_path else device
    except AttributeError as e:
        return EnrichmentResultItem(
            ok=False,
            error_type="NoSuchAttr",
            message=f"walking {sub_path!r}: {e}",
        )
    except Exception as e:  # noqa: BLE001 — capture EPICS connect failures etc.
        return EnrichmentResultItem(
            ok=False,
            error_type=type(e).__name__,
            message=str(e),
        )

    pv_name = _extract_pv_name(leaf)
    if pv_name:
        return EnrichmentResultItem(ok=True, pv_name=pv_name)

    return EnrichmentResultItem(
        ok=False,
        error_type="NotAPVLeaf",
        message=(
            f"leaf at {sub_path!r} has no .pvname (classic ophyd) "
            f"or .source (ophyd-async); not a PV-bearing signal"
        ),
    )


@app.post("/api/v1/devices/enrich", response_model=EnrichmentResponse)
async def enrich_device_paths(
    request: EnrichmentRequest,
    ophyd_cache: OphydDeviceCache = Depends(get_ophyd_cache),
):
    """Resolve dotted device paths to PV names by live ophyd introspection.

    Designed for configuration_service to call when its static resolver
    can't fill in an ophyd ``FormattedComponent`` placeholder. For each
    ``{device_class_path, prefix, sub_path}`` spec, this endpoint
    instantiates the device class (cached after the first call) and
    walks the sub-path to the leaf signal, returning the underlying
    EPICS PV name.

    The endpoint is read-only — it does not write or subscribe — but
    instantiating classic-ophyd compound devices does open EPICS Channel
    Access connections (to fetch type/units/limits during the lazy
    ``Component`` materialization). Each first-touched device pays a
    ``wait_for_connection`` of up to a few hundred ms; subsequent items
    on the cached device are fast. We run the whole batch on a worker
    thread via ``asyncio.to_thread`` so the event loop stays responsive
    for other HTTP requests + WebSocket monitor traffic during the wait.
    Per-device serialization stays inside ``OphydDeviceCache``'s lock,
    so we don't introduce concurrent first-touches on the same device.
    Failures are returned per-item; the batch never halts on first error.
    """
    results: List[EnrichmentResultItem] = await asyncio.to_thread(
        lambda: [
            _enrich_one(
                ophyd_cache,
                item.device_class_path,
                item.prefix,
                item.sub_path,
            )
            for item in request.items
        ]
    )
    return EnrichmentResponse(results=results)


@app.get("/api/v1/pv/{pv_name}/value")
async def get_pv_value_from_controller(
    pv_name: str,
    request: Request,
    format: Optional[str] = Query(
        None,
        description="Override Accept header. 'json' or 'binary' (octet-stream).",
    ),
    as_string: bool = Query(
        False, description="Return the string representation (e.g. enum label)"
    ),
    count: Optional[int] = Query(None, ge=1, description="Max waveform elements to return"),
    as_numpy: bool = Query(
        True, description="Return arrays as numpy.ndarray (JSON-serialized to list)"
    ),
    use_monitor: bool = Query(
        False,
        description=(
            "Use cached monitor value. Default false matches the one-shot "
            "semantics of this endpoint; set true to share a monitor with "
            "any existing subscription (note: pyepics auto-installs a "
            "permanent CA monitor the first time this is true for a PV)."
        ),
    ),
    timeout: float = Query(5.0, gt=0, description="CA get timeout in seconds"),
    connection_timeout: float = Query(5.0, gt=0, description="CA connection timeout in seconds"),
    ftype: Optional[int] = Query(None, description="Force non-native DBR type (power user)"),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
    settings: Settings = Depends(get_settings),
):
    """
    One-shot CA get via DeviceController (no subscription).

    Exposes the pyepics caget / ca.get knobs as query params. Returns a
    tiled-style envelope: JSON by default with `shape`/`dtype`/`ndim`/
    `nbytes` alongside the value; `Accept: application/octet-stream` (or
    `?format=binary`) returns raw bytes with the same metadata in
    `X-PV-*` headers.
    """
    try:
        await registry_client.validate_pv(pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        value = await device_controller.get_pv_value(
            pv_name,
            as_string=as_string,
            count=count,
            as_numpy=as_numpy,
            use_monitor=use_monitor,
            timeout=timeout,
            connection_timeout=connection_timeout,
            ftype=ftype,
        )
    except PVNotFoundError as e:
        logger.warning("pv_not_found", pv_name=pv_name, error=str(e))
        raise HTTPException(status_code=404, detail=str(e))

    return _build_value_response(
        request,
        pv_name=pv_name,
        value=value,
        timestamp_iso=datetime.now().isoformat(),
        size_limit=settings.response_bytesize_limit,
        format_param=format,
    )


@app.get("/api/v1/pvs/{pv_name}/value")
async def get_monitored_pv_value(
    pv_name: str,
    request: Request,
    format: Optional[str] = Query(
        None,
        description="Override Accept header. 'json' or 'binary' (octet-stream).",
    ),
    pv_monitor: PVMonitor = Depends(get_pv_monitor),
    registry_client: RegistryClient = Depends(get_registry_client),
    settings: Settings = Depends(get_settings),
):
    """
    Get current value of a PV from the monitoring subscription cache.

    Subscribes to the PV if not already subscribed. Returns the same
    tiled-style envelope as the one-shot endpoint plus the monitor's
    full metadata (connected, alarm, limits, units, access flags).
    """
    try:
        await registry_client.validate_pv(pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        # subscribe is idempotent in PVMonitorManager; calling it unconditionally
        # avoids a TOCTOU gap and reads block briefly on EPICS, so run off-loop.
        await asyncio.to_thread(pv_monitor.subscribe, pv_name)

        pv_value = await asyncio.to_thread(pv_monitor.get_value, pv_name)
        if not pv_value:
            raise HTTPException(status_code=404, detail=f"PV {pv_name} not found")
    except HTTPException:
        raise
    except PVNotFoundError as e:
        logger.warning("pv_not_found", pv_name=pv_name, error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except PVReadError as e:
        logger.warning("pv_read_failed", pv_name=pv_name, error=str(e))
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("get_monitored_pv_error", pv_name=pv_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    # Everything in PVValue that isn't already in the envelope auto-propagates;
    # new metadata fields on PVValue will appear here without edits.
    extra = pv_value.model_dump(
        exclude={"pv_name", "value", "timestamp", "shape", "dtype", "ndim", "nbytes"},
        mode="json",
    )
    return _build_value_response(
        request,
        pv_name=pv_name,
        value=pv_value.value,
        timestamp_iso=pv_value.timestamp.isoformat(),
        size_limit=settings.response_bytesize_limit,
        format_param=format,
        shape=pv_value.shape,
        dtype=pv_value.dtype,
        ndim=pv_value.ndim,
        nbytes=pv_value.nbytes,
        extra=extra,
    )


@app.get("/api/v1/pvs/connected", response_model=list[str])
async def get_connected_pvs(pv_monitor: PVMonitor = Depends(get_pv_monitor)):
    """List PVs currently connected in the monitoring subsystem."""
    return pv_monitor.get_connected_pvs()


@app.post(
    "/api/v1/device/execute",
    response_model=DeviceCommandResponse,
    dependencies=[Depends(require_writable)],
)
async def execute_device_method(
    request: DeviceCommandRequest,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Execute Ophyd device method with coordination check (High Fidelity Channel).

    Always returns a confirmed result. Use when confirmation is required.
    Raises 404/423/503/500 on various failure modes.
    """
    try:
        await registry_client.validate_device(request.device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        return await device_controller.execute_device_method(request)
    except (DeviceDisabledError, DeviceLockedError) as e:
        _raise_http_for_device_unavailable(e, "device", device_name=request.device_name)
    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", device_name=request.device_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")
    except NotImplementedError as e:
        _raise_http_501_not_implemented(
            e, "device_method_not_implemented", device_name=request.device_name
        )
    except Exception as e:
        logger.error(
            "device_command_error",
            device_name=request.device_name,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/api/v1/device/{device_name}/stop",
    response_model=DeviceCommandResponse,
    dependencies=[Depends(require_writable)],
)
async def stop_device(
    device_name: str,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """Stop a device (calls the device's stop() method with coordination check)."""
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        return await device_controller.execute_device_method(
            DeviceCommandRequest(device_name=device_name, method="stop", args=[], kwargs={})
        )
    except (DeviceDisabledError, DeviceLockedError) as e:
        _raise_http_for_device_unavailable(e, "device_stop", device_name=device_name)
    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", device_name=device_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")
    except NotImplementedError as e:
        _raise_http_501_not_implemented(e, "device_stop_not_implemented", device_name=device_name)
    except Exception as e:
        logger.error("device_stop_error", device_name=device_name, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/device/{device_path:path}", response_model=NestedDeviceResponse)
async def access_nested_device(
    device_path: str,
    request: Optional[NestedDeviceRequest] = None,
    settings: Settings = Depends(get_settings),
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Access nested device component (e.g. motor1.user_readback).

    Reads are always allowed; write methods are coordination-checked and
    rejected with 403 in read-only mode. 404/423/503/500 on failure modes.
    """
    device_name = device_path.split(".")[0]

    method = request.method if request else "read"
    value = request.value if request else None
    timeout = request.timeout if request else None

    # Read-only gate: only the write methods are blocked; component reads stay
    # available for monitoring. (Mirrors device_controller's write-method set.)
    if settings.global_read_only and method in ("set", "put", "write", "trigger", "stop"):
        raise HTTPException(status_code=403, detail=READ_ONLY_MESSAGE)

    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        result = await device_controller.access_nested_device(
            device_path=device_path, method=method, value=value, timeout=timeout
        )
        return NestedDeviceResponse(
            device_path=device_path,
            method=method,
            success=True,
            result=result,
            timestamp=datetime.now(),
            message=None,
        )
    except (DeviceDisabledError, DeviceLockedError) as e:
        _raise_http_for_device_unavailable(e, "nested_device", device_path=device_path)
    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", device_path=device_path, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")
    except NotImplementedError as e:
        _raise_http_501_not_implemented(e, "nested_device_not_implemented", device_path=device_path)
    except Exception as e:
        logger.error("nested_device_error", device_path=device_path, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/device/{device_path:path}/value")
async def get_nested_device_value(
    device_path: str,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """Get nested device component value (read-only, no coordination check)."""
    device_name = device_path.split(".")[0]
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        value = await device_controller.access_nested_device(
            device_path=device_path, method="read", value=None, timeout=None
        )
        return {
            "device_path": device_path,
            "value": value,
            "timestamp": datetime.now().isoformat(),
        }
    except NotImplementedError as e:
        _raise_http_501_not_implemented(
            e, "nested_device_read_not_implemented", device_path=device_path
        )
    except Exception as e:
        logger.error("nested_device_read_error", device_path=device_path, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/api/v1/pv-socket")
async def websocket_pv_socket(websocket: WebSocket):
    """PV monitoring WebSocket — finch `ophydSocketPVPath`."""
    await app.state.ws_manager.handle_client(websocket)


@app.websocket("/api/v1/device-socket")
async def websocket_device_socket(websocket: WebSocket):
    """Device-level monitoring WebSocket — finch `ophydSocketDevicePath`."""
    await app.state.device_ws_manager.handle_client(websocket)


@app.websocket("/api/v1/camera-socket")
async def websocket_camera_socket(websocket: WebSocket):
    """AreaDetector image streaming WebSocket — finch `ophydSocketCameraPath`."""
    await app.state.camera_ws_manager.handle_client(websocket)


@app.websocket("/api/v1/tiff-socket")
async def websocket_tiff_socket(websocket: WebSocket):
    """TIFF-detector image streaming WebSocket — finch `ophydSocketTIFFPath`."""
    await app.state.tiff_ws_manager.handle_client(websocket)


def create_app() -> FastAPI:
    """Factory used by CLI with uvicorn factory=True."""
    return app
