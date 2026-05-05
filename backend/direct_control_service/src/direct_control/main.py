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
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ._array_metadata import describe_array
from .config import Settings
from .coordination_client import CoordinationClient
from .device_controller import DeviceController
from .models import (
    CoordinationCheckError,
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDisabledError,
    DeviceLockedError,
    HealthResponse,
    NestedDeviceRequest,
    NestedDeviceResponse,
    PVNotFoundError,
    PVReadError,
    PVSetRequest,
    PVSetResponse,
    PVValue,
)
from .protocols import CoordinationService, DeviceControl, PVMonitor
from .registry_client import RegistryClient, RegistryValidationError

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
        logger.error(
            "openapi_schema_export_failed", path=path, error=str(exc), exc_info=True
        )
        raise RuntimeError(
            f"OpenAPI schema export to {path} failed: {exc}. "
            f"Unset {_OPENAPI_EXPORT_PATH_ENV} to skip export."
        ) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize clients and managers on startup, clean up on shutdown."""
    logger.info("Starting Direct Device Control + Monitoring Service")

    _maybe_export_openapi(app)

    settings = Settings()

    # Import pyepics-dependent managers after env vars are in place.
    from .monitoring.device_websocket_manager import DeviceWebSocketManager
    from .monitoring.pv_monitor import PVMonitorManager
    from .monitoring.websocket_manager import WebSocketManager

    coordination_client = CoordinationClient(settings)
    registry_client = RegistryClient(settings)
    device_controller = DeviceController(settings, coordination_client, registry_client)
    config_http = httpx.AsyncClient(base_url=settings.configuration_service_url, timeout=10.0)
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

    app.state.settings = settings
    app.state.coordination_client = coordination_client
    app.state.device_controller = device_controller
    app.state.registry_client = registry_client
    app.state.config_http = config_http
    app.state.pv_monitor = pv_monitor
    app.state.ws_manager = ws_manager
    app.state.device_ws_manager = device_ws_manager

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
        await ws_manager.close_all()
        await device_ws_manager.cleanup()
        await coordination_client.cleanup()
        await registry_client.cleanup()
        await config_http.aclose()
        await pv_monitor.cleanup()
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


def get_config_http() -> httpx.AsyncClient:
    return app.state.config_http


def get_ws_manager():
    return app.state.ws_manager


def get_device_ws_manager():
    return app.state.device_ws_manager


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
    coordination_client: CoordinationService = Depends(get_coordination_client),
    pv_monitor: PVMonitor = Depends(get_pv_monitor),
    ws_manager=Depends(get_ws_manager),
):
    """Combined health check: coordination availability and monitoring stats.

    Returns 503 when configuration_service is unreachable so LB readiness
    probes can route away. ``coordination_service_detail`` carries the
    structured reason (timeout / connect-refused / non-2xx).
    """
    coord = await coordination_client.is_service_available()
    stats = ws_manager.get_stats()

    body = HealthResponse(
        status="healthy" if coord.available else "unhealthy",
        timestamp=datetime.now(),
        coordination_service_available=coord.available,
        coordination_service_detail=coord.detail,
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
):
    coord = await coordination_client.is_service_available()
    pv_stats = ws_manager.get_stats()
    device_stats = device_ws_manager.get_stats()

    return {
        "service": "direct_control",
        "timestamp": datetime.now().isoformat(),
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


@app.post("/api/v1/pv/set", response_model=PVSetResponse)
async def set_pv(
    request: PVSetRequest,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Set EPICS PV value with coordination check (Low Fidelity Channel).

    Two modes:
    - wait=False (fire-and-forget, default): Issues write, returns immediately.
    - wait=True (put-completion): Waits for EPICS put-completion callback.

    Raises 404 if PV not in registry, 423 if device locked, 503 if
    coordination service unavailable.
    """
    try:
        await registry_client.validate_pv(request.pv_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        return await device_controller.set_pv(request)
    except (DeviceDisabledError, DeviceLockedError) as e:
        _raise_http_for_device_unavailable(e, "pv", pv_name=request.pv_name)
    except CoordinationCheckError as e:
        logger.error("coordination_check_failed", pv_name=request.pv_name, error=str(e))
        raise HTTPException(status_code=503, detail=f"Coordination check failed: {e}")
    except Exception as e:
        logger.error("set_pv_error", pv_name=request.pv_name, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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


@app.post("/api/v1/device/execute", response_model=DeviceCommandResponse)
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


@app.post("/api/v1/device/{device_name}/stop", response_model=DeviceCommandResponse)
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


async def _config_get(client: httpx.AsyncClient, path: str, *, not_found_msg: str) -> Any:
    """GET from configuration service, translating status codes to HTTPExceptions."""
    try:
        response = await client.get(path)
    except httpx.RequestError as e:
        logger.error("config_service_fetch_error", path=path, error=str(e))
        raise HTTPException(status_code=503, detail=f"Configuration service unavailable: {e}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=not_found_msg)
    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail="Failed to fetch from configuration service",
        )
    return response.json()


@app.get("/api/v1/devices")
async def list_devices(
    client: httpx.AsyncClient = Depends(get_config_http),
    device_class: Optional[str] = Query(None, description="Filter by ophyd class name"),
    readable: Optional[bool] = Query(None, description="Filter by Readable"),
    movable: Optional[bool] = Query(None, description="Filter by Movable"),
    flyable: Optional[bool] = Query(None, description="Filter by Flyable"),
):
    """List available devices (proxied from configuration service)."""
    devices = await _config_get(
        client, "/api/v1/devices", not_found_msg="Devices endpoint not found"
    )

    if device_class:
        devices = [
            d
            for d in devices
            if d.get("ophyd_class") == device_class or d.get("class") == device_class
        ]
    if readable is not None:
        devices = [d for d in devices if d.get("is_readable", True) == readable]
    if movable is not None:
        devices = [d for d in devices if d.get("is_movable", False) == movable]
    if flyable is not None:
        devices = [d for d in devices if d.get("is_flyable", False) == flyable]
    return devices


@app.get("/api/v1/devices/{device_name}")
async def get_device(
    device_name: str,
    client: httpx.AsyncClient = Depends(get_config_http),
):
    """Get device metadata (proxied from configuration service)."""
    return await _config_get(
        client,
        f"/api/v1/devices/{device_name}",
        not_found_msg=f"Device not found: {device_name}",
    )


@app.get("/api/v1/devices/{device_name}/bundle")
async def get_device_bundle(
    device_name: str,
    client: httpx.AsyncClient = Depends(get_config_http),
):
    """Get hierarchical device component tree for building control UIs."""
    device_data = await _config_get(
        client,
        f"/api/v1/devices/{device_name}",
        not_found_msg=f"Device not found: {device_name}",
    )
    pvs = device_data.get("pvs", {})
    return {
        "name": device_name,
        "class": device_data.get("ophyd_class", device_data.get("device_type", "unknown")),
        "prefix": device_data.get("prefix"),
        "is_readable": device_data.get("is_readable", True),
        "is_movable": device_data.get("is_movable", False),
        "components": _build_component_tree(pvs),
        "total_signals": len(pvs),
    }


def _build_component_tree(pvs: Dict[str, str]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}

    for component_path, pv_name in pvs.items():
        parts = component_path.split(".")
        top_level = parts[0] if parts else component_path
        groups.setdefault(top_level, []).append(
            {
                "name": component_path,
                "attr": parts[-1] if parts else component_path,
                "pv": pv_name,
                "type": "signal",
                "read_only": any(
                    ro in pv_name.upper() for ro in ["RBV", "READBACK", "STAT", "_RBK", "_MON"]
                ),
            }
        )

    components = []
    for group_name, signals in groups.items():
        if len(signals) == 1 and signals[0]["attr"] == group_name:
            components.append(signals[0])
        else:
            components.append(
                {
                    "name": group_name,
                    "attr": group_name,
                    "type": "device",
                    "components": signals,
                }
            )
    return components


@app.post("/api/v1/device/{device_path:path}", response_model=NestedDeviceResponse)
async def access_nested_device(
    device_path: str,
    request: Optional[NestedDeviceRequest] = None,
    device_controller: DeviceControl = Depends(get_device_controller),
    registry_client: RegistryClient = Depends(get_registry_client),
):
    """
    Access nested device component (e.g. motor1.user_readback).

    Coordination-checked for writes; 404/423/503/500 on failure modes.
    """
    device_name = device_path.split(".")[0]
    try:
        await registry_client.validate_device(device_name)
    except RegistryValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    method = request.method if request else "read"
    value = request.value if request else None
    timeout = request.timeout if request else None

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
        _raise_http_501_not_implemented(
            e, "nested_device_not_implemented", device_path=device_path
        )
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


def create_app() -> FastAPI:
    """Factory used by CLI with uvicorn factory=True."""
    return app
