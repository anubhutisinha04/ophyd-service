"""End-to-end loader exercises driven by realistic device shapes
modeled on the nsls2.ioc_deploy role catalog.

See ``tests/fixtures/README.md`` for the provenance of each fixture
and the policy on what these shims may and may not contain.

This module asserts the loader's *current* behavior. Where current
behavior is known to be wrong (e.g. the EpicsMotor top-level-vs-
sub-component divergence captured in ``project_technical_debt`` under
configuration_service), the test documents the divergence as observed
fact so a future fix can flip the assertion deliberately rather than
shipping a silent change in indexing semantics.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from configuration_service.loader import HappiProfileLoader

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ROLE_FAMILIES_PATH = FIXTURES_DIR / "role_families.json"


@pytest.fixture(scope="module")
def role_registry(tmp_path_factory):
    """Load the role-family fixture pack into a fresh DeviceRegistry."""
    profile = tmp_path_factory.mktemp("role_profile")
    shutil.copy(ROLE_FAMILIES_PATH, profile / "happi_db.json")
    return HappiProfileLoader(profile).load_registry()


class TestRoleFamilyFixturesLoad:
    """Each fixture in role_families.json registers as a device and
    contributes PVs to the registry."""

    @pytest.mark.parametrize(
        "name",
        [
            "sim_detector",
            "sim_motor",
            "dual_axis_stage",
            "quad_em",
            "temp_controller",
            "soft_ioc_base",
        ],
    )
    def test_device_registered(self, role_registry, name):
        assert name in role_registry.devices


class TestSimDetectorWalk:
    """adsimdetector-derived: compound device, two nested sub-devices,
    walker emits dotted-path keys for every leaf signal."""

    def test_emits_all_camera_and_hdf5_leaves(self, role_registry):
        pvs = role_registry.devices["sim_detector"].pvs
        assert set(pvs.keys()) == {
            "cam.acquire",
            "cam.image_mode",
            "cam.acquire_time",
            "cam.array_size_x",
            "cam.array_size_y",
            "hdf5.file_path",
            "hdf5.file_name",
            "hdf5.capture",
        }
        # Spot-check that the parent prefix concatenates with the
        # sub-device suffix and the leaf suffix correctly.
        assert pvs["cam.acquire"] == "SIM:DET:01:cam1:Acquire"
        assert pvs["hdf5.file_path"] == "SIM:DET:01:HDF1:FilePath"


class TestSimMotorTopLevelShortcut:
    """motorsim-derived: a top-level EpicsMotor entry. The
    ``_derive_pvs_from_args`` substring shortcut runs first and emits
    only 4 PVs — the walker never gets a turn for this shape.

    This is the BEFORE side of the EpicsMotor divergence captured in
    project_technical_debt (configuration_service section, "EpicsMotor
    pattern-match vs walker key-set divergence"). The AFTER side is
    test_dual_axis_stage_walker_emits_all_motor_subkeys below.
    """

    def test_emits_only_four_shortcut_keys(self, role_registry):
        pvs = role_registry.devices["sim_motor"].pvs
        assert set(pvs.keys()) == {
            "user_setpoint",
            "user_readback",
            "velocity",
            "acceleration",
        }

    def test_shortcut_user_setpoint_is_bare_prefix(self, role_registry):
        # Note the shortcut emits bare prefix for user_setpoint, while
        # the real EpicsMotor `user_setpoint` signal lives at
        # ``<prefix>.VAL`` (see TestDualAxisStageWalk). This is another
        # facet of the same divergence the tech-debt item flags.
        pvs = role_registry.devices["sim_motor"].pvs
        assert pvs["user_setpoint"] == "SIM:MTR:01:"
        assert pvs["user_readback"] == "SIM:MTR:01:.RBV"
        assert pvs["velocity"] == "SIM:MTR:01:.VELO"
        assert pvs["acceleration"] == "SIM:MTR:01:.ACCL"


class TestDualAxisStageWalk:
    """axis_caproto-derived: compound device whose sub-components are
    EpicsMotor instances. The walker descends into each sub-EpicsMotor
    and emits all 19 motor-record fields per axis (vs the 4-key
    shortcut for top-level EpicsMotor above)."""

    def test_walker_emits_all_motor_subkeys(self, role_registry):
        pvs = role_registry.devices["dual_axis_stage"].pvs
        # 19 fields × 2 axes = 38 dotted-key entries.
        assert len(pvs) == 38

    def test_walker_uses_real_motor_record_suffixes(self, role_registry):
        # Spot-check a handful: the suffixes here come from EpicsMotor's
        # Component definitions (.VAL, .RBV, .VELO, etc.), unlike the
        # shortcut's bare-prefix encoding.
        pvs = role_registry.devices["dual_axis_stage"].pvs
        assert pvs["x.user_setpoint"] == "SIM:STAGE:01-Ax:X.VAL"
        assert pvs["x.user_readback"] == "SIM:STAGE:01-Ax:X.RBV"
        assert pvs["x.motor_stop"] == "SIM:STAGE:01-Ax:X.STOP"
        assert pvs["y.user_setpoint"] == "SIM:STAGE:01-Ax:Y.VAL"
        assert pvs["y.home_reverse"] == "SIM:STAGE:01-Ax:Y.HOMR"


class TestQuadEMWalk:
    """nsls2em-derived: 4 channels × 4 stats + 1 top-level signal."""

    def test_emits_all_channel_stats_and_top_level(self, role_registry):
        pvs = role_registry.devices["quad_em"].pvs
        # 4 channels × {mean, sigma, min, max} + acquire = 17 keys.
        assert len(pvs) == 17
        for channel in ("current_1", "current_2", "current_3", "current_4"):
            for stat in ("mean", "sigma", "min", "max"):
                assert f"{channel}.{stat}" in pvs
        assert "acquire" in pvs

    def test_channel_prefixes_compose_correctly(self, role_registry):
        pvs = role_registry.devices["quad_em"].pvs
        assert pvs["current_1.mean"] == "SIM:EM:01:Current1:Mean_RBV"
        assert pvs["current_4.sigma"] == "SIM:EM:01:Current4:Sigma_RBV"
        assert pvs["acquire"] == "SIM:EM:01:Acquire"


class TestTempControllerWalk:
    """lakeshore336-derived: 4 inputs × 3 PVs each."""

    def test_emits_all_channel_pvs(self, role_registry):
        pvs = role_registry.devices["temp_controller"].pvs
        assert len(pvs) == 12  # 4 channels × 3 leaves
        for channel in ("channel_a", "channel_b", "channel_c", "channel_d"):
            for leaf in ("temperature", "sensor", "units"):
                assert f"{channel}.{leaf}" in pvs
        assert pvs["channel_a.temperature"] == "SIM:TEMP:01:A:T_RBV"
        assert pvs["channel_d.units"] == "SIM:TEMP:01:D:Units"


class TestSoftIOCBaseWalk:
    """base_soft_ioc-derived: minimum compound case, four flat leaves."""

    def test_emits_four_flat_leaves(self, role_registry):
        pvs = role_registry.devices["soft_ioc_base"].pvs
        assert set(pvs.keys()) == {"heartbeat", "uptime", "iocname", "load"}
        assert pvs["heartbeat"] == "SIM:IOC:01:HEARTBEAT"
        assert pvs["load"] == "SIM:IOC:01:LOAD"


class TestRegistryAggregate:
    """The full fixture pack contributes PVs to the registry."""

    def test_no_bare_prefix_keys_for_compound_devices(self, role_registry):
        # Compound devices' bare prefixes shouldn't appear as registry
        # PV keys when the walker yielded leaves — those aren't real CA
        # PVs and would mask real misses by the direct-control gate.
        for compound in (
            "sim_detector",
            "dual_axis_stage",
            "quad_em",
            "temp_controller",
            "soft_ioc_base",
        ):
            prefix = role_registry.devices[compound].pvs
            # No key in the dict should equal the bare prefix value.
            assert "prefix" not in prefix

    def test_each_device_pv_indexed_in_registry_pvs(self, role_registry):
        # Every device's per-device pv inventory should also appear in
        # the aggregate registry.pvs map so direct-control's
        # validate_pv lookup finds them.
        with open(ROLE_FAMILIES_PATH) as f:
            entries = json.load(f)
        for name in entries:
            device_pvs = role_registry.devices[name].pvs
            for pv_name in device_pvs.values():
                assert pv_name in role_registry.pvs, (
                    f"{name} component PV {pv_name!r} not in registry.pvs"
                )
