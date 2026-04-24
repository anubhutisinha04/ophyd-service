"""
Static Profile Loaders for Configuration Service (SVC-004).

Loads device definitions from beamline profile files (YAML/JSON) without
importing or instantiating any ophyd devices. The service is a pure
file-based registry: it reads profile files, constructs DeviceMetadata
objects from static data, and serves them via the REST API.

Loading strategies:
- happi: Parse happi_db.json
- bits: Parse devices.yml + iconfig.yml
- mock: Return sample data for testing

Note: Plans are NOT loaded here. Plan loading is the responsibility
of Experiment Execution Service (SVC-001), which is the single source
of truth for available plans. Plans cannot be serialized over HTTP.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .class_capabilities import get_capabilities
from .models import (
    DeviceMetadata,
    DeviceInstantiationSpec,
    DeviceLabel,
    DeviceRegistry,
)

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────

def _infer_device_label(class_name: str, labels: Optional[List[str]] = None,
                       functional_group: Optional[str] = None) -> DeviceLabel:
    """Infer DeviceLabel from class name, labels, and/or functional group."""
    # Check labels first (most reliable for BITS/KREIOS)
    if labels:
        labels_lower = [l.lower() for l in labels]
        if "motors" in labels_lower or "positioners" in labels_lower:
            return DeviceLabel.MOTOR
        if "detectors" in labels_lower or "area_detectors" in labels_lower:
            return DeviceLabel.DETECTOR
        if "flyers" in labels_lower:
            return DeviceLabel.FLYER
        if "signals" in labels_lower:
            return DeviceLabel.SIGNAL

    # Check functional group (happi)
    if functional_group:
        fg_lower = functional_group.lower()
        if "motor" in fg_lower:
            return DeviceLabel.MOTOR
        if "detector" in fg_lower or "area" in fg_lower:
            return DeviceLabel.DETECTOR
        if "flyer" in fg_lower:
            return DeviceLabel.FLYER
        if "signal" in fg_lower:
            return DeviceLabel.SIGNAL

    # Fall back to class name heuristics
    lower = class_name.lower()
    if "motor" in lower or "axis" in lower or "positioner" in lower:
        return DeviceLabel.MOTOR
    if "detector" in lower or "det" in lower:
        return DeviceLabel.DETECTOR
    if "signal" in lower:
        return DeviceLabel.SIGNAL
    if "flyable" in lower or "flyer" in lower:
        return DeviceLabel.FLYER

    return DeviceLabel.DEVICE


def _derive_pvs_from_args(class_name: str, args: List[Any],
                          kwargs: Dict[str, Any]) -> Dict[str, str]:
    """
    Derive PV names from constructor arguments when possible.

    For known class patterns (e.g., EpicsMotor prefix), generate the
    standard PV field names.
    """
    pvs: Dict[str, str] = {}
    lower = class_name.lower()

    # EpicsMotor-like: first arg is prefix
    if "motor" in lower and "epics" in lower and args:
        prefix = str(args[0])
        pvs["user_setpoint"] = prefix
        pvs["user_readback"] = f"{prefix}.RBV"
        pvs["velocity"] = f"{prefix}.VELO"
        pvs["acceleration"] = f"{prefix}.ACCL"

    # EpicsSignal / EpicsSignalRO: first arg is read PV
    elif "signal" in lower and "epics" in lower and args:
        pv = str(args[0])
        pvs["readback"] = pv

    # EpicsSignalRO with read_pv kwarg (BITS format)
    elif kwargs.get("read_pv"):
        pvs["readback"] = str(kwargs["read_pv"])

    # Devices with prefix kwarg or arg (general EPICS devices)
    elif args and isinstance(args[0], str) and ":" in str(args[0]):
        pvs["prefix"] = str(args[0])

    return pvs


# ── HappiProfileLoader ──────────────────────────────────────────────────

class HappiProfileLoader:
    """
    Load devices from Happi database format (pure JSON parsing).

    Reads happi_db.json directly and constructs device metadata from the
    JSON fields. No device instantiation or module imports.
    """

    def __init__(self, profile_path: Path):
        self.profile_path = Path(profile_path)
        self.db_path = self._find_happi_db()
        if not self.db_path:
            raise ValueError(f"No happi database found in {self.profile_path}")

    def _find_happi_db(self) -> Optional[Path]:
        """Find the happi database file."""
        for name in ["happi_db.json", "happi.json", "db.json"]:
            path = self.profile_path / name
            if path.exists():
                return path
        return None

    def load_registry(self) -> DeviceRegistry:
        """Load device registry from happi database JSON."""
        logger.info(f"Loading happi device registry from {self.db_path}")
        registry = DeviceRegistry()

        with open(self.db_path) as f:
            db = json.load(f)

        for name, entry in db.items():
            if not entry.get("active", True):
                logger.debug(f"Skipping inactive device: {name}")
                continue

            try:
                self._process_entry(name, entry, registry)
            except Exception as e:
                logger.error(f"Failed to process happi device {name}: {e}")

        logger.info(
            f"Loaded {len(registry.devices)} devices, "
            f"{len(registry.instantiation_specs)} instantiation specs "
            f"from happi database"
        )
        return registry

    def _process_entry(self, name: str, entry: Dict[str, Any],
                       registry: DeviceRegistry) -> None:
        """Process a single happi database entry."""
        device_class_path = entry.get("device_class", "")
        class_name = device_class_path.rsplit(".", 1)[-1] if device_class_path else "Unknown"
        module_name = device_class_path.rsplit(".", 1)[0] if "." in device_class_path else None

        functional_group = entry.get("functional_group")
        device_label = _infer_device_label(class_name, functional_group=functional_group)

        caps = get_capabilities(class_name)

        # Derive PVs from constructor args
        args = entry.get("args", [])
        kwargs = entry.get("kwargs", {})
        pvs = _derive_pvs_from_args(class_name, args, kwargs)

        # Also check for prefix field (KREIOS happi uses this)
        prefix = entry.get("prefix")
        if prefix and not pvs:
            pvs["prefix"] = prefix

        device_metadata = DeviceMetadata(
            name=name,
            device_label=device_label,
            ophyd_class=class_name,
            module=module_name,
            is_movable=caps.is_movable,
            is_flyable=caps.is_flyable,
            is_readable=caps.is_readable,
            is_triggerable=caps.is_triggerable,
            is_stageable=caps.is_stageable,
            is_configurable=caps.is_configurable,
            is_pausable=caps.is_pausable,
            is_stoppable=caps.is_stoppable,
            is_subscribable=caps.is_subscribable,
            is_checkable=caps.is_checkable,
            writes_external_assets=caps.writes_external_assets,
            pvs=pvs,
            beamline=entry.get("beamline"),
            location_group=entry.get("location_group"),
            functional_group=functional_group,
            documentation=entry.get("documentation"),
            labels=[functional_group] if functional_group else [],
        )

        instantiation_spec = DeviceInstantiationSpec(
            name=name,
            device_class=device_class_path,
            args=args,
            kwargs=kwargs,
            active=entry.get("active", True),
        )

        registry.add_device(device_metadata, instantiation_spec)
        logger.debug(f"Registered happi device: {name} ({device_label})")


# ── BitsProfileLoader ───────────────────────────────────────────────────

class BitsProfileLoader:
    """
    Load devices from BITS format (BCDA-APS) via pure YAML parsing.

    Reads devices.yml and iconfig.yml without importing any modules.
    """

    def __init__(self, profile_path: Path):
        self.profile_path = Path(profile_path)

        # Find config files
        self.configs_dir = self.profile_path / "configs"
        if not self.configs_dir.exists():
            self.configs_dir = self.profile_path

        self.iconfig_path = self._find_file(["iconfig.yml", "iconfig.yaml"])
        self.devices_path = self._find_file(["devices.yml", "devices.yaml"])

        if not self.devices_path:
            raise ValueError(f"No devices.yml found in {self.profile_path}")

        # Load iconfig if available
        self.iconfig: Dict[str, Any] = {}
        if self.iconfig_path:
            with open(self.iconfig_path) as f:
                self.iconfig = yaml.safe_load(f) or {}

    def _find_file(self, names: List[str]) -> Optional[Path]:
        """Find a file by trying multiple names."""
        for name in names:
            path = self.configs_dir / name
            if path.exists():
                return path
            path = self.profile_path / name
            if path.exists():
                return path
        return None

    def load_registry(self) -> DeviceRegistry:
        """Load device registry from BITS devices.yml."""
        logger.info(f"Loading BITS device registry from {self.devices_path}")
        registry = DeviceRegistry()

        with open(self.devices_path) as f:
            devices_config = yaml.safe_load(f) or {}

        beamline = (
            self.iconfig
            .get("RUN_ENGINE", {})
            .get("md", {})
            .get("beamline_id")
        )

        for module_path, device_entries in devices_config.items():
            if not isinstance(device_entries, list):
                logger.warning(f"Invalid devices entry for {module_path}")
                continue

            for entry in device_entries:
                name = entry.get("name")
                if not name:
                    logger.warning(f"Device entry missing name in {module_path}")
                    continue

                try:
                    self._process_entry(name, entry, module_path, beamline, registry)
                except Exception as e:
                    logger.error(f"Failed to process BITS device {name}: {e}")

        logger.info(
            f"Loaded {len(registry.devices)} devices, "
            f"{len(registry.instantiation_specs)} instantiation specs "
            f"from BITS config"
        )
        return registry

    def _process_entry(self, name: str, entry: Dict[str, Any],
                       module_path: str, beamline: Optional[str],
                       registry: DeviceRegistry) -> None:
        """Process a single BITS device entry."""
        creator_name = entry.get("creator", name)
        labels = entry.get("labels", [])
        prefix = entry.get("prefix")
        read_pv = entry.get("read_pv")

        # Derive class name from module path + creator
        # e.g., "ophyd.sim" -> creator "det" -> class is looked up by creator name
        # e.g., "ophyd.EpicsMotor" -> module IS the class
        # e.g., "devices.kreios_devices.KreiosDetector" -> last part is class
        parts = module_path.rsplit(".", 1)
        if len(parts) == 2 and parts[-1][0].isupper():
            # Module path ends with a class name (e.g., "ophyd.EpicsMotor")
            class_name = parts[-1]
        else:
            # Module path is a module; use creator_name as identifier
            class_name = creator_name

        caps = get_capabilities(class_name)
        device_label = _infer_device_label(class_name, labels=labels)

        # Derive PVs from prefix if available
        pvs: Dict[str, str] = {}
        if prefix:
            pvs["prefix"] = prefix
            # For known motor types, add standard PV fields
            if device_label == DeviceLabel.MOTOR:
                pvs["user_setpoint"] = prefix
                pvs["user_readback"] = f"{prefix}.RBV"
        if read_pv:
            pvs["readback"] = read_pv

        device_metadata = DeviceMetadata(
            name=name,
            device_label=device_label,
            ophyd_class=class_name,
            module=module_path,
            is_movable=caps.is_movable,
            is_flyable=caps.is_flyable,
            is_readable=caps.is_readable,
            is_triggerable=caps.is_triggerable,
            is_stageable=caps.is_stageable,
            is_configurable=caps.is_configurable,
            is_pausable=caps.is_pausable,
            is_stoppable=caps.is_stoppable,
            is_subscribable=caps.is_subscribable,
            is_checkable=caps.is_checkable,
            writes_external_assets=caps.writes_external_assets,
            pvs=pvs,
            labels=labels,
            beamline=beamline,
        )

        device_class_path = f"{module_path}.{creator_name}"
        instantiation_spec = DeviceInstantiationSpec(
            name=name,
            device_class=device_class_path,
            args=[prefix] if prefix else [],
            kwargs={"name": name, "labels": labels} if labels else {"name": name},
            active=True,
        )

        registry.add_device(device_metadata, instantiation_spec)
        logger.debug(f"Registered BITS device: {name} ({device_label})")


# ── MockProfileLoader ───────────────────────────────────────────────────

class MockProfileLoader:
    """
    Mock profile loader for testing/development.

    Returns sample device/plan data when real profile collection unavailable.
    """

    def load_registry(self) -> DeviceRegistry:
        """Load mock device registry."""
        registry = DeviceRegistry()

        registry.add_device(
            DeviceMetadata(
                name="sample_x",
                device_label=DeviceLabel.MOTOR,
                ophyd_class="EpicsMotor",
                module="ophyd.epics_motor",
                pvs={
                    "user_readback": "BL01:SAMPLE:X.RBV",
                    "user_setpoint": "BL01:SAMPLE:X",
                    "velocity": "BL01:SAMPLE:X.VELO",
                },
                hints={"fields": ["sample_x"]},
                read_attrs=["user_readback", "user_setpoint"],
                configuration_attrs=["velocity", "acceleration"],
                is_movable=True,
                is_readable=True,
                is_triggerable=True,
                is_stageable=True,
                is_configurable=True,
                is_stoppable=True,
                is_subscribable=True,
                is_checkable=True,
            ),
            DeviceInstantiationSpec(
                name="sample_x",
                device_class="ophyd.EpicsMotor",
                args=["BL01:SAMPLE:X"],
                kwargs={"name": "sample_x"},
            ),
        )

        registry.add_device(
            DeviceMetadata(
                name="det1",
                device_label=DeviceLabel.DETECTOR,
                ophyd_class="EpicsScaler",
                module="ophyd.scaler",
                pvs={
                    "count": "BL01:DET1:CNT",
                    "preset_time": "BL01:DET1:PRESET",
                },
                hints={"fields": ["det1"]},
                read_attrs=["count"],
                configuration_attrs=["preset_time"],
                is_readable=True,
                is_triggerable=True,
                is_stageable=True,
                is_configurable=True,
                is_subscribable=True,
            ),
            DeviceInstantiationSpec(
                name="det1",
                device_class="ophyd.EpicsScaler",
                args=["BL01:DET1:"],
                kwargs={"name": "det1"},
            ),
        )

        registry.add_device(
            DeviceMetadata(
                name="cam1",
                device_label=DeviceLabel.DETECTOR,
                ophyd_class="SimDetector",
                module="ophyd.areadetector.detectors",
                pvs={
                    "cam.acquire": "BL01:CAM1:cam1:Acquire",
                    "cam.acquire_time": "BL01:CAM1:cam1:AcquireTime",
                    "cam.image_mode": "BL01:CAM1:cam1:ImageMode",
                    "image": "BL01:CAM1:image1:ArrayData",
                    "image.array_size.width": "BL01:CAM1:image1:ArraySize0",
                    "image.array_size.height": "BL01:CAM1:image1:ArraySize1",
                    "stats.total": "BL01:CAM1:Stats1:Total_RBV",
                    "stats.centroid.x": "BL01:CAM1:Stats1:CentroidX_RBV",
                    "stats.centroid.y": "BL01:CAM1:Stats1:CentroidY_RBV",
                },
                hints={"fields": ["cam1_stats_total"]},
                read_attrs=["image", "stats.total"],
                configuration_attrs=["cam.acquire_time"],
                is_readable=True,
                is_triggerable=True,
                is_stageable=True,
                is_configurable=True,
                is_subscribable=True,
                writes_external_assets=True,
            ),
            DeviceInstantiationSpec(
                name="cam1",
                device_class="ophyd.areadetector.detectors.SimDetector",
                args=["BL01:CAM1:"],
                kwargs={"name": "cam1"},
            ),
        )

        return registry


# ── EmptyProfileLoader ─────────────────────────────────────────────────

class EmptyProfileLoader:
    """
    Empty profile loader — starts with zero devices.

    Use this when devices will be registered at runtime via the CRUD API,
    typically by the Experiment Execution Service (SVC-001).
    """

    def load_registry(self) -> DeviceRegistry:
        return DeviceRegistry()


# ── Factory and detection ────────────────────────────────────────────────

ProfileLoaderType = MockProfileLoader | HappiProfileLoader | BitsProfileLoader | EmptyProfileLoader


def detect_profile_type(profile_path: Path) -> str:
    """
    Auto-detect the profile type based on files present in the directory.

    Detection order (first match wins):
    1. happi: If happi_db.json, happi.json, or db.json exists
    2. bits: If configs/devices.yml or devices.yml exists

    Args:
        profile_path: Path to the profile directory

    Returns:
        One of: "happi", "bits"

    Raises:
        ValueError: If no recognizable profile format is detected
    """
    profile_path = Path(profile_path)

    if not profile_path.exists():
        raise ValueError(f"Profile path does not exist: {profile_path}")

    # Check for happi format (JSON database)
    happi_files = ["happi_db.json", "happi.json", "db.json"]
    for happi_file in happi_files:
        if (profile_path / happi_file).exists():
            logger.info(f"Auto-detected happi format (found {happi_file})")
            return "happi"

    # Check for BITS format (YAML configs)
    bits_paths = [
        profile_path / "configs" / "devices.yml",
        profile_path / "configs" / "devices.yaml",
        profile_path / "devices.yml",
        profile_path / "devices.yaml",
    ]
    for bits_path in bits_paths:
        if bits_path.exists():
            logger.info(f"Auto-detected bits format (found {bits_path.name})")
            return "bits"

    raise ValueError(
        f"Could not detect profile type for {profile_path}. "
        f"Expected one of: happi_db.json (happi) or devices.yml (bits). "
        f"For profiles with only startup scripts, use the CRUD endpoints "
        f"to register devices, or set CONFIG_LOAD_STRATEGY=empty."
    )


def create_loader(settings: "Settings") -> ProfileLoaderType:
    """
    Factory function to create appropriate loader based on settings.

    Supported load strategies:
        - auto: Auto-detect based on files present (default)
        - happi: Parse happi_db.json
        - bits: Parse devices.yml + iconfig.yml
        - mock: Use mock data for testing
        - empty: Start with zero devices (devices added via CRUD API)

    Args:
        settings: Configuration settings

    Returns:
        Loader instance implementing ProfileLoader protocol

    Raises:
        RuntimeError: If configuration is invalid
    """
    load_strategy = settings.effective_strategy

    if load_strategy == "empty":
        logger.info("Creating EmptyProfileLoader (devices will be added via CRUD)")
        return EmptyProfileLoader()

    if load_strategy == "mock":
        logger.info("Creating MockProfileLoader")
        return MockProfileLoader()

    # For auto mode, detect the profile type
    if load_strategy == "auto":
        profile_path = settings.profile_path
        if not profile_path or not profile_path.exists():
            raise RuntimeError(
                f"auto loading strategy configured but profile path not found: {profile_path}. "
                f"Set CONFIG_LOAD_STRATEGY=mock for testing, or provide a valid profile path."
            )
        try:
            load_strategy = detect_profile_type(profile_path)
            logger.info(f"Auto-detected load strategy: {load_strategy}")
        except ValueError as e:
            raise RuntimeError(str(e)) from e

    # Now create the appropriate loader
    if load_strategy == "happi":
        profile_path = settings.profile_path
        if not profile_path or not profile_path.exists():
            raise RuntimeError(
                f"happi loading strategy configured but profile path not found: {profile_path}. "
                f"Set CONFIG_LOAD_STRATEGY=mock for testing, or provide a valid profile path."
            )
        logger.info(f"Creating HappiProfileLoader from {profile_path}")
        return HappiProfileLoader(profile_path)

    elif load_strategy == "bits":
        profile_path = settings.profile_path
        if not profile_path or not profile_path.exists():
            raise RuntimeError(
                f"bits loading strategy configured but profile path not found: {profile_path}. "
                f"Set CONFIG_LOAD_STRATEGY=mock for testing, or provide a valid profile path."
            )
        logger.info(f"Creating BitsProfileLoader from {profile_path}")
        return BitsProfileLoader(profile_path)

    else:
        raise RuntimeError(
            f"Unknown load strategy: {load_strategy}. "
            f"Valid options: auto, empty, mock, happi, bits"
        )
