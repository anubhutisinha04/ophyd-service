"""M1 regression: profile loaders refuse to seed from a partial registry."""

from __future__ import annotations

import json

import pytest
import yaml

from configuration_service.loader import (
    _MAX_FAILURES_IN_RAISE,
    BitsProfileLoader,
    HappiProfileLoader,
    _walk_class_for_pvs,
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


# ---------------------------------------------------------------------------
# Fixture device classes for the class-walker tests below.
#
# They must live at module scope so `_walk_class_for_pvs` can
# `importlib.import_module(__name__)` and `getattr` them by name. Defining
# them inside a test function would put them under a local namespace the
# walker can't reach.
# ---------------------------------------------------------------------------

from ophyd import (  # noqa: E402  (module-level only to keep fixtures importable)
    Component as Cpt,
    Device,
    DynamicDeviceComponent as DDC,
    EpicsSignal,
    EpicsSignalRO,
    FormattedComponent as FmtCpt,
)


class _EnergyAxis(Device):
    setpoint = Cpt(EpicsSignal, "Enrgy-SP")
    readback = Cpt(EpicsSignalRO, "Enrgy-I")


class _FlyBlock(Device):
    start_sig = Cpt(EpicsSignal, "Fly:Start-SP")
    velocity = Cpt(EpicsSignal, "Fly:Velo-SP")


class _Mono(Device):
    """Compound device: nested sub-devices + a leaf at the top level."""

    energy = Cpt(_EnergyAxis, "")
    fly = Cpt(_FlyBlock, "")
    move_cmd = Cpt(EpicsSignal, "Cmd:Move-SP")


class _WithFmtCpt(Device):
    """Mixes a placeholder FmtCpt (must be skipped) with a normal Cpt."""

    enable = Cpt(EpicsSignal, "Enable-Sel")
    stop_sig = FmtCpt(EpicsSignal, "{self.parent.prefix}}}STOP")


class _WithStaticFmtCpt(Device):
    """FmtCpt defaults to add_prefix=(), so its suffix is treated as absolute."""

    static = FmtCpt(EpicsSignal, "Static-PV")


class _WithAbsolutePrefixCpt(Device):
    """Plain Cpt with explicit add_prefix=() — suffix is absolute, parent prefix not prepended."""

    relative = Cpt(EpicsSignal, "Rel-PV")
    absolute = Cpt(EpicsSignal, "SR:Beam:Current", add_prefix=())


class _DDCParent(Device):
    """DynamicDeviceComponent: its children should appear in the output."""

    rois = DDC(
        {
            "roi1": (EpicsSignal, "ROI1:Count", {}),
            "roi2": (EpicsSignal, "ROI2:Count", {}),
        }
    )


class _BareDevice(Device):
    """Device subclass with no components — walker should return empty."""

    pass


class _NotADevice:
    """Plain class — walker must reject it without crashing."""

    pass


class TestWalkClassForPVs:
    """`_walk_class_for_pvs` enumerates every leaf-signal PV under a Device."""

    def test_compound_device_returns_dotted_leaf_paths(self):
        pvs = _walk_class_for_pvs(f"{__name__}._Mono", "XF:23ID2-OP{Mono}")
        assert pvs == {
            "energy.setpoint": "XF:23ID2-OP{Mono}Enrgy-SP",
            "energy.readback": "XF:23ID2-OP{Mono}Enrgy-I",
            "fly.start_sig": "XF:23ID2-OP{Mono}Fly:Start-SP",
            "fly.velocity": "XF:23ID2-OP{Mono}Fly:Velo-SP",
            "move_cmd": "XF:23ID2-OP{Mono}Cmd:Move-SP",
        }

    def test_fmtcpt_with_placeholder_is_skipped(self):
        # The {self.parent.prefix} placeholder can't be resolved without a
        # live instance — same constraint as path_resolver._walk_class. The
        # plain Cpt next to it must still appear.
        pvs = _walk_class_for_pvs(f"{__name__}._WithFmtCpt", "TST:")
        assert pvs == {"enable": "TST:Enable-Sel"}

    def test_fmtcpt_without_placeholder_uses_default_add_prefix(self):
        # FmtCpt inherits Component's default add_prefix=('suffix',
        # 'write_pv'), so static FmtCpt suffixes still get prefix
        # prepended just like a plain Cpt. (Operators wanting an
        # absolute FmtCpt set add_prefix=() explicitly — covered by
        # test_cpt_with_empty_add_prefix_indexed_as_absolute.)
        pvs = _walk_class_for_pvs(f"{__name__}._WithStaticFmtCpt", "TST:")
        assert pvs == {"static": "TST:Static-PV"}

    def test_cpt_with_empty_add_prefix_indexed_as_absolute(self):
        # An explicit add_prefix=() on a plain Cpt means the suffix is
        # an absolute PV (common pattern for cross-IOC references).
        # The other Component in the same class uses default add_prefix
        # and must still get the parent prefix prepended.
        pvs = _walk_class_for_pvs(
            f"{__name__}._WithAbsolutePrefixCpt", "XF:23ID2-OP{Mono}"
        )
        assert pvs == {
            "relative": "XF:23ID2-OP{Mono}Rel-PV",
            "absolute": "SR:Beam:Current",
        }

    def test_ddc_children_are_walked(self):
        pvs = _walk_class_for_pvs(f"{__name__}._DDCParent", "DET:")
        assert pvs == {
            "rois.roi1": "DET:ROI1:Count",
            "rois.roi2": "DET:ROI2:Count",
        }

    def test_bare_device_returns_empty(self):
        assert _walk_class_for_pvs(f"{__name__}._BareDevice", "TST:") == {}

    def test_non_device_class_returns_empty(self):
        assert _walk_class_for_pvs(f"{__name__}._NotADevice", "TST:") == {}

    def test_missing_module_propagates_import_error(self):
        # An unimportable device_class means the happi DB references a
        # class that doesn't exist in this deployment. Per the no-silent
        # -fallbacks rule, the error must propagate so the caller
        # (_process_entry) can mark the entry as failed and let
        # _raise_if_partial_load decide whether to abort the load.
        with pytest.raises(ModuleNotFoundError):
            _walk_class_for_pvs("nonexistent_module_xyz.WhateverClass", "TST:")

    def test_missing_class_in_real_module_returns_empty(self):
        # The module imports fine — class lookup returning None is a
        # different shape (e.g. someone passed a non-Device class name
        # the pattern matcher should have handled). Silent return so
        # the caller falls through to prefix-only.
        assert _walk_class_for_pvs(f"{__name__}.NoSuchClass", "TST:") == {}

    def test_bare_class_name_no_module_returns_empty(self):
        # No dot → can't split into module + class.
        assert _walk_class_for_pvs("JustAName", "TST:") == {}


class TestLoaderWalksCompoundDevices:
    """End-to-end: a happi entry pointing at a compound class registers all sub-PVs."""

    def test_compound_device_entry_seeds_sub_pvs_into_registry(self, tmp_path):
        profile = tmp_path / "profile"
        entry = {
            "_id": "mono",
            "active": True,
            "args": ["{{prefix}}"],
            "kwargs": {"name": "{{name}}"},
            "type": "OphydItem",
            "device_class": f"{__name__}._Mono",
            "name": "mono",
            "prefix": "XF:23ID2-OP{Mono}",
        }
        _write_happi_profile(profile, {"mono": entry})

        registry = HappiProfileLoader(profile).load_registry()

        # The mono device is registered, AND every leaf PV under it is
        # indexed by PV name (so direct-control's per-PV validate call
        # finds them without a runtime standalone-PV registration).
        assert "mono" in registry.devices
        expected_pvs = {
            "XF:23ID2-OP{Mono}Enrgy-SP",
            "XF:23ID2-OP{Mono}Enrgy-I",
            "XF:23ID2-OP{Mono}Fly:Start-SP",
            "XF:23ID2-OP{Mono}Fly:Velo-SP",
            "XF:23ID2-OP{Mono}Cmd:Move-SP",
        }
        assert expected_pvs.issubset(set(registry.pvs))
        # And the bare prefix is NOT in the registry — it isn't a real CA
        # PV, so leaving it in would be noise that masks real misses.
        assert "XF:23ID2-OP{Mono}" not in registry.pvs

    def test_unimportable_compound_class_fails_load_loudly(self, tmp_path):
        # If the device class can't be imported (PYTHONPATH gap, missing
        # site module), the walker propagates the ImportError. The
        # loader's per-entry handler logs it into the failures list, and
        # _raise_if_partial_load refuses to seed a partial registry.
        # No silent fallback to prefix-only — operators must fix the
        # happi DB or the PYTHONPATH.
        profile = tmp_path / "profile"
        entry = {
            "_id": "ghost",
            "active": True,
            "args": ["XF:99ID-OP{Ghost}"],
            "kwargs": {"name": "{{name}}"},
            "type": "OphydItem",
            "device_class": "nonexistent_pkg_xyz.Ghost",
            "name": "ghost",
            "prefix": "XF:99ID-OP{Ghost}",
        }
        _write_happi_profile(profile, {"ghost": entry})

        with pytest.raises(RuntimeError) as excinfo:
            HappiProfileLoader(profile).load_registry()
        msg = str(excinfo.value)
        assert "ghost:" in msg
        assert "nonexistent_pkg_xyz" in msg

    def test_entry_with_empty_args_walks_using_prefix_field(self, tmp_path):
        # Some happi formats store the prefix in entry['prefix'] with
        # args=[] (no positional ctor args). The walker must still run,
        # using entry['prefix'] as the walk root, so compound devices
        # in this format get their sub-PVs indexed too.
        profile = tmp_path / "profile"
        entry = {
            "_id": "mono_alt",
            "active": True,
            "args": [],
            "kwargs": {"name": "{{name}}"},
            "type": "OphydItem",
            "device_class": f"{__name__}._Mono",
            "name": "mono_alt",
            "prefix": "XF:23ID2-OP{Mono}",
        }
        _write_happi_profile(profile, {"mono_alt": entry})

        registry = HappiProfileLoader(profile).load_registry()
        assert "mono_alt" in registry.devices
        assert "XF:23ID2-OP{Mono}Enrgy-SP" in registry.pvs
        assert "XF:23ID2-OP{Mono}Cmd:Move-SP" in registry.pvs
        # Bare prefix not retained when the walk produced real PVs.
        assert "XF:23ID2-OP{Mono}" not in registry.pvs

    def test_epics_signal_still_uses_pattern_match_not_walk(self, tmp_path):
        # Walker shouldn't run for shapes the pattern matcher already
        # handles — EpicsSignal subclass yields no Components, so a walk
        # would return {} and clobber the pattern-derived "readback" PV.
        profile = tmp_path / "profile"
        entry = {
            "_id": "scalar",
            "active": True,
            "args": ["TST:Single-SP"],
            "kwargs": {},
            "type": "OphydItem",
            "device_class": "ophyd.EpicsSignal",
            "name": "scalar",
        }
        _write_happi_profile(profile, {"scalar": entry})

        registry = HappiProfileLoader(profile).load_registry()
        assert "TST:Single-SP" in registry.pvs
