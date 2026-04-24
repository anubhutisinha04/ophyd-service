"""
Static capability lookup table for known ophyd device classes.

Replaces runtime hasattr() introspection on live device instances.
Maps class names to their protocol capability flags.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DeviceCapabilities:
    """Capability flags for a device class."""
    is_movable: bool = False
    is_flyable: bool = False
    is_readable: bool = False
    is_triggerable: bool = False
    is_stageable: bool = False
    is_configurable: bool = False
    is_pausable: bool = False
    is_stoppable: bool = False
    is_subscribable: bool = False
    is_checkable: bool = False
    writes_external_assets: bool = False


# Standard ophyd Device base capabilities
_DEVICE_BASE = DeviceCapabilities(
    is_readable=True,
    is_triggerable=True,
    is_stageable=True,
    is_configurable=True,
    is_subscribable=True,
)

# Motor-like: movable + stoppable + checkable
_MOTOR = DeviceCapabilities(
    is_movable=True,
    is_readable=True,
    is_triggerable=True,
    is_stageable=True,
    is_configurable=True,
    is_stoppable=True,
    is_subscribable=True,
    is_checkable=True,
)

# Detector-like: readable + triggerable
_DETECTOR = DeviceCapabilities(
    is_readable=True,
    is_triggerable=True,
    is_stageable=True,
    is_configurable=True,
    is_subscribable=True,
)

# Flyable device
_FLYABLE = DeviceCapabilities(
    is_flyable=True,
    is_readable=True,
    is_stageable=True,
    is_subscribable=True,
)

# Signal (read-only)
_SIGNAL_RO = DeviceCapabilities(
    is_readable=True,
    is_subscribable=True,
)

# Signal (read-write)
_SIGNAL_RW = DeviceCapabilities(
    is_readable=True,
    is_subscribable=True,
    is_movable=True,
)

# Known ophyd class names -> capabilities
KNOWN_CAPABILITIES: dict[str, DeviceCapabilities] = {
    # ophyd.sim classes
    "SynAxis": _MOTOR,
    "SynAxisNoPosition": _MOTOR,
    "SynAxisNoHints": _MOTOR,
    "SynGauss": _DETECTOR,
    "SynSignal": _SIGNAL_RO,
    "SynSignalRO": _SIGNAL_RO,
    "SynSignalWithRegistry": DeviceCapabilities(
        is_readable=True,
        is_triggerable=True,
        is_stageable=True,
        is_subscribable=True,
        writes_external_assets=True,
    ),
    "MockFlyer": _FLYABLE,
    "TrivialFlyer": _FLYABLE,

    # ophyd real device classes
    "EpicsMotor": _MOTOR,
    "PVPositioner": _MOTOR,
    "PVPositionerPC": _MOTOR,
    "PseudoPositioner": _MOTOR,
    "PseudoSingle": _MOTOR,
    "SoftPositioner": _MOTOR,

    "EpicsSignal": _SIGNAL_RW,
    "EpicsSignalRO": _SIGNAL_RO,
    "EpicsSignalWithRBV": _SIGNAL_RW,
    "InternalSignal": _SIGNAL_RO,

    "EpicsScaler": _DETECTOR,
    "EpicsPathSignal": _SIGNAL_RO,

    "Device": _DEVICE_BASE,
    "SimDetector": DeviceCapabilities(
        is_readable=True,
        is_triggerable=True,
        is_stageable=True,
        is_configurable=True,
        is_subscribable=True,
        writes_external_assets=True,
    ),

    # Area detectors
    "AreaDetector": DeviceCapabilities(
        is_readable=True,
        is_triggerable=True,
        is_stageable=True,
        is_configurable=True,
        is_subscribable=True,
        writes_external_assets=True,
    ),
    "AdscDetector": DeviceCapabilities(
        is_readable=True,
        is_triggerable=True,
        is_stageable=True,
        is_configurable=True,
        is_subscribable=True,
        writes_external_assets=True,
    ),
    "PilatusDetector": DeviceCapabilities(
        is_readable=True,
        is_triggerable=True,
        is_stageable=True,
        is_configurable=True,
        is_subscribable=True,
        writes_external_assets=True,
    ),
}


def get_capabilities(class_name: str) -> DeviceCapabilities:
    """
    Look up capabilities for a device class name.

    Falls back to heuristic name matching for unknown classes.

    Args:
        class_name: Short class name (e.g., "EpicsMotor", "SynGauss")

    Returns:
        DeviceCapabilities with appropriate flags set
    """
    # Exact match
    if class_name in KNOWN_CAPABILITIES:
        return KNOWN_CAPABILITIES[class_name]

    # Heuristic fallback based on class name patterns
    lower = class_name.lower()

    if "motor" in lower or "positioner" in lower or "axis" in lower:
        return _MOTOR
    if "flyer" in lower or "flyable" in lower:
        return _FLYABLE
    if "signalro" in lower:
        return _SIGNAL_RO
    if "signal" in lower:
        return _SIGNAL_RW
    if "detector" in lower or "scaler" in lower:
        return _DETECTOR

    # Default: assume a readable device
    return _DEVICE_BASE
