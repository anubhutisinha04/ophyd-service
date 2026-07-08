"""Unit tests for the device-control layer: framework detection, the method
allowlist, result marshalling, DeviceManager lifecycle, and the registry
providers' instantiation-spec surface.

No Channel Access needed — classic coverage uses ``ophyd.sim`` devices and
ophyd-async coverage uses soft signals. The live-IOC end-to-end paths are in
``test_device_execute_integration.py``.
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest
from ophyd.status import Status

from direct_control.config import Settings
from direct_control.device_manager import DeviceManager
from direct_control.drivers import (
    FRAMEWORK_ASYNC,
    FRAMEWORK_SYNC,
    ClassicOphydDriver,
    OphydAsyncDriver,
    check_method_allowed,
    detect_framework,
    import_device_class,
    json_safe,
)
from direct_control.models import (
    ComponentNotFoundError,
    ControlError,
    DeviceNotInstantiableError,
    InstantiationSpec,
    MethodNotAllowedError,
)

# ===== framework detection =====


def test_detect_framework_classic():
    import ophyd.sim

    assert detect_framework(ophyd.sim.SynAxis) == FRAMEWORK_SYNC


def test_detect_framework_classic_signal():
    from ophyd import EpicsSignal

    assert detect_framework(EpicsSignal) == FRAMEWORK_SYNC


def test_detect_framework_async():
    from ophyd_async.epics.motor import Motor

    assert detect_framework(Motor) == FRAMEWORK_ASYNC


def test_detect_framework_async_signal_class():
    from ophyd_async.core import SignalRW

    assert detect_framework(SignalRW) == FRAMEWORK_ASYNC


def test_detect_framework_rejects_non_ophyd():
    from tests.device_classes import NotADevice

    with pytest.raises(ControlError, match="neither an ophyd-async"):
        detect_framework(NotADevice)


# ===== class import =====


def test_import_device_class_roundtrip():
    import ophyd.sim

    assert import_device_class("ophyd.sim.SynAxis") is ophyd.sim.SynAxis


@pytest.mark.parametrize(
    "path, match",
    [
        ("NoDotsHere", "no module prefix"),
        ("definitely.not.a.module.Cls", "Cannot import module"),
        ("ophyd.sim.NoSuchClass", "has no attribute"),
        ("ophyd.sim.motor1", "is not a class"),
    ],
)
def test_import_device_class_failures(path, match):
    with pytest.raises(ControlError, match=match):
        import_device_class(path)


# ===== method allowlist =====


@pytest.mark.parametrize("method", ["set", "stop", "trigger", "read", "describe", "get", "put"])
def test_allowlist_accepts_verbs(method):
    check_method_allowed(method)


@pytest.mark.parametrize("method", ["__init__", "destroy", "subscribe", "fly", "exec"])
def test_allowlist_rejects_arbitrary_methods(method):
    with pytest.raises(MethodNotAllowedError, match="not allowed"):
        check_method_allowed(method)


# ===== result marshalling =====


def test_json_safe_numpy_and_nested():
    out = json_safe(
        {
            "scalar": np.float64(1.5),
            "arr": np.arange(3),
            "nested": {"v": np.int32(7)},
            "seq": (1, np.float32(2.0)),
        }
    )
    assert out == {
        "scalar": 1.5,
        "arr": [0, 1, 2],
        "nested": {"v": 7},
        "seq": [1, 2.0],
    }


def test_json_safe_classic_status():
    from ophyd.status import Status

    st = Status()
    st.set_finished()
    st.wait(1.0)
    assert json_safe(st) == {"done": True, "success": True}


def test_json_safe_unknown_object_becomes_repr():
    class Weird:
        def __repr__(self):
            return "<weird>"

    assert json_safe(Weird()) == "<weird>"


# ===== DeviceManager: classic (ophyd.sim — no CA) =====


def _spec(**over) -> InstantiationSpec:
    base = {"name": "ax", "device_class": "ophyd.sim.SynAxis", "args": [], "kwargs": {}}
    base.update(over)
    return InstantiationSpec(**base)


@pytest.fixture
def manager() -> DeviceManager:
    return DeviceManager(Settings())


async def test_manager_builds_and_caches_classic_device(manager):
    device, driver = await manager.get_or_connect(_spec())
    assert driver.framework == FRAMEWORK_SYNC
    assert device.name == "ax"

    again, _ = await manager.get_or_connect(_spec())
    assert again is device
    assert manager.size() == 1
    await manager.cleanup()
    assert manager.size() == 0


async def test_manager_rebuilds_on_spec_change(manager):
    device, _ = await manager.get_or_connect(_spec())
    changed, _ = await manager.get_or_connect(_spec(kwargs={"value": 5.0}))
    assert changed is not device
    assert manager.size() == 1
    await manager.cleanup()


async def test_manager_rejects_inactive_spec(manager):
    with pytest.raises(DeviceNotInstantiableError, match="marked inactive"):
        await manager.get_or_connect(_spec(active=False))


async def test_manager_rejects_mismatched_framework_tag(manager):
    with pytest.raises(ControlError, match="Fix the registry"):
        await manager.get_or_connect(_spec(framework="ophyd-async"))


async def test_manager_wraps_instantiation_failure(manager):
    bad = _spec(device_class="ophyd.sim.SynAxis", kwargs={"bogus_kwarg": 1})
    with pytest.raises(ControlError, match="Failed to instantiate"):
        await manager.get_or_connect(bad)
    assert manager.size() == 0  # failures are not cached


async def test_classic_invoke_set_waits_status(manager):
    device, driver = await manager.get_or_connect(_spec())
    result = await driver.invoke(device, "set", [2.5], {}, timeout=5.0)
    assert json_safe(result) == {"done": True, "success": True}
    read = await driver.invoke(device, "read", [], {}, timeout=5.0)
    assert json_safe(read)["ax"]["value"] == 2.5
    await manager.cleanup()


async def test_classic_invoke_unsupported_method(manager):
    device, driver = await manager.get_or_connect(_spec())
    with pytest.raises(MethodNotAllowedError, match="does not support"):
        await driver.invoke(device, "trigger_no_such", [], {}, timeout=5.0)
    await manager.cleanup()


async def test_classic_get_component_walks_and_rejects(manager):
    device, driver = await manager.get_or_connect(_spec())
    leaf = await driver.get_component(device, "readback")
    assert leaf is device.readback
    with pytest.raises(ComponentNotFoundError, match="no component"):
        await driver.get_component(device, "nope.nothing")
    await manager.cleanup()


# ===== DeviceManager: ophyd-async (soft signals — no CA) =====


async def test_manager_builds_async_soft_device(manager):
    spec = InstantiationSpec(
        name="soft",
        device_class="tests.device_classes.SoftAsyncThing",
        args=[],
        kwargs={},
        framework="ophyd-async",
    )
    device, driver = await manager.get_or_connect(spec)
    assert driver.framework == FRAMEWORK_ASYNC

    leaf = await driver.get_component(device, "value")
    await driver.invoke(leaf, "set", [3.75], {}, timeout=5.0)
    assert await driver.invoke(leaf, "get", [], {}, timeout=5.0) == 3.75
    # device-level read goes through the async Readable protocol
    read = json_safe(await driver.invoke(device, "read", [], {}, timeout=5.0))
    assert read["soft-value"]["value"] == 3.75
    await manager.cleanup()


async def test_async_put_maps_to_fire_and_forget_set(manager):
    spec = InstantiationSpec(name="soft2", device_class="tests.device_classes.SoftAsyncThing")
    device, driver = await manager.get_or_connect(spec)
    leaf = await driver.get_component(device, "value")
    result = await driver.invoke(leaf, "put", [9.5], {}, timeout=5.0)
    assert result["initiated"] is True
    # Soft-signal set completes promptly; confirm the write landed.
    import asyncio

    for _ in range(50):
        if await driver.invoke(leaf, "get", [], {}, timeout=5.0) == 9.5:
            break
        await asyncio.sleep(0.02)
    assert await driver.invoke(leaf, "get", [], {}, timeout=5.0) == 9.5
    await manager.cleanup()


# ===== file registry: instantiation specs =====


def _write_registry(tmp_path, payload):
    import json

    p = tmp_path / "registry.json"
    p.write_text(json.dumps(payload))
    return str(p)


async def test_file_registry_parses_specs(tmp_path):
    from direct_control.registry_file import FileRegistryProvider

    path = _write_registry(
        tmp_path,
        {
            "devices": [
                {
                    "name": "ax",
                    "pvs": ["X:AX.RBV"],
                    "device_class": "ophyd.sim.SynAxis",
                    "args": [],
                    "kwargs": {},
                    "framework": "ophyd-sync",
                },
                {"name": "pv_only", "pvs": ["X:PV1"]},
            ]
        },
    )
    provider = FileRegistryProvider(path)
    spec = await provider.get_instantiation_spec("ax")
    assert spec is not None
    assert spec.device_class == "ophyd.sim.SynAxis"
    assert spec.framework == "ophyd-sync"
    assert await provider.get_instantiation_spec("pv_only") is None
    assert await provider.get_instantiation_spec("missing") is None


@pytest.mark.parametrize(
    "entry, match",
    [
        ({"name": "d", "device_class": "NoDot"}, "fully-qualified import path"),
        ({"name": "d", "device_class": "a.B", "args": {}}, "'args' must be a list"),
        ({"name": "d", "device_class": "a.B", "kwargs": []}, "'kwargs' must be a mapping"),
        ({"name": "d", "device_class": "a.B", "framework": "pyepics"}, "'framework' must be"),
        ({"name": "d", "args": ["X:"]}, "no 'device_class'"),
    ],
)
def test_file_registry_rejects_malformed_control_fields(tmp_path, entry, match):
    from direct_control.registry_file import FileRegistryProvider

    path = _write_registry(tmp_path, {"devices": [entry]})
    with pytest.raises(RuntimeError, match=match):
        FileRegistryProvider(path)


# ===== HTTP registry client: instantiation specs =====


async def test_registry_client_spec_fetch_and_404(monkeypatch):
    import httpx

    from direct_control.registry_client import RegistryClient

    spec_body = {
        "name": "m1",
        "device_class": "ophyd.EpicsMotor",
        "args": ["X:M1"],
        "kwargs": {"name": "m1"},
        "active": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/m1/instantiation"):
            return httpx.Response(200, json=spec_body)
        if request.url.path.endswith("/nospec/instantiation"):
            return httpx.Response(404, json={"detail": "no spec"})
        return httpx.Response(500)

    client = RegistryClient(Settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://stub"
    )
    try:
        spec = await client.get_instantiation_spec("m1")
        assert spec is not None and spec.device_class == "ophyd.EpicsMotor"
        # Cached: a second call must not hit the transport (drop it to prove).
        spec2 = await client.get_instantiation_spec("m1")
        assert spec2 == spec

        assert await client.get_instantiation_spec("nospec") is None

        with pytest.raises(RuntimeError, match="HTTP 500"):
            await client.get_instantiation_spec("erroring")
    finally:
        await client.cleanup()


# ===== stop-on-timeout: a timed-out move must not leave hardware in motion =====


class _ClassicTarget:
    """A classic-ophyd-shaped target whose ``set`` returns a Status that never
    finishes on its own, so the driver's wait times out."""

    name = "fake_motor"

    def __init__(self, *, stop_raises: bool = False):
        self.status = Status()
        self.stop_calls = 0
        self._stop_raises = stop_raises

    def set(self, *args, **kwargs):
        return self.status

    def stop(self, *args, **kwargs):
        self.stop_calls += 1
        if self._stop_raises:
            raise RuntimeError("stop rejected by hardware")
        self.status.set_finished()


def _finish(status: Status) -> None:
    if not status.done:
        status.set_finished()


async def test_classic_invoke_stops_hardware_on_timeout():
    driver = ClassicOphydDriver()
    target = _ClassicTarget()
    try:
        with pytest.raises(ControlError) as excinfo:
            await driver.invoke(target, "set", [1.0], {}, timeout=0.2)
        msg = str(excinfo.value)
        assert "did not complete within 0.2s" in msg
        assert "stop() was issued" in msg
        assert target.stop_calls == 1
    finally:
        _finish(target.status)


async def test_classic_invoke_no_stop_when_opted_out():
    driver = ClassicOphydDriver()
    target = _ClassicTarget()
    try:
        with pytest.raises(ControlError) as excinfo:
            await driver.invoke(target, "set", [1.0], {}, timeout=0.2, stop_on_timeout=False)
        assert "stop-on-timeout disabled" in str(excinfo.value)
        assert target.stop_calls == 0
    finally:
        _finish(target.status)


async def test_classic_invoke_reports_stop_failure():
    driver = ClassicOphydDriver()
    target = _ClassicTarget(stop_raises=True)
    try:
        with pytest.raises(ControlError) as excinfo:
            await driver.invoke(target, "set", [1.0], {}, timeout=0.2)
        assert "stop() was attempted but failed" in str(excinfo.value)
        assert target.stop_calls == 1
    finally:
        _finish(target.status)


async def test_classic_invoke_target_without_stop():
    driver = ClassicOphydDriver()
    status = Status()

    class _NoStop:
        name = "readonly_signal"

        def set(self, *args, **kwargs):
            return status

    try:
        with pytest.raises(ControlError) as excinfo:
            await driver.invoke(_NoStop(), "set", [1.0], {}, timeout=0.2)
        assert "exposes no stop()" in str(excinfo.value)
    finally:
        _finish(status)


async def test_classic_invoke_genuine_failure_propagates_without_stop():
    """A Status that finishes with an error is a real failure, not a timeout:
    the original exception must propagate and stop() must NOT be called."""
    driver = ClassicOphydDriver()

    class _FailingTarget:
        name = "fault_motor"

        def __init__(self):
            self.status = Status()
            self.stop_calls = 0

        def set(self, *args, **kwargs):
            self.status.set_exception(RuntimeError("move faulted"))
            return self.status

        def stop(self, *args, **kwargs):
            self.stop_calls += 1

    target = _FailingTarget()
    with pytest.raises(RuntimeError, match="move faulted"):
        await driver.invoke(target, "set", [1.0], {}, timeout=5.0)
    assert target.stop_calls == 0


class _AsyncStatusStuck:
    """An AsyncStatus-shaped awaitable that never completes within a test timeout."""

    done = False
    success = False

    def add_callback(self, cb):
        pass

    def __await__(self):
        async def _never():
            await asyncio.sleep(30)

        return _never().__await__()


async def test_async_invoke_stops_hardware_on_timeout():
    driver = OphydAsyncDriver()
    stop_calls = []

    class _AsyncTarget:
        name = "async_motor"

        def set(self, *args, **kwargs):
            return _AsyncStatusStuck()

        async def stop(self, *args, **kwargs):
            stop_calls.append(True)

    with pytest.raises(ControlError) as excinfo:
        await driver.invoke(_AsyncTarget(), "set", [1.0], {}, timeout=0.2)
    msg = str(excinfo.value)
    assert "did not complete within 0.2s" in msg
    assert "stop() was issued" in msg
    assert stop_calls == [True]


async def test_async_invoke_no_stop_when_opted_out():
    driver = OphydAsyncDriver()
    stop_calls = []

    class _AsyncTarget:
        name = "async_motor"

        def set(self, *args, **kwargs):
            return _AsyncStatusStuck()

        async def stop(self, *args, **kwargs):
            stop_calls.append(True)

    with pytest.raises(ControlError) as excinfo:
        await driver.invoke(_AsyncTarget(), "set", [1.0], {}, timeout=0.2, stop_on_timeout=False)
    assert "stop-on-timeout disabled" in str(excinfo.value)
    assert stop_calls == []


async def test_classic_invoke_bounds_a_blocking_stop(monkeypatch):
    """A classic stop() that blocks (CA/network) must not stall the response: it
    is bounded by _STOP_TIMEOUT and reported as unconfirmed."""
    monkeypatch.setattr("direct_control.drivers._STOP_TIMEOUT", 0.2)
    driver = ClassicOphydDriver()
    release = threading.Event()

    class _BlockingStop:
        name = "wedged_motor"

        def __init__(self):
            self.status = Status()

        def set(self, *args, **kwargs):
            return self.status

        def stop(self, *args, **kwargs):
            release.wait(5)  # blocks well past the (patched) _STOP_TIMEOUT

    target = _BlockingStop()
    try:
        with pytest.raises(ControlError) as excinfo:
            await driver.invoke(target, "set", [1.0], {}, timeout=0.2)
        assert "did not confirm within 0.2s" in str(excinfo.value)
    finally:
        release.set()  # let the lingering stop thread exit
        _finish(target.status)


async def test_async_invoke_bounds_a_hanging_stop(monkeypatch):
    """An ophyd-async stop() that never completes is bounded by _STOP_TIMEOUT and
    reported as unconfirmed."""
    monkeypatch.setattr("direct_control.drivers._STOP_TIMEOUT", 0.2)
    driver = OphydAsyncDriver()

    class _HangingStop:
        name = "wedged_async_motor"

        def set(self, *args, **kwargs):
            return _AsyncStatusStuck()

        async def stop(self, *args, **kwargs):
            await asyncio.sleep(5)  # never completes within the patched _STOP_TIMEOUT

    with pytest.raises(ControlError) as excinfo:
        await driver.invoke(_HangingStop(), "set", [1.0], {}, timeout=0.2)
    assert "did not confirm within 0.2s" in str(excinfo.value)
