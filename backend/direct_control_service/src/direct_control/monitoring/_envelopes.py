"""
Shared WebSocket message envelope helpers.

All outbound WS messages in this service share the shape
``{"type": <str>, "timestamp": <iso>, **fields}``; these helpers build and
send that envelope so the two managers don't repeat it ~60 times.
"""

import asyncio
import concurrent.futures
import json
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Optional

import structlog
from fastapi import WebSocket

logger = structlog.get_logger(__name__)


# Where the threadsafe coroutine was scheduled from. Closed set so a typo
# at the call site is a type error rather than a silent log-tag drift.
ThreadsafeCallSite = Literal[
    "pv_broadcast_update",
    "device_broadcast_update",
    "pv_callback_error",
    "device_callback_error",
]


def log_threadsafe_future_exceptions(
    fut: "concurrent.futures.Future[Any]", *, where: ThreadsafeCallSite
) -> None:
    """Done-callback for ``asyncio.run_coroutine_threadsafe`` futures.

    Without it, exceptions raised inside the scheduled coroutine are
    retained on the Future but never surfaced — silently lost when the
    EPICS callback thread discards the Future reference.
    """
    try:
        exc = fut.exception()
    except (concurrent.futures.CancelledError, asyncio.CancelledError):
        return
    if exc is not None:
        logger.error(
            "threadsafe_coroutine_failed",
            where=where,
            error=str(exc),
            exc_type=type(exc).__name__,
        )


class WebSocketResponseTooLarge(Exception):
    """Raised when an outbound WS frame would exceed the configured size cap."""


class LockedWS:
    """
    Per-connection WebSocket wrapper that serializes outbound sends.

    Starlette's ``WebSocket.send_json`` is not concurrency-safe across
    coroutines. In this service a single client has three concurrent
    senders: the handler's request/response loop, fan-out broadcasts
    triggered by CA callbacks, and the heartbeat task. Without
    serialization these can interleave at the ASGI layer and produce
    protocol errors on busy connections.

    When ``max_message_bytes`` is set, outbound payloads are measured
    against it and oversize frames raise ``WebSocketResponseTooLarge``
    before anything goes on the wire. This is the WS-side parallel of
    the ``DIRECT_CONTROL_RESPONSE_BYTESIZE_LIMIT`` HTTP cap.
    """

    def __init__(
        self,
        ws: WebSocket,
        *,
        max_message_bytes: Optional[int] = None,
        send_timeout: Optional[float] = None,
    ):
        self._ws = ws
        self._send_lock = asyncio.Lock()
        self._max_message_bytes = max_message_bytes
        self._send_timeout = send_timeout

    async def accept(self) -> None:
        await self._ws.accept()

    async def close(self, code: int = 1000, reason: Optional[str] = None) -> None:
        await self._ws.close(code=code, reason=reason)

    async def send_json(self, data: Any) -> None:
        # Pre-serialize so we can enforce the size cap before the frame
        # reaches Starlette. Measuring after framing is too late. Match
        # Starlette's own serialization (compact separators, raw UTF-8)
        # so we don't inflate wire size vs. the pre-cap behavior.
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        await self.send_text(text)

    async def send_text(self, data: str) -> None:
        # send_timeout protects against a slow/wedged client TCP buffer
        # stalling a fan-out broadcast across all subscribers. TimeoutError
        # propagates so the caller can drop the update and log.
        self._check_size(data)
        async with self._send_lock:
            if self._send_timeout is None:
                await self._ws.send_text(data)
            else:
                async with asyncio.timeout(self._send_timeout):
                    await self._ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame through the same serialized, size-capped path.

        Used by the image-streaming sockets (camera/tiff) for JPEG/WebP
        frames. Shares ``_send_lock`` with ``send_text``/``send_json`` so a
        broadcast, a heartbeat, and a frame can't interleave at the ASGI
        layer, and enforces the same byte cap before the frame hits the wire.
        """
        self._raise_if_over_limit(len(data))
        async with self._send_lock:
            if self._send_timeout is None:
                await self._ws.send_bytes(data)
            else:
                async with asyncio.timeout(self._send_timeout):
                    await self._ws.send_bytes(data)

    def _check_size(self, text: str) -> None:
        limit = self._max_message_bytes
        if limit is None:
            return
        # UTF-8 is at most 4 bytes per char, so if n*4 fits the cap the
        # payload is guaranteed under it without materializing the bytes.
        # This avoids a full-size bytes allocation per frame on the hot
        # broadcast path; real payloads (mostly ASCII) always hit it.
        n = len(text)
        if n * 4 <= limit:
            return
        self._raise_if_over_limit(len(text.encode("utf-8")))

    def _raise_if_over_limit(self, size: int) -> None:
        limit = self._max_message_bytes
        if limit is not None and size > limit:
            raise WebSocketResponseTooLarge(
                f"WS message size {size} bytes exceeds "
                f"DIRECT_CONTROL_RESPONSE_BYTESIZE_LIMIT ({limit}). "
                "Slice the value or raise the limit."
            )

    async def receive_json(self) -> Any:
        return await self._ws.receive_json()

    async def receive_text(self) -> str:
        return await self._ws.receive_text()

    @property
    def query_params(self):
        return self._ws.query_params

    @property
    def headers(self):
        return self._ws.headers

    @property
    def client(self):
        return self._ws.client


async def send_event(ws, type_: str, **fields: Any) -> None:
    """Send a typed WS event with an ISO timestamp and arbitrary fields."""
    await ws.send_json({"type": type_, "timestamp": datetime.now().isoformat(), **fields})


async def send_error(ws: WebSocket, message: str, **fields: Any) -> None:
    """Send a WS error envelope.

    Per finch's ophyd-websocket contract (``finch/src/api/ophyd/
    useOphydPVSocket.tsx:171``), the human-readable text lives in an
    ``error`` field on the wire. The Python parameter name stays
    ``message`` so existing callers keep working unchanged.
    """
    await send_event(ws, "error", error=message, **fields)


async def fanout_error(
    ws_by_client: dict[str, Optional["LockedWS"]],
    message: str,
    *,
    log_event: str,
    error_envelope_fields: dict,
    log_fields: dict,
) -> None:
    """Fan out an error envelope to a pre-snapshotted set of clients.

    Caller takes the manager lock once, snapshots ``{client_id: ws}``,
    then hands the dict here so the fan-out does not reacquire the
    lock per client. Clients whose websocket is ``None`` (disconnected
    between snapshot and send-attempt) are skipped silently. Send
    failures are logged at warning under ``log_event`` with the
    caller-supplied fields and never re-raised — one wedged client
    must not poison fan-out for the others.
    """
    for client_id, websocket in ws_by_client.items():
        if websocket is None:
            continue
        try:
            await send_error(websocket, message, **error_envelope_fields)
        except Exception as send_exc:  # noqa: BLE001
            logger.warning(
                log_event,
                client_id=client_id,
                error=str(send_exc),
                **log_fields,
            )


async def _send_or_translate_failure(
    ws: "LockedWS",
    send: Callable[[], Awaitable[None]],
    *,
    log_event: str,
    log_fields: dict,
    oversize_message: str,
    error_envelope_fields: dict,
    notify_on_generic_exception: bool = False,
) -> None:
    """Run ``send`` and translate any failure into a log line and/or a
    structured error envelope. Shared by the JSON and binary send helpers
    so the failure-handling contract lives in one place.

    Three failure paths are unified here:
    - ``TimeoutError``: log a warning. The client is wedged; one missed
      update/frame is acceptable and the connection stays alive.
    - ``WebSocketResponseTooLarge``: log + emit a ``send_error`` envelope
      so the client knows their update was dropped (per the finch
      no-silent-fallbacks contract).
    - Generic ``Exception``: log. If ``notify_on_generic_exception`` is
      true, also emit an error envelope. Use this for one-shot sends
      where losing the message silently is harmful (e.g. ``meta`` on
      subscribe); leave false for per-update fan-outs where the next
      tick recovers and notifying every drop would amplify noise.
    """
    try:
        await send()
        return
    except TimeoutError:
        logger.warning(f"{log_event}_timeout", **log_fields)
        return
    except WebSocketResponseTooLarge as exc:
        logger.warning(f"{log_event}_too_large", error=str(exc), **log_fields)
        envelope_msg = oversize_message
    except Exception as exc:  # noqa: BLE001
        logger.error(f"{log_event}_failed", error=str(exc), exc_info=True, **log_fields)
        if not notify_on_generic_exception:
            return
        envelope_msg = f"send failed: {exc}"

    try:
        await send_error(ws, envelope_msg, **error_envelope_fields)
    except Exception as inner_err:  # noqa: BLE001
        logger.debug(
            f"{log_event}_error_envelope_failed",
            error=str(inner_err),
            **log_fields,
        )


async def send_payload_or_size_error(
    ws: "LockedWS",
    payload: Any,
    *,
    log_event: str,
    log_fields: dict,
    oversize_message: str,
    error_envelope_fields: dict,
    notify_on_generic_exception: bool = False,
) -> None:
    """Send a JSON ``payload`` through the size-cap-aware ``LockedWS``,
    translating failures via :func:`_send_or_translate_failure`.

    Re-uses ``send_error`` for the structured envelope so the field-name
    contract (``error`` not ``message``) lives in one place.
    """
    await _send_or_translate_failure(
        ws,
        lambda: ws.send_json(payload),
        log_event=log_event,
        log_fields=log_fields,
        oversize_message=oversize_message,
        error_envelope_fields=error_envelope_fields,
        notify_on_generic_exception=notify_on_generic_exception,
    )


async def send_bytes_or_size_error(
    ws: "LockedWS",
    data: bytes,
    *,
    log_event: str,
    log_fields: dict,
    oversize_message: str,
    error_envelope_fields: dict,
    notify_on_generic_exception: bool = False,
) -> None:
    """Binary parallel of :func:`send_payload_or_size_error` for image
    frames. A slow-client ``TimeoutError`` drops the single frame (the
    stream stays up and the next frame recovers — this is what makes the
    drop-oldest queue's backpressure actually hold); an oversize frame
    emits a structured error envelope instead of silently dying.
    """
    await _send_or_translate_failure(
        ws,
        lambda: ws.send_bytes(data),
        log_event=log_event,
        log_fields=log_fields,
        oversize_message=oversize_message,
        error_envelope_fields=error_envelope_fields,
        notify_on_generic_exception=notify_on_generic_exception,
    )


async def close_connections(
    sockets: "list[LockedWS]",
    *,
    code: int = 1001,
    reason: str = "Service shutting down",
) -> None:
    """Close a pre-snapshotted list of connections on shutdown.

    Per-socket errors are swallowed so one wedged client can't block the
    others from closing. Callers snapshot ``self._connections.values()``
    under their own lock and clear the registry before calling this, so the
    close I/O runs outside the lock.
    """
    for ws in sockets:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


async def heartbeat_loop(ws: WebSocket, interval: float) -> None:
    """
    Server-initiated WS heartbeat.

    Fires `{"type": "heartbeat", ...}` every `interval` seconds. Intended
    to keep NAT/proxy idle timers from reaping the TCP connection and to
    surface dead peers early (the next send will fail and we close).
    """
    if interval <= 0:
        return
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await send_event(ws, "heartbeat")
            except Exception as send_err:  # noqa: BLE001
                logger.info("heartbeat_send_failed", error=str(send_err))
                try:
                    await ws.close(code=1001, reason="Heartbeat send failed")
                except Exception as close_err:  # noqa: BLE001
                    logger.warning(
                        "heartbeat_close_after_send_failure_failed",
                        error=str(close_err),
                    )
                return
    except asyncio.CancelledError:
        return
