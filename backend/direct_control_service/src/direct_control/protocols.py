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

import structlog

logger = structlog.get_logger(__name__)

from .models import (
    CoordinationStatus,
    DeviceCommandRequest,
    DeviceCommandResponse,
    InstantiationSpec,
    PVSetRequest,
    PVSetResponse,
    PVUpdate,
    PVValue,
    ServiceAvailability,
)


@runtime_checkable
class CoordinationService(Protocol):
    """
    Protocol for coordination service clients.

    Implements the A4 coordination requirement: check if a device is
    available for direct control (not locked by an active plan).

    Implementations:
    - CoordinationClient: HTTP client to configuration_service (reads
      device-lock state via ``GET /api/v1/devices/{name}/status``;
      EE/queueserver write the locks via POST /devices/lock)
    - MockCoordinationClient: Always returns available (for testing)
    """

    async def check_device_available(self, device_name: str) -> CoordinationStatus:
        """
        Check if device is available for direct control.

        This is the CRITICAL A4 coordination check. It reads the device-lock
        state from configuration_service to determine whether the device is
        currently locked by an executing plan. direct_control NEVER talks
        to EE / queueserver directly.

        Args:
            device_name: Name of the device to check

        Returns:
            CoordinationStatus with device availability

        Raises:
            CoordinationCheckError: If coordination check fails
        """
        ...

    async def is_service_available(self) -> ServiceAvailability:
        """
        Check if coordination service is reachable.

        Returns:
            ServiceAvailability with `available` flag and, when not
            available, a `detail` string describing why (timeout,
            connection error, non-2xx response). Pre-S6 this returned a
            bare bool that hid the failure mode.
        """
        ...

    async def cleanup(self) -> None:
        """Cleanup resources (HTTP client, etc.)."""
        ...


@runtime_checkable
class RegistryProvider(Protocol):
    """Protocol for the device/PV existence registry.

    Confirms a PV/device exists before an operation reaches EPICS, and maps a
    PV to its owning device for the coordination gate. Implementations:
    - RegistryClient: HTTP client to configuration_service (the authoritative
      shared registry used in full beamline deployments)
    - FileRegistryProvider: a static JSON/YAML file (standalone / monitoring-
      only deployments with no configuration_service)
    """

    async def validate_pv(self, pv_name: str) -> None:
        """Raise RegistryValidationError if the PV is not registered."""
        ...

    async def validate_device(self, device_name: str) -> None:
        """Raise RegistryValidationError if the device is not registered."""
        ...

    async def get_owning_device(self, pv_name: str) -> Optional[str]:
        """Return the device owning this PV, or None for standalone/unknown PVs."""
        ...

    async def get_instantiation_spec(self, device_name: str) -> Optional["InstantiationSpec"]:
        """Return how to construct the live device, or None when the registry
        has no class/ctor information for it (device-level control is then
        unavailable for that device; PV-level operations still work).

        Raises RuntimeError if the registry backend is unreachable.
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

    async def execute_device_method(self, request: DeviceCommandRequest) -> DeviceCommandResponse:
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

    async def is_service_available(self) -> ServiceAvailability:
        """Always available for testing."""
        return ServiceAvailability(available=True)

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
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """
        Subscribe to PV updates.

        ``on_error`` (when supplied) is invoked synchronously on the CA
        listener thread when ``callback`` raises during a value or meta
        dispatch — letting the subscriber translate the failure into a
        user-visible signal instead of swallowing it.

        Raises:
            PVNotFoundError: If PV cannot be connected.
        """
        ...

    def unsubscribe(self, pv_name: str, callback: Optional[Callable] = None) -> None:
        """Unsubscribe from PV updates (callback=None removes all)."""
        ...

    def get_value(self, pv_name: str) -> Optional[PVValue]:
        """Get current PV value.

        Returns ``None`` only when the PV is not in the subscription
        cache (genuinely "we don't track it"). Raises ``PVReadError``
        if the PV is subscribed but the read itself fails — callers
        should distinguish so a transient EPICS error doesn't surface
        as a 404 "not found".
        """
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
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._subscribed[pv_name] = True
        if callback:
            self._callbacks.setdefault(pv_name, []).append(callback)
        # Mock-PV access bits are explicit so the test path doesn't depend
        # on PVValue's locked-out defaults (post-M14). A mock with no access
        # would silently break tests that assumed permissive bits.
        self._values[pv_name] = PVValue(
            pv_name=pv_name,
            value=0.0,
            timestamp=datetime.now(),
            status=0,
            severity=0,
            connected=True,
            read_access=True,
            write_access=not read_only,
        )

    def unsubscribe(self, pv_name: str, callback: Optional[Callable] = None) -> None:
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
        # Preserve the access bits that ``subscribe`` recorded on this PV
        # so set_mock_value updates don't silently flip the mock's
        # advertised access (post-M14 model defaults are locked-out).
        prior = self._values.get(pv_name)
        read_access = prior.read_access if prior is not None else True
        write_access = prior.write_access if prior is not None else True
        self._values[pv_name] = PVValue(
            pv_name=pv_name,
            value=value,
            timestamp=now,
            status=0,
            severity=0,
            connected=True,
            read_access=read_access,
            write_access=write_access,
        )
        update = PVUpdate(
            pv=pv_name,
            value=value,
            timestamp=now,
            status=0,
            severity=0,
            connected=True,
            read_access=read_access,
            write_access=write_access,
        )
        for cb in self._callbacks.get(pv_name, []):
            try:
                cb(update)
            except Exception as e:  # noqa: BLE001
                # Asyncio callback fan-out: one bad callback shouldn't kill
                # the rest, but the failure must be visible.
                logger.warning("mock_pv_callback_error", pv_name=pv_name, error=str(e))
