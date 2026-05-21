"""Unit tests for ``path_resolver.resolve``.

Tests against real ophyd classes (``EpicsSignal``, ``EpicsMotor``, ``EpicsMCA``)
so the assertions exercise the actual Component / DynamicDeviceComponent /
FormattedComponent walks. A small local class covers the
FormattedComponent-with-placeholder case in isolation, independent of any
site-specific shim.
"""

from __future__ import annotations

import pytest

from configuration_service.path_resolver import (
    Outcome,
    Resolution,
    resolve,
)


# ---------------------------------------------------------------------------
# Top-level flat happi entries (device IS the leaf signal)
# ---------------------------------------------------------------------------


def test_resolve_top_level_epics_signal_returns_prefix():
    r = resolve(
        "sample_sclr_gain",
        device_class_path="ophyd.signal.EpicsSignal",
        prefix="XF:23ID2-ES{CurrAmp:3}Gain:Val-SP",
    )
    assert r.ok
    assert r.outcome is Outcome.RESOLVED
    assert r.pv_name == "XF:23ID2-ES{CurrAmp:3}Gain:Val-SP"


def test_resolve_top_level_epics_signal_ro_returns_prefix():
    r = resolve(
        "ring_curr",
        device_class_path="ophyd.signal.EpicsSignalRO",
        prefix="XF:23ID-SR{}I-I",
    )
    assert r.ok
    assert r.pv_name == "XF:23ID-SR{}I-I"


# ---------------------------------------------------------------------------
# Component walks (standard ophyd built-ins)
# ---------------------------------------------------------------------------


def test_resolve_epics_motor_user_setpoint():
    r = resolve(
        "au_mesh.user_setpoint",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr",
    )
    assert r.ok
    assert r.pv_name == "XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr.VAL"


def test_resolve_epics_motor_user_readback():
    r = resolve(
        "au_mesh.user_readback",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr",
    )
    assert r.ok
    assert r.pv_name == "XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr.RBV"


def test_resolve_epics_motor_velocity():
    r = resolve(
        "au_mesh.velocity",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr",
    )
    assert r.ok
    assert r.pv_name == "XF:23ID2-BI{AuMesh:1-Ax:Y}Mtr.VELO"


# ---------------------------------------------------------------------------
# DynamicDeviceComponent walks (EpicsMCA.rois.roiN.lo_chan / hi_chan)
# ---------------------------------------------------------------------------


def test_resolve_through_dynamic_device_component():
    """EpicsMCA exposes ``rois`` as a DDC; the resolver must descend cleanly
    into roiN sub-components and out to the leaf signals (lo_chan/hi_chan)."""
    r = resolve(
        "mca.rois.roi2.lo_chan",
        device_class_path="ophyd.mca.EpicsMCA",
        prefix="XF:23ID2-ES{Vortex}mca1",
    )
    assert r.ok, r.message
    assert r.pv_name == "XF:23ID2-ES{Vortex}mca1.R2LO"


def test_resolve_through_dynamic_device_component_high_channel():
    r = resolve(
        "mca.rois.roi4.hi_chan",
        device_class_path="ophyd.mca.EpicsMCA",
        prefix="XF:23ID2-ES{Vortex}mca1",
    )
    assert r.ok, r.message
    assert r.pv_name == "XF:23ID2-ES{Vortex}mca1.R4HI"


def test_resolve_epics_mca_preset_real_time():
    r = resolve(
        "mca.preset_real_time",
        device_class_path="ophyd.mca.EpicsMCA",
        prefix="XF:23ID2-ES{Vortex}mca1",
    )
    assert r.ok
    assert r.pv_name == "XF:23ID2-ES{Vortex}mca1.PRTM"


# ---------------------------------------------------------------------------
# FormattedComponent — interpolation case
# ---------------------------------------------------------------------------


def _make_class_with_fmt_cpt():
    """Build a tiny class that uses FormattedComponent with interpolation.

    Isolates the FmtCpt-with-placeholder behavior from any site-specific
    shim so the test doesn't depend on `ios_devs` / `bmm_devs` etc.
    """
    from ophyd import Component as Cpt, Device, EpicsSignal, FormattedComponent as FmtCpt

    class Inner(Device):
        readback = Cpt(EpicsSignal, "Pos-I")
        # Interpolated suffix — needs a live parent to resolve.
        actuate = FmtCpt(EpicsSignal, "{self.parent.prefix}MOVE_CMD.PROC")

    class Outer(Device):
        inner = Cpt(Inner, "-Ax:Z}")

    return Outer


def test_resolve_formatted_component_with_interpolation_returns_needs_enrichment():
    """``Outer`` is defined inside a function so importlib can't reach it by
    name; exercise the walker directly to verify the FmtCpt-placeholder path."""
    cls = _make_class_with_fmt_cpt()
    from configuration_service.path_resolver import _walk_class

    outcome, where, msg = _walk_class(cls, ["inner", "actuate"])
    assert outcome is Outcome.NEEDS_ENRICHMENT
    assert "actuate" in where
    assert "{self.parent.prefix}" in (msg or "")


def test_resolve_formatted_component_with_literal_suffix_resolves():
    """``FmtCpt`` whose suffix has no ``{}`` placeholder is treated like a
    plain Component — it carries an absolute or literal suffix."""
    from ophyd import Device, EpicsSignal, FormattedComponent as FmtCpt
    from configuration_service.path_resolver import _walk_class

    class _Literal(Device):
        # add_prefix=() means "this is an absolute PV, ignore parent prefix";
        # the suffix has no {}-interpolation so static resolution works.
        absolute = FmtCpt(EpicsSignal, "ABSOLUTE:PV", add_prefix=())

    outcome, suffix, _ = _walk_class(_Literal, ["absolute"])
    assert outcome is Outcome.RESOLVED
    assert suffix == "ABSOLUTE:PV"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_resolve_unknown_module_returns_import_failed():
    r = resolve(
        "foo.bar",
        device_class_path="nonexistent_module_xyz.SomeClass",
        prefix="X:Y",
    )
    assert not r.ok
    assert r.outcome is Outcome.IMPORT_FAILED
    assert "nonexistent_module_xyz" in (r.message or "")


def test_resolve_unknown_class_in_known_module_returns_import_failed():
    r = resolve(
        "foo.bar",
        device_class_path="ophyd.NotAClassHere",
        prefix="X:Y",
    )
    assert not r.ok
    assert r.outcome is Outcome.IMPORT_FAILED
    assert "NotAClassHere" in (r.message or "")


def test_resolve_device_class_path_without_module_prefix_fails():
    r = resolve(
        "foo",
        device_class_path="JustAClassNoModule",
        prefix="X:Y",
    )
    assert r.outcome is Outcome.IMPORT_FAILED


def test_resolve_bad_attribute_returns_no_such_attr():
    r = resolve(
        "au_mesh.does_not_exist",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="X:Y",
    )
    assert not r.ok
    assert r.outcome is Outcome.NO_SUCH_ATTR
    assert "does_not_exist" in (r.message or "")


def test_resolve_bad_nested_attribute_includes_path_context():
    """When the walk gets partway and then hits a bad segment, the error
    should name both the bad segment and where we got stuck."""
    r = resolve(
        # rois is a real DDC on EpicsMCA; "foo" isn't a real ROI key.
        "mca.rois.foo",
        device_class_path="ophyd.mca.EpicsMCA",
        prefix="X:Y",
    )
    assert r.outcome is Outcome.NO_SUCH_ATTR
    assert "foo" in (r.message or "")
    assert "rois" in (r.message or "")


def test_resolve_non_component_attribute_returns_no_such_attr():
    """Method names and class-level non-Component attrs should not resolve."""
    r = resolve(
        # EpicsMotor.move() is a method, not a Component.
        "m.move",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="X:Y",
    )
    assert r.outcome is Outcome.NO_SUCH_ATTR


# ---------------------------------------------------------------------------
# Resolution dataclass behavior
# ---------------------------------------------------------------------------


def test_resolution_is_immutable_and_ok_property_works():
    r = Resolution(address="a", outcome=Outcome.RESOLVED, pv_name="X")
    assert r.ok
    r2 = Resolution(address="a", outcome=Outcome.NO_SUCH_ATTR, message="msg")
    assert not r2.ok
    with pytest.raises(Exception):
        r.address = "b"  # type: ignore[misc]  # frozen dataclass


# ---------------------------------------------------------------------------
# ophyd-async (instantiate-without-connect + walk .source URIs)
# ---------------------------------------------------------------------------


def test_resolve_ophyd_async_motor_user_setpoint():
    """ophyd-async ``Motor`` constructs signals in ``__init__``; the
    resolver must instantiate, walk, and strip the ``ca://`` scheme."""
    r = resolve(
        "m.user_setpoint",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert r.ok, r.message
    assert r.pv_name == "XF:ASY{MOT}.VAL"


def test_resolve_ophyd_async_motor_user_readback():
    r = resolve(
        "m.user_readback",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert r.ok
    assert r.pv_name == "XF:ASY{MOT}.RBV"


def test_resolve_ophyd_async_motor_velocity():
    r = resolve(
        "m.velocity",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert r.ok
    assert r.pv_name == "XF:ASY{MOT}.VELO"


def test_resolve_ophyd_async_motor_bad_attr():
    r = resolve(
        "m.does_not_exist",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert r.outcome is Outcome.NO_SUCH_ATTR
    assert "does_not_exist" in (r.message or "")


def test_resolve_ophyd_async_top_level_is_meaningless():
    """ophyd-async devices have many signals; addressing by device name
    alone has no canonical PV. Return ``NO_SUCH_ATTR`` with a helpful
    hint so the caller learns to ask for a sub-attribute."""
    r = resolve(
        "m",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert r.outcome is Outcome.NO_SUCH_ATTR
    assert "sub-attribute" in (r.message or "")


def test_resolve_ophyd_async_strips_pva_scheme():
    """Custom async device whose source is ``pva://...`` — scheme strip
    is independent of which transport the backend chose."""
    from configuration_service.path_resolver import _strip_signal_source_scheme

    assert _strip_signal_source_scheme("ca://X:Y") == "X:Y"
    assert _strip_signal_source_scheme("pva://X:Y") == "X:Y"
    assert _strip_signal_source_scheme("X:Y") == "X:Y"  # already bare


def test_resolve_dispatches_to_correct_framework():
    """Sanity check: same address shape against classic vs async resolves
    through the right walker. Different prefixes prove both paths ran."""
    classic = resolve(
        "m.user_setpoint",
        device_class_path="ophyd.epics_motor.EpicsMotor",
        prefix="XF:CLA{MOT}",
    )
    async_ = resolve(
        "m.user_setpoint",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:ASY{MOT}",
    )
    assert classic.ok and classic.pv_name == "XF:CLA{MOT}.VAL"
    assert async_.ok and async_.pv_name == "XF:ASY{MOT}.VAL"


def test_resolve_ophyd_async_device_cache_reuses_instance():
    """A batch of sibling addresses on the same (class, prefix) should
    instantiate the device once and reuse it."""
    from ophyd_async.epics.motor import Motor

    cache: dict = {}
    r1 = resolve(
        "m.user_setpoint",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:CACHE{MOT}",
        device_cache=cache,
    )
    r2 = resolve(
        "m.velocity",
        device_class_path="ophyd_async.epics.motor.Motor",
        prefix="XF:CACHE{MOT}",
        device_cache=cache,
    )
    assert r1.ok and r2.ok
    assert len(cache) == 1
    # The cached value is the (device, error) tuple; both addresses see
    # the same device instance.
    cached_device, cached_err = cache[(Motor, "XF:CACHE{MOT}")]
    assert cached_err is None
    assert cached_device is not None


def test_resolve_ophyd_async_device_cache_caches_failures():
    """Instantiation failure should be cached so subsequent addresses on
    the same (cls, prefix) short-circuit without retrying."""
    from ophyd_async.core import Device as AsyncDevice
    from configuration_service.path_resolver import _get_or_create_async_device

    construction_calls = 0

    class _AlwaysFails(AsyncDevice):
        def __init__(self, prefix, name=""):
            nonlocal construction_calls
            construction_calls += 1
            raise RuntimeError("simulated failure")

    cache: dict = {}
    device1, err1 = _get_or_create_async_device(_AlwaysFails, "X:Y", cache)
    device2, err2 = _get_or_create_async_device(_AlwaysFails, "X:Y", cache)
    assert construction_calls == 1  # second call short-circuited
    assert device1 is None and device2 is None
    assert err1 == err2 and "simulated failure" in err1


def test_resolve_ophyd_async_instantiation_failure_returns_import_failed():
    """A class that needs extra kwargs raises during instantiation;
    the resolver translates this to ``IMPORT_FAILED`` with the reason."""
    # We can't easily import a real "needs extra args" class without
    # building one, so test the path with a class that requires kwargs.
    # Define a local class inside the function so it's isolated.
    from ophyd_async.core import Device as AsyncDevice
    from configuration_service.path_resolver import _resolve_ophyd_async

    class _NeedsExtraArg(AsyncDevice):
        def __init__(self, prefix, name="", required_arg=None):
            if required_arg is None:
                raise ValueError("required_arg must be provided")
            super().__init__(name=name)

    r = _resolve_ophyd_async("d.foo", _NeedsExtraArg, "X:Y", "foo")
    assert r.outcome is Outcome.IMPORT_FAILED
    assert "required_arg" in (r.message or "")
