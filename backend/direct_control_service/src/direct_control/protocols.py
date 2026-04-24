"""
Protocol interfaces for Direct Device Control Service (SVC-003).

Defines type-safe contracts for service components following design principles:
- Python typing protocols for interface contracts
- Dependency injection support
- Separation of concerns

These protocols enable:
- Multiple coordination client implementations (HTTP, mock)
- Multiple device controller implementations
- Testing with mock implementations
- Clear interface boundaries between components
"""

from datetime import datetime
from typing import Any, Callable, List, Optional, Protocol, runtime_checkable

from .models import (
    CoordinationStatus,
    DeviceCommandRequest,
    DeviceCommandResponse,
    PVSetRequest,
    PVSetResponse,
    PVUpdate,
    PVValue,
)


@runtime_checkable
class CoordinationService(Protocol):
    """
    Protocol for coordination service clients.

    Implements the A4 coordination requirement: check if a device is
    available for direct control (not locked by an active plan).

    Implementations:
    - CoordinationClient: HTTP client to Experiment Execution Service
    - MockCoordinationClient: Always returns available (for testing)
    """

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
        ...

    async def is_service_available(self) -> bool:
        """
        Check if coordination service is reachable.

        Returns:
            True if service is available
        """
        ...

    async def cleanup(self) -> None:
        """Cleanup resources (HTTP client, etc.)."""
        ...


@runtime_checkable
class DeviceControl(Protocol):
    """
    Protocol for device control operations.

    Defines the interface for commanding EPICS PVs and Ophyd devices.

    Implementations:
    - DeviceController: Full implementation with EPICS/Ophyd
    - MockDeviceController: Returns mock responses (for testing)
    """

    async def set_pv(self, request: PVSetRequest) -> PVSetResponse:
        """
        Set EPICS PV value with coordination check.

        Two execution modes based on request.wait:
        - wait=True: Put-completion, waits for confirmation
        - wait=False: Fire-and-forget, returns immediately

        Args:
            request: PV set request

        Returns:
            PV set response with mode indication

        Raises:
            DeviceLockedError: If PV/device is locked by active plan
            ControlError: If set operation fails
        """
        ...

    async def execute_device_method(
        self, request: DeviceCommandRequest
    ) -> DeviceCommandResponse:
        """
        Execute Ophyd device method with coordination check.

        Args:
            request: Device command request

        Returns:
            Device command response

        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If command execution fails
        """
        ...

    async def get_pv_value(
        self,
        pv_name: str,
        *,
        as_string: bool = False,
        count: Optional[int] = None,
        as_numpy: bool = True,
        use_monitor: bool = True,
        timeout: float = 5.0,
        connection_timeout: float = 5.0,
        ftype: Optional[int] = None,
    ) -> Optional[Any]:
        """
        Get current PV value (read-only, no coordination check).

        Exposes pyepics caget/ca.get knobs; defaults preserve legacy behavior.
        """
        ...

    async def access_nested_device(
        self,
        device_path: str,
        method: str = "read",
        value: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Access nested device component (ophyd-websocket compatible).

        Args:
            device_path: Dot-separated device component path
            method: Method to execute (read, set, trigger, etc.)
            value: Value to set (for set method)
            timeout: Timeout in seconds

        Returns:
            Result of the operation

        Raises:
            DeviceLockedError: If device is locked by active plan
            ControlError: If operation fails
        """
        ...


class MockCoordinationClient:
    """
    Mock coordination client for testing.

    Always returns devices as available (no coordination check).
    """

    def __init__(self, always_available: bool = True):
        """
        Initialize mock client.

        Args:
            always_available: If True, devices are always available.
                              If False, devices are always locked.
        """
        self.always_available = always_available
        self.check_count = 0

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """Return mock coordination status."""
        from .models import DeviceLockStatus

        self.check_count += 1

        if self.always_available:
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )
        else:
            return CoordinationStatus(
                device_available=False,
                locked_by="mock_plan",
                status=DeviceLockStatus.LOCKED,
                timestamp=datetime.now(),
            )

    async def is_service_available(self) -> bool:
        """Always available for testing."""
        return True

    async def cleanup(self) -> None:
        """No cleanup needed for mock."""
        pass


@runtime_checkable
class PVMonitor(Protocol):
    """
    Protocol for EPICS PV monitoring.

    Defines the interface for subscribing to PV updates and retrieving
    cached values from the monitoring subsystem.

    Implementations:
    - PVMonitorManager: ophyd-based EPICS implementation
    - MockPVMonitor: returns mock data for testing
    """

    def subscribe(
        self,
        pv_name: str,
        callback: Optional[Callable[[PVUpdate], None]] = None,
        read_only: bool = False,
    ) -> None:
        """
        Subscribe to PV updates.

        Raises:
            PVNotFoundError: If PV cannot be connected.
        """
        ...

    def unsubscribe(
        self, pv_name: str, callback: Optional[Callable] = None
    ) -> None:
        """Unsubscribe from PV updates (callback=None removes all)."""
        ...

    def get_value(self, pv_name: str) -> Optional[PVValue]:
        """Get current PV value, or None if not connected."""
        ...

    def get_buffer(self, pv_name: str) -> List[PVValue]:
        """Get buffered PV values."""
        ...

    def is_connected(self, pv_name: str) -> bool:
        """Check if PV is currently connected."""
        ...

    def get_connected_pvs(self) -> List[str]:
        """List currently connected PV names."""
        ...

    async def cleanup(self) -> None:
        """Cleanup all PV connections."""
        ...


class MockPVMonitor:
    """
    Mock PV monitor for testing. Returns mock values without EPICS connection.
    """

    def __init__(self):
        self._subscribed: dict[str, bool] = {}
        self._callbacks: dict[str, list] = {}
        self._values: dict[str, PVValue] = {}

    def subscribe(
        self,
        pv_name: str,
        callback: Optional[Callable[[PVUpdate], None]] = None,
        read_only: bool = False,
    ) -> None:
        self._subscribed[pv_name] = True
        if callback:
            self._callbacks.setdefault(pv_name, []).append(callback)
        self._values[pv_name] = PVValue(
            pv_name=pv_name,
            value=0.0,
            timestamp=datetime.now(),
            status=0,
            severity=0,
            connected=True,
        )

    def unsubscribe(
        self, pv_name: str, callback: Optional[Callable] = None
    ) -> None:
        if callback and pv_name in self._callbacks:
            try:
                self._callbacks[pv_name].remove(callback)
            except ValueError:
                pass
        else:
            self._subscribed.pop(pv_name, None)
            self._callbacks.pop(pv_name, None)
            self._values.pop(pv_name, None)

    def get_value(self, pv_name: str) -> Optional[PVValue]:
        return self._values.get(pv_name)

    def get_buffer(self, pv_name: str) -> List[PVValue]:
        value = self._values.get(pv_name)
        return [value] if value else []

    def is_connected(self, pv_name: str) -> bool:
        return pv_name in self._subscribed

    def get_connected_pvs(self) -> List[str]:
        return list(self._subscribed.keys())

    async def cleanup(self) -> None:
        self._subscribed.clear()
        self._callbacks.clear()
        self._values.clear()

    def set_mock_value(self, pv_name: str, value: Any) -> None:
        """Trigger a mock update for testing callback propagation."""
        if pv_name not in self._subscribed:
            return
        now = datetime.now()
        self._values[pv_name] = PVValue(
            pv_name=pv_name, value=value, timestamp=now, status=0,
            severity=0, connected=True,
        )
        update = PVUpdate(
            pv_name=pv_name, value=value, timestamp=now, status=0,
            severity=0, connected=True,
        )
        for cb in self._callbacks.get(pv_name, []):
            try:
                cb(update)
            except Exception:
                pass
