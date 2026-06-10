"""Importable device classes for device-control tests.

Referenced by fully-qualified path (``tests.device_classes.X``) from
instantiation specs, exactly the way a beamline profile class would be.
Covers one classic-ophyd and one ophyd-async compound device, both shaped
around the caproto test IOC's PVs (see ``test_ioc.py``), plus CA-free soft
variants for unit tests.
"""

from __future__ import annotations

from ophyd import Component as Cpt
from ophyd import Device as ClassicDevice
from ophyd import EpicsSignal
from ophyd_async.core import StandardReadable, soft_signal_rw
from ophyd_async.epics.core import epics_signal_rw


class ClassicPair(ClassicDevice):
    """Classic-ophyd compound device over the test IOC (pyepics transport)."""

    m1 = Cpt(EpicsSignal, "m1")
    counter = Cpt(EpicsSignal, "counter")


class AsyncPair(StandardReadable):
    """ophyd-async compound device over the test IOC (aioca transport).

    StandardReadable (not bare Device) so the device-level ``read()`` /
    ``describe()`` verbs exist — a bare ophyd-async Device doesn't
    implement the Readable protocol.
    """

    def __init__(self, prefix: str, name: str = "") -> None:
        with self.add_children_as_readables():
            self.m1 = epics_signal_rw(float, f"{prefix}m1")
            self.counter = epics_signal_rw(int, f"{prefix}counter")
        super().__init__(name=name)


class SoftAsyncThing(StandardReadable):
    """ophyd-async device made of soft signals — connects without any IOC."""

    def __init__(self, name: str = "") -> None:
        with self.add_children_as_readables():
            self.value = soft_signal_rw(float, initial_value=0.0)
        super().__init__(name=name)


class NotADevice:
    """Neither framework — detect_framework must refuse it."""
