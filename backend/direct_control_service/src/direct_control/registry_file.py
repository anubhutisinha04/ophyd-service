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
        {"name": "sample_x", "pvs": ["BL01:SAMPLE:X.RBV", "BL01:SAMPLE:X.VAL"]}
      ],
      "standalone_pvs": ["mini:current"]
    }

``standalone_pvs`` is optional — PVs with no owning device (no device-level
lock concept). The file is parsed eagerly at construction so a missing path,
bad syntax, or malformed shape fails hard at startup rather than on the first
request. There is no silent fallback to an empty registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import structlog

from .registry_client import RegistryValidationError

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
            f"Unsupported registry file extension {suffix!r} for {path}; "
            f"use .json, .yaml, or .yml"
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

        devices = data.get("devices", [])
        if not isinstance(devices, list):
            raise RuntimeError(
                f"Registry file {path}: 'devices' must be a list, got "
                f"{type(devices).__name__}"
            )

        for entry in devices:
            if not isinstance(entry, dict) or "name" not in entry:
                raise RuntimeError(
                    f"Registry file {path}: each device must be a mapping with a "
                    f"'name', got {entry!r}"
                )
            name = entry["name"]
            if name in self._devices:
                raise RuntimeError(
                    f"Registry file {path}: duplicate device name {name!r}"
                )
            self._devices.add(name)
            for pv in entry.get("pvs", []) or []:
                if pv in self._pv_owner:
                    raise RuntimeError(
                        f"Registry file {path}: PV {pv!r} listed under more than "
                        f"one device"
                    )
                self._pv_owner[pv] = name

        for pv in data.get("standalone_pvs", []) or []:
            if pv in self._pv_owner:
                raise RuntimeError(
                    f"Registry file {path}: PV {pv!r} is both a device component "
                    f"and a standalone PV"
                )
            self._pv_owner[pv] = None

        logger.info(
            "file_registry_loaded",
            path=path,
            devices=len(self._devices),
            pvs=len(self._pv_owner),
        )

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

    async def cleanup(self) -> None:
        return None
