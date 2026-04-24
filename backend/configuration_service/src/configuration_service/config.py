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

    # SQLite database for persistent stores (device change history, standalone PVs)
    db_path: Path = Path("/var/lib/bluesky/config_service.db")

    # Enable runtime device change history (CRUD endpoints)
    device_change_history_enabled: bool = True

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

