"""
WebSocket manager for the image-streaming sockets (``camera-socket`` /
``tiff-socket``), finch's ``ophydSocketCameraPath`` / ``ophydSocketTIFFPath``.

Ported from ``bluesky/ophyd-websocket``'s ``camera_socket.py`` (BSD-3-Clause)
and adapted to direct-control conventions: the size-cap-aware ``LockedWS``
send path, structured error envelopes, off-loop EPICS teardown, and a
``close_all`` for graceful shutdown.

Design notes
------------
* **Raw-numpy path, not ``PVMonitorManager``.** The image array is a large
  NDArray waveform. ``PVMonitorManager`` converts every value to a JSON list
  (``_convert_value``), which is exactly wrong for a megapixel frame — we need
  the raw numpy array to JPEG-encode it. So this manager owns its own
  per-connection ``EpicsSignalRO`` subscriptions and never touches the shared
  monitor.
* **Per-connection signals.** Image signals are big and client-specific; there
  is one stream per socket, so signals are created/destroyed per connection
  rather than shared+ref-counted like the pv/device sockets.
* **No app-level heartbeat.** finch's camera/tiff hooks treat any non-
  ``logNormalization`` JSON text frame as a ``{x, y}`` dimensions message when
  the canvas is in ``automatic`` mode. A generic ``{"type":"heartbeat"}`` frame
  would corrupt the canvas size, so image sockets deliberately omit the
  heartbeat the pv/device sockets use; continuous frame traffic keeps the
  connection warm.
* **Read-only.** Streams are pure ``EpicsSignalRO`` monitors — no writes, so no
  A4 / DeviceControl / lock check (those gate writes).

Wire contract (server -> client): binary frames (JPEG by default), JSON
``{"x":int,"y":int,...}`` dimension messages, JSON ``{"logNormalization":bool}``.
Client -> server: a subscribe message, then optional
``{"toggleLogNormalization":bool}``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import numpy as np
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from ophyd import EpicsSignalRO
from PIL import Image

from ..config import Settings
from ..protocols import RegistryProvider
from ..registry_client import RegistryValidationError
from ._envelopes import (
    LockedWS,
    close_connections,
    send_bytes_or_size_error,
    send_error,
    send_payload_or_size_error,
)
from .image_encoders import ImageEncoder, make_encoder

logger = structlog.get_logger(__name__)


# EPICS AreaDetector DataType enum index -> numpy dtype.
DTYPE_MAP = {
    "Int8": np.int8,
    "UInt8": np.uint8,
    "Int16": np.int16,
    "UInt16": np.uint16,
    "Int32": np.int32,
    "UInt32": np.uint32,
    "Int64": np.int64,
    "UInt64": np.uint64,
    "Float32": np.float32,
    "Float64": np.float64,
}
# Fallbacks when a settings PV is a plain int rather than an EPICS ENUM (so it
# carries no ``enum_strs``). Index == the integer PV value.
COLOR_MODE_ENUM = ["Mono", "RGB1", "RGB2", "RGB3"]
DATA_TYPE_ENUM = list(DTYPE_MAP.keys())

# Settings-component name -> AreaDetector ``cam1:`` suffix. Used both for
# camera prefix-inference and for tiff ``{prefix}`` expansion (tiff is just
# camera-with-prefix-inference).
SETTING_SUFFIX = {
    "startX": "MinX",
    "startY": "MinY",
    "sizeX": "SizeX",
    "sizeY": "SizeY",
    "colorMode": "ColorMode",
    "dataType": "DataType",
}

# Queue item kinds.
_FRAME = "frame"
_DIMS = "dims"


class _StreamState:
    """Per-connection mutable state shared between the receive + stream loops.

    Both loops run on the event loop thread, so no locking is needed between
    them; the CA callbacks only ever ``call_soon_threadsafe`` onto the loop.
    """

    def __init__(self, log_normalization: bool) -> None:
        self.log_normalization = log_normalization
        self.dimensions: Optional[dict] = None
        # Set on teardown so in-flight CA callbacks stop touching signals that
        # are about to be destroyed (avoids a spurious "Unexpected channel ID"
        # warning on every normal disconnect).
        self.closing = False


class ImageStreamManager:
    """Serves one image-streaming socket kind (``camera`` or ``tiff``)."""

    def __init__(
        self,
        settings: Settings,
        kind: str,
        registry_client: Optional[RegistryProvider] = None,
    ) -> None:
        if kind not in ("camera", "tiff"):
            raise ValueError(f"ImageStreamManager kind must be camera|tiff, got {kind!r}")
        self.settings = settings
        self.kind = kind
        self.registry_client = registry_client
        self._encoder: ImageEncoder = make_encoder(
            settings.image_encoding, jpeg_quality=settings.image_jpeg_quality
        )
        self._connections: dict[str, LockedWS] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    async def handle_client(self, websocket: WebSocket) -> None:
        ws = LockedWS(
            websocket,
            max_message_bytes=self.settings.response_bytesize_limit,
            send_timeout=self.settings.ws_send_timeout,
        )
        await ws.accept()
        client_id = str(uuid.uuid4())
        async with self._lock:
            self._connections[client_id] = ws
        logger.info("image_socket_connected", client_id=client_id, kind=self.kind)

        array_signal: Optional[EpicsSignalRO] = None
        setting_signals: dict[str, EpicsSignalRO] = {}
        try:
            # 1. First message resolves which PVs to stream.
            try:
                message = await ws.receive_json()
            except WebSocketDisconnect:
                return
            image_array_pv, setting_pvs = self._resolve_pvs(message)

            # 2. Registry gate — the image ARRAY PV must exist in the
            # authoritative registry, same gate as pv-socket/device-socket.
            # We validate only the array PV (the client-controlled data
            # firehose), NOT the cam1:* settings: AreaDetector devices
            # register the image-data PV but not each scalar setting as a
            # standalone registry PV (e.g. the seeded SimDetector has
            # image1:ArrayData but no cam1:SizeX/ColorMode/DataType), and
            # finch sends those settings itself. Validating them would
            # reject legitimate cameras. The settings ride on the same
            # validated detector prefix.
            if not await self._validate_pvs(ws, [image_array_pv]):
                return

            # 3. Connect signals (blocking CA work off-loop).
            try:
                array_signal = await asyncio.to_thread(
                    _connect_signal, image_array_pv, "array_signal"
                )
                for name, pv in setting_pvs.items():
                    setting_signals[name] = await asyncio.to_thread(_connect_signal, pv, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "image_socket_pv_connect_failed",
                    client_id=client_id,
                    kind=self.kind,
                    error=str(exc),
                )
                await send_error(ws, str(exc))
                return

            await self._stream(client_id, ws, array_signal, setting_signals)

        except WebSocketDisconnect:
            logger.info("image_socket_disconnect", client_id=client_id, kind=self.kind)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "image_socket_error",
                client_id=client_id,
                kind=self.kind,
                error=str(exc),
                exc_info=True,
            )
        finally:
            await self._teardown(client_id, ws, array_signal, setting_signals)

    async def _stream(
        self,
        client_id: str,
        ws: LockedWS,
        array_signal: EpicsSignalRO,
        setting_signals: dict[str, EpicsSignalRO],
    ) -> None:
        loop = asyncio.get_running_loop()
        state = _StreamState(self.settings.image_log_normalization_default)
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.settings.image_frame_queue_size)

        # CA-thread callbacks: both only schedule an enqueue onto the event
        # loop and do NO CA I/O themselves. The image array value rides in the
        # callback's own `value`; a settings change just enqueues a recompute
        # sentinel — the actual `_compute_dimensions` (which reads several PVs)
        # runs off both the CA thread and the event loop, via to_thread in the
        # stream loop. asyncio primitives aren't threadsafe, hence call_soon.
        def array_cb(value=None, timestamp=None, **kwargs) -> None:
            if state.closing:
                return
            loop.call_soon_threadsafe(_enqueue, queue, (_FRAME, value))

        def settings_cb(value=None, timestamp=None, **kwargs) -> None:
            if state.closing:
                return
            loop.call_soon_threadsafe(_enqueue, queue, (_DIMS, None))

        for signal in setting_signals.values():
            signal.subscribe(settings_cb)
        array_signal.subscribe(array_cb)

        # Prime: emit dimensions and the current frame so a client sees an
        # image immediately rather than waiting for the next detector update.
        # The _DIMS sentinel makes the stream loop compute dims (off-loop).
        try:
            _enqueue(queue, (_DIMS, None))
            initial_frame = await asyncio.to_thread(array_signal.get)
            _enqueue(queue, (_FRAME, initial_frame))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "image_prime_failed", client_id=client_id, kind=self.kind, error=str(exc)
            )

        recv_task = asyncio.create_task(self._receive_loop(ws, state))
        stream_task = asyncio.create_task(self._stream_loop(ws, queue, state, setting_signals))
        try:
            await asyncio.wait({recv_task, stream_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Quiesce CA callbacks before signals are destroyed in _teardown.
            state.closing = True
            for task in (recv_task, stream_task):
                if not task.done():
                    task.cancel()
            # Let the cancellations land before inspecting results — calling
            # task.exception() on a just-cancelled (still pending) task raises
            # InvalidStateError, which fired on every normal disconnect and
            # masked real loop crashes.
            await asyncio.gather(recv_task, stream_task, return_exceptions=True)
            # Surface a non-cancellation crash in either loop.
            for task in (recv_task, stream_task):
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    logger.error(
                        "image_socket_loop_failed",
                        client_id=client_id,
                        kind=self.kind,
                        error=str(exc),
                    )

    async def _receive_loop(self, ws: LockedWS, state: _StreamState) -> None:
        """Handle client control messages (``toggleLogNormalization``)."""
        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                return
            if isinstance(data, dict) and "toggleLogNormalization" in data:
                state.log_normalization = bool(data["toggleLogNormalization"])
                await send_payload_or_size_error(
                    ws,
                    {"logNormalization": state.log_normalization},
                    log_event="image_lognorm_ack",
                    log_fields={"kind": self.kind},
                    oversize_message="logNormalization ack exceeds size limit",
                    error_envelope_fields={},
                )

    async def _stream_loop(
        self,
        ws: LockedWS,
        queue: asyncio.Queue,
        state: _StreamState,
        setting_signals: dict[str, EpicsSignalRO],
    ) -> None:
        log_fields = {"kind": self.kind}
        while True:
            kind, payload = await queue.get()
            if kind == _DIMS:
                # Recompute sentinel: read the setting PVs off the event loop
                # (the CA callback only enqueued this). A teardown race makes
                # the read fail; that's expected, not a fault.
                try:
                    dims = await asyncio.to_thread(_compute_dimensions, setting_signals)
                except Exception as exc:  # noqa: BLE001
                    if not state.closing:
                        logger.warning("image_dims_compute_failed", kind=self.kind, error=str(exc))
                    continue
                state.dimensions = dims
                # finch reads only x/y; extra keys (colorMode/dataType) are
                # ignored client-side but needed server-side to reshape.
                # Resilient send: a slow client drops this dims message rather
                # than tearing down the stream.
                await send_payload_or_size_error(
                    ws,
                    dims,
                    log_event="image_dims_send",
                    log_fields=log_fields,
                    oversize_message="dimensions payload exceeds size limit",
                    error_envelope_fields={},
                )
                continue
            if state.dimensions is None:
                continue  # frame arrived before dimensions are known; skip
            frame = await asyncio.to_thread(
                self._render_frame, payload, state.dimensions, state.log_normalization
            )
            if frame is None:
                continue
            # Resilient send: TimeoutError drops this one frame (the
            # drop-oldest queue's whole point), oversize emits an error
            # envelope — neither kills the stream.
            await send_bytes_or_size_error(
                ws,
                frame,
                log_event="image_frame_send",
                log_fields=log_fields,
                oversize_message="image frame exceeds size limit; frame dropped",
                error_envelope_fields={},
            )

    def _render_frame(self, raw, dimensions: dict, log_normalization: bool) -> Optional[bytes]:
        """Normalize -> reshape -> downsample -> encode. Off-loop (CPU-bound)."""
        try:
            image = _build_display_image(
                raw,
                width=dimensions["x"],
                height=dimensions["y"],
                color_mode=dimensions["colorMode"],
                data_type=dimensions["dataType"],
                log_normalization=log_normalization,
                max_dimension=self.settings.image_max_dimension,
            )
            return self._encoder.encode(image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_frame_render_failed", kind=self.kind, error=str(exc))
            return None

    async def _teardown(
        self,
        client_id: str,
        ws: LockedWS,
        array_signal: Optional[EpicsSignalRO],
        setting_signals: dict[str, EpicsSignalRO],
    ) -> None:
        async with self._lock:
            self._connections.pop(client_id, None)
        # Blocking CA teardown off-loop, matching the pv/device managers.
        signals = [s for s in (array_signal, *setting_signals.values()) if s is not None]
        for signal in signals:
            try:
                await asyncio.to_thread(signal.destroy)
            except Exception as exc:  # noqa: BLE001
                logger.warning("image_signal_destroy_failed", client_id=client_id, error=str(exc))
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("image_socket_closed", client_id=client_id, kind=self.kind)

    async def close_all(self) -> None:
        """Close every open connection (invoked on service shutdown)."""
        async with self._lock:
            sockets = list(self._connections.values())
            self._connections.clear()
        await close_connections(sockets)

    # ------------------------------------------------------------------ #
    # Registry validation + PV resolution
    # ------------------------------------------------------------------ #
    async def _validate_pvs(self, ws: LockedWS, pv_names: list[str]) -> bool:
        """Confirm the given PV(s) exist in the registry, same gate as pv-socket.

        Mirrors ``WebSocketManager._validate_pvs`` but is all-or-nothing: any
        PV failing refuses the whole connection. Callers pass the image array
        PV (the client-controlled data source); the cam1:* settings are not
        validated — see the caller for why. Returns True when all are valid
        (or no registry is configured). On failure emits a per-PV error
        envelope and returns False. A ``RuntimeError`` (config-service down)
        also fails closed — no streaming from an unvalidated PV.
        """
        if self.registry_client is None:
            return True
        results = await asyncio.gather(
            *(self.registry_client.validate_pv(pv) for pv in pv_names),
            return_exceptions=True,
        )
        ok = True
        for pv_name, result in zip(pv_names, results):
            if isinstance(result, (RegistryValidationError, RuntimeError)):
                await send_error(ws, str(result), pv=pv_name)
                ok = False
            elif isinstance(result, Exception):
                raise result
        return ok

    def _resolve_pvs(self, message: dict) -> tuple[str, dict[str, str]]:
        """Resolve the subscribe message to (image_array_pv, {name: pv}).

        camera: explicit per-setting PVs, falling back to prefix-inference from
        ``imageArray_PV`` (or the ADSim default). tiff: a bare ``{prefix}``
        expands to ``{prefix}:image1:ArrayData`` + ``{prefix}:cam1:*``.
        """
        message = message if isinstance(message, dict) else {}
        if self.kind == "tiff":
            prefix = (message.get("prefix") or self.settings.tiff_default_prefix).strip()
            image_array_pv = f"{prefix}:image1:ArrayData"
            setting_pvs = {
                name: f"{prefix}:cam1:{suffix}" for name, suffix in SETTING_SUFFIX.items()
            }
            return image_array_pv, setting_pvs

        # camera
        image_array_pv = (
            message.get("imageArray_PV") or self.settings.camera_default_image_array_pv
        ).strip()
        base = _detector_base(image_array_pv)
        setting_pvs = {}
        for name, suffix in SETTING_SUFFIX.items():
            explicit = message.get(name)
            setting_pvs[name] = explicit if explicit else f"{base}cam1:{suffix}"
        return image_array_pv, setting_pvs


# ---------------------------------------------------------------------- #
# Module-level helpers (stateless; ported from ophyd-websocket)
# ---------------------------------------------------------------------- #
def _detector_base(image_array_pv: str) -> str:
    """Detector base prefix for inferring cam1:* PVs from the image-array PV.

    AreaDetector exposes the array at ``<base>image1:ArrayData`` and the camera
    settings at ``<base>cam1:<field>``. Split on the ``image1:`` plugin token
    rather than the first ``:`` so prefixes that themselves contain ``:`` (e.g.
    ``XF:11IDB:ES{Cam:1}image1:ArrayData``) resolve correctly. Falls back to the
    leading segment for non-standard array PVs that lack ``image1:`` (in which
    case the client should pass explicit setting PVs).
    """
    token = "image1:"
    if token in image_array_pv:
        return image_array_pv.rsplit(token, 1)[0]
    head = image_array_pv.split(":")[0]
    return f"{head}:" if head else ""


def _connect_signal(pv: str, name: str) -> EpicsSignalRO:
    """Create a connected read-only signal or raise with a clear message."""
    signal = EpicsSignalRO(pv, name=name)
    try:
        signal.wait_for_connection(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        try:
            signal.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"PV {pv} connection failed: {exc}") from exc
    if not signal.connected:
        try:
            signal.destroy()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"PV {pv} could not connect")
    return signal


def _enqueue(queue: asyncio.Queue, item: tuple) -> None:
    """Drop-oldest enqueue. Runs on the event loop thread only."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("image_frame_dropped_queue_full")


def _compute_dimensions(setting_signals: dict[str, EpicsSignalRO]) -> dict:
    color_idx = int(setting_signals["colorMode"].get())
    data_idx = int(setting_signals["dataType"].get())
    color_list = getattr(setting_signals["colorMode"], "enum_strs", None) or COLOR_MODE_ENUM
    data_list = getattr(setting_signals["dataType"], "enum_strs", None) or DATA_TYPE_ENUM
    return {
        "x": round(setting_signals["sizeX"].get() - setting_signals["startX"].get()),
        "y": round(setting_signals["sizeY"].get() - setting_signals["startY"].get()),
        "colorMode": color_list[color_idx],
        "dataType": data_list[data_idx],
    }


def _normalize(array: np.ndarray, log_normalization: bool) -> np.ndarray:
    if not log_normalization:
        max_val = array.max() if array.max() > 0 else 1
        return (array / max_val * 255).astype(np.uint8)
    return _log_normalize_to_255(array)


def _log_normalize_to_255(data: np.ndarray) -> np.ndarray:
    if np.any(data < 0):
        raise ValueError("Input data must be non-negative for log normalization.")
    log_data = np.log(data.astype(np.float64) + 1.0)
    log_min, log_max = np.min(log_data), np.max(log_data)
    if log_max == log_min:
        return np.zeros_like(log_data, dtype=np.uint8)
    normalized = (log_data - log_min) / (log_max - log_min) * 255
    return normalized.astype(np.uint8)


def _reshape(array: np.ndarray, height: int, width: int, color_mode: str) -> tuple[np.ndarray, str]:
    if color_mode == "Mono":
        return array.reshape((height, width)), "L"
    if color_mode == "RGB1":
        return array.reshape((height, width, 3)), "RGB"
    if color_mode == "RGB2":
        array = array.reshape((height, width * 3))
        red, green, blue = (
            array[:, 0:width],
            array[:, width : 2 * width],
            array[:, 2 * width : 3 * width],
        )
        return np.stack((red, green, blue), axis=-1), "RGB"
    if color_mode == "RGB3":
        plane = height * width
        red = array[0:plane].reshape((height, width))
        green = array[plane : 2 * plane].reshape((height, width))
        blue = array[2 * plane : 3 * plane].reshape((height, width))
        return np.stack((red, green, blue), axis=-1), "RGB"
    raise ValueError(f"Unsupported color mode: {color_mode}")


def _build_display_image(
    raw,
    *,
    width: int,
    height: int,
    color_mode: str,
    data_type: str,
    log_normalization: bool,
    max_dimension: int,
) -> Image.Image:
    array = np.asarray(raw, dtype=DTYPE_MAP[data_type])
    array = _normalize(array, log_normalization)
    array, mode = _reshape(array, height, width, color_mode)
    image = Image.fromarray(array, mode)
    h, w = array.shape[0], array.shape[1]
    if h > max_dimension or w > max_dimension:
        # Scale both axes by one factor so the frame fits the max bounding box
        # without distorting aspect ratio.
        scale = max_dimension / max(h, w)
        new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
        return image.resize(new_size, Image.LANCZOS)
    return image
