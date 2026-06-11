"""Framework-dispatched device drivers.

direct_control drives BOTH classic ophyd (pyepics transport) and ophyd-async
(aioca/p4p transports) devices during the multi-year migration between the
two frameworks. The two frameworks do not interoperate below the bluesky
RunEngine: different transports, connection models, and Status semantics.
This module is the single place that branches on framework — everything
above it (endpoints, registry/coordination/read-only gates, models) stays
framework-agnostic.

- ``detect_framework`` classifies an imported device class authoritatively
  (issubclass checks — the same heuristic configuration_service's
  path_resolver uses).
- ``ClassicOphydDriver`` bridges the sync framework into the asyncio app:
  blocking calls run on worker threads, ``Status.wait`` included.
- ``OphydAsyncDriver`` is async-native: methods return coroutines or
  ``AsyncStatus`` objects that are awaited with a timeout.

Both drivers expose the same surface: ``connect`` / ``invoke`` / ``destroy``.
``invoke`` operates on any node of the device tree (the device itself or a
nested component), so the nested-component endpoint reuses it.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import structlog
from ophyd.ophydobj import OphydObject
from ophyd.status import StatusBase
from ophyd_async.core import Device as AsyncDevice

from .models import ComponentNotFoundError, ControlError, MethodNotAllowedError

logger = structlog.get_logger(__name__)

FRAMEWORK_SYNC = "ophyd-sync"
FRAMEWORK_ASYNC = "ophyd-async"

# Methods a caller may invoke on a device (or nested component). This is the
# bluesky verb surface plus signal-level get/put for nested leaves — NOT
# arbitrary attribute dispatch: a control endpoint that executes any named
# method on live hardware objects is an injection surface, so unknown methods
# are rejected with an explicit error instead of being looked up.
ALLOWED_METHODS = frozenset(
    {
        "set",
        "put",
        "get",
        "read",
        "describe",
        "read_configuration",
        "describe_configuration",
        "trigger",
        "stop",
    }
)


def check_method_allowed(method: str) -> None:
    """Raise MethodNotAllowedError for methods outside the allowlist."""
    if method not in ALLOWED_METHODS:
        raise MethodNotAllowedError(
            f"Method {method!r} is not allowed. Allowed methods: "
            f"{', '.join(sorted(ALLOWED_METHODS))}"
        )


def import_device_class(device_class_path: str) -> type:
    """Import a device class from its fully-qualified path, failing hard.

    Unlike ophyd_cache's enrichment path (which caches failures as strings),
    control-path import failures raise so the caller gets a real error.
    """
    if "." not in device_class_path:
        raise ControlError(
            f"device_class {device_class_path!r} has no module prefix; expected "
            f"a fully-qualified import path like 'ophyd.EpicsMotor'"
        )
    module_name, class_name = device_class_path.rsplit(".", 1)
    try:
        import importlib

        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ControlError(f"Cannot import module {module_name!r}: {e}") from e
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ControlError(f"Module {module_name!r} has no attribute {class_name!r}")
    if not isinstance(cls, type):
        raise ControlError(f"{device_class_path!r} is not a class (got {type(cls).__name__})")
    return cls


def detect_framework(cls: type) -> str:
    """Classify a device class as ophyd-async or classic ophyd.

    issubclass against the framework base classes is authoritative (same
    check configuration_service's path_resolver uses). A class belonging to
    neither framework is a hard error — there is no PV-gateway fallback at
    the device-control level.
    """
    if issubclass(cls, AsyncDevice):
        return FRAMEWORK_ASYNC
    if issubclass(cls, OphydObject):
        return FRAMEWORK_SYNC
    raise ControlError(
        f"{cls.__module__}.{cls.__qualname__} is neither an ophyd-async Device "
        f"nor a classic ophyd class; cannot drive it"
    )


def json_safe(obj: Any) -> Any:
    """Convert a method result into JSON-serializable structures.

    Handles the shapes ophyd verbs actually return: read/describe dicts
    (possibly OrderedDict with numpy values), Status objects, numpy scalars
    and arrays, and ophyd-async ``Location``/dataclass-like values. Unknown
    objects become their ``repr`` — the value is for display, and a repr is
    explicit about being non-structured (vs. dropping the field).
    """
    import numpy as np

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, StatusBase):
        return {"done": obj.done, "success": obj.success}
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    # dataclass-like (e.g. ophyd_async Location) and namedtuples
    if hasattr(obj, "_asdict"):
        return {str(k): json_safe(v) for k, v in obj._asdict().items()}
    import dataclasses

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: json_safe(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return repr(obj)


def _walk_component(device: Any, dotted_path: str) -> Any:
    """attrgetter with a typed error for bad paths."""
    import operator

    try:
        return operator.attrgetter(dotted_path)(device)
    except AttributeError as e:
        raise ComponentNotFoundError(
            f"Device {getattr(device, 'name', '?')!r} has no component " f"{dotted_path!r}: {e}"
        ) from e


def _get_method(target: Any, method: str) -> Any:
    """Look up an allowlisted method on the target, with a clear error.

    The allowlist gate ran earlier; this catches "valid verb, but this
    object doesn't implement it" (e.g. ``trigger`` on a plain signal).
    """
    attr = getattr(target, method, None)
    if attr is None or not callable(attr):
        raise MethodNotAllowedError(
            f"{type(target).__name__} object {getattr(target, 'name', '?')!r} "
            f"does not support method {method!r}"
        )
    return attr


def _log_fire_and_forget(target_name: str, method: str) -> Any:
    """Completion callback for fire-and-forget statuses: failures must surface
    in the log, since no caller is waiting on them."""

    def _cb(status: Any) -> None:
        success = getattr(status, "success", None)
        if success:
            logger.info("device_method_completed", target=target_name, method=method)
        else:
            logger.error(
                "device_method_failed_after_initiation",
                target=target_name,
                method=method,
                status=repr(status),
            )

    return _cb


class ClassicOphydDriver:
    """Drives classic-ophyd objects (pyepics transport) from the asyncio app.

    Every potentially-blocking framework call runs on a worker thread via
    ``asyncio.to_thread`` — the same bridge the monitoring subsystem already
    uses for pyepics.
    """

    framework = FRAMEWORK_SYNC

    async def connect(self, device: Any, timeout: float) -> None:
        await asyncio.to_thread(device.wait_for_connection, timeout=timeout)

    async def invoke(
        self,
        target: Any,
        method: str,
        args: list,
        kwargs: dict,
        timeout: float,
        use_put: bool = False,
    ) -> Any:
        """Call ``method`` on ``target``; wait out any returned Status.

        ``use_put=True`` makes status-returning methods fire-and-forget:
        the action is initiated and the call returns without waiting for
        completion (completion/failure is logged via a status callback).
        """
        target_name = getattr(target, "name", repr(target))
        fn = _get_method(target, method)
        result = await asyncio.to_thread(fn, *args, **kwargs)

        if isinstance(result, StatusBase):
            if use_put:
                result.add_callback(_log_fire_and_forget(target_name, method))
                return {"initiated": True, "done": result.done}
            # Status.wait raises on failure or timeout — errors propagate.
            await asyncio.to_thread(result.wait, timeout)
            return result
        return result

    async def get_component(self, device: Any, dotted_path: str) -> Any:
        """Walk a dotted sub-path to a nested component. Classic ophyd
        materializes lazy Components on first attribute access (which can
        open CA connections), so the walk runs on a worker thread."""
        return await asyncio.to_thread(_walk_component, device, dotted_path)

    async def destroy(self, device: Any) -> None:
        await asyncio.to_thread(device.destroy)


class OphydAsyncDriver:
    """Drives ophyd-async devices (aioca/p4p transports) natively."""

    framework = FRAMEWORK_ASYNC

    async def connect(self, device: Any, timeout: float) -> None:
        await device.connect(timeout=timeout)

    async def invoke(
        self,
        target: Any,
        method: str,
        args: list,
        kwargs: dict,
        timeout: float,
        use_put: bool = False,
    ) -> Any:
        """Call ``method`` on ``target``; await the coroutine/AsyncStatus.

        ophyd-async verbs return either an ``AsyncStatus``-like object
        (``set``/``trigger`` — the underlying task is already running) or a
        coroutine (``read``/``describe``/``get_value``). ``use_put=True``
        returns right after initiation for status-returning methods.
        """
        target_name = getattr(target, "name", repr(target))
        # Map the signal-level verbs onto ophyd-async's names so nested
        # leaves behave like their classic counterparts.
        if method == "get":
            method = "get_value"
        elif method == "put":
            method = "set"
            use_put = True
        fn = _get_method(target, method)
        result: Any = fn(*args, **kwargs)

        if isinstance(result, StatusBase):
            # An ophyd-async device returned a CLASSIC status — framework
            # mix-up; refuse rather than deadlock waiting on the wrong loop.
            raise ControlError(
                f"{target_name}.{method} returned a classic-ophyd Status from "
                f"an ophyd-async driver; the device class is mis-tagged"
            )
        if not inspect.isawaitable(result):
            return result

        # AsyncStatus quacks: awaitable with done/success/add_callback.
        status: Any = result
        is_status = hasattr(status, "add_callback") and hasattr(status, "done")
        if use_put and is_status:
            status.add_callback(_log_fire_and_forget(target_name, method))
            return {"initiated": True, "done": bool(status.done)}
        try:
            awaited = await asyncio.wait_for(_as_coro(result), timeout=timeout)
        except TimeoutError:
            # asyncio's TimeoutError stringifies to "" — substitute a real
            # message so the HTTP error says what actually happened.
            raise ControlError(
                f"{target_name}.{method} did not complete within {timeout}s"
            ) from None
        if is_status:
            # Awaiting an AsyncStatus yields None; report its terminal state.
            return {"done": bool(status.done), "success": bool(status.success)}
        return awaited

    async def get_component(self, device: Any, dotted_path: str) -> Any:
        """Walk a dotted sub-path. ophyd-async children are plain attributes
        (no I/O on access), so this is a direct walk."""
        return _walk_component(device, dotted_path)

    async def destroy(self, device: Any) -> None:
        # ophyd-async devices have no per-device close; aioca/p4p contexts
        # are torn down with the event loop (verified by the startup/shutdown
        # spike for this design).
        return None


async def _as_coro(awaitable: Any) -> Any:
    """Wrap any awaitable so asyncio.wait_for can schedule it as a task."""
    return await awaitable


def driver_for(framework: str) -> ClassicOphydDriver | OphydAsyncDriver:
    if framework == FRAMEWORK_ASYNC:
        return OphydAsyncDriver()
    if framework == FRAMEWORK_SYNC:
        return ClassicOphydDriver()
    raise ControlError(f"Unknown device framework {framework!r}")
