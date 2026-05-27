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


# Cap the number of failures that get embedded in the RuntimeError message.
# A registry of 10k broken entries would otherwise build a multi-MB string
# that gets re-formatted by every layer that prints the traceback. The full
# list still goes to the structured logger above the raise.
_MAX_FAILURES_IN_RAISE = 20


def _raise_if_partial_load(failures: List[str], total: int, source: str) -> None:
    """Refuse to seed from a partial registry.

    Pre-2026-05-02 the loader logged per-entry failures then announced a
    successful "Loaded N devices", silently masking dropped entries.
    """
    if not failures:
        return
    shown = failures[:_MAX_FAILURES_IN_RAISE]
    suffix = (
        f"; ...and {len(failures) - _MAX_FAILURES_IN_RAISE} more"
        if len(failures) > _MAX_FAILURES_IN_RAISE
        else ""
    )
    raise RuntimeError(
        f"Failed to load {len(failures)} of {total} {source} entries; "
        f"refusing to seed registry from partial data. Errors: "
        + "; ".join(shown)
        + suffix
    )


# ── helpers ──────────────────────────────────────────────────────────────


def _infer_device_label(
    class_name: str, labels: Optional[List[str]] = None, functional_group: Optional[str] = None
) -> DeviceLabel:
    """Infer DeviceLabel from class name, labels, and/or functional group."""
    # Check labels first (most reliable for BITS-style entries)
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


def _resolve_happi_templates(value: Any, prefix: Optional[str], name: Optional[str]) -> Any:
    """
    Substitute happi's `{{prefix}}` and `{{name}}` placeholders.

    Walks lists and dicts; leaves non-string scalars untouched. Raises
    ValueError if a token is encountered with no substitution available
    (prefix=None or name=None) — leaving the literal in place would seed
    the registry with bogus PVs like ``{{prefix}}.RBV``.
    """
    if isinstance(value, str):
        if "{{prefix}}" in value:
            if prefix is None:
                raise ValueError(
                    "unresolved {{prefix}} template in {!r}: entry has no 'prefix' field".format(
                        value
                    )
                )
            value = value.replace("{{prefix}}", prefix)
        if "{{name}}" in value:
            if name is None:
                raise ValueError(
                    "unresolved {{name}} template in {!r}: no name available".format(value)
                )
            value = value.replace("{{name}}", name)
        return value
    if isinstance(value, list):
        return [_resolve_happi_templates(v, prefix, name) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_happi_templates(v, prefix, name) for k, v in value.items()}
    return value


def _derive_pvs_from_args(
    class_name: str, args: List[Any], kwargs: Dict[str, Any]
) -> Dict[str, str]:
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


def _walk_class_for_pvs(device_class_path: str, prefix: str) -> Dict[str, str]:
    """Import a compound ophyd Device class and enumerate every leaf-signal PV.

    Returns a dict keyed by dotted attribute path (e.g. ``"energy.setpoint"``,
    ``"fly.start_sig"``) mapping to the absolute PV name. Returns ``{}``
    when the class can't be walked statically:

      - ``device_class_path`` has no module prefix (can't import)
      - the class isn't a classic-ophyd ``Device`` subclass
      - the class has no Components

    Other failure modes propagate to the caller (``_process_entry``) so the
    happi entry lands in the partial-load failures list and ``
    _raise_if_partial_load`` can refuse to seed the registry rather than
    accepting a silent partial walk:

      - module import raises (PYTHONPATH gap, broken transitive dep, etc.)
      - any exception inside the walk (broken custom Device subclass,
        cyclic Component graph hitting recursion limit, etc.)

    Components are skipped (no PV emitted) when their PV can't be derived
    statically:

      - FmtCpts whose suffix contains a real ``{...}`` placeholder
      - Cpts whose ``suffix`` is None
      - Any Component where ``"suffix" not in cpt.add_prefix`` — the
        suffix is absolute and prepending parent prefix would fabricate
        a wrong PV. FmtCpt defaults to ``add_prefix=()``, so a static
        FmtCpt suffix is treated as absolute and indexed as-is.

    The walk runs at registry-load time. It pays one class import per
    compound entry (cached by ``importlib`` after the first import), which
    is acceptable for the hundreds-of-entries scale a typical happi DB hits.
    """
    if "." not in device_class_path:
        return {}

    # Lazy imports keep loader.py free of an ophyd dependency unless the
    # registry actually contains a compound device that triggers this path.
    import importlib

    try:
        from ophyd import (
            Component,
            Device,
            DynamicDeviceComponent,
            FormattedComponent,
        )
        from .path_resolver import _has_format_placeholder
    except ImportError as e:
        # ophyd is a hard dependency; if it fails to import here the
        # service is fundamentally broken — log loud and let the entry
        # fall back to prefix-only so the partial-load guard can see it.
        logger.warning(
            f"ophyd unavailable; class walk for {device_class_path!r} "
            f"skipped: {e}"
        )
        return {}

    module_name, class_name = device_class_path.rsplit(".", 1)
    # Import failures and walk-time exceptions intentionally propagate.
    # _process_entry's per-entry try/except routes them into the partial
    # -load failures list, and _raise_if_partial_load decides whether to
    # abort. Silently returning {} here would mask both a misconfigured
    # PYTHONPATH and a buggy custom Device class.
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, Device)):
        return {}

    def _is_subdevice(cpt_cls) -> bool:
        return isinstance(cpt_cls, type) and issubclass(cpt_cls, Device)

    def _absolute_pv(cpt, parent_full_prefix: str) -> Optional[str]:
        """Mirror ophyd's ``maybe_add_prefix`` for the ``suffix`` attribute.

        Returns the absolute CA PV name for this Component, or None when
        we can't resolve statically (no suffix, FmtCpt placeholder, etc.).
        """
        suffix = cpt.suffix
        if suffix is None:
            return None
        if isinstance(cpt, FormattedComponent):
            if _has_format_placeholder(suffix):
                # Needs a live parent instance to interpolate.
                return None
            suffix = suffix.format()
        # ophyd Component.add_prefix lists the kwargs that get parent
        # prefix prepended. Default for both Cpt and FmtCpt is
        # ('suffix', 'write_pv'). Operators opt sub-components out of
        # prefix prepending by passing add_prefix=() (common for
        # cross-IOC references where the suffix is an absolute PV).
        add_prefix = getattr(cpt, "add_prefix", ("suffix", "write_pv"))
        if "suffix" in add_prefix:
            return parent_full_prefix + suffix
        return suffix

    pvs: Dict[str, str] = {}

    def walk(cur_cls, path_parts: List[str], current_prefix: str) -> None:
        for attr_name in getattr(cur_cls, "component_names", ()):
            cpt = getattr(cur_cls, attr_name, None)
            if cpt is None:
                continue
            new_path = path_parts + [attr_name]

            if isinstance(cpt, DynamicDeviceComponent):
                # DDC contributes no suffix; descend into the dynamically
                # -built sub-class carrying the current prefix forward.
                walk(cpt.cls, new_path, current_prefix)
                continue
            if not isinstance(cpt, Component):
                continue

            full_pv = _absolute_pv(cpt, current_prefix)
            if full_pv is None:
                continue

            if _is_subdevice(cpt.cls):
                walk(cpt.cls, new_path, full_pv)
            else:
                pvs[".".join(new_path)] = full_pv

    walk(cls, [], prefix)
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
        failures: List[str] = []

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
                failures.append(f"{name}: {e}")

        _raise_if_partial_load(failures, len(db), "happi")

        logger.info(
            f"Loaded {len(registry.devices)} devices, "
            f"{len(registry.instantiation_specs)} instantiation specs "
            f"from happi database"
        )
        return registry

    def _process_entry(self, name: str, entry: Dict[str, Any], registry: DeviceRegistry) -> None:
        """Process a single happi database entry."""
        device_class_path = entry.get("device_class") or ""
        if not device_class_path:
            raise ValueError("missing required 'device_class' field")
        class_name = device_class_path.rsplit(".", 1)[-1]
        module_name = device_class_path.rsplit(".", 1)[0] if "." in device_class_path else None

        functional_group = entry.get("functional_group")
        device_label = _infer_device_label(class_name, functional_group=functional_group)

        caps = get_capabilities(class_name)

        # Resolve happi's {{prefix}} / {{name}} templates before deriving PVs
        # or building the instantiation spec. Happi's own loader does this in
        # happi.loader.from_container; we emulate it to stay faithful to the
        # format. Raises if a token has no substitute available.
        prefix = entry.get("prefix")
        args = _resolve_happi_templates(entry.get("args", []), prefix, name)
        kwargs = _resolve_happi_templates(entry.get("kwargs", {}), prefix, name)

        pvs = _derive_pvs_from_args(class_name, args, kwargs)

        # Compound-device case: the pattern match returned only the
        # device prefix (or nothing). Try to walk the class for leaf-
        # signal sub-PVs so they're indexed in the registry at load
        # time, instead of falling back to a runtime registration
        # workaround. The walk uses args[0] as the prefix when
        # available; if args is empty (happi format that stores the
        # prefix in the `prefix` field) we fall back to entry['prefix'].
        # Import / walk failures propagate to the per-entry handler in
        # load_registry.
        walk_prefix: Optional[str] = None
        if args and isinstance(args[0], str):
            walk_prefix = str(args[0])
        elif prefix:
            walk_prefix = prefix
        if walk_prefix and (not pvs or set(pvs.keys()) == {"prefix"}):
            walked = _walk_class_for_pvs(device_class_path, walk_prefix)
            if walked:
                # Replace prefix-only with the full sub-PV inventory.
                # The bare prefix isn't a real CA PV, so keeping it
                # alongside the sub-PVs would be noise.
                pvs = walked

        # Fallback: entry has `prefix` set but the walker either wasn't
        # applicable (no walkable class) or returned nothing usable.
        # Index the bare prefix so the device at least has SOME PV entry.
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
        failures: List[str] = []
        total_entries = 0

        with open(self.devices_path) as f:
            devices_config = yaml.safe_load(f) or {}

        beamline = self.iconfig.get("RUN_ENGINE", {}).get("md", {}).get("beamline_id")

        for module_path, device_entries in devices_config.items():
            if not isinstance(device_entries, list):
                logger.error(f"Invalid devices entry for {module_path}")
                failures.append(f"{module_path}: not a list of device entries")
                # Count the malformed module as one entry so the aggregate
                # "Failed to load N of M" message is sensible — otherwise a
                # broken module-level value reports failures out of zero.
                total_entries += 1
                continue

            for entry in device_entries:
                total_entries += 1
                name = entry.get("name")
                if not name:
                    logger.error(f"Device entry missing name in {module_path}")
                    failures.append(f"{module_path}: device entry missing required 'name' field")
                    continue

                try:
                    self._process_entry(name, entry, module_path, beamline, registry)
                except Exception as e:
                    logger.error(f"Failed to process BITS device {name}: {e}")
                    failures.append(f"{name}: {e}")

        _raise_if_partial_load(failures, total_entries, "BITS")

        logger.info(
            f"Loaded {len(registry.devices)} devices, "
            f"{len(registry.instantiation_specs)} instantiation specs "
            f"from BITS config"
        )
        return registry

    def _process_entry(
        self,
        name: str,
        entry: Dict[str, Any],
        module_path: str,
        beamline: Optional[str],
        registry: DeviceRegistry,
    ) -> None:
        """Process a single BITS device entry."""
        creator_name = entry.get("creator", name)
        labels = entry.get("labels", [])
        prefix = entry.get("prefix")
        read_pv = entry.get("read_pv")

        # Derive class name from module path + creator
        # e.g., "ophyd.sim" -> creator "det" -> class is looked up by creator name
        # e.g., "ophyd.EpicsMotor" -> module IS the class
        # e.g., "mybl.devices.MyDetector" -> last part is class
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
            f"Unknown load strategy: {load_strategy}. Valid options: auto, empty, mock, happi, bits"
        )
