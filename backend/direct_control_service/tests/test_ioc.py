"""
caproto-based test IOC for direct-control integration tests.

Exposes a small set of PVs covering the shapes we care about:
- `IOC:m1`            scalar float with putter (for set_pv + subscribe tests)
- `IOC:counter`       scalar int (for envelope / bytesize tests)
- `IOC:wf1`           1-D waveform (for array envelope + binary mode tests)
- `IOC:shutter`       enum (for `as_string=true` + enum_strs tests)

Adapted from ophyd-websocket/src/tests/test_ioc.py (BSD-3-Clause).

Run directly for a standalone IOC:
    python -m tests.test_ioc --list-pvs
"""

from caproto import ChannelType
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run


class DirectControlTestIOC(PVGroup):
    """Small IOC with the PV shapes the test suite exercises."""

    m1 = pvproperty(value=0.0, dtype=float, doc="Scalar float setpoint")
    counter = pvproperty(value=0, dtype=int, doc="Scalar int counter")
    wf1 = pvproperty(
        value=[float(i) for i in range(20)],
        max_length=20,
        doc="1-D waveform, 20 elements",
    )
    shutter = pvproperty(
        value="Closed",
        enum_strings=["Closed", "Open", "Moving"],
        record="mbbi",
        dtype=ChannelType.ENUM,
        doc="Enum PV with three states",
    )

    @m1.putter
    async def m1(self, instance, value):
        return value

    @counter.putter
    async def counter(self, instance, value):
        return value


if __name__ == "__main__":
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="IOC:",
        desc="Test IOC for bluesky-direct-control-service tests",
    )
    ioc = DirectControlTestIOC(**ioc_options)
    run(ioc.pvdb, **run_options)
