"""
EPICS PV monitoring manager using ophyd EpicsSignal.

Handles EPICS Channel Access subscriptions and PV value caching.
Uses ophyd (pyepics) for compatibility with ophyd-websocket patterns.

Implements: PVMonitor protocol
"""

import os
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime
from typing import Literal, NamedTuple

import numpy as np
import structlog

from .._array_metadata import describe_array
from ..config import Settings
from ..models import PVNotFoundError, PVReadError, PVUpdate, PVValue

# Set EPICS env vars before importing ophyd/pyepics
# pyepics reads these at import time
_epics_addr = os.environ.get("DIRECT_CONTROL_EPICS_CA_ADDR_LIST")
_epics_auto = os.environ.get("DIRECT_CONTROL_EPICS_CA_AUTO_ADDR_LIST", "YES")

if _epics_addr:
    os.environ["EPICS_CA_ADDR_LIST"] = _epics_addr
if _epics_auto:
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = _epics_auto

from ophyd import (  # noqa: E402  (EPICS_CA_* env must be set before ophyd/pyepics import)
    EpicsSignal,
    EpicsSignalRO,
)

logger = structlog.get_logger(__name__)


class _Subscriber(NamedTuple):
    """A registered (callback, on_error) pair for a subscribed PV.

    The optional ``on_error`` is invoked synchronously on the CA thread
    when ``callback`` raises during a value or meta update — letting the
    subscriber translate the failure into a user-visible signal (e.g. a
    ``pv_error`` WebSocket envelope) instead of having it disappear into
    a log line.
    """

    callback: Callable[["PVUpdate"], None]
    on_error: Callable[[BaseException], None] | None


class PVMonitorManager:
    """
    Manages EPICS PV monitoring subscriptions using ophyd.

    Uses ophyd's EpicsSignal for Channel Access connections and provides
    async-friendly interface for PV value updates.

    Implements: PVMonitor protocol
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._signals: dict[str, EpicsSignal] = {}
        self._buffers: dict[str, deque[PVValue]] = defaultdict(
            lambda: deque(maxlen=settings.pv_buffer_size)
        )
        self._callbacks: dict[str, list[_Subscriber]] = defaultdict(list)
        self._connection_status: dict[str, bool] = {}
        self._latest_values: dict[str, PVValue] = {}
        self._lock = threading.RLock()
        # Per-PV lock serializing the (blocking) first connect for a PV so two
        # concurrent first-touches don't open two CA connections. Held only
        # around the connect + initial read, NOT ``self._lock`` — so a dead
        # PV's connection timeout can't stall value/meta fan-out for others.
        self._connect_locks: dict[str, threading.Lock] = {}

        logger.info(
            "ophyd_pv_monitor_initialized",
            epics_ca_addr_list=settings.epics_ca_addr_list,
        )

    def subscribe(
        self,
        pv_name: str,
        callback: Callable[[PVUpdate], None] | None = None,
        read_only: bool = False,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        # Fast path: already connected — just register the callback. Avoids
        # taking the per-PV connect lock for the common repeat-subscribe case.
        with self._lock:
            if pv_name in self._signals:
                if callback:
                    self._callbacks[pv_name].append(_Subscriber(callback, on_error))
                return

        # Slow path: the blocking CA work — EpicsSignal construction,
        # wait_for_connection (up to 5 s for a dead PV), and the initial read
        # in _signal_to_pv_value — runs OUTSIDE self._lock. Holding self._lock
        # across it would stall every PV's value/meta fan-out, which reacquires
        # the same lock on the CA dispatch thread. A per-PV connect lock keeps
        # concurrent first-touches on the same PV from opening two connections
        # (mirrors OphydDeviceCache's per-key locking).
        connect_lock = self._get_connect_lock(pv_name)
        with connect_lock:
            with self._lock:
                if pv_name in self._signals:
                    if callback:
                        self._callbacks[pv_name].append(_Subscriber(callback, on_error))
                    return

            logger.info("subscribing_to_pv", pv_name=pv_name)
            signal = None
            try:
                signal = (
                    EpicsSignalRO(pv_name, name=pv_name)
                    if read_only
                    else EpicsSignal(pv_name, name=pv_name)
                )
                signal.wait_for_connection(timeout=5.0)

                if not signal.connected:
                    logger.error("pv_connection_failed", pv_name=pv_name)
                    raise PVNotFoundError(f"PV {pv_name} connection timeout")

                # Read initial value FIRST. If this fails (e.g. dtype
                # mismatch in _signal_to_pv_value), bail before registering —
                # a signal left in _signals with no buffer would silently
                # break later get_value() calls.
                initial_value = self._signal_to_pv_value(pv_name, signal)
            except Exception as e:
                logger.error("pv_subscription_error", pv_name=pv_name, error=str(e))
                if signal is not None:
                    self._destroy_signal_quietly(signal, pv_name)
                # Drop the connect lock for a PV that never established a
                # subscription. No unsubscribe path reaches a failed PV, so
                # without this a repeatedly-failing PV would leave its entry in
                # _connect_locks forever. Guarded on _signals so we never remove
                # the lock for a live subscription a racing subscriber committed.
                with self._lock:
                    if pv_name not in self._signals:
                        self._connect_locks.pop(pv_name, None)
                raise PVNotFoundError(f"PV {pv_name} subscription failed: {e}") from e

            # Commit the connected signal + initial value under the lock. If a
            # racing subscriber already committed one (possible if an
            # interleaved unsubscribe recycled the connect lock), keep theirs
            # and destroy ours — overwriting _signals would orphan a live CA
            # connection with a callback that nothing can ever tear down.
            stale_signal = None
            with self._lock:
                if pv_name in self._signals:
                    stale_signal = signal
                else:
                    self._signals[pv_name] = signal
                    self._connection_status[pv_name] = True
                    self._latest_values[pv_name] = initial_value
                    self._buffers[pv_name].append(initial_value)

                    # Registering the CA monitors is a local (non-blocking)
                    # operation, so it's fine under the lock — but it can still
                    # fail (e.g. the channel dropped between connect and
                    # subscribe). Roll the commit back if it does: a signal left
                    # in _signals with no live monitor would serve a frozen
                    # cached value forever.
                    try:
                        signal.subscribe(
                            lambda value, timestamp=None, **kwargs: self._handle_value_update(
                                pv_name, value, timestamp
                            ),
                            event_type="value",
                        )
                        signal.subscribe(
                            lambda **kwargs: self._handle_meta_update(pv_name, **kwargs),
                            event_type="meta",
                        )
                    except Exception as e:
                        logger.error("pv_subscription_error", pv_name=pv_name, error=str(e))
                        self._signals.pop(pv_name, None)
                        self._connection_status.pop(pv_name, None)
                        self._latest_values.pop(pv_name, None)
                        self._buffers.pop(pv_name, None)
                        # PV never subscribed — drop its connect lock too (see
                        # the connect-failure path above for the rationale).
                        self._connect_locks.pop(pv_name, None)
                        self._destroy_signal_quietly(signal, pv_name)
                        raise PVNotFoundError(f"PV {pv_name} subscription failed: {e}") from e

                    logger.info("pv_connected", pv_name=pv_name, connected=True)

                if callback:
                    self._callbacks[pv_name].append(_Subscriber(callback, on_error))

        if stale_signal is not None:
            self._destroy_signal_quietly(stale_signal, pv_name)

    @staticmethod
    def _destroy_signal_quietly(signal: EpicsSignal, pv_name: str) -> None:
        """Best-effort CA teardown of a signal, logging (not raising) on error."""
        try:
            signal.destroy()
        except Exception as destroy_err:  # noqa: BLE001
            logger.warning("pv_signal_destroy_failed", pv_name=pv_name, error=str(destroy_err))

    def _get_connect_lock(self, pv_name: str) -> threading.Lock:
        """Return the per-PV connect lock, creating it on first use.

        Guarded by ``self._lock`` only for the O(1) dict access — the actual
        blocking connect runs under the returned lock, never under
        ``self._lock``.
        """
        with self._lock:
            lock = self._connect_locks.get(pv_name)
            if lock is None:
                lock = threading.Lock()
                self._connect_locks[pv_name] = lock
            return lock

    def _dispatch_subscriber(
        self,
        pv_name: str,
        sub: _Subscriber,
        update: "PVUpdate",
        *,
        source: Literal["value", "meta"],
    ) -> None:
        """Invoke a subscriber's callback and route any exception to its on_error.

        Runs on the CA listener thread. The outer ``try`` keeps a single
        broken subscriber from poisoning fan-out for the others on this
        PV. ``exc_info=True`` preserves the traceback in the log so a
        future failure isn't a one-line mystery, and ``on_error`` (when
        provided) lets the subscriber translate the failure into a
        user-visible signal — e.g. a ``pv_error`` WS envelope.
        """
        try:
            sub.callback(update)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pv_callback_failed",
                pv_name=pv_name,
                source=source,
                error=str(exc),
                exc_info=True,
            )
            if sub.on_error is None:
                return
            try:
                sub.on_error(exc)
            except Exception as inner:  # noqa: BLE001
                logger.error(
                    "pv_callback_on_error_raised",
                    pv_name=pv_name,
                    source=source,
                    error=str(inner),
                    exc_info=True,
                )

    def _handle_value_update(self, pv_name: str, value, timestamp):
        with self._lock:
            if pv_name not in self._signals:
                return

            shape, dtype, ndim, nbytes = describe_array(value)
            converted_value = self._convert_value(value)
            ts = datetime.fromtimestamp(timestamp) if timestamp else datetime.now()
            read_access, write_access = self._extract_access_bits(pv_name)

            pv_value = PVValue(
                pv_name=pv_name,
                value=converted_value,
                timestamp=ts,
                status=0,
                severity=0,
                connected=True,
                shape=shape,
                dtype=dtype,
                ndim=ndim,
                nbytes=nbytes,
                read_access=read_access,
                write_access=write_access,
            )
            self._latest_values[pv_name] = pv_value
            self._buffers[pv_name].append(pv_value)

            update = PVUpdate(
                pv=pv_name,
                value=converted_value,
                timestamp=ts,
                status=0,
                severity=0,
                connected=True,
                read_access=read_access,
                write_access=write_access,
            )
            callbacks = list(self._callbacks.get(pv_name, []))

        for sub in callbacks:
            self._dispatch_subscriber(pv_name, sub, update, source="value")

    def _handle_meta_update(self, pv_name: str, **kwargs):
        if "connected" not in kwargs:
            return
        connected = kwargs["connected"]

        with self._lock:
            previous = self._connection_status.get(pv_name)
            if previous == connected:
                return
            # The first meta event after subscribe is ~always `connected=True`
            # and duplicates the initial value the subscribe path already sent;
            # skip that specific transition but still track the state.
            first_and_connected = previous is None and connected
            self._connection_status[pv_name] = connected
            if first_and_connected:
                return
            callbacks = list(self._callbacks.get(pv_name, []))
            latest = self._latest_values.get(pv_name)
            read_access, write_access = self._extract_access_bits(pv_name)

        if connected:
            logger.info("pv_reconnected", pv_name=pv_name)
        else:
            logger.warning("pv_disconnected", pv_name=pv_name)

        # Broadcast the connection state change so subscribers see it without
        # waiting for the next value update (or forever, if there isn't one).
        update = PVUpdate(
            pv=pv_name,
            value=latest.value if latest else None,
            timestamp=datetime.now(),
            status=0,
            severity=0,
            connected=connected,
            read_access=read_access,
            write_access=write_access,
        )
        for sub in callbacks:
            self._dispatch_subscriber(pv_name, sub, update, source="meta")

    def _extract_access_bits(self, pv_name: str) -> tuple[bool, bool]:
        """Read (read_access, write_access) from a subscribed signal's CA PV.

        Caller must hold ``self._lock``. Returns ``(False, False)`` if the
        signal is missing or extraction fails — matches the no-silent-fallback
        policy in ``_signal_to_pv_value``: defaulting to permissive bits
        would let a UI render writable controls for a PV we never confirmed
        write access on. Pre-M14 the streaming-update path skipped this
        entirely and PVUpdate's pydantic defaults silently reported
        ``read_access=True, write_access=False`` regardless of CA reality.
        """
        signal = self._signals.get(pv_name)
        if signal is None:
            return False, False
        try:
            pv = getattr(signal, "_read_pv", None)
            if pv is None:
                return False, False
            return (
                bool(getattr(pv, "read_access", False)),
                bool(getattr(pv, "write_access", False)),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("access_bits_extraction_error", pv_name=pv_name, error=str(e))
            return False, False

    def _convert_value(self, value):
        # No int-array→ASCII heuristic. Pre-M11 we rendered any uint/int
        # array whose values were all <256 as a string (after dropping
        # zeros). That collapsed legitimate uint8 readbacks (status bytes,
        # image strips) into garbled chars and silently dropped data.
        # Surface arrays as-is; the ``dtype``/``shape`` fields on PVValue
        # carry the structure for any client that needs to decode a
        # DBR_CHAR waveform back to string. EPICS DBR_STRING (≤40 chars)
        # still arrives as ``str`` and falls through; bytes is decoded
        # explicitly below.
        if isinstance(value, np.ndarray):
            return value.tolist()
        elif isinstance(value, np.integer):
            return int(value)
        elif isinstance(value, np.floating):
            return float(value)
        elif isinstance(value, np.bool_):
            return bool(value)
        elif isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _signal_to_pv_value(self, pv_name: str, signal: EpicsSignal) -> PVValue:
        raw = signal.get()
        shape, dtype, ndim, nbytes = describe_array(raw)
        value = self._convert_value(raw)
        timestamp = datetime.now()
        if signal.timestamp:
            try:
                timestamp = datetime.fromtimestamp(signal.timestamp)
            except Exception:
                pass

        units = precision = enum_strs = None
        lower_ctrl_limit = upper_ctrl_limit = None
        lower_disp_limit = upper_disp_limit = None
        # Default to no access — assume locked-out until EPICS confirms otherwise.
        # Defaulting write_access=True would let a UI render writable controls for a
        # PV we never confirmed write access on.
        read_access = write_access = False

        try:
            pv = getattr(signal, "_read_pv", None)
            if pv is not None:
                units = getattr(pv, "units", None)
                precision = getattr(pv, "precision", None)
                enum_strs = getattr(pv, "enum_strs", None)
                lower_ctrl_limit = getattr(pv, "lower_ctrl_limit", None)
                upper_ctrl_limit = getattr(pv, "upper_ctrl_limit", None)
                lower_disp_limit = getattr(pv, "lower_disp_limit", None)
                upper_disp_limit = getattr(pv, "upper_disp_limit", None)
                read_access = getattr(pv, "read_access", False)
                write_access = getattr(pv, "write_access", False)
                if enum_strs and isinstance(enum_strs, tuple):
                    enum_strs = list(enum_strs)
        except Exception as e:
            logger.debug("metadata_extraction_error", pv_name=pv_name, error=str(e))

        return PVValue(
            pv_name=pv_name,
            value=value,
            timestamp=timestamp,
            status=0,
            severity=0,
            connected=signal.connected,
            shape=shape,
            dtype=dtype,
            ndim=ndim,
            nbytes=nbytes,
            units=units,
            precision=precision,
            enum_strs=enum_strs,
            lower_ctrl_limit=lower_ctrl_limit,
            upper_ctrl_limit=upper_ctrl_limit,
            lower_disp_limit=lower_disp_limit,
            upper_disp_limit=upper_disp_limit,
            read_access=read_access,
            write_access=write_access,
        )

    def unsubscribe(self, pv_name: str, callback: Callable | None = None) -> None:
        signal_to_destroy = None
        with self._lock:
            if callback:
                if pv_name in self._callbacks:
                    # Identity match on the value callback — the on_error
                    # paired with it (if any) goes away with the entry.
                    self._callbacks[pv_name] = [
                        sub for sub in self._callbacks[pv_name] if sub.callback is not callback
                    ]
            else:
                self._callbacks.pop(pv_name, None)

            if not self._callbacks.get(pv_name) and pv_name in self._signals:
                logger.info("disconnecting_pv", pv_name=pv_name)
                signal_to_destroy = self._signals.pop(pv_name, None)
                self._connection_status.pop(pv_name, None)
                self._buffers.pop(pv_name, None)
                self._latest_values.pop(pv_name, None)
                # Drop the connect lock too so it doesn't accumulate one entry
                # per distinct PV ever seen. A concurrent re-subscribe simply
                # recreates it; correctness there rests on the commit-time
                # re-check in subscribe(), not on the lock's identity.
                self._connect_locks.pop(pv_name, None)

        # destroy() does CA TCP teardown + drops pyepics _PVcache_ entry via
        # ophyd finalizers; run outside self._lock so concurrent subscribers
        # aren't blocked on network I/O.
        if signal_to_destroy is not None:
            try:
                signal_to_destroy.destroy()
            except Exception as e:  # noqa: BLE001
                logger.warning("pv_destroy_failed", pv_name=pv_name, error=str(e))

    def get_value(self, pv_name: str) -> PVValue | None:
        """See ``PVMonitor.get_value``: ``None`` if not subscribed, else
        the cached value or a fresh read; raises ``PVReadError`` if the
        on-demand read fails."""
        with self._lock:
            if pv_name in self._latest_values:
                return self._latest_values[pv_name]

            if pv_name in self._signals:
                signal = self._signals[pv_name]
                try:
                    pv_value = self._signal_to_pv_value(pv_name, signal)
                except Exception as e:
                    logger.warning("pv_read_failed", pv_name=pv_name, error=str(e))
                    raise PVReadError(f"read failed for {pv_name}: {e}") from e
                self._latest_values[pv_name] = pv_value
                return pv_value

            return None

    def get_buffer(self, pv_name: str) -> list[PVValue]:
        with self._lock:
            return list(self._buffers.get(pv_name, []))

    def is_connected(self, pv_name: str) -> bool:
        with self._lock:
            return self._connection_status.get(pv_name, False)

    def get_connected_pvs(self) -> list[str]:
        with self._lock:
            return [name for name, status in self._connection_status.items() if status]

    async def cleanup(self):
        logger.info("cleaning_up_pv_connections")
        with self._lock:
            signals = list(self._signals.values())
            self._signals.clear()
            self._callbacks.clear()
            self._connection_status.clear()
            self._buffers.clear()
            self._latest_values.clear()
            self._connect_locks.clear()

        for signal in signals:
            try:
                signal.destroy()
            except Exception:  # noqa: BLE001
                pass
