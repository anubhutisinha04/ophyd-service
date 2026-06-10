# flake8: noqa
"""Test profile collection — device definitions.

Device-creation patterns mirror real profile collections:
- compound Device classes imported from a package (bmm-profile-collection
  style: ``from bmm_tools.devices.motors import ...``) — here ``localdevs``,
  which the pod mounts on PYTHONPATH in BOTH the queueserver worker and the
  configuration-service containers. Classes must be importable outside this
  script so the introspected ``device_class`` in each DeviceInstantiationSpec
  can be re-imported by config-service (PV walking) and by consume-mode
  workers (re-instantiation from the registry).
- direct EpicsMotor / EpicsSignal instantiation (pdf-profile-collection
  11-motors.py style), pointed at the pod's simulated IOCs.

PV sources:
- caproto mini_beamline (``mini:*``) — detectors and signal-style motors
- caproto fake_motor_record (``sim:mtr1/2``) — full motor records
"""
print(f"Loading file {__file__!r}")

from localdevs import Det, Spot
from ophyd import EpicsMotor
from ophyd.signal import EpicsSignal, EpicsSignalRO

# Detectors (compound devices)
det_spot = Spot("mini:dot", name="det_spot")
det_pinhole = Det("mini:ph", name="det_pinhole")
det_edge = Det("mini:edge", name="det_edge")

# Sample stage (full motor records, fake_motor_record IOC)
sample_x = EpicsMotor("sim:mtr1", name="sample_x", labels=["positioners"])
sample_y = EpicsMotor("sim:mtr2", name="sample_y", labels=["positioners"])

# Beamline axes exposed as bare setpoint PVs (mini_beamline IOC)
ph_motor = EpicsSignal("mini:ph:mtr", name="ph_motor", labels=["positioners"])
edge_motor = EpicsSignal("mini:edge:mtr", name="edge_motor", labels=["positioners"])

# Machine status
ring_current = EpicsSignalRO("mini:current", name="ring_current")
