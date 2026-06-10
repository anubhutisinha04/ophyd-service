"""
Registry validation client for Configuration Service.

Validates that PV and device names exist in the Configuration Service registry
before allowing operations. Uses a TTL cache to avoid per-request HTTP round-trips.
"""

import time
from typing import Dict, Optional, Tuple

import httpx
import structlog
from pydantic import ValidationError

from .config import Settings
from .models import InstantiationSpec

logger = structlog.get_logger(__name__)


class RegistryValidationError(Exception):
    """Raised when a PV or device is not found in the device/PV registry.

    Backend-neutral: the registry may be configuration_service (http backend)
    or a local file (file backend), so the message does not name a specific
    source.
    """

    def __init__(self, name: str, resource_type: str = "resource"):
        self.name = name
        self.resource_type = resource_type
        super().__init__(f"{resource_type.upper()} '{name}' not found in the registry")


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
        # PV -> owning device name (None for standalone PVs). Populated as a
        # side effect of validate_pv so the disabled-state gate can map a
        # PV-keyed write to the device-keyed lock/disable state on
        # configuration_service without an extra round-trip.
        self._pv_owner_cache: Dict[str, Tuple[Optional[str], float]] = {}
        # device -> instantiation spec (None = device has no spec). Specs
        # change rarely; the TTL bounds how long a registry edit takes to
        # reach the DeviceManager (which also rebuilds on spec change).
        self._spec_cache: Dict[str, Tuple[Optional[InstantiationSpec], float]] = {}

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
                now = time.monotonic()
                self._pv_cache[pv_name] = (True, now)
                # Capture the owning device for the disabled-state gate.
                try:
                    self._pv_owner_cache[pv_name] = (
                        response.json().get("device_name"),
                        now,
                    )
                except Exception as e:  # noqa: BLE001
                    # Body-parse failure here is unusual (configuration_service
                    # returned 200 but a malformed body). Validation itself
                    # still succeeds — get_owning_device will refetch on next
                    # call. Warn (not debug) so a persistent issue is visible.
                    logger.warning(
                        "pv_owner_capture_failed",
                        pv_name=pv_name,
                        error=str(e),
                    )
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

    async def get_owning_device(self, pv_name: str) -> Optional[str]:
        """Return the device that owns this PV in the registry, or None for
        standalone PVs (PVs with no device-level lock/disable state).

        Used by the coordination check on PV-keyed writes: a PV's commandability
        is gated by its owning device's enabled/locked state, not by the PV
        itself. Standalone PVs have no such gate.
        """
        entry = self._pv_owner_cache.get(pv_name)
        if entry is not None and time.monotonic() - entry[1] <= self._cache_ttl:
            return entry[0]

        client = await self._get_client()
        try:
            response = await client.get(f"/api/v1/pvs/{pv_name}")
        except httpx.RequestError as e:
            logger.error("configuration_service_unavailable", error=str(e))
            raise RuntimeError("Configuration service unavailable") from e

        if response.status_code != 200:
            # PV not in registry — caller will hit the validate_pv gate
            # separately. Don't cache as None here: that would shadow a real
            # owner once the PV gets registered.
            return None
        device_name: Optional[str] = response.json().get("device_name")
        self._pv_owner_cache[pv_name] = (device_name, time.monotonic())
        return device_name

    async def get_instantiation_spec(self, device_name: str) -> Optional[InstantiationSpec]:
        """Fetch the device's instantiation spec from configuration_service.

        Returns None when the spec endpoint 404s. Callers run
        ``validate_device`` first, so a 404 here means "device exists but has
        no instantiation spec" — device-level control is unavailable for it.

        Raises:
            RuntimeError: configuration_service unreachable, returned an
                unexpected status, or returned a malformed spec body.
        """
        entry = self._spec_cache.get(device_name)
        if entry is not None and time.monotonic() - entry[1] <= self._cache_ttl:
            return entry[0]

        client = await self._get_client()
        try:
            response = await client.get(f"/api/v1/devices/{device_name}/instantiation")
        except httpx.RequestError as e:
            logger.error("configuration_service_unavailable", error=str(e))
            raise RuntimeError("Configuration service unavailable") from e

        if response.status_code == 404:
            self._spec_cache[device_name] = (None, time.monotonic())
            return None
        if response.status_code != 200:
            # Don't cache: an unexpected status (5xx, auth proxy hiccup) must
            # not masquerade as "no spec" for the next TTL window.
            logger.warning(
                "registry_spec_unexpected_status",
                device_name=device_name,
                status_code=response.status_code,
            )
            raise RuntimeError(
                f"Instantiation-spec lookup for {device_name!r} returned "
                f"HTTP {response.status_code}"
            )

        try:
            spec = InstantiationSpec.model_validate(response.json())
        except (ValidationError, ValueError) as e:
            logger.error(
                "registry_spec_malformed",
                device_name=device_name,
                error=str(e),
            )
            raise RuntimeError(f"Instantiation spec for {device_name!r} is malformed: {e}") from e
        self._spec_cache[device_name] = (spec, time.monotonic())
        return spec

    async def cleanup(self) -> None:
        """Cleanup HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
