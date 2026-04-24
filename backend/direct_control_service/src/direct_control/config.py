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

    # Service dependencies
    experiment_execution_url: str = "http://localhost:8001"
    configuration_service_url: str = "http://localhost:8004"

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

    # Max PV (pv-socket) or device (device-socket) subscriptions per WS client.
    # Protects the service and upstream IOCs from runaway subscribe storms.
    max_subscriptions_per_client: int = 1000

    # PV buffering
    pv_buffer_size: int = 100
    pv_update_rate_limit: float = 0.1

    # Maximum response bytesize for PV value endpoints (covers binary + JSON).
    # Oversized arrays return 400 with a "slice or raise the limit" message.
    response_bytesize_limit: int = 100_000_000  # 100 MB

    # Observability
    enable_metrics: bool = True
    metrics_port: int = 9003
    enable_tracing: bool = False
