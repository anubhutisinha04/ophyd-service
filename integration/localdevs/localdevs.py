"""
Small ophyd wrapper classes for the simulated IOCs in the pod.

Cannibalized from bluesky-pods' localdevs.py (which had itself removed its
dependency on the `nslsii` package). Kept minimal: only the classes our
happi_db.json references. Add more when Phase 2/3 IOCs join the pod.

Used by:
- configuration_service: inert — HappiProfileLoader does pure JSON parsing,
  never imports this file. But it's worth shipping here so other in-pod
  consumers can reach it.
- bluesky RunEngine / queueserver (Phase 4): imports and instantiates these
  classes from the happi database. Mount this directory into whichever
  container needs to construct the devices.
"""

from ophyd import Device, Component as Cpt
from ophyd.signal import EpicsSignal, EpicsSignalRO


class Det(Device):
    """Compound detector: just the `det` + `exp` PVs under a prefix."""
    det = Cpt(EpicsSignal, ":det", kind="hinted")
    exp = Cpt(EpicsSignal, ":exp", kind="config")


class Spot(Device):
    """Compound detector with a 2D image sum, exposure, and shutter."""
    img = Cpt(EpicsSignal, ":det")
    roi = Cpt(EpicsSignal, ":img_sum", kind="hinted")
    exp = Cpt(EpicsSignal, ":exp", kind="config")
    shutter_open = Cpt(EpicsSignal, ":shutter_open", kind="config")
    array_size = Cpt(EpicsSignalRO, ":ArraySize_RBV", kind="config")

    def trigger(self):
        return self.img.trigger()
