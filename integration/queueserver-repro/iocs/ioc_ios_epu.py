"""Simulated EPU IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID-ID{EPU:N}`` (N = 1, 2) that the IOS
happi entries reference via ``ios_devs.EPU1`` / ``ios_devs.EPU2``. EPU1
exposes gap, phase, FLT interpolator, RLT interpolator, and table-select;
EPU2 exposes just gap and phase.

Phase 2 was echo-only. Phase 3 adds FLT/RLT calc dynamics: writing
``Val:Inp1-SP`` or ``Val:InpOff1-SP`` recomputes the corresponding
``Val:Out1-I`` as ``Inp1-SP + InpOff1-SP``. Real-beamline interpolator
uses a lookup table indexed by the table-select; the linear sum here is
the simplest dynamic that lets the exerciser verify a non-echo readback
without standing up the table data.

Reference: ``ios-profile-collection/startup/10-machine.py``::

    class EPU1(Device):
        gap = Cpt(GapMotor1, '-Ax:Gap}')       # → Pos-I, Pos-SP
        phase = Cpt(PhaseMotor1, '-Ax:Phase}') # → Pos-I, Pos-SP
        flt = Cpt(Interpolator, '-FLT}')       # → 10 sub-PVs
        rlt = Cpt(Interpolator, '-RLT}')
        table = Cpt(EpicsSignal, '}Val:Table-Sel')

PVs are declared flat (not via SubGroup) because caproto's SubGroup
re-expands the combined prefix, which fails on NSLS-II's unbalanced
``{...}`` style (parent prefix has an unclosed ``{``; sub-component
provides the closing brace). The trade-off is verbose pvproperty
declarations but correct CA semantics.

Phase 2 of the IOS use case (current-amp + EPU echo); paired with
``ioc_ios_curramp.py`` and ``ioc_ios_pgm.py``.
"""

import logging

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


log = logging.getLogger(__name__)


def _ro_float(name: str, initial: float = 0.0, units: str = "") -> pvproperty:
    return pvproperty(
        value=initial, name=name, read_only=True, precision=4, units=units
    )


def _rw_float(name: str, initial: float = 0.0, units: str = "") -> pvproperty:
    return pvproperty(value=initial, name=name, precision=4, units=units)


def _rw_string(name: str, initial: str = "") -> pvproperty:
    return pvproperty(value=initial, name=name, report_as_string=True, max_length=80)


class IosEpuIOC(PVGroup):
    """Both undulators in one IOC process — EPU:1 (full) + EPU:2 (gap+phase).

    All pvproperty names are relative to the IOC prefix ``XF:23ID-ID{EPU``.
    The channel number (``:1`` / ``:2``) is baked into each name; the
    closing brace of the prefix comes from ``}}`` in each name string.
    """

    # ─── EPU1 gap + phase ────────────────────────────────────────────────
    epu1_gap_i  = _ro_float(":1-Ax:Gap}}Pos-I", initial=25.0, units="mm")
    epu1_gap_sp = _rw_float(":1-Ax:Gap}}Pos-SP", initial=25.0, units="mm")
    epu1_phase_i  = _ro_float(":1-Ax:Phase}}Pos-I", initial=0.0, units="mm")
    epu1_phase_sp = _rw_float(":1-Ax:Phase}}Pos-SP", initial=0.0, units="mm")

    # ─── EPU1 table select ───────────────────────────────────────────────
    # The Ni_L preset writes epu_table=4 here. Real EPICS is mbbo with
    # named tables; for echo we just take an integer setpoint.
    epu1_table = pvproperty(value=4, name=":1}}Val:Table-Sel")

    # ─── EPU1 FLT interpolator (10 PVs) ──────────────────────────────────
    epu1_flt_inp        = _rw_float(":1-FLT}}Val:Inp1-SP")
    epu1_flt_inp_off    = _rw_float(":1-FLT}}Val:InpOff1-SP")
    epu1_flt_inp_enbl   = pvproperty(
        value="Enabled",
        name=":1-FLT}}Enbl:Inp1-Sel",
        enum_strings=("Disabled", "Enabled"),
        dtype=ChannelType.ENUM,
    )
    epu1_flt_inp_dol    = _rw_string(":1-FLT}}Val:Inp1-SP.DOL$")
    epu1_flt_out_i      = _ro_float(":1-FLT}}Val:Out1-I")
    epu1_flt_out_enbl   = pvproperty(
        value="Enable",
        name=":1-FLT}}Enbl:Out1-Sel",
        enum_strings=("Disable", "Enable"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )
    epu1_flt_calc_out   = _rw_string(":1-FLT}}Calc1.OUT$")
    epu1_flt_dband      = _rw_float(":1-FLT}}Val:DBand1-SP")
    epu1_flt_out_drv    = _ro_float(":1-FLT}}Val:OutDrv1-I")
    epu1_flt_interp_sts = pvproperty(
        value="OK",
        name=":1-FLT}}Sts:Interp1-Sts",
        enum_strings=("OK", "Error"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )

    # ─── EPU1 RLT interpolator (same shape as FLT) ──────────────────────
    epu1_rlt_inp        = _rw_float(":1-RLT}}Val:Inp1-SP")
    epu1_rlt_inp_off    = _rw_float(":1-RLT}}Val:InpOff1-SP")
    epu1_rlt_inp_enbl   = pvproperty(
        value="Enabled",
        name=":1-RLT}}Enbl:Inp1-Sel",
        enum_strings=("Disabled", "Enabled"),
        dtype=ChannelType.ENUM,
    )
    epu1_rlt_inp_dol    = _rw_string(":1-RLT}}Val:Inp1-SP.DOL$")
    epu1_rlt_out_i      = _ro_float(":1-RLT}}Val:Out1-I")
    epu1_rlt_out_enbl   = pvproperty(
        value="Enable",
        name=":1-RLT}}Enbl:Out1-Sel",
        enum_strings=("Disable", "Enable"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )
    epu1_rlt_calc_out   = _rw_string(":1-RLT}}Calc1.OUT$")
    epu1_rlt_dband      = _rw_float(":1-RLT}}Val:DBand1-SP")
    epu1_rlt_out_drv    = _ro_float(":1-RLT}}Val:OutDrv1-I")
    epu1_rlt_interp_sts = pvproperty(
        value="OK",
        name=":1-RLT}}Sts:Interp1-Sts",
        enum_strings=("OK", "Error"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )

    # ─── EPU2 gap + phase ────────────────────────────────────────────────
    epu2_gap_i  = _ro_float(":2-Ax:Gap}}Pos-I", initial=25.0, units="mm")
    epu2_gap_sp = _rw_float(":2-Ax:Gap}}Pos-SP", initial=25.0, units="mm")
    epu2_phase_i  = _ro_float(":2-Ax:Phase}}Pos-I", initial=0.0, units="mm")
    epu2_phase_sp = _rw_float(":2-Ax:Phase}}Pos-SP", initial=0.0, units="mm")

    # ─── FLT / RLT calc (Phase 3) ────────────────────────────────────────
    # Writing Inp1-SP or InpOff1-SP recomputes Out1-I = Inp1-SP + InpOff1-SP.
    # Pure linear sum, not the real lookup-table interpolation. Sufficient
    # for the exerciser to verify a non-echo output responds to either input.

    @epu1_flt_inp.putter
    async def _on_flt_inp(self, _instance, value):
        await self.epu1_flt_out_i.write(float(value) + float(self.epu1_flt_inp_off.value))

    @epu1_flt_inp_off.putter
    async def _on_flt_off(self, _instance, value):
        await self.epu1_flt_out_i.write(float(self.epu1_flt_inp.value) + float(value))

    @epu1_rlt_inp.putter
    async def _on_rlt_inp(self, _instance, value):
        await self.epu1_rlt_out_i.write(float(value) + float(self.epu1_rlt_inp_off.value))

    @epu1_rlt_inp_off.putter
    async def _on_rlt_off(self, _instance, value):
        await self.epu1_rlt_out_i.write(float(self.epu1_rlt_inp.value) + float(value))


def main():
    # caproto runs the prefix through str.format() for macro expansion; the
    # literal `{` must be doubled. After expansion the prefix is
    # "XF:23ID-ID{EPU"; each pvproperty name supplies its channel (":N")
    # and the closing brace ("}}" → "}") before its component name.
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="XF:23ID-ID{{EPU",
        desc="IOS EPU undulator simulation IOC — gap/phase echo + FLT/RLT calc.",
    )
    ioc = IosEpuIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
