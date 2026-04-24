"""
Protocol interfaces for Configuration Service (SVC-004).

Defines type-safe contracts for service components following design principles:
- Python typing protocols for interface contracts
- Dependency injection support
- Separation of concerns

These protocols enable:
- Multiple loader implementations (YAML, happi, BITS, mock)
- Testing with mock implementations
- Clear interface boundaries between components

Note: Plan catalog is NOT maintained by Configuration Service.
Plans are the responsibility of Experiment Execution Service (SVC-001),
which is the single source of truth for available plans.
"""

from typing import Dict, List, Optional, Protocol, runtime_checkable

from .models import (
    DeviceMetadata,
    DeviceInstantiationSpec,
    DeviceLabel,
    PVMetadata,
    DeviceRegistry,
)


@runtime_checkable
class ProfileLoader(Protocol):
    """
    Protocol for profile collection loaders.

    Implementations:
    - HappiProfileLoader: Parse happi_db.json
    - BitsProfileLoader: Parse devices.yml + iconfig.yml
    - MockProfileLoader: Return mock data for testing
    - EmptyProfileLoader: Start with zero devices (populated via CRUD)

    Note: Plans are NOT loaded here. Plan loading is the responsibility
    of Experiment Execution Service (SVC-001).
    """

    def load_registry(self) -> DeviceRegistry:
        """
        Load device registry from profile collection.

        Returns:
            DeviceRegistry with all devices indexed
        """
        ...


@runtime_checkable
class DeviceRegistryProtocol(Protocol):
    """
    Protocol for device registry operations.

    Defines the interface for querying device metadata.
    """

    def get_device(self, name: str) -> Optional[DeviceMetadata]:
        """Get device by name."""
        ...

    def list_devices(
        self,
        device_label: Optional[DeviceLabel] = None,
        pattern: Optional[str] = None,
        ophyd_class: Optional[str] = None,
    ) -> List[str]:
        """List device names with optional filtering."""
        ...

    def get_pv(self, pv_name: str) -> Optional[PVMetadata]:
        """Get PV metadata by name."""
        ...

    def search_pvs(self, pattern: str) -> List[str]:
        """Search PVs by glob pattern."""
        ...

    def add_device(self, device: DeviceMetadata) -> None:
        """Add or update device in registry."""
        ...

    def remove_device(self, name: str) -> bool:
        """Remove device from registry.

        Returns True if device was found and removed, False if not found.
        """
        ...

    def update_device(
        self,
        device: DeviceMetadata,
        instantiation_spec: Optional[DeviceInstantiationSpec] = None
    ) -> bool:
        """Update an existing device in registry.

        Returns True if device existed and was updated, False if not found.
        """
        ...


class ConfigurationState:
    """
    Container for configuration service state.

    Holds the loaded device registry.
    Used for dependency injection into FastAPI routes.

    Note: Plan catalog is NOT maintained here. Plans are the responsibility
    of Experiment Execution Service (SVC-001).
    """

    def __init__(self, registry: DeviceRegistryProtocol):
        """
        Initialize configuration state.

        Args:
            registry: Device registry instance
        """
        self._registry = registry

    @property
    def registry(self) -> DeviceRegistryProtocol:
        """Get device registry."""
        return self._registry

    def get_pv_list(self) -> List[str]:
        """Get sorted list of all PV names from the registry."""
        if hasattr(self._registry, 'pvs'):
            return sorted(self._registry.pvs.keys())  # type: ignore
        return []

    def get_all_pvs(self) -> Dict[str, Dict[str, str]]:
        """Get all PVs organized by device from the registry."""
        result: Dict[str, Dict[str, str]] = {}
        if hasattr(self._registry, 'devices'):
            for name, device in self._registry.devices.items():  # type: ignore
                if device.pvs:
                    result[name] = device.pvs
        return result
