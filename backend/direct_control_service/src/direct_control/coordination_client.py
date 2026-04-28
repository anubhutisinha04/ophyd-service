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

from .models import CoordinationStatus, DeviceLockStatus, CoordinationCheckError
from .config import Settings


logger = structlog.get_logger(__name__)


def _map_lock_status(available: bool, lock_status: str) -> DeviceLockStatus:
    """Map configuration_service's status fields to the local DeviceLockStatus enum."""
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
            lock_status_str = data.get("lock_status", "unlocked")
            status = CoordinationStatus(
                device_available=available,
                locked_by=data.get("locked_by_plan"),
                status=_map_lock_status(available, lock_status_str),
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

    async def is_service_available(self) -> bool:
        """Check whether configuration_service is reachable."""
        try:
            client = await self._get_client()
            response = await client.get("/health", timeout=2.0)
            return response.status_code == 200
        except Exception:
            return False

    async def cleanup(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
