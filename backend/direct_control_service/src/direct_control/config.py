"""
Configuration settings for Direct Device Control Service.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Required: no default so a forgotten DIRECT_CONTROL_CONFIGURATION_SERVICE_URL
    # fails at startup instead of silently pointing at localhost:8004.
    configuration_service_url: str

    # EPICS configuration
    epics_ca_addr_list: Optional[str] = None
    epics_ca_auto_addr_list: bool = True
    epics_ca_max_array_bytes: int = 1000000

    # Coordination settings
    coordination_check_enabled: bool = True
    coordination_timeout: float = 5.0

    # Command timeout
    command_timeout: float = 30.0

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
    pv_update_rate_limit: float = 0.1

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
