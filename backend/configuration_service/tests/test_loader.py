"""M1 regression: profile loaders refuse to seed from a partial registry."""

from __future__ import annotations

import json

import pytest
import yaml

from configuration_service.loader import (
    _MAX_FAILURES_IN_RAISE,
    BitsProfileLoader,
    HappiProfileLoader,
)


def _write_happi_profile(profile_dir, entries: dict) -> None:
    """HappiProfileLoader takes a *directory* containing happi_db.json."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "happi_db.json").write_text(json.dumps(entries))


def _good_happi_entry(name: str, prefix: str = "TST:") -> dict:
    return {
        "_id": name,
        "active": True,
        "args": ["{{prefix}}"],
        "kwargs": {"name": "{{name}}"},
        "type": "OphydItem",
        "device_class": "ophyd.EpicsMotor",
        "name": name,
        "prefix": prefix,
    }


def _write_bits_profile(profile_dir, devices: dict, iconfig: dict | None = None) -> None:
    configs = profile_dir / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "devices.yml").write_text(yaml.safe_dump(devices))
    if iconfig is not None:
        (configs / "iconfig.yml").write_text(yaml.safe_dump(iconfig))


def _good_bits_entry(name: str, prefix: str = "TST:") -> dict:
    return {"name": name, "prefix": prefix, "device_class": "EpicsMotor"}


class TestHappiPartialFailure:
    """HappiProfileLoader.load_registry raises RuntimeError if any entry fails."""

    def test_partial_failure_raises_with_aggregate_message(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile"
        _write_happi_profile(
            profile,
            {
                "good_a": _good_happi_entry("good_a"),
                "bad_one": _good_happi_entry("bad_one"),
                "good_b": _good_happi_entry("good_b"),
                "bad_two": _good_happi_entry("bad_two"),
            },
        )

        loader = HappiProfileLoader(profile)
        real_process = loader._process_entry

        def fake_process(name, entry, registry):
            if name.startswith("bad"):
                raise ValueError(f"simulated failure for {name}")
            real_process(name, entry, registry)

        monkeypatch.setattr(loader, "_process_entry", fake_process)

        with pytest.raises(RuntimeError) as excinfo:
            loader.load_registry()

        msg = str(excinfo.value)
        assert "Failed to load 2 of 4 happi entries" in msg
        assert "bad_one: simulated failure for bad_one" in msg
        assert "bad_two: simulated failure for bad_two" in msg
        assert "refusing to seed registry from partial data" in msg

    def test_all_good_loads_normally(self, tmp_path):
        profile = tmp_path / "profile"
        _write_happi_profile(
            profile,
            {"alpha": _good_happi_entry("alpha"), "beta": _good_happi_entry("beta")},
        )
        registry = HappiProfileLoader(profile).load_registry()
        assert "alpha" in registry.devices
        assert "beta" in registry.devices

    def test_inactive_entries_are_not_failures(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile"
        inactive = _good_happi_entry("dormant")
        inactive["active"] = False
        _write_happi_profile(
            profile,
            {"active_one": _good_happi_entry("active_one"), "dormant": inactive},
        )

        loader = HappiProfileLoader(profile)
        real_process = loader._process_entry
        seen: list[str] = []

        def tracking_process(name, entry, registry):
            seen.append(name)
            real_process(name, entry, registry)

        monkeypatch.setattr(loader, "_process_entry", tracking_process)
        registry = loader.load_registry()

        assert seen == ["active_one"]
        assert "active_one" in registry.devices
        assert "dormant" not in registry.devices


class TestHappiTemplateResolution:
    """M5 regression: unresolved `{{prefix}}` / `{{name}}` tokens fail loud."""

    def test_missing_prefix_with_template_arg_is_a_failure(self, tmp_path):
        profile = tmp_path / "profile"
        # Entry has args=["{{prefix}}"] but no `prefix` field — pre-fix this
        # would seed the registry with the literal "{{prefix}}" PV name.
        bad = _good_happi_entry("orphan")
        bad.pop("prefix")
        _write_happi_profile(
            profile,
            {"good": _good_happi_entry("good"), "orphan": bad},
        )

        with pytest.raises(RuntimeError) as excinfo:
            HappiProfileLoader(profile).load_registry()

        msg = str(excinfo.value)
        assert "Failed to load 1 of 2 happi entries" in msg
        assert "orphan:" in msg
        assert "{{prefix}}" in msg

    def test_missing_prefix_without_template_still_loads(self, tmp_path):
        # No `{{prefix}}` token in args/kwargs → no resolution needed → no raise.
        profile = tmp_path / "profile"
        no_template = {
            "_id": "plain",
            "active": True,
            "args": ["TST:HARDCODED"],
            "kwargs": {},
            "type": "OphydItem",
            "device_class": "ophyd.EpicsSignal",
            "name": "plain",
        }
        _write_happi_profile(profile, {"plain": no_template})
        registry = HappiProfileLoader(profile).load_registry()
        assert "plain" in registry.devices


class TestHappiRequiredFields:
    """M4 regression: missing `device_class` is a hard failure, not "Unknown"."""

    def test_missing_device_class_raises(self, tmp_path):
        profile = tmp_path / "profile"
        bad = _good_happi_entry("orphan")
        bad.pop("device_class")
        _write_happi_profile(
            profile,
            {"good": _good_happi_entry("good"), "orphan": bad},
        )

        with pytest.raises(RuntimeError) as excinfo:
            HappiProfileLoader(profile).load_registry()

        msg = str(excinfo.value)
        assert "Failed to load 1 of 2 happi entries" in msg
        assert "orphan:" in msg
        assert "device_class" in msg

    def test_empty_device_class_raises(self, tmp_path):
        profile = tmp_path / "profile"
        bad = _good_happi_entry("blank")
        bad["device_class"] = ""
        _write_happi_profile(profile, {"blank": bad})

        with pytest.raises(RuntimeError) as excinfo:
            HappiProfileLoader(profile).load_registry()

        assert "device_class" in str(excinfo.value)


class TestBitsPartialFailure:
    """BitsProfileLoader.load_registry raises RuntimeError if any entry fails."""

    def test_partial_failure_raises_with_aggregate_message(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile"
        _write_bits_profile(
            profile,
            {
                "ophyd": [
                    _good_bits_entry("good_a"),
                    _good_bits_entry("bad_one"),
                    _good_bits_entry("good_b"),
                ],
            },
        )

        loader = BitsProfileLoader(profile)
        real_process = loader._process_entry

        def fake_process(name, entry, module_path, beamline, registry):
            if name.startswith("bad"):
                raise ValueError(f"simulated failure for {name}")
            real_process(name, entry, module_path, beamline, registry)

        monkeypatch.setattr(loader, "_process_entry", fake_process)

        with pytest.raises(RuntimeError) as excinfo:
            loader.load_registry()

        msg = str(excinfo.value)
        assert "Failed to load 1 of 3 BITS entries" in msg
        assert "bad_one: simulated failure for bad_one" in msg
        assert "refusing to seed registry from partial data" in msg

    def test_missing_name_raises(self, tmp_path):
        profile = tmp_path / "profile"
        _write_bits_profile(
            profile,
            {
                "ophyd": [
                    _good_bits_entry("named"),
                    {"prefix": "TST:other", "device_class": "EpicsMotor"},
                ],
            },
        )

        with pytest.raises(RuntimeError) as excinfo:
            BitsProfileLoader(profile).load_registry()

        msg = str(excinfo.value)
        assert "missing required 'name' field" in msg
        assert "Failed to load 1 of 2 BITS entries" in msg

    def test_non_list_module_raises(self, tmp_path):
        profile = tmp_path / "profile"
        _write_bits_profile(
            profile,
            {
                "ophyd": [_good_bits_entry("ok")],
                "broken_module": "not a list",
            },
        )

        with pytest.raises(RuntimeError) as excinfo:
            BitsProfileLoader(profile).load_registry()

        assert "broken_module: not a list of device entries" in str(excinfo.value)

    def test_all_good_loads_normally(self, tmp_path):
        profile = tmp_path / "profile"
        _write_bits_profile(
            profile,
            {"ophyd": [_good_bits_entry("alpha"), _good_bits_entry("beta")]},
        )
        registry = BitsProfileLoader(profile).load_registry()
        assert "alpha" in registry.devices
        assert "beta" in registry.devices


class TestPartialLoadMessageCap:
    """The aggregate RuntimeError message caps the embedded failure list.

    A registry of thousands of broken entries would otherwise build a
    multi-MB string that gets re-formatted by every traceback layer.
    """

    def test_message_caps_failure_list_with_overflow_suffix(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile"
        # One past the cap so the suffix is exercised.
        n_failures = _MAX_FAILURES_IN_RAISE + 1
        _write_happi_profile(
            profile,
            {f"bad_{i}": _good_happi_entry(f"bad_{i}") for i in range(n_failures)},
        )

        loader = HappiProfileLoader(profile)

        def always_raise(name, entry, registry):
            raise ValueError(f"simulated failure for {name}")

        monkeypatch.setattr(loader, "_process_entry", always_raise)

        with pytest.raises(RuntimeError) as excinfo:
            loader.load_registry()

        msg = str(excinfo.value)
        assert f"Failed to load {n_failures} of {n_failures} happi entries" in msg
        assert "...and 1 more" in msg
        # First N appear by name; the (N+1)th must NOT be inlined.
        assert "bad_0: simulated failure for bad_0" in msg
        assert f"bad_{_MAX_FAILURES_IN_RAISE}:" not in msg

    def test_message_omits_suffix_when_at_or_under_cap(self, tmp_path, monkeypatch):
        profile = tmp_path / "profile"
        _write_happi_profile(
            profile,
            {f"bad_{i}": _good_happi_entry(f"bad_{i}") for i in range(_MAX_FAILURES_IN_RAISE)},
        )

        loader = HappiProfileLoader(profile)
        monkeypatch.setattr(
            loader, "_process_entry", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        )

        with pytest.raises(RuntimeError) as excinfo:
            loader.load_registry()
        assert "...and " not in str(excinfo.value)
