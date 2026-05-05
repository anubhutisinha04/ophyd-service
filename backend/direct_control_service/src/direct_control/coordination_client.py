"""
Coordination client — mediates device-lock checks via configuration_service.

direct_control never talks to EE/queueserver directly. Lock state lives in
configuration_service (the device registry); this client is the read side
of that contract:

    EE / queueserver  --POST /api/v1/devices/lock-->  configuration_service
    direct_control    --GET  /api/v1/devices/{name}/status-->  configuration_service

See `feedback_direct_control_no_ee_polling` memory for the architectural
intent.
"""

import httpx
import structlog
from datetime import datetime
from typing import Optional

from .models import (
    CoordinationCheckError,
    CoordinationStatus,
    DeviceLockStatus,
    ServiceAvailability,
)
from .config import Settings


logger = structlog.get_logger(__name__)


def _map_lock_status(
    available: bool, enabled: bool, lock_status: str
) -> DeviceLockStatus:
    """Map configuration_service's status fields to DeviceLockStatus.

    Precedence: DISABLED beats LOCKED beats AVAILABLE. A disabled device
    that's also locked is reported as DISABLED — it's the more permanent
    block, and the operator should fix that first before the lock matters.
    """
    if not enabled:
        return DeviceLockStatus.DISABLED
    if lock_status == "locked":
        return DeviceLockStatus.LOCKED
    if available and lock_status == "unlocked":
        return DeviceLockStatus.AVAILABLE
    return DeviceLockStatus.UNKNOWN


class CoordinationClient:
    """
    HTTP client for querying device coordination status from configuration_service.

    Implements the A4 coordination requirement: prevent direct control when
    a device is locked by an active plan.

    Implements: CoordinationService protocol
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.configuration_service_url
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.settings.coordination_timeout,
            )
        return self._client

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """
        Check if a device is available for direct control.

        Queries `GET /api/v1/devices/{name}/status` on configuration_service.
        Returns AVAILABLE when the device is unlocked or absent from the lock
        registry. Returns LOCKED with `locked_by` populated when EE/queueserver
        has an active lock on the device.

        Raises CoordinationCheckError if configuration_service is unreachable
        or returns an unexpected status.
        """
        if not self.settings.coordination_check_enabled:
            logger.warning(
                "coordination_check_disabled",
                device_name=device_name,
                note="Allowing command without coordination check (testing mode)",
            )
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )

        endpoint = f"/api/v1/devices/{device_name}/status"
        try:
            client = await self._get_client()

            logger.debug(
                "checking_device_coordination",
                device_name=device_name,
                url=f"{self.base_url}{endpoint}",
            )

            response = await client.get(endpoint)

            if response.status_code == 404:
                # The name passed in isn't registered as a device in
                # configuration_service. This is normal when callers use a
                # raw PV name (e.g. "mini:current") that has no device-level
                # lock concept. Lock contention is only possible when
                # EE/queueserver has registered a device-level lock; absence
                # of a registry entry implies no lock could exist.
                logger.info(
                    "device_not_in_lock_registry",
                    device_name=device_name,
                    note="No device-level lock concept; assuming available",
                )
                return CoordinationStatus(
                    device_available=True,
                    locked_by=None,
                    status=DeviceLockStatus.AVAILABLE,
                    timestamp=datetime.now(),
                )

            response.raise_for_status()
            data = response.json()

            available = bool(data.get("available", False))
            enabled = bool(data.get("enabled", True))
            lock_status_str = data.get("lock_status", "unlocked")
            mapped_status = _map_lock_status(available, enabled, lock_status_str)
            # device_available drives the boolean gate in device_controller:
            # any non-AVAILABLE status blocks commands (locked, disabled, unknown).
            status = CoordinationStatus(
                device_available=(mapped_status == DeviceLockStatus.AVAILABLE),
                locked_by=data.get("locked_by_plan"),
                status=mapped_status,
                timestamp=datetime.now(),
            )

            logger.info(
                "coordination_check_result",
                device_name=device_name,
                available=status.device_available,
                locked_by=status.locked_by,
                status=status.status.value,
            )

            return status

        except httpx.HTTPStatusError as e:
            logger.error(
                "coordination_check_http_error",
                device_name=device_name,
                status_code=e.response.status_code,
                error=str(e),
            )
            raise CoordinationCheckError(
                f"Coordination check failed: HTTP {e.response.status_code}"
            ) from e

        except httpx.RequestError as e:
            logger.error(
                "coordination_check_connection_error",
                device_name=device_name,
                error=str(e),
            )
            raise CoordinationCheckError(
                f"Cannot reach configuration_service for coordination: {e}"
            ) from e

        except Exception as e:
            logger.error(
                "coordination_check_unexpected_error",
                device_name=device_name,
                error=str(e),
                exc_info=True,
            )
            raise CoordinationCheckError(f"Unexpected coordination check error: {e}") from e

    async def is_service_available(self) -> ServiceAvailability:
        """Probe configuration_service /health; return structured detail.

        Distinguishes timeout / connect-refused / non-2xx so /health can
        surface why upstream is unhealthy rather than reporting bare False.
        Honors ``coordination_check_enabled`` (testing mode skips the
        round-trip).
        """
        if not self.settings.coordination_check_enabled:
            return ServiceAvailability(available=True)

        try:
            client = await self._get_client()
            response = await client.get("/health", timeout=2.0)
        except httpx.TimeoutException as exc:
            logger.warning("configuration_service_health_timeout", error=str(exc))
            return ServiceAvailability(
                available=False,
                detail=f"timeout reaching configuration_service /health: {exc}",
            )
        except httpx.RequestError as exc:
            logger.warning("configuration_service_health_unreachable", error=str(exc))
            return ServiceAvailability(
                available=False,
                detail=f"cannot reach configuration_service /health: {exc}",
            )

        if response.status_code == 200:
            return ServiceAvailability(available=True)

        logger.warning(
            "configuration_service_health_non_200",
            status_code=response.status_code,
        )
        return ServiceAvailability(
            available=False,
            detail=(
                f"configuration_service /health returned HTTP "
                f"{response.status_code}"
            ),
        )

    async def cleanup(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
