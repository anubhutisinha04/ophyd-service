"""Simulated VLS-PGM monochromator IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-OP{Mono`` that direct-control caputs to
when the frontend periodic-table widget fires an X-ray edge preset. Mirrors
the ophyd device classes ``PGM`` / ``PGMEnergy`` / ``MonoFly`` defined in the
IOS profile collection.

Dynamics:
    * ``Enrgy-I`` slews toward ``Enrgy-SP`` at a fixed slew rate (eV/s).
    * Writing ``Cmd:Stop-Cmd`` (slow-move stop only) freezes ``Enrgy-I`` and
      pins ``Enrgy-SP`` to it. No-op during a fly scan — fly aborts go through
      ``Cmd:Stop-Cmd.PROC`` (per the IOS ophyd shim, where ``PGMEnergy.stop_signal``
      and ``MonoFly.fly_stop`` are independent signals).
    * Writing ``Cmd:FlyStart-Cmd.PROC`` sweeps ``Enrgy-I`` from ``Enrgy:Start-SP``
      to ``Enrgy:Stop-SP`` at ``Enrgy:FlyVelo-SP`` eV/s. Inputs are validated
      before the scan starts; invalid inputs (re-entrant, velo<=0, range outside
      Enrgy-SP's ctrl_limits) raise from the putter so the caput fails visibly.
    * ``Sts:Move-Sts`` / ``Sts:Scan-Sts`` reflect motion state.

Momentary-action PVs (``Cmd:Stop-Cmd``, ``Cmd:Stop-Cmd.PROC``,
``Cmd:FlyStart-Cmd.PROC``) self-clear to 0 after acting so monitor clients
gating on 0→1 edges see every trigger.

Motor sub-axes (``-Ax:MirP/MirX/GrtP/GrtX}Mtr``) are deferred — the periodic-
table preset doesn't caput them.
"""

import asyncio
import logging
import math

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


log = logging.getLogger(__name__)


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
        """Background slew loop nudging Enrgy-I toward Enrgy-SP.

        Each iteration is wrapped so a transient write failure can't kill the
        background task — slewing would silently freeze otherwise.
        """
        while True:
            try:
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
            except Exception:
                log.exception("slew loop iteration failed; continuing")

    @Stop_Cmd.putter
    async def _on_stop_cmd(self, _instance, value):
        """Slow-move stop only. No-op during a fly scan."""
        if not value:
            return
        if self._fly_task and not self._fly_task.done():
            # Fly aborts are Cmd:Stop-Cmd.PROC; slow-move stop must not
            # affect a concurrent fly (ophyd shim treats the two signals
            # as independent).
            self._schedule_self_clear(self.Stop_Cmd)
            return
        await self.Enrgy_SP.write(self.Enrgy_I.value)
        self._schedule_self_clear(self.Stop_Cmd)

    @FlyStart.putter
    async def _on_fly_start(self, _instance, value):
        """Validate inputs up front and raise on invalid/re-entrant.

        Pre-validation moves errors to the caput response (visible to the
        caller) instead of crashing the background task or silently no-opping.
        """
        if not value:
            return
        try:
            if self._fly_task and not self._fly_task.done():
                raise ValueError("fly scan already running")
            velo = abs(self.FlyVelo.value)
            if velo <= 0:
                raise ValueError(
                    f"Enrgy:FlyVelo-SP must be > 0; got {self.FlyVelo.value}"
                )
            lo = self.Enrgy_SP.lower_ctrl_limit
            hi = self.Enrgy_SP.upper_ctrl_limit
            for label, val in (
                ("Enrgy:Start-SP", self.Start_SP.value),
                ("Enrgy:Stop-SP", self.Stop_SP.value),
            ):
                if val < lo or val > hi:
                    raise ValueError(
                        f"{label}={val} outside Enrgy-SP ctrl_limits [{lo}, {hi}]"
                    )
            self._stop_requested = False
            self._fly_task = asyncio.create_task(self._run_fly())
            self._fly_task.add_done_callback(self._on_fly_done)
        finally:
            self._schedule_self_clear(self.FlyStart)

    @FlyStopProc.putter
    async def _on_fly_stop(self, _instance, value):
        if not value:
            return
        self._stop_requested = True
        self._schedule_self_clear(self.FlyStopProc)

    def _schedule_self_clear(self, pv) -> None:
        """Schedule a fire-and-forget reset of a momentary-action PV to 0.

        Must be scheduled (not awaited inline) so the outer caproto write
        first stores the client-written value, letting DBE_VALUE monitors
        observe the 0→1 edge. The async task then writes 0 a tick later,
        giving the 1→0 edge so the next trigger again produces 0→1.
        """
        task = asyncio.create_task(self._self_clear_after(pv))
        task.add_done_callback(self._on_self_clear_done)

    async def _self_clear_after(self, pv) -> None:
        # Brief delay so the caproto write that triggered this putter has
        # finished storing/publishing the client-written value (typically 1).
        await asyncio.sleep(0.05)
        await pv.write(0)

    def _on_self_clear_done(self, task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("self-clear task failed: %s", exc, exc_info=exc)

    def _on_fly_done(self, task: "asyncio.Task") -> None:
        """Surface fly-task exceptions instead of letting asyncio swallow them."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("fly task crashed: %s", exc, exc_info=exc)

    async def _run_fly(self):
        start = self.Start_SP.value
        stop = self.Stop_SP.value
        velo = abs(self.FlyVelo.value)
        # Defensive guard against direct callers; the putter pre-validates.
        if velo <= 0:
            log.error("_run_fly entered with velo=%r; aborting", self.FlyVelo.value)
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
            # Best-effort writes: one failure must not skip the others, so
            # Move-Sts can't get stuck at "Moving" because the SP-pin raised.
            for pv, val in (
                (self.Scan_Sts, "Idle"),
                # Pin SP so the slow-move loop doesn't pull us back to the
                # pre-fly setpoint.
                (self.Enrgy_SP, self.Enrgy_I.value),
                (self.Move_Sts, "Done"),
            ):
                try:
                    await pv.write(val)
                except Exception:
                    log.exception("fly finally: write to %s failed", pv.name)
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
