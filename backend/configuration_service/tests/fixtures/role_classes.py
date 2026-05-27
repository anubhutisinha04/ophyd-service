"""Community-clean Device shims modeled on representative
nsls2.ioc_deploy roles.

The roles in ``nsls2.ioc_deploy/roles/device_roles/`` describe what
PVs real IOCs publish per device family (AreaDetector cameras, motor
controllers, electrometers, temperature controllers, soft-IOC bases,
...). Their example configs use NSLS-II PV conventions like
``XF:31ID1-ES{Sim-Cam:1}``. To exercise the loader's class walker
against realistic compound-device shapes without baking NSLS-II
conventions into ``main``, we mirror the PV shape of one
representative role per family here using upstream ophyd primitives
(``Device``, ``Component``, ``EpicsSignal``, ``EpicsSignalRO``,
``EpicsMotor``) and generic sanitized prefixes.

Each shim's PV shape is described in ``../fixtures/role_families.json``
via a happi entry that points at the class path. The matching test in
``tests/test_loader_role_fixtures.py`` parametrizes over those entries
and asserts the walker's dotted-key output.

Classes MUST live at module scope so ``_walk_class_for_pvs`` can do
``importlib.import_module(...)`` + ``getattr(module, name)``.
"""

from __future__ import annotations

from ophyd import (
    Component as Cpt,
    Device,
    EpicsMotor,
    EpicsSignal,
    EpicsSignalRO,
)


# ── adsimdetector ───────────────────────────────────────────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/adsimdetector/
# Shape: areaDetector camera with cam plugin + HDF5 file writer.


class _SimCamPlugin(Device):
    acquire = Cpt(EpicsSignal, "Acquire")
    image_mode = Cpt(EpicsSignal, "ImageMode")
    acquire_time = Cpt(EpicsSignal, "AcquireTime")
    array_size_x = Cpt(EpicsSignalRO, "ArraySizeX_RBV")
    array_size_y = Cpt(EpicsSignalRO, "ArraySizeY_RBV")


class _SimHDF5Plugin(Device):
    file_path = Cpt(EpicsSignal, "FilePath")
    file_name = Cpt(EpicsSignal, "FileName")
    capture = Cpt(EpicsSignal, "Capture")


class _SimDetector(Device):
    cam = Cpt(_SimCamPlugin, "cam1:")
    hdf5 = Cpt(_SimHDF5Plugin, "HDF1:")


# ── motorsim (top-level EpicsMotor) ─────────────────────────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/motorsim/
# Top-level EpicsMotor takes the 4-key shortcut in
# _derive_pvs_from_args before the walker runs. No shim needed —
# the happi entry uses "ophyd.EpicsMotor" directly.


# ── axis_caproto (compound with motor sub-components) ───────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/axis_caproto/
# Shape: multi-axis stage with EpicsMotor sub-components. The walker
# descends into each sub-EpicsMotor and emits its full 19-key signal
# set per axis — divergent from the 4-key top-level shortcut above.


class _DualAxisStage(Device):
    x = Cpt(EpicsMotor, "Ax:X")
    y = Cpt(EpicsMotor, "Ax:Y")


# ── nsls2em (quad electrometer) ─────────────────────────────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/nsls2em/
# Shape: 4 current channels each with mean/sigma/min/max statistics
# sub-PVs, plus a top-level acquire trigger.


class _QuadEMChannel(Device):
    mean = Cpt(EpicsSignalRO, "Mean_RBV")
    sigma = Cpt(EpicsSignalRO, "Sigma_RBV")
    min = Cpt(EpicsSignalRO, "Min_RBV")
    max = Cpt(EpicsSignalRO, "Max_RBV")


class _QuadEM(Device):
    current_1 = Cpt(_QuadEMChannel, "Current1:")
    current_2 = Cpt(_QuadEMChannel, "Current2:")
    current_3 = Cpt(_QuadEMChannel, "Current3:")
    current_4 = Cpt(_QuadEMChannel, "Current4:")
    acquire = Cpt(EpicsSignal, "Acquire")


# ── lakeshore336 (multi-channel temperature controller) ─────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/lakeshore336/
# Shape: 4 input channels (A-D) each with temperature + sensor +
# engineering-units PVs.


class _TempInputChannel(Device):
    temperature = Cpt(EpicsSignalRO, "T_RBV")
    sensor = Cpt(EpicsSignalRO, "S_RBV")
    units = Cpt(EpicsSignal, "Units")


class _TempController(Device):
    channel_a = Cpt(_TempInputChannel, "A:")
    channel_b = Cpt(_TempInputChannel, "B:")
    channel_c = Cpt(_TempInputChannel, "C:")
    channel_d = Cpt(_TempInputChannel, "D:")


# ── base_soft_ioc (utility heartbeat/uptime) ────────────────────────────
# Source role: nsls2.ioc_deploy/roles/device_roles/base_soft_ioc/
# Shape: minimal soft-IOC exposing standard iocStats PVs.


class _SoftIOCBase(Device):
    heartbeat = Cpt(EpicsSignalRO, "HEARTBEAT")
    uptime = Cpt(EpicsSignalRO, "UPTIME")
    iocname = Cpt(EpicsSignalRO, "IOCNAME")
    load = Cpt(EpicsSignalRO, "LOAD")
