"""Lazy, threadsafe cache of instantiated ophyd devices.

Instantiating a classic-ophyd compound device opens EPICS Channel Access
connections to every leaf signal (to fetch type/units/limits), which takes
hundreds of milliseconds for a real beamline device. The same is true for
ophyd-async device classes once you call ``.connect()`` (though
ophyd-async instantiation alone does no I/O).

Direct-control's enrichment endpoint needs to instantiate a device, walk
its attribute tree to a leaf signal, and read either the signal's
``pvname`` (classic ophyd) or ``.source`` (ophyd-async) so it can return
the resolved PV. We pay the instantiation cost once per
``(device_class_path, prefix)`` pair and keep the live device around for
subsequent requests.

Cache invariants:
- A successful instantiation is kept indefinitely (no TTL). Beamline
  device classes don't change at runtime; if the registry is updated to
  point at a new class, callers should explicitly evict.
- A failed instantiation is *also* cached, as the failure reason, so we
  don't repeatedly pay the import + ctor cost for a class that's broken.
"""
from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CacheKey:
    device_class_path: str
    prefix: str


@dataclass(frozen=True)
class CacheEntry:
    """Successful entry has ``device`` set; failed entry has ``error``."""

    device: Optional[object] = None
    error: Optional[str] = None


class OphydDeviceCache:
    """Threadsafe ``(class_path, prefix) -> live device`` cache.

    First touch instantiates and caches; subsequent touches return the
    cached instance. Lookup is O(1) after the first call. Use
    ``evict(...)`` or ``clear()`` if a deploy changes device classes
    underneath the running service.
    """

    def __init__(self) -> None:
        self._entries: dict[CacheKey, CacheEntry] = {}
        # Per-key lock so concurrent first-touches on the same device
        # don't trigger two instantiations. A single module-level lock
        # would serialize all cache lookups across the service.
        self._key_locks: dict[CacheKey, threading.Lock] = {}
        self._dict_lock = threading.Lock()

    def get_or_instantiate(
        self, device_class_path: str, prefix: str
    ) -> CacheEntry:
        """Return the cached device for ``(class_path, prefix)`` or
        instantiate + cache it. Failures are cached too — see module doc.
        """
        key = CacheKey(device_class_path=device_class_path, prefix=prefix)

        existing = self._entries.get(key)
        if existing is not None:
            return existing

        # Acquire the per-key lock so a second concurrent caller waits
        # rather than racing the constructor.
        with self._dict_lock:
            if key not in self._key_locks:
                self._key_locks[key] = threading.Lock()
            key_lock = self._key_locks[key]

        with key_lock:
            # Re-check under the lock; another thread may have populated
            # the entry while we were waiting.
            existing = self._entries.get(key)
            if existing is not None:
                return existing

            entry = _instantiate(device_class_path, prefix)
            self._entries[key] = entry
            return entry

    def evict(self, device_class_path: str, prefix: str) -> bool:
        """Drop a cached entry. Returns True if something was evicted."""
        key = CacheKey(device_class_path=device_class_path, prefix=prefix)
        with self._dict_lock:
            # Drop the orphaned per-key lock too — otherwise ``_key_locks``
            # grows monotonically across evict/re-instantiate cycles.
            self._key_locks.pop(key, None)
            return self._entries.pop(key, None) is not None

    def clear(self) -> int:
        """Drop everything. Returns the count of evicted entries."""
        with self._dict_lock:
            count = len(self._entries)
            self._entries.clear()
            self._key_locks.clear()
            return count

    def size(self) -> int:
        return len(self._entries)


def _instantiate(device_class_path: str, prefix: str) -> CacheEntry:
    """Import the class and construct an instance with ``prefix``.

    Errors at any step are captured into ``CacheEntry.error`` so the
    cache can short-circuit subsequent identical requests.
    """
    if "." not in device_class_path:
        return CacheEntry(
            error=f"device_class '{device_class_path}' has no module prefix"
        )
    module_name, class_name = device_class_path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        return CacheEntry(error=f"ImportError: {e}")

    cls = getattr(module, class_name, None)
    if cls is None:
        return CacheEntry(
            error=f"module {module_name!r} has no attribute {class_name!r}"
        )

    # The "_enrich" name is a sentinel — it's purely cosmetic on the
    # device object for logs; ophyd uses it in some error messages.
    try:
        device = cls(prefix, name="_enrich")
    except Exception as e:  # noqa: BLE001 — propagate the actual reason
        return CacheEntry(error=f"Instantiation failed: {type(e).__name__}: {e}")

    return CacheEntry(device=device)
