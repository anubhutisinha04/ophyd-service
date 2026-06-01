"""
Contract tests for the image-streaming sockets (camera-socket / tiff-socket).

Mirrors finch's consumer contract (``finch/src/components/Camera/hooks/
useCameraCanvas.ts`` + ``useTIFFCanvas.ts``): on connect the client sends a
subscribe message, then receives JSON ``{x, y}`` dimension messages and binary
JPEG frames, and may send ``{toggleLogNormalization}`` to get back a
``{logNormalization}`` state message.

Driven against the caproto test IOC (``tests/conftest.py::test_ioc``), which
serves a 4x4 Mono/UInt8 frame at ``IOC:image1:ArrayData`` + ``IOC:cam1:*``.
"""

import io
import json

import pytest
from PIL import Image

from direct_control.monitoring._envelopes import LockedWS, send_bytes_or_size_error
from direct_control.monitoring.image_encoders import (
    JpegEncoder,
    PngEncoder,
    WebpEncoder,
    make_encoder,
)

JPEG_SOI = b"\xff\xd8\xff"  # JPEG start-of-image marker

# Camera subscribe pointing the manager at the test IOC's AreaDetector PVs.
# Prefix-inference expands the bare image PV to IOC:cam1:* settings.
_CAMERA_SUBSCRIBE = {"imageArray_PV": "IOC:image1:ArrayData"}
# tiff subscribe is just a prefix; expands to IOC:image1:ArrayData + IOC:cam1:*.
_TIFF_SUBSCRIBE = {"prefix": "IOC"}


def _recv_first_bytes(ws, *, max_msgs=50):
    """Return the first binary frame, skipping interleaved JSON messages."""
    for _ in range(max_msgs):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            return msg["bytes"]
    raise AssertionError("no binary frame received")


def _recv_json_where(ws, predicate, *, max_msgs=50):
    """Return the first JSON (text) message matching ``predicate``."""
    for _ in range(max_msgs):
        msg = ws.receive()
        text = msg.get("text")
        if text is None:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if predicate(data):
            return data
    raise AssertionError("expected json message not received")


# --------------------------------------------------------------------------- #
# Encoder unit tests (no IOC needed)
# --------------------------------------------------------------------------- #
def test_make_encoder_selects_format():
    assert isinstance(make_encoder("jpeg"), JpegEncoder)
    assert isinstance(make_encoder("JPG", jpeg_quality=80), JpegEncoder)
    assert isinstance(make_encoder("png"), PngEncoder)
    assert isinstance(make_encoder("webp"), WebpEncoder)


def test_make_encoder_rejects_unknown():
    """Fail hard on a bad config string rather than silently defaulting to JPEG."""
    with pytest.raises(ValueError, match="Unsupported"):
        make_encoder("tiff")


def test_jpeg_encoder_produces_decodable_jpeg():
    image = Image.new("L", (4, 4), color=128)
    data = JpegEncoder().encode(image)
    assert data.startswith(JPEG_SOI)
    assert Image.open(io.BytesIO(data)).size == (4, 4)


# --------------------------------------------------------------------------- #
# Resilient frame send (a slow client / oversize frame must not kill the stream)
# --------------------------------------------------------------------------- #
class _FakeWS:
    """Minimal LockedWS target capturing text/binary sends."""

    def __init__(self):
        self.texts: list[str] = []
        self.binaries: list[bytes] = []

    async def send_text(self, data: str) -> None:
        self.texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binaries.append(data)


async def test_send_bytes_or_size_error_oversize_emits_envelope_not_raise():
    """An oversize frame yields a structured error envelope, never a raise
    (which would tear down the stream)."""
    fake = _FakeWS()
    # Cap large enough for the (small) error envelope, small enough to reject
    # the frame — so we exercise the oversize-frame path, not envelope rejection.
    ws = LockedWS(fake, max_message_bytes=500)
    await send_bytes_or_size_error(
        ws,
        b"x" * 10_000,
        log_event="t",
        log_fields={},
        oversize_message="frame too big",
        error_envelope_fields={},
    )
    assert fake.binaries == []  # oversize frame was dropped, not sent
    assert len(fake.texts) == 1
    env = json.loads(fake.texts[0])
    assert env["type"] == "error" and env["error"] == "frame too big"


async def test_send_bytes_or_size_error_sends_frame_under_cap():
    fake = _FakeWS()
    ws = LockedWS(fake, max_message_bytes=1000)
    await send_bytes_or_size_error(
        ws,
        b"abc",
        log_event="t",
        log_fields={},
        oversize_message="frame too big",
        error_envelope_fields={},
    )
    assert fake.binaries == [b"abc"]
    assert fake.texts == []


# --------------------------------------------------------------------------- #
# camera-socket
# --------------------------------------------------------------------------- #
def test_camera_socket_sends_dimensions_then_jpeg_frame(client, test_ioc):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json(_CAMERA_SUBSCRIBE)

        dims = _recv_json_where(ws, lambda d: "x" in d and "y" in d)
        assert dims["x"] == 4 and dims["y"] == 4
        assert dims["colorMode"] == "Mono"
        assert dims["dataType"] == "UInt8"

        frame = _recv_first_bytes(ws)
        assert frame.startswith(JPEG_SOI), "frame must be JPEG (finch decodes image/jpeg)"
        # Frame must be a real decodable image of the advertised size.
        assert Image.open(io.BytesIO(frame)).size == (4, 4)


def test_camera_socket_toggle_log_normalization(client, test_ioc):
    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json(_CAMERA_SUBSCRIBE)
        # Drain the priming dims so the toggle reply is unambiguous.
        _recv_json_where(ws, lambda d: "x" in d and "y" in d)

        ws.send_json({"toggleLogNormalization": False})
        reply = _recv_json_where(ws, lambda d: "logNormalization" in d)
        assert reply["logNormalization"] is False


def test_camera_socket_bad_pv_emits_error(client, test_ioc):
    """An unresolvable image PV yields a structured error envelope, not silence."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/api/v1/camera-socket") as ws:
        ws.send_json({"imageArray_PV": "NOPE:image1:ArrayData"})
        try:
            err = _recv_json_where(ws, lambda d: d.get("type") == "error")
            assert "error" in err
        except WebSocketDisconnect:
            # Acceptable: finch's hook surfaces the failure via socket close.
            pass


# --------------------------------------------------------------------------- #
# tiff-socket (camera-with-prefix-inference)
# --------------------------------------------------------------------------- #
def test_tiff_socket_prefix_resolves_and_streams(client, test_ioc):
    with client.websocket_connect("/api/v1/tiff-socket") as ws:
        ws.send_json(_TIFF_SUBSCRIBE)

        dims = _recv_json_where(ws, lambda d: "x" in d and "y" in d)
        assert dims["x"] == 4 and dims["y"] == 4

        frame = _recv_first_bytes(ws)
        assert frame.startswith(JPEG_SOI)
        assert Image.open(io.BytesIO(frame)).size == (4, 4)
