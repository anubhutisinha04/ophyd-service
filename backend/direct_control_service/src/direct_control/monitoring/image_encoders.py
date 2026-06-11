"""
Pluggable wire-format encoders for the image-streaming sockets.

The camera-socket / tiff-socket pipeline produces a display-ready PIL image
(normalize -> reshape -> downsample); this module turns that image into the
bytes that go on the wire. The format is swappable via the ``ImageEncoder``
protocol so the manager doesn't hard-code JPEG.

Consumer constraint: finch decodes frames with
``createImageBitmap(new Blob([data], {type:'image/jpeg'}))``. Browsers can
``createImageBitmap`` JPEG/PNG/WebP, so those are realistic encoders — but
they cannot decode TIFF, which is why there is deliberately no TIFF encoder
here ("TIFF" in tiff-socket names the detector class, not the wire format).
JPEG stays the default until the frontend negotiates a wider media type.
"""

from __future__ import annotations

import io
from typing import Protocol, runtime_checkable

from PIL import Image


@runtime_checkable
class ImageEncoder(Protocol):
    """Serializes a display-ready PIL image to wire bytes."""

    #: MIME type for the produced bytes (e.g. ``"image/jpeg"``).
    media_type: str

    def encode(self, image: Image.Image) -> bytes:
        """Return the encoded frame bytes for ``image``."""
        ...


class _PILEncoder:
    """Base encoder that serializes via Pillow's ``Image.save``."""

    media_type: str
    _pil_format: str
    _save_kwargs: dict

    def __init__(self, media_type: str, pil_format: str, **save_kwargs: object) -> None:
        self.media_type = media_type
        self._pil_format = pil_format
        self._save_kwargs = save_kwargs

    def encode(self, image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format=self._pil_format, **self._save_kwargs)
        return buffer.getvalue()


class JpegEncoder(_PILEncoder):
    """Lossy JPEG — the finch default."""

    def __init__(self, quality: int = 100) -> None:
        super().__init__("image/jpeg", "JPEG", quality=quality)


class PngEncoder(_PILEncoder):
    """Lossless PNG — larger frames; usable where finch widens the blob type."""

    def __init__(self) -> None:
        super().__init__("image/png", "PNG")


class WebpEncoder(_PILEncoder):
    """WebP — better quality-per-byte than JPEG for live view."""

    def __init__(self, quality: int = 90) -> None:
        super().__init__("image/webp", "WEBP", quality=quality)


def make_encoder(name: str, *, jpeg_quality: int = 100) -> ImageEncoder:
    """Map a config string (``DIRECT_CONTROL_IMAGE_ENCODING``) to an encoder.

    Fails hard on an unknown name rather than silently falling back to JPEG —
    a typo in config should surface at startup, not ship the wrong format.
    """
    key = name.strip().lower()
    if key in ("jpeg", "jpg"):
        return JpegEncoder(quality=jpeg_quality)
    if key == "png":
        return PngEncoder()
    if key == "webp":
        return WebpEncoder()
    raise ValueError(
        f"Unsupported DIRECT_CONTROL_IMAGE_ENCODING={name!r}. "
        "Supported: jpeg, png, webp (TIFF is not browser-decodable)."
    )
