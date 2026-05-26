"""Simulated synApps scaler IOC for the IOS demo.

Serves the PVs at prefix ``XF:23ID2-ES{Sclr:1}.*`` referenced by the IOS
happi entry ``ios_devs.DodgyEpicsScaler``.

Dynamics:
    * Writing ``.CNT`` = 1 starts a preset-time count: the IOC clears
      ``.S1``..``.S4`` and ``.T``, then increments them at simulated rates
      every TICK_S seconds until ``.T`` reaches ``.TP``. ``.CNT`` then
      auto-clears to 0 (busy/done semantics).
    * Writing ``.CNT`` = 0 mid-count aborts; ``.S{N}`` retain their current
      values, ``.T`` retains current elapsed.

Subset: 4 channels (.S1-.S4 + .NM1-.NM4) — synApps supports 32 but the
IOS preset only touches the first few. Rates per channel are deterministic
(no noise) for stable tests.

Phase 3 of the IOS use case (dynamic IOCs).
"""

import asyncio
import logging

from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


log = logging.getLogger(__name__)


# Simulated clock frequency (Hz). Real synApps scalers often run at 10 MHz.
CLOCK_FREQ_HZ = 1.0e7

# Background channel count rates (cts/sec). Index 0 = .S1, etc.
# .S1 is the clock channel (ticks at CLOCK_FREQ_HZ); .S2..S4 are detectors.
_CHANNEL_RATES = (
    CLOCK_FREQ_HZ,  # S1: clock
    50_000.0,       # S2: I0 monitor
    5_000.0,        # S3: PD
    1_000.0,        # S4: aumesh
)

N_CHANNELS = len(_CHANNEL_RATES)

# Tick period for the counting loop (seconds).
TICK_S = 0.05


class ScalerIOC(PVGroup):
    """synApps scaler at XF:23ID2-ES{Sclr:1}.*"""

    # ─── Acquisition control ─────────────────────────────────────────────
    # caproto stores integers for our purposes. Real scaler uses bo/bi
    # records; the IOS exerciser writes 1/0 and reads the value back.
    CNT = pvproperty(value=0, name=".CNT")
    # NOTE: .CONT (continuous-vs-one-shot) intentionally NOT served —
    # the IOC implements one-shot only. Declaring it would silently
    # accept caput .CONT=1 while doing nothing (a 'I'm fine' silent
    # failure). Per no-silent-fallbacks: any client that needs auto-
    # count sees a clean 'no such PV' instead of a phantom success.
    TP = pvproperty(value=1.0, name=".TP", precision=4, units="s")
    T = pvproperty(
        value=0.0, name=".T", read_only=True, precision=4, units="s"
    )
    FREQ = pvproperty(
        value=CLOCK_FREQ_HZ,
        name=".FREQ",
        read_only=True,
        precision=2,
        units="Hz",
    )

    # ─── Channel counts (S1-S4) and names (NM1-NM4) ──────────────────────
    # S1..S4 are float (caproto DOUBLE) — int32 (LONG) would overflow at
    # CLOCK_FREQ_HZ × TP > 2^31, i.e. TP > 215 s on the clock channel.
    # DOUBLE handles up to ~2^53 cleanly which covers any realistic TP.
    S1 = pvproperty(value=0.0, name=".S1", read_only=True, precision=1)
    S2 = pvproperty(value=0.0, name=".S2", read_only=True, precision=1)
    S3 = pvproperty(value=0.0, name=".S3", read_only=True, precision=1)
    S4 = pvproperty(value=0.0, name=".S4", read_only=True, precision=1)
    NM1 = pvproperty(value="clock", name=".NM1", report_as_string=True, max_length=40)
    NM2 = pvproperty(value="I0", name=".NM2", report_as_string=True, max_length=40)
    NM3 = pvproperty(value="PD", name=".NM3", report_as_string=True, max_length=40)
    NM4 = pvproperty(value="aumesh", name=".NM4", report_as_string=True, max_length=40)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._count_task: asyncio.Task | None = None
        # Sentinel: set by _run_count's finally before its CNT=0
        # auto-clear write so the recursive putter invocation knows the
        # task is already exiting and skips trying to cancel itself.
        # Avoids the self-cancel-from-own-finally fragility.
        self._auto_clearing = False

    @CNT.putter
    async def _on_cnt_write(self, _instance, value):
        if value:
            # Start a new count. If a previous count is in flight, cancel
            # it cleanly first.
            if self._count_task and not self._count_task.done():
                self._count_task.cancel()
                try:
                    await self._count_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Surface any real exception that escaped the prior
                    # task's done-callback — losing it silently would
                    # hide bugs in _run_count under spam-CNT-1 patterns.
                    log.exception("prior scaler count task raised on cancel-await")
            self._count_task = asyncio.create_task(self._run_count())
            self._count_task.add_done_callback(self._on_count_done)
        else:
            # CNT=0. Distinguish external abort (cancel the task) from
            # the task's own finally auto-clearing (no-op — already exiting).
            if self._auto_clearing:
                return
            if self._count_task and not self._count_task.done():
                self._count_task.cancel()

    def _on_count_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("scaler count task crashed: %s", exc, exc_info=exc)

    async def _run_count(self):
        chan_pvs = (self.S1, self.S2, self.S3, self.S4)
        try:
            # Validation + zero-writes inside the try so a failure here
            # still triggers the finally's CNT auto-clear. Otherwise
            # CNT would stay at 1 forever and the IOC would silently
            # look "busy" with no signal that the count never started.
            tp = float(self.TP.value)
            if tp <= 0:
                raise ValueError(f"TP must be > 0; got {tp}")
            await self.T.write(0.0)
            for pv in chan_pvs:
                await pv.write(0)
            elapsed = 0.0
            while elapsed < tp:
                # Sleep first, then update — keeps the asyncio cancellation
                # point at the start of each iteration.
                tick = min(TICK_S, tp - elapsed)
                await asyncio.sleep(tick)
                elapsed += tick
                await self.T.write(elapsed)
                for pv, rate in zip(chan_pvs, _CHANNEL_RATES):
                    await pv.write(float(rate * elapsed))
            # Final exact-value pin in case rounding drifted.
            await self.T.write(tp)
            for pv, rate in zip(chan_pvs, _CHANNEL_RATES):
                await pv.write(float(rate * tp))
        finally:
            # Auto-clear CNT regardless of how we exited. The sentinel
            # prevents the recursive CNT=0 putter call from trying to
            # cancel us (we're already in our own finally). Note: this
            # WILL momentarily fire a 1→0→1 monitor sequence if cancel
            # races a new CNT=1 — acceptable for the smoke test.
            self._auto_clearing = True
            try:
                await self.CNT.write(0)
            except Exception:
                log.exception("scaler CNT auto-clear failed")
            finally:
                self._auto_clearing = False


def main():
    # Prefix has matched braces; escape both. After expansion:
    # XF:23ID2-ES{Sclr:1}. Per-PV names start with '.' (record fields).
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="XF:23ID2-ES{{Sclr:1}}",
        desc="IOS synApps scaler simulation IOC (.CNT/.TP/.S1..S4 preset-time count).",
    )
    ioc = ScalerIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
