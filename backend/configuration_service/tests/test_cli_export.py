"""Tests for the ``configuration-service export`` CLI subcommand.

Exercises ``_run_export`` directly with a constructed argparse namespace. The
function builds its own throwaway SQLite database, so no DB fixture is needed.
"""

import json
from argparse import Namespace

import yaml

from configuration_service.cli import _run_export
from configuration_service.loader import BitsProfileLoader


def _export_args(**overrides) -> Namespace:
    """Build an export namespace with mock-data defaults, overridable per test."""
    base = {
        "command": "export",
        "format": "happi",
        "output": None,
        "profile_path": None,
        "load_strategy": "mock",
        "use_mock_data": True,
    }
    base.update(overrides)
    return Namespace(**base)


def test_export_bits_mock_writes_roundtrippable_yaml(tmp_path):
    """`export --format bits` writes a devices.yml that reloads via BitsProfileLoader."""
    out = tmp_path / "devices.yml"
    rc = _run_export(_export_args(format="bits", output=str(out)))
    assert rc == 0

    data = yaml.safe_load(out.read_text())
    assert isinstance(data, dict) and data  # non-empty guarneri mapping

    # Round-trip: the exported file reloads into an equivalent registry.
    profile = tmp_path / "profile" / "configs"
    profile.mkdir(parents=True)
    (profile / "devices.yml").write_text(out.read_text())
    reloaded = BitsProfileLoader(tmp_path / "profile").load_registry()
    assert reloaded.devices
    assert reloaded.instantiation_specs


def test_export_happi_mock_to_stdout(capsys):
    """`export --format happi` (default) prints valid happi JSON to stdout."""
    rc = _run_export(_export_args(format="happi", output=None))
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict) and payload
    # happi entries carry the canonical id/device_class fields.
    entry = next(iter(payload.values()))
    assert entry["_id"] == entry["name"]
    assert "device_class" in entry


def test_export_requires_profile_path_for_disk_strategies(capsys):
    """A disk-backed strategy without a profile path exits 2 with guidance."""
    rc = _run_export(_export_args(format="bits", load_strategy="auto", use_mock_data=False))
    assert rc == 2
    assert "profile-path" in capsys.readouterr().err
