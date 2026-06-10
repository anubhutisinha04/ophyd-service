"""
caproto-based test IOC for direct-control integration tests.

Exposes a small set of PVs covering the shapes we care about:
- `IOC:m1`            scalar float with putter (for set_pv + subscribe tests)
- `IOC:counter`       scalar int (for envelope / bytesize tests)
- `IOC:wf1`           1-D waveform (for array envelope + binary mode tests)
- `IOC:shutter`       enum (for `as_string=true` + enum_strs tests)
- `IOC:cam1:*` + `IOC:image1:ArrayData`  AreaDetector-shaped PVs (camera/tiff sockets)

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

    # AreaDetector-shaped PVs for the camera-socket / tiff-socket tests.
    # A 4x4 Mono/UInt8 frame: dims = (SizeX-MinX, SizeY-MinY) = 4x4 = 16 elems.
    # DataType index 1 == "UInt8", ColorMode index 0 == "Mono"
    # (see image_stream_manager.DATA_TYPE_ENUM / COLOR_MODE_ENUM).
    cam1_MinX = pvproperty(value=0, dtype=int, name="cam1:MinX", doc="ROI min X")
    cam1_MinY = pvproperty(value=0, dtype=int, name="cam1:MinY", doc="ROI min Y")
    cam1_SizeX = pvproperty(value=4, dtype=int, name="cam1:SizeX", doc="Frame width")
    cam1_SizeY = pvproperty(value=4, dtype=int, name="cam1:SizeY", doc="Frame height")
    cam1_ColorMode = pvproperty(value=0, dtype=int, name="cam1:ColorMode", doc="Color mode index")
    cam1_DataType = pvproperty(value=1, dtype=int, name="cam1:DataType", doc="Data type index")
    image1_ArrayData = pvproperty(
        value=list(range(16)),
        max_length=4096,
        name="image1:ArrayData",
        doc="Flattened image array",
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
