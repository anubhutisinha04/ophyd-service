"""
Coordination client for checking device availability with SVC-001.

Implements the CoordinationService protocol for the critical A4 coordination
requirement via dependency injection.
"""

import httpx
import structlog
from datetime import datetime
from typing import Optional

from .models import CoordinationStatus, DeviceLockStatus, CoordinationCheckError
from .config import Settings


logger = structlog.get_logger(__name__)


class CoordinationClient:
    """
    HTTP client for querying device coordination status from Experiment Execution Service.

    This implements the A4 coordination requirement: prevent direct control
    when device is locked by an active plan execution.

    Implements: CoordinationService protocol
    """

    def __init__(self, settings: Settings):
        """Initialize coordination client."""
        self.settings = settings
        self.base_url = settings.experiment_execution_url
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.settings.coordination_timeout,
            )
        return self._client
    
    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """
        Check if device is available for direct control.
        
        This is the CRITICAL A4 coordination check. It queries SVC-001
        (Experiment Execution Service) to determine if the device is
        currently locked by an executing plan.
        
        Args:
            device_name: Name of the device to check
        
        Returns:
            CoordinationStatus with device availability
        
        Raises:
            CoordinationCheckError: If coordination check fails
        """
        if not self.settings.coordination_check_enabled:
            logger.warning(
                "coordination_check_disabled",
                device_name=device_name,
                note="Allowing command without coordination check (testing mode)"
            )
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )
        
        try:
            client = await self._get_client()
            
            logger.debug(
                "checking_device_coordination",
                device_name=device_name,
                url=f"{self.base_url}/api/v1/coordination/devices/{device_name}/status"
            )
            
            response = await client.get(
                f"/api/v1/coordination/devices/{device_name}/status"
            )
            
            if response.status_code == 404:
                # Device not found in coordination state - treat as available
                logger.info(
                    "device_not_in_coordination_state",
                    device_name=device_name,
                    note="Device not tracked, assuming available"
                )
                return CoordinationStatus(
                    device_available=True,
                    locked_by=None,
                    status=DeviceLockStatus.AVAILABLE,
                    timestamp=datetime.now(),
                )
            
            response.raise_for_status()
            data = response.json()
            
            status = CoordinationStatus(
                device_available=data.get("available", False),
                locked_by=data.get("locked_by"),
                status=DeviceLockStatus(data.get("status", "unknown")),
                timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now().isoformat())),
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
                f"Cannot reach Experiment Execution Service: {e}"
            ) from e
        
        except Exception as e:
            logger.error(
                "coordination_check_unexpected_error",
                device_name=device_name,
                error=str(e),
                exc_info=True,
            )
            raise CoordinationCheckError(
                f"Unexpected coordination check error: {e}"
            ) from e
    
    async def is_service_available(self) -> bool:
        """
        Check if Experiment Execution Service is reachable.
        
        Returns:
            True if service is available
        """
        try:
            client = await self._get_client()
            response = await client.get("/health", timeout=2.0)
            return response.status_code == 200
        except Exception:
            return False
    
    async def cleanup(self):
        """Cleanup HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
