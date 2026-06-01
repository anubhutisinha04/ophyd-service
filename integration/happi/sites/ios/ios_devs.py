"""IOS-specific ophyd device classes ported from the IOS profile collection.

Ported from `ios-profile-collection/startup/*.py` (01-classes, 10-machine,
10-optics, 11-valves, 20-detectors, 21-specs_analyzer, 22-xspress3).
Each class mirrors the source's Component structure 1:1.

Vanilla ophyd dependencies plus `nslsii` (only used to build Xspress3IOS).
No bluesky, kafka, pyOlog, amostra, or other profile-runtime deps.
"""
from __future__ import annotations

import datetime
import time

from ophyd import (
    Component as Cpt,
    Device,
    EpicsDXP,
    EpicsMCA,
    EpicsMotor,
    EpicsSignal,
    EpicsSignalRO,
    FormattedComponent as FmtCpt,
    PVPositioner,
    PVPositionerPC,
)
from ophyd.areadetector.base import ADComponent, EpicsSignalWithRBV
from ophyd.areadetector.cam import CamBase
from ophyd.areadetector.detectors import DetectorBase
from ophyd.areadetector.filestore_mixins import (
    FileStoreIterativeWrite,
    FileStorePluginBase,
)
from ophyd.areadetector.plugins import HDF5Plugin
from ophyd.areadetector.trigger_mixins import SingleTrigger
from ophyd.device import DynamicDeviceComponent as DDC, Staged
from ophyd.ophydobj import Kind
from ophyd.scaler import _scaler_fields
from ophyd.signal import waveform_to_string
from ophyd.status import DeviceStatus


# ---------------------------------------------------------------------------
# 01-classes.py — Vortex MCA
# ---------------------------------------------------------------------------


class Vortex(Device):
    mca = Cpt(EpicsMCA, "mca1")
    vortex = Cpt(EpicsDXP, "dxp1:")

    @property
    def trigger_signals(self):
        return [self.mca.erase_start]

    def describe(self):
        ret = super().describe()
        ret["vortex_mca_spectrum"].setdefault("dtype_str", "<i8")
        return ret


# ---------------------------------------------------------------------------
# 10-machine.py — EPU undulators
# ---------------------------------------------------------------------------


class GapMotor1(PVPositionerPC):
    readback = Cpt(EpicsSignalRO, "Pos-I")
    setpoint = Cpt(EpicsSignal, "Pos-SP")
    stop_signal = FmtCpt(
        EpicsSignal, "SR:C23-ID:G1A{EPU:1-Ax:Gap}-Mtr.STOP", add_prefix=()
    )
    stop_value = 1


class PhaseMotor1(PVPositionerPC):
    readback = Cpt(EpicsSignalRO, "Pos-I")
    setpoint = Cpt(EpicsSignal, "Pos-SP")
    stop_signal = FmtCpt(
        EpicsSignal, "SR:C23-ID:G1A{EPU:1-Ax:Phase}-Mtr.STOP", add_prefix=()
    )
    stop_value = 1


class GapMotor2(PVPositionerPC):
    readback = Cpt(EpicsSignalRO, "Pos-I")
    setpoint = Cpt(EpicsSignal, "Pos-SP")
    stop_signal = FmtCpt(
        EpicsSignal, "SR:C23-ID:G1A{EPU:2-Ax:Gap}-Mtr.STOP", add_prefix=()
    )
    stop_value = 1


class PhaseMotor2(PVPositionerPC):
    readback = Cpt(EpicsSignalRO, "Pos-I")
    setpoint = Cpt(EpicsSignal, "Pos-SP")
    stop_signal = FmtCpt(
        EpicsSignal, "SR:C23-ID:G1A{EPU:2-Ax:Phase}-Mtr.STOP", add_prefix=()
    )
    stop_value = 1


class Interpolator(Device):
    input = Cpt(EpicsSignal, "Val:Inp1-SP")
    input_offset = Cpt(EpicsSignal, "Val:InpOff1-SP")
    input_link = Cpt(EpicsSignal, "Enbl:Inp1-Sel", string=True)
    input_pv = Cpt(EpicsSignal, "Val:Inp1-SP.DOL$", string=True)
    output = Cpt(EpicsSignalRO, "Val:Out1-I")
    output_link = Cpt(EpicsSignalRO, "Enbl:Out1-Sel", string=True)
    output_pv = Cpt(EpicsSignal, "Calc1.OUT$", string=True)
    output_deadband = Cpt(EpicsSignal, "Val:DBand1-SP")
    output_drive = Cpt(EpicsSignalRO, "Val:OutDrv1-I")
    interpolation_status = Cpt(EpicsSignalRO, "Sts:Interp1-Sts", string=True)


class EPU1(Device):
    gap = Cpt(GapMotor1, "-Ax:Gap}")
    phase = Cpt(PhaseMotor1, "-Ax:Phase}")
    flt = Cpt(Interpolator, "-FLT}")
    rlt = Cpt(Interpolator, "-RLT}")
    table = Cpt(EpicsSignal, "}Val:Table-Sel")


class EPU2(Device):
    gap = Cpt(GapMotor2, "-Ax:Gap}")
    phase = Cpt(PhaseMotor2, "-Ax:Phase}")


# ---------------------------------------------------------------------------
# 10-optics.py — mirrors, PGM, slits
# ---------------------------------------------------------------------------


class MirrorAxis(PVPositioner):
    readback = Cpt(EpicsSignalRO, "Mtr_MON")
    setpoint = Cpt(EpicsSignal, "Mtr_POS_SP")
    actuate = FmtCpt(EpicsSignal, "{self.parent.prefix}}}MOVE_CMD.PROC")
    actual_value = 1
    stop_signal = FmtCpt(EpicsSignal, "{self.parent.prefix}}}STOP_CMD.PROC")
    stop_value = 1
    done = FmtCpt(EpicsSignalRO, "{self.parent.prefix}}}BUSY_STS")
    done_value = 0


class FeedbackLoop(Device):
    enable = Cpt(EpicsSignal, "Sts:FB-Sel")
    setpoint = Cpt(EpicsSignal, "PID-SP")
    requested_value = Cpt(EpicsSignalRO, "PID.VAL")
    actual_value = Cpt(EpicsSignalRO, "PID.CVAL")
    output_value = Cpt(EpicsSignalRO, "PID.OVAL")
    error = Cpt(EpicsSignalRO, "PID.Err")
    high_limit = Cpt(EpicsSignal, "PID.DRVH")
    low_limit = Cpt(EpicsSignal, "PID.DRVL")
    delta_t = Cpt(EpicsSignalRO, "PID.DT")
    min_delta_t = Cpt(EpicsSignal, "PID.MDT")
    scan_mode = Cpt(EpicsSignal, "PID.SCAN")
    deadband = Cpt(EpicsSignal, "Val:Sbl-SP")


class Mirror(Device):
    z = Cpt(MirrorAxis, "-Ax:Z}")
    y = Cpt(MirrorAxis, "-Ax:Y}")
    x = Cpt(MirrorAxis, "-Ax:X}")
    pit = Cpt(MirrorAxis, "-Ax:Pit}")
    yaw = Cpt(MirrorAxis, "-Ax:Yaw}")
    rol = Cpt(MirrorAxis, "-Ax:Rol}")


class M1bMirror(Mirror):
    fbl = Cpt(FeedbackLoop, "XF:23ID2-OP{FBck}", add_prefix="")


class MotorMirror(Device):
    """Mirror driven by EpicsMotors (used for M3A in the profile)."""

    x = Cpt(EpicsMotor, "-Ax:XAvg}Mtr")
    pit = Cpt(EpicsMotor, "-Ax:P}Mtr")
    bdr = Cpt(EpicsMotor, "-Ax:Bdr}Mtr")


class PGMEnergy(PVPositionerPC):
    readback = Cpt(EpicsSignalRO, "}Enrgy-I")
    setpoint = Cpt(EpicsSignal, "}Enrgy-SP", limits=(200, 2200))
    stop_signal = Cpt(EpicsSignal, "}Cmd:Stop-Cmd")
    stop_value = 1


class MonoFly(Device):
    start_sig = Cpt(EpicsSignal, "}Enrgy:Start-SP")
    stop_sig = Cpt(EpicsSignal, "}Enrgy:Stop-SP")
    velocity = Cpt(EpicsSignal, "}Enrgy:FlyVelo-SP")

    fly_start = Cpt(EpicsSignal, "}Cmd:FlyStart-Cmd.PROC")
    fly_stop = Cpt(EpicsSignal, "}Cmd:Stop-Cmd.PROC")
    scan_status = Cpt(EpicsSignalRO, "}Sts:Scan-Sts", string=True)


class PGM(Device):
    energy = Cpt(PGMEnergy, "")
    pit = Cpt(EpicsMotor, "-Ax:MirP}Mtr")
    x = Cpt(EpicsMotor, "-Ax:MirX}Mtr")
    grt_pit = Cpt(EpicsMotor, "-Ax:GrtP}Mtr")
    grt_x = Cpt(EpicsMotor, "-Ax:GrtX}Mtr")
    fly = Cpt(MonoFly, "")
    move_status = Cpt(EpicsSignalRO, "}Sts:Move-Sts", string=True)


class SlitsGapCenter(Device):
    xg = Cpt(EpicsMotor, "-Ax:XGap}Mtr")
    xc = Cpt(EpicsMotor, "-Ax:XCtr}Mtr")
    yg = Cpt(EpicsMotor, "-Ax:YGap}Mtr")
    yc = Cpt(EpicsMotor, "-Ax:YCtr}Mtr")


# ---------------------------------------------------------------------------
# 11-valves.py — gate valves and shutters
# ---------------------------------------------------------------------------


class Valve(Device):
    open_cmd = Cpt(EpicsSignal, "Cmd:Opn-Cmd")
    close_cmd = Cpt(EpicsSignal, "Cmd:Cls-Cmd")
    pos_sts = Cpt(EpicsSignal, "Pos-Sts")


_TIME_FMTSTR = "%Y-%m-%d %H:%M:%S"


class TwoButtonShutter(Device):
    open_cmd = Cpt(EpicsSignal, "Cmd:Opn-Cmd", string=True)
    open_val = "Not Closed"

    close_cmd = Cpt(EpicsSignal, "Cmd:Cls-Cmd", string=True)
    close_val = "Closed"

    status = Cpt(EpicsSignalRO, "Pos-Sts", string=True)
    fail_to_close = Cpt(EpicsSignalRO, "Sts:FailCls-Sts", string=True)
    fail_to_open = Cpt(EpicsSignalRO, "Sts:FailOpn-Sts", string=True)

    open_str = "Open"
    close_str = "Close"

    def set(self, val):
        if self._set_st is not None:
            raise RuntimeError("trying to set while a set is in progress")

        cmd_map = {self.open_str: self.open_cmd, self.close_str: self.close_cmd}
        target_map = {self.open_str: self.open_val, self.close_str: self.close_val}

        cmd_sig = cmd_map[val]
        target_val = target_map[val]

        st = self._set_st = DeviceStatus(self)
        enums = self.status.enum_strs

        def shutter_cb(value, timestamp, **kwargs):
            value = enums[int(value)]
            if value == target_val:
                self._set_st._finished()
                self._set_st = None
                self.status.clear_sub(shutter_cb)

        cmd_enums = cmd_sig.enum_strs
        count = 0

        def cmd_retry_cb(value, timestamp, **kwargs):
            nonlocal count
            value = cmd_enums[int(value)]
            count += 1
            if count > 5:
                cmd_sig.clear_sub(cmd_retry_cb)
                st._finished(success=False)
            if value == "None":
                if not st.done:
                    time.sleep(0.5)
                    cmd_sig.set(1)
                    ts = datetime.datetime.fromtimestamp(timestamp).strftime(_TIME_FMTSTR)
                    print(f"** ({ts}) Had to reactuate shutter while {val}ing")
                else:
                    cmd_sig.clear_sub(cmd_retry_cb)

        cmd_sig.subscribe(cmd_retry_cb, run=False)
        cmd_sig.set(1)
        self.status.subscribe(shutter_cb)
        return st

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._set_st = None
        self.read_attrs = ["status"]


# ---------------------------------------------------------------------------
# 20-detectors.py — scaler with retry-on-None signal
# ---------------------------------------------------------------------------


class DodgyEpicsSignal(EpicsSignal):
    def get(self, *, as_string=None, connection_timeout=1.0, **kwargs):
        if as_string is None:
            as_string = self._string

        with self._metadata_lock:
            if not self._read_pv.connected:
                if not self._read_pv.wait_for_connection(connection_timeout):
                    raise TimeoutError(f"Failed to connect to {self._read_pv.pvname}")
            ret = None
            while ret is None:
                ret = self._read_pv.get(as_string=as_string, **kwargs)
                if ret is None:
                    print(f"Failed to get value, retrying to read {self}")

        if as_string:
            return waveform_to_string(ret)
        return ret


class DodgyEpicsScaler(Device):
    """synApps Scaler record using DodgyEpicsSignal everywhere."""

    count = Cpt(DodgyEpicsSignal, ".CNT", trigger_value=1)
    count_mode = Cpt(DodgyEpicsSignal, ".CONT", string=True)

    delay = Cpt(DodgyEpicsSignal, ".DLY")
    auto_count_delay = Cpt(DodgyEpicsSignal, ".DLY1")

    channels = DDC(_scaler_fields(DodgyEpicsSignal, "chan", ".S", range(1, 33)))
    names = DDC(_scaler_fields(DodgyEpicsSignal, "name", ".NM", range(1, 33)))

    time = Cpt(DodgyEpicsSignal, ".T")
    freq = Cpt(DodgyEpicsSignal, ".FREQ")

    preset_time = Cpt(DodgyEpicsSignal, ".TP")
    auto_count_time = Cpt(DodgyEpicsSignal, ".TP1")

    presets = DDC(_scaler_fields(DodgyEpicsSignal, "preset", ".PR", range(1, 33)))
    gates = DDC(_scaler_fields(DodgyEpicsSignal, "gate", ".G", range(1, 33)))

    update_rate = Cpt(DodgyEpicsSignal, ".RATE")
    auto_count_update_rate = Cpt(DodgyEpicsSignal, ".RAT1")

    egu = Cpt(DodgyEpicsSignal, ".EGU")

    def __init__(self, prefix, *, read_attrs=None, configuration_attrs=None,
                 name=None, parent=None, **kwargs):
        if read_attrs is None:
            read_attrs = ["channels", "time"]
        if configuration_attrs is None:
            configuration_attrs = [
                "preset_time", "presets", "gates", "names", "freq",
                "auto_count_time", "count_mode", "delay", "auto_count_delay", "egu",
            ]
        super().__init__(prefix, read_attrs=read_attrs,
                         configuration_attrs=configuration_attrs,
                         name=name, parent=parent, **kwargs)
        self.stage_sigs.update([("count_mode", 0)])
        for channel in ["channels.chan2", "channels.chan3", "channels.chan4"]:
            getattr(self, channel).kind = "hinted"


# ---------------------------------------------------------------------------
# 21-specs_analyzer.py — SPECS hemispherical analyzer
# ---------------------------------------------------------------------------


class SpecsDetectorCam(CamBase):
    """CamBase subclass for the SPECS detector."""

    connect = ADComponent(EpicsSignal, "CONNECTED_RBV", write_pv="CONNECT", kind=Kind.omitted)
    status_message = ADComponent(EpicsSignalRO, "StatusMessage_RBV", string=True, kind=Kind.omitted)
    server_name = ADComponent(EpicsSignalRO, "SERVER_NAME_RBV", string=True, kind=Kind.omitted)
    protocol_version = ADComponent(EpicsSignalRO, "PROTOCOL_VERSION_RBV", string=True, kind=Kind.omitted)

    pass_energy = ADComponent(EpicsSignalWithRBV, "PASS_ENERGY")
    low_energy = ADComponent(EpicsSignalWithRBV, "LOW_ENERGY")
    high_energy = ADComponent(EpicsSignalWithRBV, "HIGH_ENERGY")
    energy_width = ADComponent(EpicsSignalRO, "ENERGY_WIDTH_RBV")
    kinetic_energy = ADComponent(EpicsSignalWithRBV, "KINETIC_ENERGY")
    retarding_ratio = ADComponent(EpicsSignalWithRBV, "RETARDING_RATIO")
    step_size = ADComponent(EpicsSignalWithRBV, "STEP_SIZE")
    fat_values = ADComponent(EpicsSignalWithRBV, "VALUES")
    samples = ADComponent(EpicsSignalWithRBV, "SAMPLES")
    lens_mode = ADComponent(EpicsSignalWithRBV, "LENS_MODE")

    scan_range = ADComponent(EpicsSignalWithRBV, "SCAN_RANGE", kind=Kind.config)
    acquire_mode = ADComponent(EpicsSignalWithRBV, "ACQ_MODE", kind=Kind.config)
    define_spectrum = ADComponent(EpicsSignalWithRBV, "DEFINE_SPECTRUM", kind=Kind.config)
    validate_spectrum = ADComponent(EpicsSignalWithRBV, "VALIDATE_SPECTRUM", kind=Kind.config)
    safe_state = ADComponent(EpicsSignalWithRBV, "SAFE_STATE", kind=Kind.config)
    pause_acq = ADComponent(EpicsSignalWithRBV, "PAUSE_RBV", kind=Kind.config)

    current_point = ADComponent(EpicsSignalRO, "CURRENT_POINT_RBV", kind=Kind.omitted)
    current_channel = ADComponent(EpicsSignalRO, "CURRENT_CHANNEL_RBV", kind=Kind.omitted)
    region_time_left = ADComponent(EpicsSignalRO, "REGION_TIME_LEFT_RBV", kind=Kind.omitted)
    region_progress = ADComponent(EpicsSignalRO, "REGION_PROGRESS_RBV", kind=Kind.omitted)
    total_time_left = ADComponent(EpicsSignalRO, "TOTAL_TIME_LEFT_RBV", kind=Kind.omitted)
    progress = ADComponent(EpicsSignalRO, "PROGRESS_RBV", kind=Kind.omitted)
    total_points_iteration = ADComponent(EpicsSignalRO, "TOTAL_POINTS_ITERATION_RBV", kind=Kind.omitted)
    total_points = ADComponent(EpicsSignalRO, "TOTAL_POINTS_RBV", kind=Kind.omitted)


class FileStoreHDF5Single(FileStorePluginBase):
    """`Single` mode HDF5 filestore mixin — one hdf5 file per trigger."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filestore_spec = "AD_HDF5_SINGLE"
        self.stage_sigs.update([
            ("file_template", "%s%s_%6.6d.h5"),
            ("file_write_mode", "Single"),
        ])

    def get_frames_per_point(self):
        return self.parent.cam.num_images.get()

    def stage(self):
        super().stage()
        self._fn = self._fp
        resource_kwargs = {
            "template": self.file_template.get(),
            "filename": self.file_name.get(),
            "frame_per_point": self.get_frames_per_point(),
        }
        self._generate_resource(resource_kwargs)


class FileStoreHDF5SingleIterativeWrite(FileStoreHDF5Single, FileStoreIterativeWrite):
    pass


class SpecsHDF5Plugin(HDF5Plugin, FileStoreHDF5SingleIterativeWrite):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filestore_spec = "SPECS_HDF5_SINGLE_DATAFRAME"

    key = "/entry/data/data"
    column_names = ("spectrum",)

    def stage(self):
        super().stage()
        self._fn = self._fp
        resource_kwargs = {
            "template": self.file_template.get(),
            "filename": self.file_name.get(),
            "key": self.key,
            "column_names": self.column_names,
            "frame_per_point": self.get_frames_per_point(),
        }
        self._generate_resource(resource_kwargs)


class SpecsSingleTrigger(SingleTrigger):
    """SingleTrigger variant that names the measured spectrum `spectrum` and
    only generates HDF5 data when `acquisition_mode == 'spectrum'`.
    """

    def trigger(self):
        if self._staged != Staged.yes:
            raise RuntimeError(
                f"The {self.name} detector is not ready to trigger, "
                f"call `{self.name}.stage()` prior to triggering"
            )

        self._status = self._status_type(self)
        self.cam.acquire.put(1, wait=False)
        if self.acquisition_mode == "spectrum":
            self.hdf1.generate_datum("spectrum", time.time(), {})
        return self._status


class SpecsDetector(SpecsSingleTrigger, DetectorBase):
    """SPECS analyzer — cam + count + count_enable. (`hdf1` is commented out
    in the IOS profile; preserved here as a comment for fidelity.)
    """

    cam = ADComponent(SpecsDetectorCam, "cam1:")
    # hdf1 = ADComponent(SpecsHDF5Plugin, suffix="HDF1:",
    #                    write_path_template="/GPFS/xf23id/xf23id2/data/specs/%Y/%m/%d/",
    #                    root="/GPFS/xf23id/xf23id2/")

    count = ADComponent(EpicsSignalRO, "Stats5:Total_RBV", kind=Kind.hinted)
    count_enable = ADComponent(EpicsSignal, "Stats5:EnableCallbacks", kind=Kind.omitted)

    acquisition_mode = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_sigs.update({self.cam.safe_state: 0})
        self.stage_sigs.update({self.count_enable: 1})
        self.stage_sigs.update({self.cam.data_type: 9})
        self.count.kind = Kind.hinted


# ---------------------------------------------------------------------------
# 22-xspress3.py — dynamically-built Xspress3 class
# ---------------------------------------------------------------------------
#
# Built exactly as the IOS profile builds it. Requires `nslsii` at import
# time. If nslsii is absent (community environments without site packages),
# Xspress3IOS is set to None and the corresponding happi entry can't be
# loaded — see the sub-README for the rationale.

try:
    from ophyd import Component as _Cpt  # alias to keep build_xspress3_class's Cpt unambiguous
    from ophyd.areadetector import Xspress3Detector
    from nslsii.areadetector.xspress3 import (
        Xspress3HDF5Plugin,
        Xspress3Trigger,
        build_xspress3_class,
    )

    Xspress3IOS = build_xspress3_class(
        channel_numbers=(1,),
        mcaroi_numbers=(1, 2, 3, 4),
        image_data_key="data",
        xspress3_parent_classes=(Xspress3Detector, Xspress3Trigger),
        extra_class_members={
            "hdf5plugin": _Cpt(
                Xspress3HDF5Plugin,
                "HDF1:",
                name="h5p",
                root_path="/nsls2/data3/ios/legacy/xspress3_data",
                path_template="/nsls2/data3/ios/legacy/xspress3_data/%Y/%m/%d",
                resource_kwargs={},
            ),
        },
    )
except ImportError:
    Xspress3IOS = None
