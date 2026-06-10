"""Live-device manager for device-level control.

Owns the name → (live connected device, driver) cache behind
``execute_device_method`` and nested-component access. Distinct from
``ophyd_cache`` on purpose: that cache instantiates devices keyed by
``(class_path, prefix)`` with a sentinel name purely for PV-name enrichment;
this one builds devices keyed by registry NAME, connects them, dispatches a
framework-matched driver, and owns their shutdown.

Policies:
- Per-name asyncio lock: concurrent first requests for the same device
  serialize on one instantiate+connect; different devices proceed in parallel.
- Failures are NOT cached. An IOC that is down now may be up on the next
  request — re-trying is correct, and the per-name lock prevents stampedes.
- A changed instantiation spec (class/args/kwargs differ from the cached
  build) destroys the old instance and rebuilds, so registry updates take
  effect without a service restart.
- ``cleanup()`` destroys every cached classic device so its CA channels are
  released on graceful shutdown (ophyd-async needs no per-device teardown).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from .config import Settings
from .drivers import (
    FRAMEWORK_SYNC,
    detect_framework,
    driver_for,
    import_device_class,
)
from .models import ControlError, DeviceNotInstantiableError, InstantiationSpec

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _Entry:
    device: Any
    driver: Any
    fingerprint: str


def _fingerprint(spec: InstantiationSpec) -> str:
    """Stable identity of a build, so a registry spec change is detected."""
    return f"{spec.device_class}|{spec.args!r}|{sorted(spec.kwargs.items())!r}"


class DeviceManager:
    """name → live connected device, built from an InstantiationSpec."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._entries: dict[str, _Entry] = {}
        # All access is on the single event loop; per-name locks serialize
        # the slow instantiate+connect, not the dict operations.
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_or_connect(self, spec: InstantiationSpec) -> tuple[Any, Any]:
        """Return the live (device, driver) for this spec, building if needed.

        Raises:
            DeviceNotInstantiableError: spec is marked inactive.
            ControlError: bad class path, framework-tag mismatch,
                instantiation failure, or connect failure/timeout.
        """
        if not spec.active:
            raise DeviceNotInstantiableError(
                f"Device {spec.name!r} is marked inactive in its instantiation "
                f"spec; activate it in the registry before device-level control."
            )

        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        async with lock:
            fp = _fingerprint(spec)
            entry = self._entries.get(spec.name)
            if entry is not None:
                if entry.fingerprint == fp:
                    return entry.device, entry.driver
                # Spec changed underneath us — rebuild against the new spec.
                logger.info(
                    "device_spec_changed_rebuilding",
                    device_name=spec.name,
                    old=entry.fingerprint,
                    new=fp,
                )
                await self._destroy_entry(spec.name, entry)

            device, driver = await self._build(spec)
            self._entries[spec.name] = _Entry(device=device, driver=driver, fingerprint=fp)
            logger.info(
                "device_instantiated",
                device_name=spec.name,
                device_class=spec.device_class,
                framework=driver.framework,
            )
            return device, driver

    async def _build(self, spec: InstantiationSpec) -> tuple[Any, Any]:
        cls = import_device_class(spec.device_class)
        framework = detect_framework(cls)
        if spec.framework is not None and spec.framework != framework:
            raise ControlError(
                f"Registry tags device {spec.name!r} as {spec.framework!r} but "
                f"{spec.device_class} is a {framework} class. Fix the registry "
                f"entry; the tag is never silently overridden."
            )
        driver = driver_for(framework)

        kwargs = dict(spec.kwargs)
        kwargs.setdefault("name", spec.name)
        try:
            if framework == FRAMEWORK_SYNC:
                # Classic ctors create pyepics PV wrappers (I/O-adjacent);
                # keep them off the event loop.
                device = await asyncio.to_thread(cls, *spec.args, **kwargs)
            else:
                device = cls(*spec.args, **kwargs)
        except ControlError:
            raise
        except Exception as e:
            raise ControlError(
                f"Failed to instantiate {spec.device_class} for device "
                f"{spec.name!r}: {type(e).__name__}: {e}"
            ) from e

        try:
            await driver.connect(device, timeout=self._settings.device_connect_timeout)
        except BaseException as e:
            # Release whatever the half-connected device grabbed, then
            # surface the real failure.
            try:
                await driver.destroy(device)
            except Exception as destroy_err:  # noqa: BLE001
                logger.warning(
                    "device_destroy_after_failed_connect_failed",
                    device_name=spec.name,
                    error=str(destroy_err),
                )
            if isinstance(e, asyncio.CancelledError):
                raise
            raise ControlError(
                f"Device {spec.name!r} ({spec.device_class}) failed to connect "
                f"within {self._settings.device_connect_timeout}s: "
                f"{type(e).__name__}: {e}"
            ) from e
        return device, driver

    async def _destroy_entry(self, name: str, entry: _Entry) -> None:
        try:
            await entry.driver.destroy(entry.device)
        except Exception as e:  # noqa: BLE001 — shutdown/rebuild must proceed
            logger.warning("device_destroy_failed", device_name=name, error=str(e))
        self._entries.pop(name, None)

    def size(self) -> int:
        return len(self._entries)

    def device_names(self) -> list[str]:
        return list(self._entries)

    async def cleanup(self) -> None:
        """Destroy all live devices (releases classic-ophyd CA channels)."""
        for name, entry in list(self._entries.items()):
            await self._destroy_entry(name, entry)
        self._locks.clear()
        # aioca keeps a process-global channel cache whose callbacks are bound
        # to the event loop they were created on. This manager is shutting
        # down with its loop — purge so channels can't outlive it (matters for
        # in-process restarts and test harnesses that cycle loops).
        import aioca

        aioca.purge_channel_caches()
        logger.info("device_manager_cleaned_up")
