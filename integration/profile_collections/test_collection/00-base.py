# flake8: noqa
"""Test profile collection — RunEngine setup.

Modeled on real beamline profile collections (pdf-/bmm-profile-collection
startup/00-base.py) but stripped to what runs in the integration pod: no
nslsii, no kafka/tiled/redis document publishing, no facility paths. The
queueserver worker executes these files in alphabetical order.
"""
print(f"Loading file {__file__!r}")

from bluesky import RunEngine
from bluesky.callbacks.best_effort import BestEffortCallback
from ophyd.signal import EpicsSignalBase

# Simulated IOCs answer quickly; fail fast instead of hanging env-open when
# a PV is missing or the IOC container is down.
EpicsSignalBase.set_defaults(timeout=10, connection_timeout=10, write_timeout=10)

RE = RunEngine({})

bec = BestEffortCallback()
bec.disable_plots()  # headless container
RE.subscribe(bec)
