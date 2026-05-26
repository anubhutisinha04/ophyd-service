"""Simulated VLS-PGM monochromator IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-OP{Mono`` that direct-control caputs to
when the frontend periodic-table widget fires an X-ray edge preset. Mirrors
the ophyd device classes ``PGM`` / ``PGMEnergy`` / ``MonoFly`` defined in the
IOS profile collection.

Dynamics:
    * ``Enrgy-I`` slews toward ``Enrgy-SP`` at a fixed slew rate (eV/s).
    * Writing ``Cmd:Stop-Cmd`` halts motion — slow move pins ``Enrgy-SP``
      to current ``Enrgy-I``; if a fly is in flight it is also aborted (so
      bluesky's ``RunEngine.abort()`` path, which writes Stop-Cmd via
      ``PGMEnergy.stop_signal``, halts everything). ``Cmd:Stop-Cmd.PROC``
      is the fly-specific aborter for callers that want fly-only stop.
    * Writing ``Cmd:FlyStart-Cmd.PROC`` sweeps ``Enrgy-I`` from ``Enrgy:Start-SP``
      to ``Enrgy:Stop-SP`` at ``Enrgy:FlyVelo-SP`` eV/s. Inputs are validated
      before the scan starts AND captured at validation time (no TOCTOU re-read);
      invalid inputs (re-entrant, velo<=0, range outside Enrgy-SP's ctrl_limits)
      raise from the putter so the caput fails visibly.
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
        # Pending self-clear task per PV — new triggers cancel the prior
        # pending clear so rapid retriggers don't clobber the visible value
        # ahead of the latest trigger's intended 50ms window.
        self._self_clear_tasks: dict[int, asyncio.Task] = {}

    @Enrgy_SP.startup
    async def _slew_loop(self, _instance, async_lib):
        """Background slew loop nudging Enrgy-I toward Enrgy-SP.

        Sleep sits OUTSIDE the try so loop-closed / cancelled errors at
        shutdown propagate and let the task exit cleanly. The try wraps
        only the writes (which can fail transiently) — attribute lookups
        and arithmetic are intentionally NOT swallowed so refactor bugs
        (AttributeError, TypeError) crash loud rather than spinning in
        a silent log torrent.
        """
        while True:
            await async_lib.library.sleep(TICK_S)
            if self._fly_task is not None and not self._fly_task.done():
                continue
            sp = self.Enrgy_SP.value
            i = self.Enrgy_I.value
            delta = sp - i
            settled = abs(delta) <= SETTLE_TOL_EV
            try:
                if settled:
                    if self.Move_Sts.value != "Done":
                        await self.Move_Sts.write("Done")
                    continue
                step = math.copysign(min(abs(delta), SLEW_EV_PER_S * TICK_S), delta)
                await self.Enrgy_I.write(i + step)
                if self.Move_Sts.value != "Moving":
                    await self.Move_Sts.write("Moving")
            except Exception:
                log.exception("slew loop write failed; continuing")

    @Stop_Cmd.putter
    async def _on_stop_cmd(self, _instance, value):
        """Stop slow move; also abort any concurrent fly.

        Operator-safety priority: if someone hits Stop, all motion stops.
        The ophyd shim distinguishes Cmd:Stop-Cmd (PGMEnergy.stop_signal,
        slow-move) and Cmd:Stop-Cmd.PROC (MonoFly.fly_stop, fly-only), but
        RunEngine.abort() only knows about PGMEnergy.stop_signal — so this
        signal must halt flies too or panic-Stop silently fails to abort
        a running fly. SP-pin happens via the fly's own finally when the
        abort lands; outside fly we pin SP here directly.
        """
        if not value:
            return
        if self._fly_task and not self._fly_task.done():
            log.warning("Cmd:Stop-Cmd received during fly; aborting fly")
            self._stop_requested = True
            # Skip SP pin during fly — _run_fly's finally pins SP to current
            # Enrgy-I, which is the moment-of-stop value we want.
        else:
            await self.Enrgy_SP.write(self.Enrgy_I.value)
        self._schedule_self_clear(self.Stop_Cmd)

    @FlyStart.putter
    async def _on_fly_start(self, _instance, value):
        """Validate inputs up front and raise on invalid/re-entrant.

        Validated values are passed as args to ``_run_fly`` so a concurrent
        caput to Start/Stop/FlyVelo between this putter and task entry
        can't slip in an out-of-range value (the C1 TOCTOU from PR #21
        review).
        """
        if not value:
            return
        if self._fly_task and not self._fly_task.done():
            raise ValueError("fly scan already running")
        velo = abs(self.FlyVelo.value)
        if velo <= 0:
            raise ValueError(
                f"Enrgy:FlyVelo-SP must be > 0; got {self.FlyVelo.value}"
            )
        start = self.Start_SP.value
        stop = self.Stop_SP.value
        lo = self.Enrgy_SP.lower_ctrl_limit
        hi = self.Enrgy_SP.upper_ctrl_limit
        for label, val in (("Enrgy:Start-SP", start), ("Enrgy:Stop-SP", stop)):
            if val < lo or val > hi:
                raise ValueError(
                    f"{label}={val} outside Enrgy-SP ctrl_limits [{lo}, {hi}]"
                )
        self._stop_requested = False
        self._fly_task = asyncio.create_task(self._run_fly(start, stop, velo))
        self._fly_task.add_done_callback(self._on_fly_done)
        # Only schedule self-clear on the success path — a validation raise
        # left FlyStart at 0 (caproto rejected the write), so no clear needed.
        self._schedule_self_clear(self.FlyStart)

    @FlyStopProc.putter
    async def _on_fly_stop(self, _instance, value):
        if not value:
            return
        self._stop_requested = True
        self._schedule_self_clear(self.FlyStopProc)

    def _schedule_self_clear(self, pv) -> None:
        """Schedule a reset-to-0 of a momentary-action PV after a brief delay.

        Cancels any pending self-clear for the same PV so rapid retriggers
        don't have the first clear fire mid-window of the second trigger.
        Scheduled (not awaited) so caproto's outer write first stores the
        client-written value — that gives DBE_VALUE monitors the 0→1 edge
        before the async task writes 0 and produces the 1→0 edge.
        """
        key = id(pv)
        prev = self._self_clear_tasks.get(key)
        if prev is not None and not prev.done():
            prev.cancel()
        task = asyncio.create_task(self._self_clear_after(pv))
        task.add_done_callback(self._on_self_clear_done)
        self._self_clear_tasks[key] = task

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

    async def _run_fly(self, start: float, stop: float, velo: float):
        # start/stop/velo are pre-validated by _on_fly_start. The defensive
        # assert catches any future direct caller (e.g. a test) that
        # bypasses the putter — fails loud per no-silent-fallbacks.
        assert velo > 0, f"_run_fly requires velo>0, got {velo!r}"
        direction = 1.0 if stop >= start else -1.0
        try:
            # Entry writes inside try so a failure on any of them still
            # triggers the finally cleanup — Scan-Sts/Move-Sts can never
            # be left stuck "Scanning" / "Moving" by an entry-time error.
            await self.Enrgy_I.write(start)
            await self.Scan_Sts.write("Scanning")
            await self.Move_Sts.write("Moving")
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
            # Best-effort writes. Ordered so that the most state-corrupting
            # failure is least likely: pin SP first (so the slew loop sees
            # consistent state on its next tick), then Move-Sts (so motion
            # state is unambiguous), then Scan-Sts (cosmetic last).
            for pv, val in (
                (self.Enrgy_SP, self.Enrgy_I.value),
                (self.Move_Sts, "Done"),
                (self.Scan_Sts, "Idle"),
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
