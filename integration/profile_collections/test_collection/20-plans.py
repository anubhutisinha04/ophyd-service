# flake8: noqa
"""Test profile collection — plans.

Standard bluesky plans plus one custom plan with a per-point dwell, used by
the integration exerciser to hold a plan in the EXECUTING state long enough
to probe device locks from direct-control mid-plan.
"""
print(f"Loading file {__file__!r}")

from bluesky.plan_stubs import mv, sleep as plan_sleep, trigger_and_read
from bluesky.plans import count, grid_scan, list_scan, rel_scan, scan
from bluesky.preprocessors import run_decorator, stage_decorator


def dwell_scan(detectors, motor, start, stop, num, dwell=0.5, md=None):
    """1D step scan with a fixed dwell at each point.

    ``num * dwell`` gives a predictable minimum runtime, which the
    integration tests use as a window for mid-plan assertions.
    """
    step_size = (stop - start) / max(num - 1, 1)

    @stage_decorator(list(detectors) + [motor])
    @run_decorator(md=md or {})
    def inner():
        for i in range(num):
            yield from mv(motor, start + i * step_size)
            yield from plan_sleep(dwell)
            yield from trigger_and_read(list(detectors) + [motor])

    return (yield from inner())
