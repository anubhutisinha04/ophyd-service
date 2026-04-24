"""
Registry validation client for Configuration Service.

Validates that PV and device names exist in the Configuration Service registry
before allowing operations. Uses a TTL cache to avoid per-request HTTP round-trips.
"""

import time
from typing import Dict, Optional, Tuple

import httpx
import structlog

from .config import Settings


logger = structlog.get_logger(__name__)


class RegistryValidationError(Exception):
    """Raised when a PV or device is not found in the Configuration Service registry."""

    def __init__(self, name: str, resource_type: str = "resource"):
        self.name = name
        self.resource_type = resource_type
        super().__init__(
            f"{resource_type.upper()} '{name}' not found in Configuration Service registry"
        )


class RegistryClient:
    """
    HTTP client for validating PV/device existence against Configuration Service.

    Every PV/device operation must confirm the target exists in the
    authoritative registry before reaching EPICS.

    Uses a TTL cache (30s default) to avoid per-request HTTP round-trips.
    """

    def __init__(self, settings: Settings, cache_ttl: float = 30.0):
        self.base_url = settings.configuration_service_url
        self._client: Optional[httpx.AsyncClient] = None
        self._cache_ttl = cache_ttl
        # Cache: key -> (exists: bool, timestamp: float)
        self._pv_cache: Dict[str, Tuple[bool, float]] = {}
        self._device_cache: Dict[str, Tuple[bool, float]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=5.0,
            )
        return self._client

    def _cache_get(self, cache: Dict[str, Tuple[bool, float]], key: str) -> Optional[bool]:
        """Check cache for a key, return None if expired or missing."""
        entry = cache.get(key)
        if entry is None:
            return None
        exists, ts = entry
        if time.monotonic() - ts > self._cache_ttl:
            del cache[key]
            return None
        return exists

    async def validate_pv(self, pv_name: str) -> None:
        """
        Validate that a PV exists in the Configuration Service registry.

        Args:
            pv_name: EPICS PV name to validate

        Raises:
            RegistryValidationError: If PV not found in registry
        """
        cached = self._cache_get(self._pv_cache, pv_name)
        if cached is True:
            return
        if cached is False:
            raise RegistryValidationError(pv_name, "PV")

        try:
            client = await self._get_client()
            response = await client.get(f"/api/v1/pvs/{pv_name}")

            if response.status_code == 200:
                self._pv_cache[pv_name] = (True, time.monotonic())
                return
            elif response.status_code == 404:
                self._pv_cache[pv_name] = (False, time.monotonic())
                raise RegistryValidationError(pv_name, "PV")
            else:
                logger.warning(
                    "registry_pv_check_unexpected_status",
                    pv_name=pv_name,
                    status_code=response.status_code,
                )
                raise RegistryValidationError(pv_name, "PV")

        except httpx.RequestError as e:
            logger.error("configuration_service_unavailable", error=str(e))
            raise RuntimeError("Configuration service unavailable") from e

    async def validate_device(self, device_name: str) -> None:
        """
        Validate that a device exists in the Configuration Service registry.

        Args:
            device_name: Device name to validate

        Raises:
            RegistryValidationError: If device not found in registry
        """
        cached = self._cache_get(self._device_cache, device_name)
        if cached is True:
            return
        if cached is False:
            raise RegistryValidationError(device_name, "Device")

        try:
            client = await self._get_client()
            response = await client.get(f"/api/v1/devices/{device_name}")

            if response.status_code == 200:
                self._device_cache[device_name] = (True, time.monotonic())
                return
            elif response.status_code == 404:
                self._device_cache[device_name] = (False, time.monotonic())
                raise RegistryValidationError(device_name, "Device")
            else:
                logger.warning(
                    "registry_device_check_unexpected_status",
                    device_name=device_name,
                    status_code=response.status_code,
                )
                raise RegistryValidationError(device_name, "Device")

        except httpx.RequestError as e:
            logger.error("configuration_service_unavailable", error=str(e))
            raise RuntimeError("Configuration service unavailable") from e

    async def cleanup(self):
        """Cleanup HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
