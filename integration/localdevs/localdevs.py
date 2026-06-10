"""
Small ophyd wrapper classes for the simulated IOCs in the pod.

Cannibalized from bluesky-pods' localdevs.py (which had itself removed its
dependency on the `nslsii` package). Kept minimal — only classes any pod's
happi_db.json references. Add more when new IOCs join the pod.

Used by:
- configuration_service: HappiProfileLoader imports compound device classes
  named in the happi DB so its `_walk_class_for_pvs` helper can index each
  leaf-signal PV at registry-load time. Mount this directory into the
  configuration_service container and add it to PYTHONPATH so the walker
  can reach these classes. If the import fails, the loader logs a warning
  and falls back to prefix-only indexing for that entry.
- bluesky RunEngine / queueserver (future): imports and instantiates these
  classes from the happi database. Mount this directory into whichever
  container needs to construct the devices.
"""

import threading

from ophyd import Component as Cpt, Device, DeviceStatus, Signal
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


class RandomWalk(Device):
    """`caproto.ioc_examples.random_walk` shape: a stepping-time + walk PV."""
    dt = Cpt(EpicsSignal, "dt", kind="config")
    x = Cpt(EpicsSignal, "x", kind="hinted")


class ADSimDetector(Device):
    """`ioc_adsim.py` shape — a simulated AreaDetector under a `13SIM1:` prefix.

    Components mirror the PVs the direct-control camera-socket / tiff-socket
    read: the cam1 region/format settings plus the image1 array. Prefix is the
    detector base (e.g. ``13SIM1:``), so component suffixes carry the cam1:/
    image1: plugin segments.
    """
    min_x = Cpt(EpicsSignal, "cam1:MinX", kind="config")
    min_y = Cpt(EpicsSignal, "cam1:MinY", kind="config")
    size_x = Cpt(EpicsSignal, "cam1:SizeX", kind="config")
    size_y = Cpt(EpicsSignal, "cam1:SizeY", kind="config")
    color_mode = Cpt(EpicsSignal, "cam1:ColorMode", kind="config")
    data_type = Cpt(EpicsSignal, "cam1:DataType", kind="config")
    acquire = Cpt(EpicsSignal, "cam1:Acquire", kind="normal")
    acquire_time = Cpt(EpicsSignal, "cam1:AcquireTime", kind="config")
    array_counter = Cpt(EpicsSignalRO, "image1:ArrayCounter_RBV", kind="normal")
    image = Cpt(EpicsSignalRO, "image1:ArrayData", kind="hinted")


class SetInProgress(RuntimeError):
    """Raised when a Eurotherm `set` lands while another set is already in flight."""


class Eurotherm(Device):
    """
    Temperature controller wrapper that returns `done` only once the readback
    has stayed within `tolerance` of the setpoint for `equilibrium_time` seconds.

    Adapted from bluesky-pods/localdevs.py — itself adapted from nslsii to
    drop the nslsii dependency.
    """

    equilibrium_time = Cpt(Signal, value=5, kind="config")
    timeout = Cpt(Signal, value=500, kind="config")
    tolerance = Cpt(Signal, value=1, kind="config")

    setpoint = Cpt(EpicsSignal, "T-SP", kind="normal")
    readback = Cpt(EpicsSignal, "T-RB", kind="hinted")

    def __init__(self, pv_prefix, **kwargs):
        super().__init__(pv_prefix, **kwargs)
        self._set_lock = threading.Lock()
        self._cb_timer = None
        self._cid = None

    def set(self, value):
        if not self._set_lock.acquire(blocking=False):
            raise SetInProgress(
                f"attempting to set {self.name} while a set is in progress"
            )

        set_value = value
        status = DeviceStatus(self)
        initial_timestamp = None

        equilibrium_time = self.equilibrium_time.get()
        tolerance = self.tolerance.get()

        def timer_cleanup():
            print(f"Set of {self.name} timed out after {self.timeout.get()} s")
            self._set_lock.release()
            self.readback.clear_sub(status_indicator)
            status._finished(success=False)

        self._cb_timer = threading.Timer(self.timeout.get(), timer_cleanup)

        def status_indicator(value, timestamp, **kwargs):
            if not self._cb_timer.is_alive():
                self._cb_timer.start()

            nonlocal initial_timestamp
            if abs(value - set_value) < tolerance:
                if initial_timestamp:
                    if (timestamp - initial_timestamp) > equilibrium_time:
                        status._finished()
                        self._cb_timer.cancel()
                        self._set_lock.release()
                        self.readback.clear_sub(status_indicator)
                else:
                    initial_timestamp = timestamp
            else:
                initial_timestamp = None

        self.setpoint.put(set_value)
        self._cid = self.readback.subscribe(status_indicator)
        return status

    def stop(self, success=False):
        self._set_lock.release()
        self._cb_timer.cancel()
        self.readback.unsubscribe(self._cid)
        self.set(self.readback.get())


class Thermo(Eurotherm):
    """`caproto.ioc_examples.thermo_sim` shape — overrides PV names."""
    readback = Cpt(EpicsSignal, "I", kind="hinted")
    setpoint = Cpt(EpicsSignal, "SP", kind="normal")
    K = Cpt(EpicsSignal, "K", kind="config")
    omega = Cpt(EpicsSignal, "omega", kind="config")
    Tvar = Cpt(EpicsSignal, "Tvar", kind="config")
