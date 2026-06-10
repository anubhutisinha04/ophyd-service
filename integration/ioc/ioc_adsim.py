"""
caproto-simulated AreaDetector ("ADSim") IOC for the full integration pod.

Serves the minimal AreaDetector PV surface the direct-control camera-socket /
tiff-socket need, under the default `13SIM1:` prefix the sockets fall back to:

    13SIM1:cam1:MinX / MinY / SizeX / SizeY    region + frame size
    13SIM1:cam1:ColorMode (0=Mono) / DataType (1=UInt8)
    13SIM1:cam1:Acquire / AcquireTime          acquisition control
    13SIM1:image1:ArrayData                    flattened frame (live)
    13SIM1:image1:ArrayCounter_RBV             frame counter

`image1:ArrayData` updates ~5 Hz with a Gaussian blob orbiting the frame over a
faint diagonal gradient, so a connected camera-socket shows live motion. This
is a stand-in for a real EPICS areaDetector ADSimDetector IOC (which can't be
built in this sandbox); the PV names + shapes match so the sockets behave
identically against either.

Run directly (default prefix 13SIM1:):
    python ioc_adsim.py --list-pvs --interfaces 0.0.0.0
"""

import numpy as np
from caproto.server import PVGroup, ioc_arg_parser, pvproperty, run

# Frame geometry. Non-square on purpose so x/y handling is exercised.
WIDTH = 256
HEIGHT = 192
_N = WIDTH * HEIGHT

# ColorMode / DataType are integer indices into the AreaDetector enums the
# camera-socket mirrors (COLOR_MODE_ENUM / DATA_TYPE_ENUM): 0=Mono, 1=UInt8.
_COLOR_MODE_MONO = 0
_DATA_TYPE_UINT8 = 1


class ADSimDetectorIOC(PVGroup):
    """Minimal AreaDetector simulator: camera settings + a live image array."""

    # --- cam1: settings the camera-socket reads to size/interpret the frame ---
    min_x = pvproperty(value=0, name="cam1:MinX", doc="ROI origin X")
    min_y = pvproperty(value=0, name="cam1:MinY", doc="ROI origin Y")
    size_x = pvproperty(value=WIDTH, name="cam1:SizeX", doc="Frame width (px)")
    size_y = pvproperty(value=HEIGHT, name="cam1:SizeY", doc="Frame height (px)")
    color_mode = pvproperty(value=_COLOR_MODE_MONO, name="cam1:ColorMode", doc="0=Mono")
    data_type = pvproperty(value=_DATA_TYPE_UINT8, name="cam1:DataType", doc="1=UInt8")

    # --- acquisition control (Acquire gates frame generation) ---
    acquire = pvproperty(value=1, name="cam1:Acquire", doc="1=acquiring, 0=stopped")
    acquire_time = pvproperty(value=0.2, name="cam1:AcquireTime", doc="Exposure (s)")

    # --- image plugin output ---
    array_counter = pvproperty(value=0, name="image1:ArrayCounter_RBV", doc="Frame count")
    image = pvproperty(
        value=[0] * _N,
        max_length=_N,
        name="image1:ArrayData",
        doc="Flattened Mono/UInt8 frame, row-major (height rows of width)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._frame = 0
        # Precompute the pixel grid once; only the blob center moves per frame.
        self._yy, self._xx = np.mgrid[0:HEIGHT, 0:WIDTH]
        self._gradient = (self._xx + self._yy) / (WIDTH + HEIGHT)  # 0..1 diagonal

    def _render(self) -> list:
        """Build the next frame: an orbiting Gaussian blob over a gradient."""
        self._frame += 1
        phase = self._frame * 0.15
        cx = WIDTH / 2 + (WIDTH / 3) * np.cos(phase)
        cy = HEIGHT / 2 + (HEIGHT / 3) * np.sin(phase)
        sigma = WIDTH / 10
        blob = np.exp(-(((self._xx - cx) ** 2 + (self._yy - cy) ** 2) / (2 * sigma**2)))
        frame = 0.25 * self._gradient + 0.75 * blob  # 0..1
        return (frame * 255).astype(np.uint8).ravel().astype(int).tolist()

    @image.scan(period=0.2)
    async def image(self, instance, async_lib):
        # Honor Acquire: when stopped, hold the last frame.
        if not self.acquire.value:
            return
        await instance.write(self._render())
        await self.array_counter.write(self._frame)


if __name__ == "__main__":
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="13SIM1:",
        desc="Simulated AreaDetector (Mono/UInt8 moving blob) for the integration pod",
    )
    ioc = ADSimDetectorIOC(**ioc_options)
    run(ioc.pvdb, **run_options)
