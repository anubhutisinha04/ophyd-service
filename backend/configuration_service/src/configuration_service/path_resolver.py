"""Dotted-path → PV-name resolution for both ophyd frameworks.

Supports two device-class frameworks:

- **Classic ophyd**: signals are class-level ``Component`` declarations.
  Resolution walks the class tree without instantiating; no Python-side
  side effects.

- **ophyd-async**: signals are created in ``__init__``. Resolution
  instantiates the device once (still no EPICS connection — that requires
  an explicit ``await device.connect()``), then walks the live attribute
  tree and reads each leaf signal's ``.source`` URI.

Both frameworks return the same ``Resolution`` shape so the endpoint is
framework-agnostic. Dispatch by ``isinstance(cls, ophyd_async.core.Device)``.

Limitations:
- Classic ``FormattedComponent`` (``FmtCpt``) suffixes that contain ``{}``
  interpolation placeholders cannot be resolved statically; they need a
  live device instance to evaluate ``{self.parent.prefix}`` etc. Those
  return ``Outcome.NEEDS_ENRICHMENT`` so a downstream service
  (direct-control, when it instantiates the device) can fill them in.
- ophyd-async classes that raise on instantiation (e.g. require extra
  ctor args) return ``Outcome.IMPORT_FAILED`` with the exception detail.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# Strip any lowercase URI scheme (ca://, pva://, mock://, soft://, etc.).
# ophyd-async backends are free to introduce new schemes; matching by shape
# avoids hard-coding the current set.
_URI_SCHEME_RE = re.compile(r"^[a-z]+://")


class Outcome(str, Enum):
    """Per-address result kind."""

    RESOLVED = "resolved"
    DEVICE_NOT_FOUND = "device_not_found"
    IMPORT_FAILED = "import_failed"
    NO_SUCH_ATTR = "no_such_attr"
    NEEDS_ENRICHMENT = "needs_enrichment"


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a single dotted address."""

    address: str
    outcome: Outcome
    pv_name: Optional[str] = None
    message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.RESOLVED


def _split_address(address: str) -> tuple[str, str]:
    """Split ``device.attr.attr`` into ``(device_name, dotted_subpath)``.

    Top-level address with no dots (e.g. ``sample_sclr_gain``) returns
    ``(address, "")``.
    """
    head, _, tail = address.partition(".")
    return head, tail


def _import_class(device_class_path: str):
    """Import ``module.ClassName`` and return the class object.

    Raises ``ImportError`` (or ``AttributeError``) on failure — callers
    translate those into ``Outcome.IMPORT_FAILED``.
    """
    if "." not in device_class_path:
        raise ImportError(
            f"device_class '{device_class_path}' has no module prefix"
        )
    module_name, class_name = device_class_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ImportError(f"module {module_name!r} has no attribute {class_name!r}")
    return cls


def _walk_class(cls, parts: list[str]) -> tuple[Outcome, str, Optional[str]]:
    """Walk a chain of attribute names on a class, collecting PV-suffix pieces.

    Returns ``(outcome, suffix_or_path_so_far, optional_message)``:
    - ``(RESOLVED, suffix, None)`` — chain walked cleanly; concatenated suffix is returned.
    - ``(NEEDS_ENRICHMENT, path_so_far, reason)`` — hit a ``FmtCpt`` with interpolation.
    - ``(NO_SUCH_ATTR, path_so_far, bad_segment)`` — attribute missing or not a Component.
    """
    # Lazy import so configuration_service imports don't drag ophyd into the
    # graph unless someone actually calls the resolver.
    from ophyd import Component, DynamicDeviceComponent, FormattedComponent

    current = cls
    suffix_pieces: list[str] = []

    for i, attr in enumerate(parts):
        path_so_far = ".".join(parts[:i]) if i else ""
        cpt = getattr(current, attr, None)

        if cpt is None:
            return Outcome.NO_SUCH_ATTR, path_so_far, attr

        # FormattedComponent with {} placeholders depends on a live parent
        # to interpolate (e.g. ``{self.parent.prefix}}}MOVE_CMD.PROC``).
        # We refuse statically rather than emitting a wrong PV.
        if isinstance(cpt, FormattedComponent):
            if "{" in cpt.suffix:
                return (
                    Outcome.NEEDS_ENRICHMENT,
                    path_so_far + ("." if path_so_far else "") + attr,
                    f"FmtCpt suffix has placeholders: {cpt.suffix!r}",
                )
            suffix_pieces.append(cpt.suffix)
            current = cpt.cls
        elif isinstance(cpt, DynamicDeviceComponent):
            # DDC carries a dynamically-built sub-class; walk into it.
            # The DDC itself contributes no suffix — children carry their own.
            current = cpt.cls
        elif isinstance(cpt, Component):
            suffix_pieces.append(cpt.suffix)
            current = cpt.cls
        else:
            # Attribute exists but isn't a Component (could be a method,
            # classvar, etc.) — treat as bad-path.
            return Outcome.NO_SUCH_ATTR, path_so_far, attr

    return Outcome.RESOLVED, "".join(suffix_pieces), None


def _is_ophyd_async_class(cls) -> bool:
    """True if ``cls`` is part of the ophyd-async framework.

    ophyd-async is optional — if it isn't installed, every class is treated
    as classic ophyd and the classic walker runs.
    """
    try:
        from ophyd_async.core import Device as AsyncDevice
    except ImportError:
        return False
    return isinstance(cls, type) and issubclass(cls, AsyncDevice)


def _strip_signal_source_scheme(source: str) -> str:
    """``"<scheme>://X:Y"`` → ``"X:Y"``. Leave scheme-less strings alone.

    ophyd-async ``Signal.source`` URIs use scheme prefixes that name the
    backend (``ca://`` / ``pva://`` for live EPICS, ``mock://`` /
    ``soft://`` for in-memory backends). The downstream PV-write path
    expects bare PV strings, so strip any single lowercase scheme.
    """
    return _URI_SCHEME_RE.sub("", source, count=1)


def _walk_async_instance(device, parts: list[str]) -> tuple[Outcome, str, Optional[str]]:
    """Walk an instantiated ophyd-async device's attribute tree.

    Returns ``(RESOLVED, pv_name, None)`` for leaves that expose ``.source``,
    or a typed failure for missing attrs / non-signal leaves.
    """
    current = device
    for i, attr in enumerate(parts):
        path_so_far = ".".join(parts[:i]) if i else ""
        sub = getattr(current, attr, None)
        if sub is None:
            return Outcome.NO_SUCH_ATTR, path_so_far, attr
        current = sub

    source = getattr(current, "source", None)
    if source is None:
        # We reached an intermediate device instead of a leaf signal.
        # Treat as bad path so the caller learns to ask for a deeper attr.
        return (
            Outcome.NO_SUCH_ATTR,
            ".".join(parts[:-1]) if len(parts) > 1 else "",
            f"'{parts[-1]}' is not a leaf signal (no .source attribute)",
        )

    return Outcome.RESOLVED, _strip_signal_source_scheme(str(source)), None


def _get_or_create_async_device(
    cls, prefix: str, cache: Optional[dict]
) -> tuple[object, Optional[str]]:
    """Return ``(device, error_message)``; exactly one is non-None.

    Honors the optional ``(cls, prefix) → (device, err)`` cache so a batch
    of addresses against the same device reuses one instantiation. A failed
    instantiation is cached too — every subsequent address for that same
    (cls, prefix) short-circuits with the same error rather than retrying.
    """
    key = (cls, prefix)
    if cache is not None and key in cache:
        return cache[key]

    try:
        # The "_resolve" name is a sentinel: never reaches EPICS, just gets
        # attached to the in-memory device object for log readability.
        result = (cls(prefix, name="_resolve"), None)
    except Exception as e:  # noqa: BLE001 — propagate the actual reason
        result = (None, f"Instantiation failed: {type(e).__name__}: {e}")

    if cache is not None:
        cache[key] = result
    return result


def _resolve_ophyd_async(
    address: str,
    cls,
    prefix: str,
    sub_path: str,
    *,
    device_cache: Optional[dict] = None,
) -> Resolution:
    """Instantiate-without-connect, then walk ``.source`` URIs.

    ``device_cache``: optional dict keyed by ``(cls, prefix)`` to reuse a
    single instantiation across multiple sibling addresses (e.g. a 12-item
    batch all targeting ``motor.X``). The cache stores either the live
    device or a (None, error_message) tuple so a failed instantiation
    short-circuits subsequent addresses on the same device.
    """
    device, err = _get_or_create_async_device(cls, prefix, device_cache)
    if err is not None:
        return Resolution(
            address=address,
            outcome=Outcome.IMPORT_FAILED,
            message=err,
        )

    if not sub_path:
        # Top-level happi entry whose class IS the device — for ophyd-async
        # there is no single "the PV", so this case is meaningless. Return
        # NO_SUCH_ATTR with a hint so the caller fixes the address.
        return Resolution(
            address=address,
            outcome=Outcome.NO_SUCH_ATTR,
            message=(
                "ophyd-async devices require a sub-attribute "
                "(e.g. 'motor.user_setpoint'); top-level addressing is "
                "only meaningful for single-signal classic-ophyd entries."
            ),
        )

    parts = sub_path.split(".")
    outcome, value, msg = _walk_async_instance(device, parts)

    if outcome is Outcome.RESOLVED:
        return Resolution(address=address, outcome=Outcome.RESOLVED, pv_name=value)
    # NO_SUCH_ATTR
    where = value
    bad = msg
    detail = (
        f"{bad} (at '{where}')" if where else f"{bad}"
    )
    return Resolution(address=address, outcome=Outcome.NO_SUCH_ATTR, message=detail)


def _resolve_ophyd_classic(address: str, cls, prefix: str, sub_path: str) -> Resolution:
    """Classic-ophyd path: walk Components at the class level."""
    if not sub_path:
        # Device IS the leaf (top-level EpicsSignal/EpicsMotor happi entry).
        return Resolution(address=address, outcome=Outcome.RESOLVED, pv_name=prefix)

    parts = sub_path.split(".")
    outcome, suffix_or_path, msg = _walk_class(cls, parts)

    if outcome is Outcome.RESOLVED:
        return Resolution(
            address=address,
            outcome=Outcome.RESOLVED,
            pv_name=prefix + suffix_or_path,
        )
    if outcome is Outcome.NEEDS_ENRICHMENT:
        return Resolution(
            address=address,
            outcome=Outcome.NEEDS_ENRICHMENT,
            message=f"at '{suffix_or_path}': {msg}",
        )
    # NO_SUCH_ATTR
    bad_seg = msg
    where = suffix_or_path
    detail = (
        f"no Component '{bad_seg}' on '{where}'"
        if where
        else f"no Component '{bad_seg}' on top-level class"
    )
    return Resolution(
        address=address,
        outcome=Outcome.NO_SUCH_ATTR,
        message=detail,
    )


def resolve(
    address: str,
    *,
    device_class_path: str,
    prefix: str,
    device_cache: Optional[dict] = None,
) -> Resolution:
    """Resolve a single dotted address to a PV name.

    ``device_class_path`` is the fully-qualified import path of the device's
    ophyd class (e.g. ``"ios_devs.Vortex"`` for classic ophyd or
    ``"ophyd_async.epics.motor.Motor"`` for ophyd-async), and ``prefix``
    is what the device was constructed with.

    ``address`` may be either ``"<device>"`` (top-level — classic-ophyd
    only, e.g. for happi entries whose class is a single EpicsSignal) or
    ``"<device>.<attr>.<attr>..."`` (walks the device class tree).

    Framework is detected automatically: ``ophyd_async.core.Device``
    subclasses are instantiated and walked at the instance level (no
    EPICS connection is opened); everything else is walked statically
    via class-level Components.

    ``device_cache`` (optional dict) is honored only by the ophyd-async
    path. Pass an empty dict per request to amortize instantiation across
    a batch of sibling addresses; the classic path is purely static and
    already costs nothing.
    """
    _, sub_path = _split_address(address)

    try:
        cls = _import_class(device_class_path)
    except (ImportError, AttributeError) as e:
        return Resolution(
            address=address,
            outcome=Outcome.IMPORT_FAILED,
            message=f"{type(e).__name__}: {e}",
        )

    if _is_ophyd_async_class(cls):
        return _resolve_ophyd_async(
            address, cls, prefix, sub_path, device_cache=device_cache
        )
    return _resolve_ophyd_classic(address, cls, prefix, sub_path)
