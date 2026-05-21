"""BMM-specific ophyd device classes ported from the BMM profile collection.

Ported from:
  - `bmm_tools/src/bmm_tools/{devices,tools}/*.py` (the BMM facility-helper
    package — analogous to IOS's `nslsii.areadetector.xspress3`)
  - `bmm-profile-collection/startup/BMM/*.py` (profile-internal classes)

Vanilla ophyd dependencies only. Facility-specific dynamic-class builders
(`nslsii.areadetector.xspress3.build_xspress3_class`) are imported under a
`try/except ImportError` guard — in environments without nslsii the affected
classes resolve to ``None`` and the happi entries pointing at them will fail
loudly on instantiation rather than masking the missing dependency.

No bluesky / kafka / databroker / pyOlog / amostra / redis / IPython /
matplotlib runtime deps. Plan-only methods (`open_plan`, `close_plan`,
`dark_current`, `to`, `auto_align`, `measure_xrf`, `plot`, etc.), display
helpers (`status`, `wh`, `where`, `boxedtext` wrappers), and macro-builder
classes are intentionally omitted; this file describes *device structure*,
not runtime behavior. Profile-internal globals (``PROPOSALS``,
``md['cycle']``, ``BMMuser``, ``user_ns``, ``kafka``, ``rkvs``) are replaced
with placeholder strings — instantiation works against any IOC; actual
filestore plugin paths should be set at deployment.

Inventory walked from `startup/BMM/user_ns/{base,bmm,bmm_end,dcm,detectors,
dwelltime,gonio,instruments,motors,utilities}.py`; see README.md for the
device list this shim supports.
"""
from __future__ import annotations

import threading
import time

from ophyd import (
    ADComponent as ADCpt,
    Component as Cpt,
    Device,
    DeviceStatus,
    EpicsSignal,
    EpicsSignalRO,
    Kind,
    PseudoPositioner,
    PseudoSingle,
    PVPositioner,
    PVPositionerPC,
    QuadEM,
    Signal,
)
from ophyd.areadetector import AreaDetector, ImagePlugin
from ophyd.areadetector.base import EpicsSignalWithRBV
from ophyd.areadetector.detectors import DetectorBase, ProsilicaDetector
from ophyd.areadetector.filestore_mixins import (
    FileStoreIterativeWrite,
    FileStorePluginBase,
)
from ophyd.areadetector.plugins import (
    HDF5Plugin_V33,
    JPEGPlugin_V33,
)
from ophyd.pseudopos import pseudo_position_argument, real_position_argument
from ophyd.quadem import QuadEMPort
from ophyd.signal import DerivedSignal


# ---------------------------------------------------------------------------
# nslsii — optional facility dependency, only used for Xspress3 dynamic build
# ---------------------------------------------------------------------------

try:
    from nslsii.ad33 import SingleTriggerV33
except ImportError:
    SingleTriggerV33 = None

try:
    from nslsii.areadetector.xspress3 import (
        Xspress3HDF5Plugin,
        build_xspress3_class,
    )
except ImportError:
    Xspress3HDF5Plugin = None
    build_xspress3_class = None


# ---------------------------------------------------------------------------
# Placeholder path templates
# Profile-internal: f"{PROPOSALS}/{md['cycle']}/{md['data_session']}/assets/..."
# Shim placeholder; override at deployment if you actually trigger acquisition.
# ---------------------------------------------------------------------------

_FILESTORE_BASE = "/tmp/bmm/assets"


# ===========================================================================
# Motors  —  ported from bmm_tools/devices/motors.py
# ===========================================================================


from ophyd import EpicsMotor  # placed after blanket imports so MRO is obvious
from ophyd import PositionerBase
from ophyd.epics_motor import required_for_connection, AlarmSeverity
from ophyd.utils.epics_pvs import fmt_time


class DeadbandMixin(Device, PositionerBase):
    """Move-completion tolerance for EpicsMotor subclasses.

    See `bmm_tools/devices/motors.py` for the full implementation;
    we retain the move-state Components so the device introspects with
    the expected attributes.
    """

    tolerance = Cpt(Signal, value=-1, kind="omitted")
    move_latch = Cpt(Signal, value=0, kind="omitted")

    def __init__(self, *args, tolerance=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tolerance is not None:
            self.tolerance.put(tolerance)


class DeadbandEpicsMotor(DeadbandMixin, EpicsMotor):
    pass


class EpicsMotorWithDial(EpicsMotor):
    dial = Cpt(EpicsSignal, ".DVAL", kind="omitted")


class FMBOEpicsMotor(EpicsMotor):
    """FMBO MCS8 motor controller — status / encoder / home / homing-velocity."""

    resolution = Cpt(EpicsSignal, ".MRES", kind="omitted")
    encoder = Cpt(EpicsSignal, ".REP", kind="omitted")

    ampen = Cpt(EpicsSignal, "_AMPEN_STS", kind="omitted")
    hocpl = Cpt(EpicsSignal, "_HOCPL_STS", kind="omitted")
    amfae = Cpt(EpicsSignal, "_AMFAE_STS", kind="omitted")
    amfe = Cpt(EpicsSignal, "_AMFE_STS", kind="omitted")

    enc_lss = Cpt(EpicsSignal, "_ENC_LSS_STS", kind="omitted")
    clear_enc_lss = Cpt(EpicsSignal, "_ENC_LSS_CLR_CMD.PROC", kind="omitted")

    home_signal = Cpt(EpicsSignal, "_HOME_CMD.PROC", kind="omitted")
    hvel_sp = Cpt(EpicsSignal, "_HVEL_SP.A", kind="omitted")


class FMBOThinEpicsMotor(EpicsMotor):
    """FMBO motor that exposes only the bare minimum used in slits."""

    hlm = Cpt(EpicsSignal, ".HLM", kind="omitted")
    llm = Cpt(EpicsSignal, ".LLM", kind="omitted")
    kill_cmd = Cpt(EpicsSignal, "_KILL_CMD.PROC", kind="omitted")
    enable_cmd = Cpt(EpicsSignal, "_ENA_CMD.PROC", kind="omitted")


class XAFSEpicsMotor(FMBOEpicsMotor):
    hlm = Cpt(EpicsSignal, ".HLM", kind="omitted")
    llm = Cpt(EpicsSignal, ".LLM", kind="omitted")
    kill_cmd = Cpt(EpicsSignal, "_KILL_CMD.PROC", kind="omitted")
    enable_cmd = Cpt(EpicsSignal, "_ENA_CMD.PROC", kind="omitted")


class BMMDeadBandMotor(DeadbandEpicsMotor, XAFSEpicsMotor):
    pass


class VacuumEpicsMotor(FMBOEpicsMotor):
    hlm = Cpt(EpicsSignal, ".HLM", kind="omitted")
    llm = Cpt(EpicsSignal, ".LLM", kind="omitted")
    kill_cmd = Cpt(EpicsSignal, "_KILL_CMD.PROC", kind="omitted")
    enable_cmd = Cpt(EpicsSignal, "_ENA_CMD.PROC", kind="omitted")


class EndStationEpicsMotor(EpicsMotor):
    """End-station Delta Tau motor — different KILL PV shape from FMBO."""

    hlm = Cpt(EpicsSignal, ".HLM", kind="omitted")
    llm = Cpt(EpicsSignal, ".LLM", kind="omitted")
    kill_cmd = Cpt(EpicsSignal, ":KILL", kind="omitted")
    spmg = Cpt(EpicsSignal, ".SPMG", kind="omitted")
    setpoint = Cpt(EpicsSignal, ".VAL", kind="omitted")


class EncodedEndStationEpicsMotor(EndStationEpicsMotor):
    pass


from numpy import arctan2, tan


class Mirrors(PseudoPositioner):
    """5-jack focusing mirror with pseudo (vertical/lateral/pitch/roll/yaw) axes.

    m2 instances also expose a bender motor allocated in __init__ (separate
    PV outside the mirror prefix).
    """

    def __init__(self, *args, mirror_length, mirror_width, **kwargs):
        self.mirror_length = mirror_length
        self.mirror_width = mirror_width
        super().__init__(*args, **kwargs)

        if self.name == "m2":
            self.bender = XAFSEpicsMotor(
                "XF:06BMA-OP{Mir:M2-Ax:Bend}Mtr", name="m2_bender"
            )
        else:
            self.bender = None

    vertical = Cpt(PseudoSingle, limits=(-8, 8))
    lateral = Cpt(PseudoSingle, limits=(-16, 16))
    pitch = Cpt(PseudoSingle, limits=(-5.5, 5.5))
    roll = Cpt(PseudoSingle, limits=(-3, 3))
    yaw = Cpt(PseudoSingle, limits=(-3, 3))

    yu = Cpt(XAFSEpicsMotor, "YU}Mtr")
    ydo = Cpt(XAFSEpicsMotor, "YDO}Mtr")
    ydi = Cpt(XAFSEpicsMotor, "YDI}Mtr")
    xu = Cpt(VacuumEpicsMotor, "XU}Mtr")
    xd = Cpt(VacuumEpicsMotor, "XD}Mtr")

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        return self.RealPosition(
            xu=pseudo_pos.lateral
            - 0.5 * self.mirror_length * tan(pseudo_pos.yaw / 1000),
            xd=pseudo_pos.lateral
            + 0.5 * self.mirror_length * tan(pseudo_pos.yaw / 1000),
            yu=pseudo_pos.vertical
            - 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000),
            ydo=pseudo_pos.vertical
            + 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000)
            + 0.5 * self.mirror_width * tan(pseudo_pos.roll / 1000),
            ydi=pseudo_pos.vertical
            + 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000)
            - 0.5 * self.mirror_width * tan(pseudo_pos.roll / 1000),
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(
            lateral=(real_pos.xu + real_pos.xd) / 2,
            yaw=1000 * arctan2(real_pos.xd - real_pos.xu, self.mirror_length),
            vertical=(real_pos.yu + (real_pos.ydo + real_pos.ydi) / 2) / 2,
            pitch=1000
            * arctan2(
                (real_pos.ydo + real_pos.ydi) / 2 - real_pos.yu, self.mirror_length
            ),
            roll=1000 * arctan2(real_pos.ydo - real_pos.ydi, self.mirror_width),
        )


class XAFSTable(PseudoPositioner):
    """3-jack XAFS table with pseudo (vertical/pitch/roll) axes."""

    def __init__(self, *args, mirror_length, mirror_width, **kwargs):
        self.mirror_length = mirror_length
        self.mirror_width = mirror_width
        super().__init__(*args, **kwargs)

    vertical = Cpt(PseudoSingle, limits=(5, 145))
    pitch = Cpt(PseudoSingle, limits=(-8, 6))
    roll = Cpt(PseudoSingle, limits=(5, 5))

    yu = Cpt(EpicsMotor, "YU}Mtr")
    ydo = Cpt(EpicsMotor, "YDO}Mtr")
    ydi = Cpt(EpicsMotor, "YDI}Mtr")

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        return self.RealPosition(
            yu=pseudo_pos.vertical
            - 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000),
            ydo=pseudo_pos.vertical
            + 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000)
            + 0.5 * self.mirror_width * tan(pseudo_pos.roll / 1000),
            ydi=pseudo_pos.vertical
            + 0.5 * self.mirror_length * tan(pseudo_pos.pitch / 1000)
            - 0.5 * self.mirror_width * tan(pseudo_pos.roll / 1000),
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(
            vertical=(real_pos.yu + (real_pos.ydo + real_pos.ydi) / 2) / 2,
            pitch=1000
            * arctan2(
                (real_pos.ydo + real_pos.ydi) / 2 - real_pos.yu, self.mirror_length
            ),
            roll=1000 * arctan2(real_pos.ydo - real_pos.ydi, self.mirror_width),
        )


# ===========================================================================
# Wheel motor  —  ported from BMM/wheel.py
# ===========================================================================


class WheelMotor(EndStationEpicsMotor):
    """48-slot sample-wheel motor; user sets slotone / x_motor on the instance."""

    def describe(self):
        res = super().describe()
        try:
            res["xafs_wheel_user_setpoint"]["dtype_str"] = "<f8"
        except Exception:
            pass
        return res


# ===========================================================================
# Slits  —  ported from bmm_tools/devices/slits.py
# ===========================================================================


class StandardSlits(PseudoPositioner):
    """4-blade slits using FMBOThinEpicsMotor; pseudo (vsize/vcenter/hsize/hcenter)."""

    def __init__(self, *args, **kwargs):
        self.nominal = [7.0, 1.0, 0.0, 0.0]  # hsize, vsize, hcenter, vcenter
        super().__init__(*args, **kwargs)

    vsize = Cpt(PseudoSingle, limits=(-15, 20))
    vcenter = Cpt(PseudoSingle, limits=(-15, 10))
    hsize = Cpt(PseudoSingle, limits=(-1, 20))
    hcenter = Cpt(PseudoSingle, limits=(-10, 10))

    top = Cpt(FMBOThinEpicsMotor, "T}Mtr")
    bottom = Cpt(FMBOThinEpicsMotor, "B}Mtr")
    inboard = Cpt(FMBOThinEpicsMotor, "I}Mtr")
    outboard = Cpt(FMBOThinEpicsMotor, "O}Mtr")

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        return self.RealPosition(
            top=pseudo_pos.vcenter + pseudo_pos.vsize / 2,
            bottom=pseudo_pos.vcenter - pseudo_pos.vsize / 2,
            outboard=pseudo_pos.hcenter + pseudo_pos.hsize / 2,
            inboard=pseudo_pos.hcenter - pseudo_pos.hsize / 2,
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(
            hsize=real_pos.outboard - real_pos.inboard,
            hcenter=(real_pos.outboard + real_pos.inboard) / 2,
            vsize=real_pos.top - real_pos.bottom,
            vcenter=(real_pos.top + real_pos.bottom) / 2,
        )


class GonioSlits(PseudoPositioner):
    """Goniometer slits; parity differs from StandardSlits (positive = open)."""

    vsize = Cpt(PseudoSingle, limits=(-1, 20))
    vcenter = Cpt(PseudoSingle, limits=(-10, 10))
    hsize = Cpt(PseudoSingle, limits=(-1, 20))
    hcenter = Cpt(PseudoSingle, limits=(-10, 10))

    t = Cpt(EpicsMotor, "T}Mtr")
    b = Cpt(EpicsMotor, "B}Mtr")
    i = Cpt(EpicsMotor, "I}Mtr")
    o = Cpt(EpicsMotor, "O}Mtr")

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        return self.RealPosition(
            t=pseudo_pos.vsize / 2 + pseudo_pos.vcenter,
            b=pseudo_pos.vsize / 2 - pseudo_pos.vcenter,
            o=pseudo_pos.hsize / 2 + pseudo_pos.hcenter,
            i=pseudo_pos.hsize / 2 - pseudo_pos.hcenter,
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(
            hsize=real_pos.o + real_pos.i,
            hcenter=(real_pos.o - real_pos.i) / 2,
            vsize=real_pos.t + real_pos.b,
            vcenter=(real_pos.t - real_pos.b) / 2,
        )


# ===========================================================================
# Actuators (shutters)  —  ported from bmm_tools/devices/actuators.py
# ===========================================================================


class EPS_Shutter(Device):
    state = Cpt(EpicsSignal, "Pos-Sts")
    cls = Cpt(EpicsSignal, "Cmd:Cls-Cmd")
    opn = Cpt(EpicsSignal, "Cmd:Opn-Cmd")
    error = Cpt(EpicsSignal, "Err-Sts")
    permit = Cpt(EpicsSignal, "Permit:Enbl-Sts")
    enabled = Cpt(EpicsSignal, "Enbl-Sts")
    maxcount = 4
    openval = 1  # FS1 inverts these on the live instance; happi keeps defaults
    closeval = 0


class BMPS_Shutter(Device):
    state = Cpt(EpicsSignal, "Sts:BM_BMPS_Opn-Sts")


class IDPS_Shutter(Device):
    state = Cpt(EpicsSignal, "Sts:BM_PS_OpnA3-Sts")


# ===========================================================================
# Front end BPM  —  ported from bmm_tools/devices/frontend.py
# ===========================================================================


class FEBPM(Device):
    x = Cpt(EpicsSignalRO, "X-I")
    y = Cpt(EpicsSignalRO, "Y-I")


# ===========================================================================
# Busy  —  ported from bmm_tools/devices/busy.py
# ===========================================================================


class BusyStatus(DeviceStatus):
    """A 'busy' status that completes after a fixed delay (seconds)."""

    def __init__(self, device, delay, *, tick=0.1, **kwargs):
        super().__init__(device, **kwargs)
        start = time.monotonic()
        deadline = start + delay

        def loop():
            current = time.monotonic()
            while current < deadline:
                time.sleep(tick)
                current = time.monotonic()
            self.set_finished()

        threading.Thread(target=loop, daemon=True).start()


class Busy(Device):
    """Settable sleep; `set(delay_seconds)` returns a BusyStatus."""

    def set(self, delay):
        return BusyStatus(self, delay, tick=max(1, min(0.1, delay / 100)))


# ===========================================================================
# Vacuum / temperature / DI-water  —  bmm_tools/devices/utilities.py
# ===========================================================================


class TCG(Device):
    pressure = Cpt(EpicsSignalRO, "-TCG:1}P:Raw-I")


class FEVac(Device):
    """Front-end vacuum: 6 cold cathode gauges + 6 ion pumps."""

    p1 = Cpt(EpicsSignal, "CCG:1}P:Raw-I")
    p2 = Cpt(EpicsSignal, "CCG:2}P:Raw-I")
    p3 = Cpt(EpicsSignal, "CCG:3}P:Raw-I")
    p4 = Cpt(EpicsSignal, "CCG:4}P:Raw-I")
    p5 = Cpt(EpicsSignal, "CCG:5}P:Raw-I")
    p6 = Cpt(EpicsSignal, "CCG:6}P:Raw-I")
    c1 = Cpt(EpicsSignal, "IP:1}P-I")
    c2 = Cpt(EpicsSignal, "IP:2}P-I")
    c3 = Cpt(EpicsSignal, "IP:3}P-I")
    c4 = Cpt(EpicsSignal, "IP:4}P-I")
    c5 = Cpt(EpicsSignal, "IP:5}P-I")
    c6 = Cpt(EpicsSignal, "IP:6}P-I")


class OneWireTC(Device):
    temperature = Cpt(EpicsSignal, "-I")
    warning = Cpt(EpicsSignal, ":Hi-SP")
    alarm = Cpt(EpicsSignal, "-I.HIHI")


class BMM_DIWater(Device):
    dcm_flow = Cpt(EpicsSignal, "F:2-I")
    dm1_flow = Cpt(EpicsSignal, "F:1-I")
    return_pressure = Cpt(EpicsSignal, "P:Return-I")
    return_temperature = Cpt(EpicsSignal, "T:Return-I")
    supply_pressure = Cpt(EpicsSignal, "P:Supply-I")
    supply_temperature = Cpt(EpicsSignal, "T:Supply-I")


# ===========================================================================
# DCM  —  ported from bmm_tools/devices/dcm.py
# ===========================================================================


from numpy import arcsin, cos, pi, sin

# HBARC value used by BMM's physics module (eV*Angstrom)
_HBARC = 1973.269788  # bmm_tools/tools/physics.py

# Si crystal d-spacings (Angstrom) approximated from BMM_dcm parameters.
# Real values come from bmm_tools/optics/dcm_parameters.py; we hardcode the
# defaults so DCM doesn't need to import that module at class-define time.
_DSPACING_111 = 3.13556
_DSPACING_311 = 1.63747


class DCM(PseudoPositioner):
    """Double crystal monochromator with pseudo `energy` axis.

    pitch / roll / x / y are extra motors allocated in __init__ outside the
    Cpt mechanism (since they live on different PV prefixes than bragg/para/perp).
    """

    def __init__(self, *args, crystal="111", mode="fixed", offset=30, **kwargs):
        self._crystal = crystal
        self.offset = offset
        self.mode = mode
        self.suppress_channel_cut = False
        self.roll_111 = -0.2633
        self.acc_fast = 0.2

        self.pitch = VacuumEpicsMotor(
            "XF:06BMA-OP{Mono:DCM1-Ax:P2}Mtr", name="dcm_pitch"
        )
        self.roll = VacuumEpicsMotor(
            "XF:06BMA-OP{Mono:DCM1-Ax:R2}Mtr", name="dcm_roll"
        )
        self.x = XAFSEpicsMotor("XF:06BMA-OP{Mono:DCM1-Ax:X}Mtr", name="dcm_x")
        self._y = XAFSEpicsMotor("XF:06BMA-OP{Mono:DCM1-Ax:Y}Mtr", name="dcm_y")

        super().__init__(*args, **kwargs)

    @property
    def _twod(self):
        return 2 * (_DSPACING_311 if self._crystal == "311" else _DSPACING_111)

    @property
    def _pseudo_channel_cut(self):
        if self.suppress_channel_cut:
            return False
        return "channel" in self.mode

    energy = Cpt(PseudoSingle, limits=(2900, 25000))
    bragg = Cpt(BMMDeadBandMotor, "Bragg}Mtr")
    para = Cpt(VacuumEpicsMotor, "Par2}Mtr")
    perp = Cpt(VacuumEpicsMotor, "Per2}Mtr")

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        wavelen = 2 * pi * _HBARC / pseudo_pos.energy
        angle = arcsin(wavelen / self._twod)
        if self._pseudo_channel_cut:
            return self.RealPosition(
                bragg=180 * arcsin(wavelen / self._twod) / pi,
                para=self.para.user_readback.get(),
                perp=self.perp.user_readback.get(),
            )
        return self.RealPosition(
            bragg=180 * arcsin(wavelen / self._twod) / pi,
            para=self.offset / (2 * sin(angle)),
            perp=self.offset / (2 * cos(angle)),
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(
            energy=2 * pi * _HBARC / (self._twod * sin(real_pos.bragg * pi / 180))
        )


# ===========================================================================
# KillSwitch  —  ported from bmm_tools/tools/killswitch.py
# ===========================================================================


class KillSwitch(Device):
    """DIODE kill switches for the 5 Phytron amplifiers on MC02..MC06."""

    dcm = Cpt(EpicsSignal, "OutPt00:Data-Sel")
    slits2 = Cpt(EpicsSignal, "OutPt01:Data-Sel")
    m2 = Cpt(EpicsSignal, "OutPt02:Data-Sel")
    m3 = Cpt(EpicsSignal, "OutPt03:Data-Sel")
    dm3 = Cpt(EpicsSignal, "OutPt04:Data-Sel")


# ===========================================================================
# Electrometer  —  ported from BMM/electrometer.py
# ===========================================================================


class Nanoize(DerivedSignal):
    """nA derivation tied to dwell time; profile-runtime computes the divisor.

    Stub: the BMM profile pulls `_locked_dwell_time.dwell_time` for the
    divisor. Without that runtime device we keep the signal pass-through.
    """

    def forward(self, value):
        return value

    def inverse(self, value):
        return value


class BMMQuadEM(QuadEM):
    """NSLS2 QuadEM with 4 channels (I0/It/Ir/Iy)."""

    _default_read_attrs = ["I0", "It", "Ir", "Iy"]
    port_name = Cpt(Signal, value="EM180")
    conf = Cpt(QuadEMPort, port_name="EM180")
    em_range = Cpt(EpicsSignalWithRBV, "Range", string=True)
    I0 = Cpt(Nanoize, derived_from="current1.mean_value")
    It = Cpt(Nanoize, derived_from="current2.mean_value")
    Ir = Cpt(Nanoize, derived_from="current3.mean_value")
    Iy = Cpt(Nanoize, derived_from="current4.mean_value")

    compute_current_offset1 = Cpt(EpicsSignal, "ComputeCurrentOffset1.PROC")
    compute_current_offset2 = Cpt(EpicsSignal, "ComputeCurrentOffset2.PROC")
    compute_current_offset3 = Cpt(EpicsSignal, "ComputeCurrentOffset3.PROC")
    compute_current_offset4 = Cpt(EpicsSignal, "ComputeCurrentOffset4.PROC")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acquisition_signal = self.acquire
        self.configuration_attrs = [
            "integration_time",
            "averaging_time",
            "em_range",
            "num_averaged",
            "values_per_read",
        ]


class BMMDualEM(QuadEM):
    """2-channel NSLS2 integrated ion-chamber QuadEM variant."""

    _default_read_attrs = ["Ia", "Ib"]
    port_name = Cpt(Signal, value="NSLS2_IC")
    conf = Cpt(QuadEMPort, port_name="NSLS2_IC")
    em_range = Cpt(EpicsSignalWithRBV, "Range", string=True)
    Ia = Cpt(Nanoize, derived_from="current1.mean_value")
    Ib = Cpt(Nanoize, derived_from="current2.mean_value")
    state = Cpt(EpicsSignal, "Acquire")

    calibration_mode = Cpt(EpicsSignal, "CalibrationMode")
    copy_adc_offsets = Cpt(EpicsSignal, "CopyADCOffsets.PROC")
    compute_current_offset1 = Cpt(EpicsSignal, "ComputeCurrentOffset1.PROC")
    compute_current_offset2 = Cpt(EpicsSignal, "ComputeCurrentOffset2.PROC")
    sigma1 = Cpt(EpicsSignal, "Current1:Sigma_RBV")
    sigma2 = Cpt(EpicsSignal, "Current1:Sigma_RBV")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._acquisition_signal = self.acquire
        self.configuration_attrs = [
            "integration_time",
            "averaging_time",
            "em_range",
            "num_averaged",
            "values_per_read",
        ]


class IntegratedIC(BMMDualEM):
    bias = Cpt(EpicsSignal, "BiasVoltage")
    capacitor_range = Cpt(EpicsSignal, "Range")


# ===========================================================================
# LakeShore 331  —  ported from BMM/lakeshore.py
# ===========================================================================


class LSatSetPoint(DerivedSignal):
    """Done condition: temperature >= setpoint; latched once reached.

    Stub: the live profile references module-level `lssp` / `lakeshore_done_flag`
    EpicsSignals; we provide pass-through math without the latch.
    """

    def __init__(self, trigger, *, parent=None, **kwargs):
        trigger = getattr(parent, trigger)
        super().__init__(derived_from=trigger, parent=parent, **kwargs)

    def forward(self, value):
        return value

    def inverse(self, value):
        return 0  # never-done without the runtime latch


class LakeShore(PVPositioner):
    """LakeShore 331 temperature controller (RS232 via Moxa terminal server)."""

    readback = Cpt(EpicsSignalRO, "CONTROL")
    setpoint = Cpt(EpicsSignalWithRBV, "SP")
    done = Cpt(LSatSetPoint, trigger="readback")

    p = Cpt(EpicsSignalWithRBV, "P")
    i = Cpt(EpicsSignalWithRBV, "I")
    d = Cpt(EpicsSignalWithRBV, "D")
    ramp_rate = Cpt(EpicsSignalWithRBV, "RAMP_RATE")
    ramp = Cpt(EpicsSignalWithRBV, "RAMP")
    power = Cpt(EpicsSignalWithRBV, "HEAT_RNG")

    input_sel = Cpt(EpicsSignal, "INPUT_SEL")
    units_sel = Cpt(EpicsSignal, "UNITS_SEL")

    sample_a = Cpt(EpicsSignalRO, "SAMPLE_A")
    sample_b = Cpt(EpicsSignalRO, "SAMPLE_B")
    heater_pwr = Cpt(EpicsSignalRO, "HEATER_PWR")

    temp_scan_rate = Cpt(EpicsSignal, "READ.SCAN")
    ctrl_scan_rate = Cpt(EpicsSignal, "READ_RDAT_SCALC.SCAN")

    serial = Cpt(EpicsSignal, "SERIAL")
    deadband = 3.0


# ===========================================================================
# Linkam T96  —  ported from BMM/linkam.py
# ===========================================================================


class AtSetpoint(DerivedSignal):
    """Bit-2 of Linkam status code = at-setpoint flag."""

    def __init__(self, parent_attr, *, parent=None, **kwargs):
        code_signal = getattr(parent, parent_attr)
        super().__init__(derived_from=code_signal, parent=parent, **kwargs)

    def forward(self, value):
        return value

    def inverse(self, value):
        return 1 if (int(value) & 2 == 2) else 0


class Linkam(PVPositioner):
    """Linkam T96 controller (RS232 via Moxa)."""

    readback = Cpt(EpicsSignalRO, "TEMP")
    setpoint = Cpt(EpicsSignal, "SETPOINT:SET")
    status_code = Cpt(EpicsSignal, "STATUS")
    done = Cpt(AtSetpoint, parent_attr="status_code")

    init = Cpt(EpicsSignal, "INIT")
    model_array = Cpt(EpicsSignal, "MODEL")
    serial_array = Cpt(EpicsSignal, "SERIAL")
    stage_model_array = Cpt(EpicsSignal, "STAGE:MODEL")
    stage_serial_array = Cpt(EpicsSignal, "STAGE:SERIAL")
    firm_ver = Cpt(EpicsSignal, "FIRM:VER")
    hard_ver = Cpt(EpicsSignal, "HARD:VER")
    ctrllr_err = Cpt(EpicsSignal, "CTRLLR:ERR")
    config = Cpt(EpicsSignal, "CONFIG")
    stage_config = Cpt(EpicsSignal, "STAGE:CONFIG")
    disable = Cpt(EpicsSignal, "DISABLE")
    dsc = Cpt(EpicsSignal, "DSC")
    RR_set = Cpt(EpicsSignal, "RAMPRATE:SET")
    RR = Cpt(EpicsSignal, "RAMPRATE")
    ramptime = Cpt(EpicsSignal, "RAMPTIME")
    startheat = Cpt(EpicsSignal, "STARTHEAT")
    holdtime_set = Cpt(EpicsSignal, "HOLDTIME:SET")
    holdtime = Cpt(EpicsSignal, "HOLDTIME")
    power = Cpt(EpicsSignalRO, "POWER")
    lnp_speed = Cpt(EpicsSignal, "LNP_SPEED")
    lnp_mode_set = Cpt(EpicsSignal, "LNP_MODE:SET")
    lnp_speed_set = Cpt(EpicsSignal, "LNP_SPEED:SET")

    deadband = 1.0


# ===========================================================================
# Glancing-angle stage  —  ported from BMM/glancing_angle.py
# ===========================================================================


class GlancingAngle(Device):
    """8-spinner glancing-angle XAS stage.

    The runtime class also references `xafs_garot` for rotation and runs
    auto-alignment plans; only the spinner Components are in the happi
    shape.
    """

    spinner1 = Cpt(EpicsSignal, "OutPt08:Data-Sel")
    spinner2 = Cpt(EpicsSignal, "OutPt09:Data-Sel")
    spinner3 = Cpt(EpicsSignal, "OutPt10:Data-Sel")
    spinner4 = Cpt(EpicsSignal, "OutPt11:Data-Sel")
    spinner5 = Cpt(EpicsSignal, "OutPt12:Data-Sel")
    spinner6 = Cpt(EpicsSignal, "OutPt13:Data-Sel")
    spinner7 = Cpt(EpicsSignal, "OutPt14:Data-Sel")
    spinner8 = Cpt(EpicsSignal, "OutPt15:Data-Sel")


# ===========================================================================
# BMMSnapshot (webcam capture)  —  ported from BMM/camera_device.py
# ===========================================================================


class ExternalFileReference(Signal):
    """Pure-software signal that describe()s an image stored in an external file."""

    def __init__(self, *args, shape=(1080, 1920, 3), **kwargs):
        super().__init__(*args, **kwargs)
        self.shape = shape

    def describe(self):
        res = super().describe()
        res[self.name].update(
            {"external": "FILESTORE:", "dtype": "array", "shape": self.shape}
        )
        return res


class BMMSnapshot(Device):
    """Webcam snapshot device with single Component for the external image ref.

    Constructor takes `root=<path>` and `which=<xas|xrd|usb|analog>` kwargs;
    we accept and store them so happi entries that supply these instantiate.
    """

    image = Cpt(
        ExternalFileReference, value="", kind=Kind.normal
    )

    def __init__(self, *args, root=None, which="analog", **kwargs):
        super().__init__(*args, **kwargs)
        self._root = root
        self._which = which


# ===========================================================================
# AxisCaprotoCam (axis webcam via caproto IOC)  —  bmm_tools/devices/axis_webcam.py
# ===========================================================================


class AxisCaprotoCam(Device):
    """Axis web camera driven by a caproto IOC."""

    write_dir = Cpt(EpicsSignal, "write_dir", string=True)
    file_name = Cpt(EpicsSignal, "file_name", string=True)
    full_file_path = Cpt(EpicsSignalRO, "full_file_path", string=True)
    ioc_stage = Cpt(EpicsSignal, "stage", string=True)
    acquire = Cpt(EpicsSignal, "acquire", string=True)
    image = Cpt(ExternalFileReference, kind=Kind.normal)

    def __init__(self, *args, root_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        # root_dir is required by the runtime; happi entries supply it.
        self._root_dir = root_dir


# ===========================================================================
# Pilatus 100k  —  ported from bmm_tools/devices/pilatus.py
# ===========================================================================


class BMMFileStoreHDF5(FileStorePluginBase):
    """HDF5 filestore that emits AD_HDF5 resource records and stages capture."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filestore_spec = "AD_HDF5"
        self.stage_sigs.update(
            [
                ("file_template", "%s%s_%6.6d.h5"),
                ("file_write_mode", "Capture"),
                ("capture", 1),
            ]
        )

    def get_frames_per_point(self):
        return self.parent.cam.num_images.get()


class BMMHDF5Plugin(HDF5Plugin_V33, BMMFileStoreHDF5, FileStoreIterativeWrite):
    pass


class BMMPilatus(AreaDetector):
    image = Cpt(ImagePlugin, "image1:")
    hdf5 = Cpt(
        BMMHDF5Plugin,
        "HDF1:",
        write_path_template=f"{_FILESTORE_BASE}/pilatus100k-1/%Y/%m/%d/",
        read_path_template=f"{_FILESTORE_BASE}/pilatus100k-1/%Y/%m/%d/",
        read_attrs=[],
        root=f"{_FILESTORE_BASE}/pilatus100k-1/",
    )
    stats = Cpt(EpicsSignalRO, "Stats1:Total_RBV")
    roi2 = Cpt(EpicsSignalRO, "ROIStat1:2:Total_RBV")
    roi3 = Cpt(EpicsSignalRO, "ROIStat1:3:Total_RBV")

    cam_file_path = Cpt(EpicsSignalWithRBV, "cam1:FilePath")
    cam_file_name = Cpt(EpicsSignalWithRBV, "cam1:FileName")
    cam_file_number = Cpt(EpicsSignalWithRBV, "cam1:FileNumber")
    cam_auto_increment = Cpt(EpicsSignalWithRBV, "cam1:AutoIncrement")
    cam_file_template = Cpt(EpicsSignalWithRBV, "cam1:FileTemplate")
    cam_full_file_name = Cpt(EpicsSignalRO, "cam1:FullFileName_RBV")
    cam_file_format = Cpt(EpicsSignalWithRBV, "cam1:FileFormat")

    threshold_energy = Cpt(EpicsSignalWithRBV, "cam1:ThresholdEnergy")
    photon_energy = Cpt(EpicsSignalWithRBV, "cam1:Energy")
    gain = Cpt(EpicsSignal, "cam1:GainMenu")


if SingleTriggerV33 is not None:

    class BMMPilatusSingleTrigger(SingleTriggerV33, BMMPilatus):
        pass

else:  # nslsii unavailable
    BMMPilatusSingleTrigger = None


# ===========================================================================
# USB cameras  —  ported from bmm_tools/devices/usb_camera.py
# ===========================================================================


class BMMFileStoreJPEG(FileStorePluginBase):
    """JPEG filestore for USB cams; emits BMM_USBCAM resource records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filestore_spec = "BMM_USBCAM"
        self.stage_sigs.update(
            [
                ("file_template", "%s%s_%6.6d.jpeg"),
                ("file_write_mode", "Single"),
            ]
        )

    def get_frames_per_point(self):
        return self.parent.cam.num_images.get()


class BMMJPEGPlugin(JPEGPlugin_V33, BMMFileStoreJPEG):
    pass


class BMMUVC(ProsilicaDetector):
    image = Cpt(ImagePlugin, "image1:")
    jpeg = Cpt(
        BMMJPEGPlugin,
        "JPEG1:",
        write_path_template=f"{_FILESTORE_BASE}/usbcam/%Y/%m/%d/",
        read_path_template=f"{_FILESTORE_BASE}/usbcam/%Y/%m/%d/",
        read_attrs=[],
        root=f"{_FILESTORE_BASE}/usbcam/",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_sigs.update([(self.cam.trigger_mode, "Internal")])


if SingleTriggerV33 is not None:

    class BMMUVCSingleTrigger(SingleTriggerV33, BMMUVC):
        pass

else:
    BMMUVCSingleTrigger = None


# ===========================================================================
# Eiger 1M  —  ported from BMM/eiger.py
# ===========================================================================


class BMMEiger(AreaDetector):
    image = Cpt(ImagePlugin, "image1:")
    hdf5 = Cpt(
        BMMHDF5Plugin,
        "HDF1:",
        write_path_template=f"{_FILESTORE_BASE}/eiger1m-1/%Y/%m/%d/",
        read_path_template=f"{_FILESTORE_BASE}/eiger1m-1/%Y/%m/%d/",
        read_attrs=[],
        root=f"{_FILESTORE_BASE}/eiger1m-1/",
    )
    stats = Cpt(EpicsSignalRO, "Stats1:Total_RBV")
    roi2 = Cpt(EpicsSignalRO, "ROIStat1:2:Total_RBV")
    roi3 = Cpt(EpicsSignalRO, "ROIStat1:3:Total_RBV")

    cam_file_path = Cpt(EpicsSignalWithRBV, "cam1:FilePath")
    cam_file_name = Cpt(EpicsSignalWithRBV, "cam1:FileName")
    cam_file_number = Cpt(EpicsSignalWithRBV, "cam1:FileNumber")
    cam_auto_increment = Cpt(EpicsSignalWithRBV, "cam1:AutoIncrement")
    cam_file_template = Cpt(EpicsSignalWithRBV, "cam1:FileTemplate")
    cam_full_file_name = Cpt(EpicsSignalRO, "cam1:FullFileName_RBV")
    cam_file_format = Cpt(EpicsSignalWithRBV, "cam1:FileFormat")

    threshold_energy = Cpt(EpicsSignalWithRBV, "cam1:ThresholdEnergy")
    photon_energy = Cpt(EpicsSignalWithRBV, "cam1:PhotonEnergy")


if SingleTriggerV33 is not None:

    class BMMEigerSingleTrigger(SingleTriggerV33, BMMEiger):
        pass

else:
    BMMEigerSingleTrigger = None


# ===========================================================================
# Dante 7-element SDD  —  ported from BMM/dante.py
# ===========================================================================


from ophyd.areadetector.base import ADBase


class DanteCamBase(ADBase):
    """Stub for Dante's CamBase — minimal AD-shape so BMMDante introspects."""

    pass


class BMMDanteFileStoreHDF5(FileStorePluginBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filestore_spec = "AD_HDF5"


class BMMDanteHDF5Plugin(HDF5Plugin_V33, BMMDanteFileStoreHDF5, FileStoreIterativeWrite):
    pass


class BMMDante(DetectorBase):
    """7-channel Dante SDD (channel 8 disabled per profile)."""

    cam = Cpt(DanteCamBase, "dante:")
    acquire_time = ADCpt(EpicsSignal, "dante:PresetReal")
    acquire = ADCpt(EpicsSignal, "dante:EraseStart")

    mca1 = ADCpt(EpicsSignal, "mca1")
    mca2 = ADCpt(EpicsSignal, "mca2")
    mca3 = ADCpt(EpicsSignal, "mca3")
    mca4 = ADCpt(EpicsSignal, "mca4")
    mca5 = ADCpt(EpicsSignal, "mca5")
    mca6 = ADCpt(EpicsSignal, "mca6")
    mca7 = ADCpt(EpicsSignal, "mca7")

    hdf5 = Cpt(
        BMMDanteHDF5Plugin,
        "HDF1:",
        write_path_template=f"{_FILESTORE_BASE}/dante-1/%Y/%m/%d/",
        read_path_template=f"{_FILESTORE_BASE}/dante-1/%Y/%m/%d/",
        read_attrs=[],
        root=f"{_FILESTORE_BASE}/dante-1/",
    )
    roi1 = Cpt(EpicsSignalRO, "ROIStat1:1:Total_RBV")
    roi2 = Cpt(EpicsSignalRO, "ROIStat1:2:Total_RBV")
    roi3 = Cpt(EpicsSignalRO, "ROIStat1:3:Total_RBV")
    roi4 = Cpt(EpicsSignalRO, "ROIStat1:4:Total_RBV")
    roi5 = Cpt(EpicsSignalRO, "ROIStat1:5:Total_RBV")
    roi6 = Cpt(EpicsSignalRO, "ROIStat1:6:Total_RBV")
    roi7 = Cpt(EpicsSignalRO, "ROIStat1:7:Total_RBV")

    nchannels = 8


if SingleTriggerV33 is not None:

    class BMMDanteSingleTrigger(SingleTriggerV33, BMMDante):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Runtime profile also allocates DanteChannel objects for each
            # MCA; channel objects depend on a custom redis-backed builder
            # we don't replicate here.

else:
    BMMDanteSingleTrigger = None


# ===========================================================================
# Xspress3 1 / 4 / 7 element  —  built dynamically via nslsii
# ===========================================================================
# BMM's profile defines `BMMXspress3DetectorBase` (an empty subclass of
# nslsii.detectors.xspress3.Xspress3Detector) and then builds 1/4/7-element
# variants by calling `nslsii.areadetector.xspress3.build_xspress3_class`.
# We mirror the same call here, guarded so the shim still loads without nslsii.

if build_xspress3_class is not None:
    try:
        from nslsii.areadetector.xspress3 import Xspress3Detector as _Xspress3Det

        class BMMXspress3DetectorBase(_Xspress3Det):
            pass

        BMMXspress3Detector_1Element = build_xspress3_class(
            channel_numbers=(1,),
            mcaroi_numbers=range(1, 21),
            image_data_key="xrf",
            xspress3_parent_classes=(BMMXspress3DetectorBase,),
        )
        BMMXspress3Detector_4Element = build_xspress3_class(
            channel_numbers=(1, 2, 3, 4),
            mcaroi_numbers=range(1, 21),
            image_data_key="xrf",
            xspress3_parent_classes=(BMMXspress3DetectorBase,),
        )
        BMMXspress3Detector_7Element = build_xspress3_class(
            channel_numbers=(1, 2, 3, 4, 5, 6, 7),
            mcaroi_numbers=range(1, 21),
            image_data_key="xrf",
            xspress3_parent_classes=(BMMXspress3DetectorBase,),
        )
    except Exception:  # nslsii layout may differ across versions
        BMMXspress3DetectorBase = None
        BMMXspress3Detector_1Element = None
        BMMXspress3Detector_4Element = None
        BMMXspress3Detector_7Element = None
else:
    BMMXspress3DetectorBase = None
    BMMXspress3Detector_1Element = None
    BMMXspress3Detector_4Element = None
    BMMXspress3Detector_7Element = None


# ===========================================================================
# Dwell-time pseudo-positioner  —  ported from BMM/dwelltime.py
# ===========================================================================


class QuadEMDwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "AveragingTime")
    readback = Cpt(EpicsSignalRO, "AveragingTime_RBV")


class StruckDwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "TP")
    readback = Cpt(EpicsSignalRO, "TP")


class IC0DwellTime(QuadEMDwellTime):
    pass


class IC1DwellTime(QuadEMDwellTime):
    pass


class IC2DwellTime(QuadEMDwellTime):
    pass


class Xspress3DwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "det1:AcquireTime")
    readback = Cpt(EpicsSignalRO, "det1:AcquireTime_RBV")


class PilatusDwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "cam1:AcquireTime")
    readback = Cpt(EpicsSignalRO, "cam1:AcquireTime_RBV")


class EigerDwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "cam1:AcquireTime")
    readback = Cpt(EpicsSignalRO, "cam1:AcquireTime_RBV")


class DanteDwellTime(PVPositionerPC):
    setpoint = Cpt(EpicsSignal, "dante:PresetReal")
    readback = Cpt(EpicsSignalRO, "dante:PresetReal")


class LockedDwellTimes(PseudoPositioner):
    """Sync QuadEM / Xspress3 / IC0/1/2 / Pilatus / Eiger / Dante dwell times.

    The live profile reads ``with_quadem``, ``with_ic0`` etc. flags from
    `BMM/user_ns/dwelltime.py` to *conditionally* register components.  The
    shim cannot read those runtime flags at class-definition time, so we
    declare every channel; happi entries that depend on a missing detector's
    PV will fail to connect, which matches the profile's actual behavior.
    """

    dwell_time = Cpt(PseudoSingle, kind="hinted")
    quadem_dwell_time = Cpt(QuadEMDwellTime, "XF:06BM-BI{EM:1}EM180:", egu="seconds")
    struck_dwell_time = Cpt(StruckDwellTime, "XF:06BM-ES:1{Sclr:1}.", egu="seconds")
    ic0_dwell_time = Cpt(IC0DwellTime, "XF:06BM-BI{IC:0}EM180:", egu="seconds")
    ic1_dwell_time = Cpt(IC1DwellTime, "XF:06BM-BI{IC:1}EM180:", egu="seconds")
    ic2_dwell_time = Cpt(IC2DwellTime, "XF:06BM-BI{IC:3}EM180:", egu="seconds")
    xspress3_dwell_time = Cpt(
        Xspress3DwellTime, "XF:06BM-ES{Xsp:1}:", egu="seconds"
    )
    pilatus_dwell_time = Cpt(
        PilatusDwellTime, "XF:06BMB-ES{Det:PIL100k}:", egu="seconds"
    )
    eiger_dwell_time = Cpt(
        PilatusDwellTime, "XF:06BM-ES{Det-Eiger:1}", egu="seconds"
    )
    dante_dwell_time = Cpt(
        DanteDwellTime, "XF:06BM-ES{Dante-Det:1}", egu="seconds"
    )

    @pseudo_position_argument
    def forward(self, pseudo_pos):
        return self.RealPosition(
            quadem_dwell_time=pseudo_pos.dwell_time,
            struck_dwell_time=pseudo_pos.dwell_time,
            ic0_dwell_time=pseudo_pos.dwell_time,
            ic1_dwell_time=pseudo_pos.dwell_time,
            ic2_dwell_time=pseudo_pos.dwell_time,
            xspress3_dwell_time=pseudo_pos.dwell_time,
            pilatus_dwell_time=pseudo_pos.dwell_time,
            eiger_dwell_time=pseudo_pos.dwell_time,
            dante_dwell_time=pseudo_pos.dwell_time,
        )

    @real_position_argument
    def inverse(self, real_pos):
        return self.PseudoPosition(dwell_time=real_pos.ic0_dwell_time)
