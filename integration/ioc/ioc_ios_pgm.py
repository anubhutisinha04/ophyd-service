"""Simulated VLS-PGM monochromator IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-OP{Mono`` that direct-control caputs to
when the frontend periodic-table widget fires an X-ray edge preset. Mirrors
the ophyd device classes ``PGM`` / ``PGMEnergy`` / ``MonoFly`` defined in the
IOS profile collection.

Dynamics:
    * ``Enrgy-I`` slews toward ``Enrgy-SP`` at a fixed slew rate (eV/s).
    * Writing ``Cmd:Stop-Cmd`` freezes ``Enrgy-I`` and pins ``Enrgy-SP`` to it.
    * Writing ``Cmd:FlyStart-Cmd.PROC`` sweeps ``Enrgy-I`` from ``Enrgy:Start-SP``
      to ``Enrgy:Stop-SP`` at ``Enrgy:FlyVelo-SP`` eV/s.
    * ``Sts:Move-Sts`` / ``Sts:Scan-Sts`` reflect motion state.

Motor sub-axes (``-Ax:MirP/MirX/GrtP/GrtX}Mtr``) are deferred — the periodic-
table preset doesn't caput them.
"""

import asyncio
import math

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


# Slow-move slew rate (eV/s). Fast enough for tests to complete in seconds,
# slow enough that callers see a non-trivial "Moving" interval.
SLEW_EV_PER_S = 50.0

TICK_S = 0.1
SETTLE_TOL_EV = 0.01


class PGMIOC(PVGroup):
    """Caproto IOC for the IOS VLS-PGM monochromator.

    Cross-reference: ``ios-profile-collection/startup/10-optics.py`` defines
    the ``PGM``/``PGMEnergy``/``MonoFly`` ophyd shim at the matching prefix.
    """

    # caproto runs each pvproperty name through str.format() too; literal
    # `{` and `}` must be doubled. The leading `}}` here resolves to the
    # literal `}` that closes the prefix's `{Mono`.
    Enrgy_SP = pvproperty(
        value=850.0,
        name="}}Enrgy-SP",
        lower_ctrl_limit=200.0,
        upper_ctrl_limit=2200.0,
        units="eV",
        precision=3,
    )
    Enrgy_I = pvproperty(
        value=850.0,
        name="}}Enrgy-I",
        read_only=True,
        units="eV",
        precision=3,
    )
    Stop_Cmd = pvproperty(value=0, name="}}Cmd:Stop-Cmd")

    Start_SP = pvproperty(value=850.0, name="}}Enrgy:Start-SP", units="eV", precision=3)
    Stop_SP = pvproperty(value=860.0, name="}}Enrgy:Stop-SP", units="eV", precision=3)
    FlyVelo = pvproperty(value=0.2, name="}}Enrgy:FlyVelo-SP", units="eV/s", precision=4)

    FlyStart = pvproperty(value=0, name="}}Cmd:FlyStart-Cmd.PROC")
    FlyStopProc = pvproperty(value=0, name="}}Cmd:Stop-Cmd.PROC")

    Scan_Sts = pvproperty(
        value="Idle",
        name="}}Sts:Scan-Sts",
        enum_strings=("Idle", "Scanning"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )
    Move_Sts = pvproperty(
        value="Done",
        name="}}Sts:Move-Sts",
        enum_strings=("Done", "Moving"),
        dtype=ChannelType.ENUM,
        read_only=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fly_task: asyncio.Task | None = None
        self._stop_requested = False

    @Enrgy_SP.startup
    async def _slew_loop(self, _instance, async_lib):
        """Background slew loop nudging Enrgy-I toward Enrgy-SP."""
        while True:
            await async_lib.library.sleep(TICK_S)
            if self._fly_task is not None and not self._fly_task.done():
                continue
            sp = self.Enrgy_SP.value
            i = self.Enrgy_I.value
            delta = sp - i
            if abs(delta) <= SETTLE_TOL_EV:
                if self.Move_Sts.value != "Done":
                    await self.Move_Sts.write("Done")
                continue
            step = math.copysign(min(abs(delta), SLEW_EV_PER_S * TICK_S), delta)
            await self.Enrgy_I.write(i + step)
            if self.Move_Sts.value != "Moving":
                await self.Move_Sts.write("Moving")

    @Stop_Cmd.putter
    async def _on_stop_cmd(self, _instance, value):
        if value:
            await self.Enrgy_SP.write(self.Enrgy_I.value)
            if self._fly_task and not self._fly_task.done():
                self._stop_requested = True

    @FlyStart.putter
    async def _on_fly_start(self, _instance, value):
        if not value:
            return
        if self._fly_task and not self._fly_task.done():
            return
        self._stop_requested = False
        self._fly_task = asyncio.create_task(self._run_fly())

    @FlyStopProc.putter
    async def _on_fly_stop(self, _instance, value):
        if value:
            self._stop_requested = True

    async def _run_fly(self):
        start = self.Start_SP.value
        stop = self.Stop_SP.value
        velo = abs(self.FlyVelo.value)
        if velo <= 0:
            return
        direction = 1.0 if stop >= start else -1.0
        await self.Enrgy_I.write(start)
        await self.Scan_Sts.write("Scanning")
        await self.Move_Sts.write("Moving")
        try:
            while not self._stop_requested:
                current = self.Enrgy_I.value
                remaining = (stop - current) * direction
                if remaining <= 0:
                    await self.Enrgy_I.write(stop)
                    break
                step = direction * min(remaining, velo * TICK_S)
                await self.Enrgy_I.write(current + step)
                await asyncio.sleep(TICK_S)
        finally:
            await self.Scan_Sts.write("Idle")
            # Pin SP so the slow-move loop doesn't pull us back to the pre-fly setpoint.
            await self.Enrgy_SP.write(self.Enrgy_I.value)
            await self.Move_Sts.write("Done")
            self._stop_requested = False


def main():
    # caproto runs the prefix through str.format() for macro expansion; literal
    # `{` and `}` must be doubled. The final prefix after expansion is
    # "XF:23ID2-OP{Mono" so concatenated PVs become "XF:23ID2-OP{Mono}Enrgy-SP" etc.
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="XF:23ID2-OP{{Mono",
        desc="IOS VLS-PGM monochromator simulation IOC.",
    )
    ioc = PGMIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
