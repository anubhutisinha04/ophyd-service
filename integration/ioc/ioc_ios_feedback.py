"""Simulated M1B1 feedback-loop IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-OP{FBck}*`` referenced by the IOS
happi entry ``ios_devs.M1bMirror.fbl`` (a ``FeedbackLoop`` device that
overrides ``add_prefix=""`` so the FBck prefix is literal, not relative
to the mirror's prefix).

Dynamics:
    * When ``Sts:FB-Sel`` is ``"On"``, a background task converges
      ``PID.CVAL`` toward ``PID-SP`` with a simple first-order step
      (gain * error per tick, no integral/derivative — pure "P" loop).
    * Once |error| <= deadband, CVAL stays at SP.
    * When ``Sts:FB-Sel`` is ``"Off"``, CVAL stays where it is.
    * ``PID.OVAL`` mirrors CVAL for simplicity (real PID's OVAL is the
      drive signal, separate from CVAL the measured value).

Phase 3 of the IOS use case (dynamic IOCs).
"""

import logging

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


log = logging.getLogger(__name__)


# Convergence rate (fraction of error per tick). 0.2 = ~16% per tick → ~10
# ticks to settle (~1 second at TICK_S=0.1).
P_GAIN = 0.2
TICK_S = 0.1


class FeedbackIOC(PVGroup):
    """M1B1 PID feedback loop at XF:23ID2-OP{FBck}.*"""

    Sts_FB_Sel = pvproperty(
        value="Off",
        name="Sts:FB-Sel",
        enum_strings=("Off", "On"),
        dtype=ChannelType.ENUM,
    )
    PID_SP = pvproperty(value=0.0, name="PID-SP", precision=4)
    PID_VAL = pvproperty(
        value=0.0, name="PID.VAL", read_only=True, precision=4
    )
    PID_CVAL = pvproperty(
        value=0.0, name="PID.CVAL", read_only=True, precision=4
    )
    PID_OVAL = pvproperty(
        value=0.0, name="PID.OVAL", read_only=True, precision=4
    )
    PID_Err = pvproperty(
        value=0.0, name="PID.Err", read_only=True, precision=4
    )
    Val_Sbl_SP = pvproperty(value=0.01, name="Val:Sbl-SP", precision=4)

    # ─── Contract PVs (declared but not actively used by the loop) ───────
    # ios_devs.FeedbackLoop declares these as required Components; ophyd's
    # FeedbackLoop.__init__ wait_for_connection() on them would time out
    # if the IOC doesn't serve them. They're inert here — the loop uses
    # constants for high/low limits and a fixed TICK_S — but their CA
    # presence keeps the ophyd-side device-class walk happy when the
    # resolver bug for `add_prefix=""` Components is fixed and direct-
    # control starts walking m1b1.fbl.* live.
    PID_DRVH = pvproperty(value=1.0e9, name="PID.DRVH", precision=4)
    PID_DRVL = pvproperty(value=-1.0e9, name="PID.DRVL", precision=4)
    PID_DT = pvproperty(
        value=TICK_S, name="PID.DT", read_only=True, precision=4, units="s"
    )
    PID_MDT = pvproperty(value=TICK_S, name="PID.MDT", precision=4, units="s")
    PID_SCAN = pvproperty(value=0, name="PID.SCAN")

    @Sts_FB_Sel.startup
    async def _loop(self, _instance, async_lib):
        """Background loop — runs forever; only acts when FB-Sel == 'On'.

        Sleep sits OUTSIDE the try so loop-closed / cancelled errors at
        shutdown propagate and let the task exit cleanly (avoids the
        busy-log torrent that would otherwise happen on docker compose
        down). The try wraps only the writes — attribute lookups,
        arithmetic, and branch decisions are intentionally NOT swallowed
        so refactor bugs (AttributeError, TypeError) crash loud rather
        than spinning in silent log spam at TICK_S frequency. Mirrors the
        pattern in ios_pgm._slew_loop.
        """
        while True:
            await async_lib.library.sleep(TICK_S)
            if self.Sts_FB_Sel.value != "On":
                continue
            sp = float(self.PID_SP.value)
            cval = float(self.PID_CVAL.value)
            deadband = float(self.Val_Sbl_SP.value)
            error = sp - cval
            settled = abs(error) <= deadband
            if settled and cval == sp:
                # Nothing to write; SP already exactly hit.
                continue
            try:
                if settled:
                    # Settled but cval != sp by sub-deadband: pin to SP
                    # exactly so readers see a stable value rather than
                    # the last sub-deadband drift.
                    await self.PID_CVAL.write(sp)
                    await self.PID_OVAL.write(sp)
                    await self.PID_Err.write(0.0)
                else:
                    new_cval = cval + P_GAIN * error
                    await self.PID_CVAL.write(new_cval)
                    await self.PID_OVAL.write(new_cval)
                    await self.PID_Err.write(sp - new_cval)
                    # VAL mirrors SP (requested target) per the ophyd
                    # shim's `requested_value = Cpt(EpicsSignalRO,
                    # "PID.VAL")` naming.
                    if float(self.PID_VAL.value) != sp:
                        await self.PID_VAL.write(sp)
            except Exception:
                log.exception("feedback loop write failed; continuing")


def main():
    # Prefix has matched braces; escape both. After expansion:
    # XF:23ID2-OP{FBck}. Per-PV names have no braces.
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="XF:23ID2-OP{{FBck}}",
        desc="IOS M1B1 PID feedback simulation IOC (Sts:FB-Sel, PID-SP, PID.CVAL).",
    )
    ioc = FeedbackIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
