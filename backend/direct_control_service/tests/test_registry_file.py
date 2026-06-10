"""File-backed registry provider (standalone / monitoring-only mode)."""

from __future__ import annotations

import json

import pytest

from direct_control.registry_client import RegistryValidationError
from direct_control.registry_file import FileRegistryProvider

_REGISTRY = {
    "devices": [
        {"name": "sample_x", "pvs": ["BL01:SAMPLE:X.RBV", "BL01:SAMPLE:X.VAL"]},
        {"name": "sample_y", "pvs": ["BL01:SAMPLE:Y.RBV"]},
    ],
    "standalone_pvs": ["mini:current"],
}


def _write(tmp_path, data, name="registry.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data) if isinstance(data, (dict, list)) else data)
    return str(p)


@pytest.fixture
def provider(tmp_path):
    return FileRegistryProvider(_write(tmp_path, _REGISTRY))


async def test_validate_device_known_and_unknown(provider):
    await provider.validate_device("sample_x")  # no raise
    with pytest.raises(RegistryValidationError):
        await provider.validate_device("no_such_device")


async def test_validate_pv_known_and_unknown(provider):
    await provider.validate_pv("BL01:SAMPLE:X.RBV")  # device component
    await provider.validate_pv("mini:current")  # standalone
    with pytest.raises(RegistryValidationError):
        await provider.validate_pv("NO:SUCH:PV")


async def test_get_owning_device(provider):
    assert await provider.get_owning_device("BL01:SAMPLE:X.RBV") == "sample_x"
    # Standalone PV: known, but no owning device.
    assert await provider.get_owning_device("mini:current") is None
    # Unknown PV also returns None (caught by the separate validate_pv gate).
    assert await provider.get_owning_device("NO:SUCH:PV") is None


def test_missing_file_fails_hard(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        FileRegistryProvider(str(tmp_path / "nope.json"))


def test_unsupported_extension_fails_hard(tmp_path):
    p = tmp_path / "registry.txt"
    p.write_text("{}")
    with pytest.raises(RuntimeError, match="Unsupported registry file extension"):
        FileRegistryProvider(str(p))


def test_non_mapping_top_level_fails_hard(tmp_path):
    with pytest.raises(RuntimeError, match="must contain a mapping"):
        FileRegistryProvider(_write(tmp_path, ["not", "a", "mapping"]))


def test_duplicate_device_fails_hard(tmp_path):
    data = {"devices": [{"name": "d", "pvs": []}, {"name": "d", "pvs": []}]}
    with pytest.raises(RuntimeError, match="duplicate device name"):
        FileRegistryProvider(_write(tmp_path, data))


def test_pv_under_two_devices_fails_hard(tmp_path):
    data = {
        "devices": [
            {"name": "a", "pvs": ["SHARED:PV"]},
            {"name": "b", "pvs": ["SHARED:PV"]},
        ]
    }
    with pytest.raises(RuntimeError, match="listed more than once"):
        FileRegistryProvider(_write(tmp_path, data))


def test_pv_both_component_and_standalone_fails_hard(tmp_path):
    data = {
        "devices": [{"name": "a", "pvs": ["DUP:PV"]}],
        "standalone_pvs": ["DUP:PV"],
    }
    with pytest.raises(RuntimeError, match="listed more than once"):
        FileRegistryProvider(_write(tmp_path, data))


def test_string_pvs_value_fails_hard(tmp_path):
    """A bare string for 'pvs' must fail, not iterate into single-char PVs."""
    data = {"devices": [{"name": "d", "pvs": "BL01:X"}]}
    with pytest.raises(RuntimeError, match="'pvs' must be a list"):
        FileRegistryProvider(_write(tmp_path, data))


def test_non_string_device_name_fails_hard(tmp_path):
    data = {"devices": [{"name": ["a", "b"], "pvs": []}]}
    with pytest.raises(RuntimeError, match="'name' must be a non-empty string"):
        FileRegistryProvider(_write(tmp_path, data))


def test_non_string_pv_fails_hard(tmp_path):
    data = {"devices": [{"name": "d", "pvs": [123]}]}
    with pytest.raises(RuntimeError, match="must be a non-empty string"):
        FileRegistryProvider(_write(tmp_path, data))


def test_device_missing_name_fails_hard(tmp_path):
    data = {"devices": [{"pvs": ["X"]}]}
    with pytest.raises(RuntimeError, match="must be a mapping with a 'name'"):
        FileRegistryProvider(_write(tmp_path, data))


async def test_empty_registry_is_valid(tmp_path):
    prov = FileRegistryProvider(_write(tmp_path, {"devices": []}))
    # Empty but well-formed: every lookup is a clean miss, not a crash.
    with pytest.raises(RegistryValidationError):
        await prov.validate_device("x")


async def test_yaml_registry_loads(tmp_path):
    pytest.importorskip("yaml")
    import yaml

    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump(_REGISTRY))
    prov = FileRegistryProvider(str(p))
    await prov.validate_device("sample_x")
