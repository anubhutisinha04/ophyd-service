"""
Configuration settings for Direct Device Control Service.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Shared 403/WS-error detail for read-only mode. Defined here (not in main) so
# both the REST layer and the WebSocket managers use the identical message
# without a circular import.
READ_ONLY_MESSAGE = (
    "Service is in read-only mode (DIRECT_CONTROL_GLOBAL_READ_ONLY=true); "
    "control/write operations are disabled. Monitoring (reads, subscriptions, "
    "image sockets) remains available."
)


class Settings(BaseSettings):
    """
    Configuration for the merged Direct Device Control + Device Monitoring service.

    Settings can be overridden via environment variables with the
    DIRECT_CONTROL_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="DIRECT_CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Service configuration
    host: str = "0.0.0.0"
    port: int = 8003
    log_level: str = "info"

    # Service dependencies. configuration_service is the only HTTP backend
    # direct_control talks to: it owns the device registry, the per-PV
    # validation gate, AND the device-lock state that the coordination
    # check reads (EE/queueserver writes the locks; we read them).
    # Required for the http/auto registry backends — the validator below
    # fails startup if it is unset there, instead of silently pointing at
    # localhost:8004. Optional ONLY in standalone mode (registry_backend=
    # file), which has no configuration_service at all.
    configuration_service_url: str | None = None

    # Registry backend:
    #   "http" (default) — validate PV/device existence against
    #     configuration_service over HTTP.
    #   "file" — read a static device/PV registry from a local JSON/YAML file.
    #     A complete, first-class standalone mode: the service is fully featured
    #     (control + monitor) using the file as the registry in place of
    #     configuration_service. There is no config-service in this mode, so the
    #     device-lock coordination check (which reads lock state from
    #     configuration_service) is inherently not applicable and is turned off
    #     automatically — access is instead governed by global_read_only.
    #   "auto" — prefer configuration_service; if it is unreachable at startup
    #     and registry_file_path is set, run fully featured on the file registry
    #     (same standalone mode as "file"). If config-service is down and no file
    #     is configured, fail to start. The chosen backend is logged and exposed
    #     via /health and /api/v1/stats, so the switch is never silent.
    # The file carries ONLY the static registry (what devices/PVs exist); it
    # cannot carry device-lock coordination state (runtime, shared, mutable),
    # which is why file/standalone mode runs without that check.
    registry_backend: str = "http"  # http | file | auto
    # Path to the JSON/YAML registry file. Required when registry_backend=file;
    # optional for "auto" (no file => auto is http-or-fail).
    registry_file_path: str | None = None

    # Deployment-wide control switch. When true (the DEFAULT), the service is
    # MONITOR-ONLY: every control/write operation (PV set, batch set, device
    # execute/stop) is rejected with HTTP 403 over BOTH REST and WebSocket,
    # while reads, subscriptions, and image (camera/tiff) sockets stay open.
    # Safe-by-default: a fresh/unconfigured deployment cannot move hardware — an
    # operator must explicitly set DIRECT_CONTROL_GLOBAL_READ_ONLY=false to
    # enable full control. Orthogonal to the device-lock coordination check:
    # this is a static deployment-level gate, locks are per-device and runtime.
    global_read_only: bool = True

    # EPICS configuration
    epics_ca_addr_list: str | None = None
    epics_ca_auto_addr_list: bool = True
    epics_ca_max_array_bytes: int = 1000000

    # Coordination settings
    coordination_check_enabled: bool = True
    coordination_timeout: float = 5.0

    # Startup readiness probe against configuration_service. Because every
    # registry-validated read/write and the device-lock coordination gate
    # depend on configuration_service, a misconfigured or not-yet-started
    # config-service is otherwise invisible at boot and only surfaces later as
    # per-request 503s. With the probe enabled (default), startup blocks until
    # config-service answers /health, retrying for up to
    # config_service_startup_timeout seconds (absorbs compose/k8s start
    # ordering) and then FAILS HARD rather than serving in a half-broken state.
    # Set false only for monitoring-only deployments that never touch the
    # registry (raw pv-socket / image sockets), or for tests.
    config_service_startup_probe: bool = True
    config_service_startup_timeout: float = 60.0
    config_service_startup_probe_interval: float = 2.0

    # Command timeout
    command_timeout: float = 30.0

    # When an awaited device method (set/trigger/...) exceeds its timeout, issue
    # stop() on the target to halt hardware that is likely still in motion. On by
    # default for beamline safety; set false to opt out (the timeout is still
    # reported, with a note that the device may still be moving).
    stop_on_command_timeout: bool = True

    # Ctrl-limit safety gate on PV writes: when true, ``set_pv`` reads the
    # target's ``lower_ctrl_limit`` / ``upper_ctrl_limit`` (LOPR/HOPR on the
    # IOC record) and refuses writes outside that range with ValueLimitError
    # (HTTP 422). Skipped for records with no limits declared (or the EPICS
    # "unlimited" convention of both bounds equal to 0), non-numeric values,
    # and any write that opts out via ``PVSetRequest.check_limits=False``.
    # Costs one CA metadata round-trip per write (``get_ctrlvars`` is
    # re-issued on every set_pv, not cached — LOPR/HOPR can change on the
    # IOC, and caching them would let the gate go stale). pyepics does
    # cache the PV object itself so the CA channel is reused across writes,
    # only the DBR_CTRL_* fetch is repeated. Turn off for setups where the
    # IOC advertises misleading LOPR/HOPR (miscalibrated records, or DAQ
    # PVs where the operator is authoritative rather than the record).
    check_ctrl_limits: bool = True
    # How long ``set_pv`` waits for the ctrl-limit metadata to arrive from the
    # IOC before proceeding with the write (fail-open on timeout so a slow
    # metadata channel doesn't block a legitimate write). The pyepics default
    # of 5 s is aggressive for a background metadata GET; keep it short so a
    # dead IOC surfaces via the connection-timeout path in ``_execute_put``
    # rather than this one.
    ctrl_limit_read_timeout: float = 1.0

    # Connection timeout for instantiating a live device for device-level
    # control (DeviceManager). Covers classic-ophyd wait_for_connection and
    # ophyd-async Device.connect across all the device's signals.
    device_connect_timeout: float = 10.0

    # WebSocket configuration
    ws_max_connections: int = 100
    ws_heartbeat_interval: int = 30
    ws_message_queue_size: int = 1000
    # Per-message send timeout. A slow/stuck client cannot stall broadcasts
    # to its peers — once exceeded, the update is dropped for that client
    # and logged.
    ws_send_timeout: float = 5.0

    # Max PV (pv-socket) or device (device-socket) subscriptions per WS client.
    # Protects the service and upstream IOCs from runaway subscribe storms.
    max_subscriptions_per_client: int = 1000

    # PV buffering
    pv_buffer_size: int = 100

    # TTL eviction for callback-less PV monitors created by the REST
    # ``GET /api/v1/pvs/{pv_name}/value`` endpoint. That call subscribes a
    # PV to warm the monitor cache, returns the value, and never calls
    # unsubscribe — the CA connection would otherwise live until process
    # shutdown. A background sweep every ``pv_monitor_sweep_interval``
    # seconds tears down any PV that has zero WS callbacks AND has not been
    # subscribed / read (touched) within ``pv_monitor_idle_ttl``. WS
    # subscribers keep their PVs alive regardless of idle time (any
    # ``_callbacks`` entry pins the monitor). Set the interval to 0 to
    # disable the sweep entirely (useful in tests where deterministic
    # eviction is preferred). Both fields are validated non-negative at
    # startup so a misconfigured negative value fails hard instead of
    # silently disabling the sweep (interval < 0) or evicting every
    # callback-less monitor on the first cycle (ttl < 0).
    pv_monitor_idle_ttl: float = Field(300.0, ge=0.0)
    pv_monitor_sweep_interval: float = Field(60.0, ge=0.0)

    # Maximum response bytesize for PV value endpoints (covers binary + JSON).
    # Oversized arrays return 400 with a "slice or raise the limit" message.
    response_bytesize_limit: int = 100_000_000  # 100 MB

    # Image streaming (camera-socket / tiff-socket).
    # Per-connection frame queue. The image-array CA callback pushes RAW (un-
    # encoded) NDArray frames here; the streaming loop drains + encodes.
    # Drop-oldest on overflow so a fast detector can't outrun a slow client.
    # Keep this small: each queued frame is a full raw image (megabytes at
    # detector sizes), and for a live view a deep queue only adds latency by
    # showing stale frames. A handful is enough to absorb bursts.
    image_frame_queue_size: int = 8
    # Downsample any frame wider/taller than this (LANCZOS) before encoding,
    # to cap per-frame wire size and browser decode cost.
    image_max_dimension: int = 2500
    # Wire encoding for image frames. The ImageEncoder Protocol makes this
    # pluggable, but the consumer (finch) currently only decodes JPEG reliably
    # via createImageBitmap — keep "jpeg" unless the frontend negotiates more.
    image_encoding: str = "jpeg"  # jpeg | png | webp
    image_jpeg_quality: int = 100
    # Default normalization on connect; client can flip via toggleLogNormalization.
    image_log_normalization_default: bool = True
    # PV resolution defaults. camera-socket falls back to the ADSimDetector
    # image array; tiff-socket expands a bare {prefix} to {prefix}:image1:ArrayData
    # plus {prefix}:cam1:* settings (same suffixes as camera).
    camera_default_image_array_pv: str = "13SIM1:image1:ArrayData"
    tiff_default_prefix: str = "13PIL1"

    # Observability
    enable_metrics: bool = True
    metrics_port: int = 9003
    enable_tracing: bool = False

    @model_validator(mode="after")
    def _validate_registry_backend(self) -> "Settings":
        if self.registry_backend not in ("http", "file", "auto"):
            raise ValueError(
                f"registry_backend must be 'http', 'file', or 'auto', got {self.registry_backend!r}"
            )
        if self.registry_backend == "file" and not self.registry_file_path:
            raise ValueError("registry_backend='file' requires DIRECT_CONTROL_REGISTRY_FILE_PATH")
        if self.registry_backend != "file" and not self.configuration_service_url:
            raise ValueError(
                f"registry_backend={self.registry_backend!r} requires "
                f"DIRECT_CONTROL_CONFIGURATION_SERVICE_URL (configuration_service is "
                f"the registry + device-lock authority). Only standalone mode "
                f"(DIRECT_CONTROL_REGISTRY_BACKEND=file) runs without it."
            )
        return self


class _CorsSettings(BaseSettings):
    """CORS configuration, read at import time for the module-level app.

    Kept separate from ``Settings`` because the main settings object (with its
    registry-backend validators and EPICS-env coupling) is intentionally
    deferred to the app lifespan, whereas the CORS policy must be known when the
    module-level ``CORSMiddleware`` is added at import. Same ``DIRECT_CONTROL_``
    env prefix:
      - DIRECT_CONTROL_CORS_ORIGINS — JSON list, e.g. '["https://ui.example"]'
        (defaults to '["*"]' for the dev inner-loop)
      - DIRECT_CONTROL_CORS_ALLOW_CREDENTIALS — bool (default false)
    """

    model_config = SettingsConfigDict(
        env_prefix="DIRECT_CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    cors_origins: list[str] = ["*"]
    cors_allow_credentials: bool = False


def resolve_cors_config() -> tuple[list[str], bool]:
    """Return ``(allow_origins, allow_credentials)`` for the CORS middleware.

    Credentials are force-disabled whenever a wildcard origin is configured:
    ``allow_origins=["*"]`` + ``allow_credentials=True`` makes Starlette reflect
    the caller's Origin and echo ``Access-Control-Allow-Credentials``, which lets
    any site drive credentialed cross-origin requests. Auth here is upstream via
    bearer headers (not cookies), so credentials stay off unless an explicit
    origin allowlist is set.
    """
    cfg = _CorsSettings()
    allow_credentials = cfg.cors_allow_credentials
    if allow_credentials and "*" in cfg.cors_origins:
        allow_credentials = False
    return cfg.cors_origins, allow_credentials
