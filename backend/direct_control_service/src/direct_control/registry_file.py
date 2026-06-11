"""File-backed registry provider.

Reads a static device/PV registry from a local JSON or YAML file instead of
querying configuration_service. Use for standalone / monitoring-only
deployments that have no configuration_service.

Provides the same existence-validation surface as ``RegistryClient`` (the
RegistryProvider protocol: ``validate_pv`` / ``validate_device`` /
``get_owning_device`` / ``cleanup``), but NOT device-lock coordination state:
locks are runtime, shared, mutable state that a static file cannot represent,
so file mode is normally paired with ``coordination_check_enabled=false``.

File schema (JSON shown; YAML is the same shape)::

    {
      "devices": [
        {"name": "sample_x", "pvs": ["BL01:SAMPLE:X.RBV", "BL01:SAMPLE:X.VAL"]},
        {
          "name": "m1",
          "pvs": ["BL01:M1.RBV"],
          "device_class": "ophyd.EpicsMotor",
          "args": ["BL01:M1"],
          "kwargs": {},
          "framework": "ophyd-sync"
        }
      ],
      "standalone_pvs": ["mini:current"]
    }

``standalone_pvs`` is optional — PVs with no owning device (no device-level
lock concept). ``device_class``/``args``/``kwargs``/``framework`` are
optional per device: with ``device_class`` present the device supports
device-level control (``/api/v1/device/execute`` instantiates a live ophyd /
ophyd-async object from it); without it the device is PV-gateway-only.
``framework`` ("ophyd-sync" | "ophyd-async") is an advisory tag checked
against the imported class at instantiation time — a mismatch is a hard
error. The file is parsed eagerly at construction so a missing path, bad
syntax, or malformed shape fails hard at startup rather than on the first
request. There is no silent fallback to an empty registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import structlog

from .models import InstantiationSpec
from .registry_client import RegistryValidationError

_VALID_FRAMEWORKS = ("ophyd-sync", "ophyd-async")

logger = structlog.get_logger(__name__)


def _load_registry_file(path: str) -> dict:
    """Read + parse the registry file, failing hard on any problem."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Registry file not found: {path}")

    text = p.read_text()
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                f"Registry file {path} is YAML but PyYAML is not installed. "
                f"Install pyyaml or use a .json registry file."
            ) from exc
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise RuntimeError(
            f"Unsupported registry file extension {suffix!r} for {path}; use .json, .yaml, or .yml"
        )

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Registry file {path} must contain a mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


class FileRegistryProvider:
    """Registry provider backed by a static JSON/YAML file.

    Implements the RegistryProvider protocol used by direct_control's write and
    monitoring paths.
    """

    def __init__(self, path: str):
        self._path = path
        data = _load_registry_file(path)

        self._devices: set[str] = set()
        # pv -> owning device name (None for standalone PVs)
        self._pv_owner: Dict[str, Optional[str]] = {}
        # device name -> instantiation spec, for devices that carry class info
        self._specs: Dict[str, InstantiationSpec] = {}

        devices = data.get("devices", [])
        if not isinstance(devices, list):
            raise RuntimeError(
                f"Registry file {path}: 'devices' must be a list, got {type(devices).__name__}"
            )

        for entry in devices:
            if not isinstance(entry, dict) or "name" not in entry:
                raise RuntimeError(
                    f"Registry file {path}: each device must be a mapping with a "
                    f"'name', got {entry!r}"
                )
            name = entry["name"]
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    f"Registry file {path}: device 'name' must be a non-empty string, got {name!r}"
                )
            if name in self._devices:
                raise RuntimeError(f"Registry file {path}: duplicate device name {name!r}")
            self._devices.add(name)
            pvs = entry.get("pvs", []) or []
            if not isinstance(pvs, list):
                raise RuntimeError(
                    f"Registry file {path}: device {name!r} 'pvs' must be a list, "
                    f"got {type(pvs).__name__}"
                )
            for pv in pvs:
                self._register_pv(path, pv, name)

            spec = self._parse_spec(path, name, entry)
            if spec is not None:
                self._specs[name] = spec

        standalone = data.get("standalone_pvs", []) or []
        if not isinstance(standalone, list):
            raise RuntimeError(
                f"Registry file {path}: 'standalone_pvs' must be a list, got "
                f"{type(standalone).__name__}"
            )
        for pv in standalone:
            self._register_pv(path, pv, None)

        logger.info(
            "file_registry_loaded",
            path=path,
            devices=len(self._devices),
            pvs=len(self._pv_owner),
            instantiable_devices=len(self._specs),
        )

    @staticmethod
    def _parse_spec(path: str, name: str, entry: dict) -> Optional[InstantiationSpec]:
        """Parse the optional device-control fields of one device entry.

        Returns None when the entry has no ``device_class`` (PV-gateway-only
        device). Any malformed control field fails hard at load — a registry
        that promises device control must be fully well-formed.
        """
        device_class = entry.get("device_class")
        control_extras = [k for k in ("args", "kwargs", "framework") if k in entry]
        if device_class is None:
            if control_extras:
                raise RuntimeError(
                    f"Registry file {path}: device {name!r} has {control_extras} "
                    f"but no 'device_class'; add the class path or remove them"
                )
            return None

        if not isinstance(device_class, str) or "." not in device_class:
            raise RuntimeError(
                f"Registry file {path}: device {name!r} 'device_class' must be a "
                f"fully-qualified import path (e.g. 'ophyd.EpicsMotor'), "
                f"got {device_class!r}"
            )
        args = entry.get("args", [])
        if not isinstance(args, list):
            raise RuntimeError(
                f"Registry file {path}: device {name!r} 'args' must be a list, "
                f"got {type(args).__name__}"
            )
        kwargs = entry.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise RuntimeError(
                f"Registry file {path}: device {name!r} 'kwargs' must be a "
                f"mapping, got {type(kwargs).__name__}"
            )
        framework = entry.get("framework")
        if framework is not None and framework not in _VALID_FRAMEWORKS:
            raise RuntimeError(
                f"Registry file {path}: device {name!r} 'framework' must be one "
                f"of {_VALID_FRAMEWORKS}, got {framework!r}"
            )
        return InstantiationSpec(
            name=name,
            device_class=device_class,
            args=args,
            kwargs=kwargs,
            framework=framework,
        )

    def _register_pv(self, path: str, pv: object, owner: Optional[str]) -> None:
        """Add one PV→owner mapping, failing hard on bad type or duplicate."""
        if not isinstance(pv, str) or not pv:
            where = f"device {owner!r}" if owner is not None else "standalone_pvs"
            raise RuntimeError(
                f"Registry file {path}: PV in {where} must be a non-empty string, got {pv!r}"
            )
        if pv in self._pv_owner:
            prior = self._pv_owner[pv]
            raise RuntimeError(
                f"Registry file {path}: PV {pv!r} is listed more than once "
                f"(already owned by {prior!r})"
            )
        self._pv_owner[pv] = owner

    async def validate_pv(self, pv_name: str) -> None:
        """Raise RegistryValidationError if the PV is not in the file registry."""
        if pv_name not in self._pv_owner:
            raise RegistryValidationError(pv_name, "PV")

    async def validate_device(self, device_name: str) -> None:
        """Raise RegistryValidationError if the device is not in the file registry."""
        if device_name not in self._devices:
            raise RegistryValidationError(device_name, "Device")

    async def get_owning_device(self, pv_name: str) -> Optional[str]:
        """Return the device owning this PV, or None for standalone/unknown PVs.

        Matches RegistryClient: unknown PVs return None here and are caught by
        the separate validate_pv gate, so this never shadows a real owner.
        """
        return self._pv_owner.get(pv_name)

    async def get_instantiation_spec(self, device_name: str) -> Optional[InstantiationSpec]:
        """Return the device's spec, or None when its entry has no class info
        (PV-gateway-only device — device-level control unavailable)."""
        return self._specs.get(device_name)

    async def cleanup(self) -> None:
        return None
