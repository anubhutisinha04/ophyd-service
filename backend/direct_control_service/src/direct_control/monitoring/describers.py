"""
Device Describer Plugins for Device Monitoring Service.

Provides a pluggable system for generating device descriptions:
- Default ophyd device describer
- Protocol-based interface for custom describers
- Registry for managing multiple describers

Based on as-ophyd-api's describer pattern.

Example:
    >>> registry = DescriberRegistry()
    >>> registry.register(MyCustomDescriber())
    >>> description = registry.describe(my_device)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# Alarm severity mapping
ALARM_SEVERITY_NAMES = {
    0: "NO_ALARM",
    1: "MINOR",
    2: "MAJOR",
    3: "INVALID",
}


@runtime_checkable
class DeviceDescriber(Protocol):
    """Protocol for device description plugins."""

    def can_describe(self, device: Any) -> bool:
        """
        Check if this describer can handle the given device.

        Parameters
        ----------
        device : Any
            Device to check

        Returns
        -------
        bool
            True if this describer can describe the device
        """
        ...

    def describe(self, device: Any) -> Dict[str, Any]:
        """
        Generate description for a device.

        Parameters
        ----------
        device : Any
            Device to describe

        Returns
        -------
        dict
            Device description with at minimum: name, class
        """
        ...


class BaseDescriber(ABC):
    """Base class for device describers."""

    @abstractmethod
    def can_describe(self, device: Any) -> bool:
        """Check if this describer can handle the device."""
        pass

    @abstractmethod
    def describe(self, device: Any) -> Dict[str, Any]:
        """Generate description for the device."""
        pass


class OphydDescriber(BaseDescriber):
    """
    Default describer for ophyd devices.

    Extracts:
    - Basic info (name, class, prefix)
    - Component hierarchy
    - Protocol compliance (Readable, Movable, etc.)
    - Configuration attributes
    """

    def can_describe(self, device: Any) -> bool:
        """Check if device is an ophyd Device."""
        try:
            from ophyd import Device
            return isinstance(device, Device)
        except ImportError:
            return False

    def describe(self, device: Any) -> Dict[str, Any]:
        """Describe an ophyd device."""
        description = {
            "name": getattr(device, "name", str(device)),
            "class": type(device).__name__,
            "module": type(device).__module__,
        }

        # Add prefix if available
        if hasattr(device, "prefix"):
            description["prefix"] = device.prefix

        # Check protocol compliance
        description["protocols"] = self._get_protocols(device)

        # Get component names
        if hasattr(device, "component_names"):
            description["components"] = list(device.component_names)

        # Check if readable/movable
        description["is_readable"] = self._is_readable(device)
        description["is_movable"] = self._is_movable(device)
        description["is_flyable"] = self._is_flyable(device)

        # Get configuration attributes
        if hasattr(device, "configuration_attrs"):
            description["configuration_attrs"] = list(device.configuration_attrs)

        # Get read attributes
        if hasattr(device, "read_attrs"):
            description["read_attrs"] = list(device.read_attrs)

        return description

    def _get_protocols(self, device: Any) -> List[str]:
        """Get list of Bluesky protocols the device implements."""
        protocols = []

        try:
            from bluesky.protocols import (
                Readable, Movable, Flyable, Stageable,
                Pausable, Stoppable, Triggerable, Locatable
            )

            if isinstance(device, Readable):
                protocols.append("Readable")
            if isinstance(device, Movable):
                protocols.append("Movable")
            if isinstance(device, Flyable):
                protocols.append("Flyable")
            if isinstance(device, Stageable):
                protocols.append("Stageable")
            if isinstance(device, Pausable):
                protocols.append("Pausable")
            if isinstance(device, Stoppable):
                protocols.append("Stoppable")
            if isinstance(device, Triggerable):
                protocols.append("Triggerable")
            if isinstance(device, Locatable):
                protocols.append("Locatable")
        except ImportError:
            pass

        return protocols

    def _is_readable(self, device: Any) -> bool:
        """Check if device is readable."""
        try:
            from bluesky.protocols import Readable
            return isinstance(device, Readable)
        except ImportError:
            return hasattr(device, "read")

    def _is_movable(self, device: Any) -> bool:
        """Check if device is movable."""
        try:
            from bluesky.protocols import Movable
            return isinstance(device, Movable)
        except ImportError:
            return hasattr(device, "set")

    def _is_flyable(self, device: Any) -> bool:
        """Check if device is flyable."""
        try:
            from bluesky.protocols import Flyable
            return isinstance(device, Flyable)
        except ImportError:
            return hasattr(device, "kickoff") and hasattr(device, "complete")


class OphydAsyncDescriber(BaseDescriber):
    """Describer for ophyd-async devices."""

    def can_describe(self, device: Any) -> bool:
        """Check if device is an ophyd-async Device."""
        try:
            from ophyd_async.core import Device
            return isinstance(device, Device)
        except ImportError:
            return False

    def describe(self, device: Any) -> Dict[str, Any]:
        """Describe an ophyd-async device."""
        description = {
            "name": getattr(device, "name", str(device)),
            "class": type(device).__name__,
            "module": type(device).__module__,
            "async": True,
        }

        # Get children
        if hasattr(device, "_children"):
            description["components"] = list(device._children.keys())

        return description


class SignalDescriber(BaseDescriber):
    """Describer for ophyd Signals."""

    def can_describe(self, device: Any) -> bool:
        """Check if device is an ophyd Signal."""
        try:
            from ophyd import Signal
            return isinstance(device, Signal)
        except ImportError:
            return False

    def describe(self, device: Any) -> Dict[str, Any]:
        """Describe an ophyd Signal."""
        description = {
            "name": getattr(device, "name", str(device)),
            "class": type(device).__name__,
            "module": type(device).__module__,
            "type": "signal",
        }

        # Get PV name if EpicsSignal
        if hasattr(device, "pvname"):
            description["pv"] = device.pvname

        # Get read-only status
        description["read_only"] = not hasattr(device, "set") or not callable(getattr(device, "set", None))

        return description


class FallbackDescriber(BaseDescriber):
    """Fallback describer for unknown device types."""

    def can_describe(self, device: Any) -> bool:
        """Always returns True - handles anything."""
        return True

    def describe(self, device: Any) -> Dict[str, Any]:
        """Generate basic description for unknown device."""
        return {
            "name": getattr(device, "name", str(device)),
            "class": type(device).__name__,
            "module": type(device).__module__,
        }


class DescriberRegistry:
    """
    Registry of device describers.

    Maintains ordered list of describers, checking each in order
    until one can handle the device.

    Default describers (in order):
    1. SignalDescriber - for ophyd Signals
    2. OphydDescriber - for ophyd Devices
    3. OphydAsyncDescriber - for ophyd-async Devices
    4. FallbackDescriber - for anything else
    """

    def __init__(self):
        self._describers: List[BaseDescriber] = [
            SignalDescriber(),
            OphydDescriber(),
            OphydAsyncDescriber(),
            FallbackDescriber(),
        ]

    def register(self, describer: BaseDescriber, priority: int = 0):
        """
        Register a custom describer.

        Parameters
        ----------
        describer : BaseDescriber
            Describer to register
        priority : int
            Position in describer list (0 = highest priority)
        """
        self._describers.insert(priority, describer)
        logger.info(f"Registered describer: {type(describer).__name__} at priority {priority}")

    def unregister(self, describer_class: type):
        """
        Unregister a describer by class.

        Parameters
        ----------
        describer_class : type
            Class of describer to remove
        """
        self._describers = [d for d in self._describers if not isinstance(d, describer_class)]

    def describe(self, device: Any) -> Dict[str, Any]:
        """
        Describe a device using registered describers.

        Parameters
        ----------
        device : Any
            Device to describe

        Returns
        -------
        dict
            Device description
        """
        for describer in self._describers:
            if describer.can_describe(device):
                try:
                    return describer.describe(device)
                except Exception as e:
                    logger.warning(
                        f"Describer {type(describer).__name__} failed for {device}: {e}"
                    )
                    continue

        # Should never reach here due to FallbackDescriber
        return {"name": str(device), "class": "unknown"}

    def describe_all(self, devices: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        Describe multiple devices.

        Parameters
        ----------
        devices : dict
            Dictionary of device_name -> device

        Returns
        -------
        dict
            Dictionary of device_name -> description
        """
        return {name: self.describe(device) for name, device in devices.items()}


# Global registry instance
_registry: Optional[DescriberRegistry] = None


def get_describer_registry() -> DescriberRegistry:
    """Get the global describer registry."""
    global _registry
    if _registry is None:
        _registry = DescriberRegistry()
    return _registry


def describe_device(device: Any) -> Dict[str, Any]:
    """
    Convenience function to describe a device.

    Parameters
    ----------
    device : Any
        Device to describe

    Returns
    -------
    dict
        Device description
    """
    return get_describer_registry().describe(device)
