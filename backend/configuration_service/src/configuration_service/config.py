"""
Configuration for Configuration Service (SVC-004).

Uses pydantic-settings for environment-based configuration.
"""

from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings.

    Loaded from environment variables with CONFIG_ prefix.

    Profile Collection Integration:
        The service loads device registries from beamline profile collections:
        1. happi: Parse happi_db.json
        2. bits: Parse devices.yml + iconfig.yml
        3. mock: Use mock data for testing without profile collection

        For profiles that use startup scripts (IPython-style), devices
        should be registered via the CRUD API — typically by the
        Experiment Execution Service (SVC-001) at startup.

    Set CONFIG_PROFILE_PATH environment variable to point to
    the profile collection directory (e.g., /opt/bluesky/profile_collection).
    """

    # Service identification
    service_name: str = "configuration_service"
    service_id: str = "SVC-004"

    # Profile collection configuration
    # Can be set via CONFIG_PROFILE_PATH
    profile_path: Optional[Path] = None

    # Loading strategy: "auto", "empty", "happi", "bits", or "mock"
    # auto: Auto-detect based on files present in profile_path (default)
    # empty: Start with zero devices (populated via CRUD API by EE service)
    # happi: Parse happi_db.json (LCLS/SLAC format)
    # bits: Parse devices.yml + iconfig.yml (BCDA-APS format)
    # mock: Use mock data for testing
    load_strategy: str = "auto"

    # Shortcut: if True, overrides load_strategy to "mock"
    use_mock_data: bool = False

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8004

    # Logging
    log_level: str = "INFO"

    # CORS (if needed for web UI)
    cors_origins: list[str] = ["*"]

    # Metrics
    metrics_enabled: bool = True
    metrics_port: int = 9004

    # Database connection for persistent stores (device registry + audit log,
    # standalone PVs). SQLAlchemy DSN; the backend is chosen by the scheme:
    #   postgresql+psycopg://user:pass@host:5432/config_service   (production)
    #   sqlite+pysqlite:////var/lib/config_service/config.db       (single-node/dev)
    # Required when device_change_history_enabled is True (the default); startup
    # fails hard if it is unset in that case.
    database_url: str = ""

    # Enable runtime device change history (CRUD endpoints). When True (default),
    # configuration_service persists to the database_url backend. When False,
    # the registry is loaded from the profile on every startup with no DB.
    device_change_history_enabled: bool = True

    # "lock_all" availability policy. When True, the moment ANY device lock is
    # held (i.e. a plan is running), EVERY registered device reports
    # locked/unavailable to direct-control — not just the devices the plan
    # named. Lock acquisition/release semantics are unchanged; only how
    # availability is derived from lock state changes. This setting is the
    # boot default; the policy is runtime-changeable via
    # GET/PUT /api/v1/devices/lock/policy. Standalone PVs (no owning device)
    # have no device-level lock concept and are not affected.
    lock_all: bool = False

    # Live-enrichment fallback for the path resolver.
    # When the resolver returns ``needs_enrichment`` (typically a classic
    # ophyd FormattedComponent with a {placeholder}), configuration_service
    # asks direct-control to instantiate the device and read the real PV
    # name. If unset, enrichment is disabled and those paths stay as
    # ``needs_enrichment`` outcomes (matching pre-feature behavior).
    #
    # Default timeout 30s budgets for cold-cache batches: per-device
    # first-touch ≈ 200-500ms (pyepics wait_for_connection), so a 20-
    # unique-device batch can run several seconds on the first hit.
    # After warm-up the cache amortizes it; size to your expected
    # cold batch (unique_devices * 0.5s, with headroom).
    direct_control_url: Optional[str] = None
    direct_control_timeout: float = 30.0

    model_config = SettingsConfigDict(
        env_prefix="CONFIG_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def effective_strategy(self) -> str:
        """Resolved load strategy, accounting for the use_mock_data shortcut."""
        return "mock" if self.use_mock_data else self.load_strategy
