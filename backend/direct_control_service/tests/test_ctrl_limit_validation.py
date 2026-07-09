"""Ctrl-limit safety gate on PV writes.

``set_pv`` refuses to send a numeric value that would land outside the
IOC-advertised ``lower_ctrl_limit`` / ``upper_ctrl_limit`` — the value
never reaches EPICS. Turned on by default (``Settings.check_ctrl_limits``),
opt-out per-request via ``PVSetRequest.check_limits=False``.

Unit coverage lives here because the branching decisions (numeric vs not,
one-sided limits, EPICS "unlimited" convention of both bounds equal to 0,
fail-open on metadata read failure) are pure Python and stubbing pyepics
via ``_read_ctrl_limits`` is orders of magnitude faster than a live IOC
round-trip. The HTTP status mapping (422, no PV-health report) and the
batch integration are also pinned here.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from direct_control.config import Settings
from direct_control.device_controller import DeviceController
from direct_control.models import (
    CoordinationStatus,
    DeviceLockStatus,
    ValueLimitError,
)

# ---------------------------------------------------------------------------
# Unit tests: _validate_ctrl_limits branching
# ---------------------------------------------------------------------------


def _controller(*, check_ctrl_limits: bool = True) -> DeviceController:
    """Build a DeviceController with the coordination + registry deps stubbed.

    ``set_pv`` is not exercised here; only ``_validate_ctrl_limits`` is
    called directly. The stubs exist so pydantic-settings can resolve and
    the controller can be instantiated.
    """
    settings = Settings(check_ctrl_limits=check_ctrl_limits)
    coord = MagicMock()
    coord.check_device_available = AsyncMock(
        return_value=CoordinationStatus(
            device_available=True,
            locked_by=None,
            status=DeviceLockStatus.AVAILABLE,
            timestamp=datetime.now(),
        )
    )
    registry = MagicMock()
    registry.get_owning_device = AsyncMock(return_value=None)
    device_manager = MagicMock()
    return DeviceController(settings, coord, registry, device_manager)


async def _validate(dc: DeviceController, value, *, check_limits=None):
    """Shorthand for the exercised call."""
    await dc._validate_ctrl_limits(
        pv_name="IOC:x",
        value=value,
        check_limits=check_limits,
        connection_timeout=1.0,
    )


async def test_value_inside_limits_passes(monkeypatch):
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0.0, 10.0)))
    await _validate(dc, 5.0)  # no exception


async def test_value_at_lower_bound_is_inclusive(monkeypatch):
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0.0, 10.0)))
    await _validate(dc, 0.0)
    await _validate(dc, 10.0)


async def test_value_below_lower_raises(monkeypatch):
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0.0, 10.0)))
    with pytest.raises(ValueLimitError, match="below lower_ctrl_limit"):
        await _validate(dc, -0.5)


async def test_value_above_upper_raises(monkeypatch):
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0.0, 10.0)))
    with pytest.raises(ValueLimitError, match="above upper_ctrl_limit"):
        await _validate(dc, 11.0)


async def test_epics_unlimited_convention_skips(monkeypatch):
    """Both bounds equal to 0 is EPICS's "no limits enforced" flag; every
    unlimited record advertises exactly this pair. Must not reject any
    value, including negatives."""
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0, 0)))
    await _validate(dc, -1_000_000)
    await _validate(dc, 1_000_000)


async def test_both_limits_none_skips(monkeypatch):
    """A record with no LOPR/HOPR concept (stringout, waveform) — the read
    returns (None, None) and the check bypasses."""
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(None, None)))
    await _validate(dc, 42.0)


async def test_one_sided_lower_only(monkeypatch):
    """Upper unbounded (upper=None). The lower still enforces."""
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(0.0, None)))
    await _validate(dc, 1_000_000)  # upper unbounded, must pass
    with pytest.raises(ValueLimitError, match="below lower_ctrl_limit"):
        await _validate(dc, -0.1)


async def test_one_sided_upper_only(monkeypatch):
    """Lower unbounded (lower=None). The upper still enforces."""
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", AsyncMock(return_value=(None, 10.0)))
    await _validate(dc, -1_000_000)  # lower unbounded, must pass
    with pytest.raises(ValueLimitError, match="above upper_ctrl_limit"):
        await _validate(dc, 10.1)


async def test_string_value_skips(monkeypatch):
    """Strings never trigger the ctrl-limit check (no numeric comparison)."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, "Open")
    # And we didn't even bother reading the limits.
    read.assert_not_called()


async def test_none_value_skips(monkeypatch):
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, None)
    read.assert_not_called()


async def test_list_value_skips(monkeypatch):
    """Waveform writes: skip. Element-wise checking is not attempted."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, [1.0, 2.0, 100.0])
    read.assert_not_called()


async def test_complex_value_skips(monkeypatch):
    """Complex numbers don't compare cleanly against float limits; they fail
    the ``isinstance(value, (int, float))`` guard and bypass the check."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, 1 + 2j)
    read.assert_not_called()


async def test_bool_value_is_checked(monkeypatch):
    """Booleans are meaningful for bo records (limits 0..1); do check them."""
    read = AsyncMock(return_value=(0, 1))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, True)
    await _validate(dc, False)
    assert read.await_count == 2


async def test_per_request_opt_out(monkeypatch):
    """``check_limits=False`` on the request bypasses even when the value is
    out of range — the operator override."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, 100.0, check_limits=False)
    read.assert_not_called()


async def test_per_request_opt_in_overrides_disabled_setting(monkeypatch):
    """``check_limits=True`` forces the check even when the global setting is
    off — the per-request flag wins."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller(check_ctrl_limits=False)
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    with pytest.raises(ValueLimitError):
        await _validate(dc, 11.0, check_limits=True)


async def test_global_setting_off_skips(monkeypatch):
    """``check_ctrl_limits=False`` in Settings bypasses the check."""
    read = AsyncMock(return_value=(0.0, 10.0))
    dc = _controller(check_ctrl_limits=False)
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, 100.0)
    read.assert_not_called()


async def test_read_failure_fails_open(monkeypatch):
    """If the metadata read raises, the write proceeds (the actual put path
    is the authoritative failure point)."""
    read = AsyncMock(side_effect=RuntimeError("simulated ca timeout"))
    dc = _controller()
    monkeypatch.setattr(dc, "_read_ctrl_limits", read)
    await _validate(dc, 100.0)


# ---------------------------------------------------------------------------
# HTTP: 422 mapping + no PV-health report
# ---------------------------------------------------------------------------


class _ControllerRejectsWithLimit:
    """Stub controller whose ``set_pv`` always raises ValueLimitError."""

    async def set_pv(self, request):
        raise ValueLimitError(
            f"PV {request.pv_name!r}: value {request.value} is above upper_ctrl_limit 10.0"
        )


def test_set_pv_returns_422_on_ctrl_limit_rejection(app, client):
    """A ValueLimitError from the controller surfaces as HTTP 422 (not 500)."""
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerRejectsWithLimit()

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 100.0},
    )
    assert r.status_code == 422, r.text
    assert "upper_ctrl_limit" in r.json()["detail"]


async def test_set_pv_ctrl_limit_rejection_skips_pv_health(app, client, install_config_http_stub):
    """A ctrl-limit refusal never reports PV health — the value never left
    the service, so there is no IOC-side health signal to report."""
    from direct_control.main import get_device_controller

    posted: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    install_config_http_stub(_handler)
    app.dependency_overrides[get_device_controller] = lambda: _ControllerRejectsWithLimit()

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 100.0},
    )
    assert r.status_code == 422

    # Give the (nonexistent) fire-and-forget task a chance to run — a
    # regression that DOES report would have posted /pvs/IOC:m1/failure here.
    import asyncio

    await asyncio.sleep(0.05)
    assert posted == [], f"PV-health endpoint was hit by a ctrl-limit rejection: {posted}"


# ---------------------------------------------------------------------------
# Batch: ValueLimitError halts with per-item status_code=422
# ---------------------------------------------------------------------------


class _ControllerRejectsMatchingValue:
    """Stub controller whose ``set_pv`` raises ValueLimitError for a specific
    value and succeeds otherwise. Lets the batch test drive the halt at a
    known item."""

    def __init__(self, reject_value):
        self.reject_value = reject_value

    async def set_pv(self, request):
        from direct_control.models import CommandMode, PVSetResponse

        if request.value == self.reject_value:
            raise ValueLimitError(f"PV {request.pv_name!r}: value out of range")
        return PVSetResponse(
            pv_name=request.pv_name,
            success=True,
            value_set=request.value,
            timestamp=datetime.now(),
            coordination_checked=True,
            mode=CommandMode.FIRE_AND_FORGET,
            message="ok",
        )


def test_batch_set_ctrl_limit_rejection_halts(app, client):
    """One item raising ValueLimitError halts the batch with status_code=422
    on that row; subsequent items are not attempted."""
    from direct_control.main import get_device_controller

    app.dependency_overrides[get_device_controller] = lambda: _ControllerRejectsMatchingValue(
        reject_value=999.0
    )

    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 1.0},
                {"pv_name": "IOC:m1", "value": 999.0},
                {"pv_name": "IOC:counter", "value": 3},  # must not be attempted
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["applied"] == 1
    assert body["requested"] == 3
    assert len(body["results"]) == 2
    assert body["results"][0]["success"] is True
    failure = body["results"][1]
    assert failure["success"] is False
    assert failure["status_code"] == 422
    assert failure["error_type"] == "ValueLimitError"
    assert failure["coordination_checked"] is True


# ---------------------------------------------------------------------------
# _read_ctrl_limits fail-open surface
# ---------------------------------------------------------------------------


async def test_read_ctrl_limits_returns_none_when_pv_wont_connect(monkeypatch):
    """A connect failure returns (None, None) and lets the write proceed to
    its own failure at put time — the gate must not be stricter than the put."""
    dc = _controller()
    monkeypatch.setattr(dc, "_connect", AsyncMock(return_value=None))
    lower, upper = await dc._read_ctrl_limits("IOC:x", connection_timeout=1.0)
    assert lower is None and upper is None


async def test_read_ctrl_limits_returns_none_when_get_ctrlvars_raises(monkeypatch):
    """A pyepics get_ctrlvars exception is swallowed to (None, None); the
    write path fires and surfaces the real failure there."""
    dc = _controller()

    fake_pv = MagicMock()
    fake_pv.get_ctrlvars = MagicMock(side_effect=RuntimeError("simulated"))
    monkeypatch.setattr(dc, "_connect", AsyncMock(return_value=fake_pv))

    lower, upper = await dc._read_ctrl_limits("IOC:x", connection_timeout=1.0)
    assert lower is None and upper is None


# ---------------------------------------------------------------------------
# End-to-end against the test IOC: real ctrl-limit metadata from EPICS
# ---------------------------------------------------------------------------


def test_e2e_ctrl_limit_blocks_out_of_range_write(client):
    """Against the live test IOC: writing 100 to ``IOC:m1_lim`` (limits [0, 10])
    is refused with 422 and the IOC value is unchanged.

    Pins the pyepics integration (``get_ctrlvars`` + ``lower_ctrl_limit`` /
    ``upper_ctrl_limit`` attributes) — the unit tests stub pyepics, this one
    doesn't.
    """
    # Read the initial value to compare later. Skip the caget noise if the
    # PV isn't reachable — the caproto IOC fixture is autouse-scoped but a
    # missing caproto install skips it.
    from epics import caget

    before = caget("IOC:m1_lim", timeout=2.0)
    if before is None:
        pytest.skip("test IOC not reachable; skipping live-IOC ctrl-limit test")

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1_lim", "value": 100.0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 422, r.text
    assert "upper_ctrl_limit" in r.json()["detail"]

    # The IOC value must be unchanged — the value never left the service.
    after = caget("IOC:m1_lim", timeout=2.0)
    assert after == before


def test_e2e_ctrl_limit_allows_in_range_write(client):
    """Against the live test IOC: writing 5 to ``IOC:m1_lim`` (limits [0, 10])
    passes the gate and reaches the IOC (HTTP 200)."""
    from epics import caget

    if caget("IOC:m1_lim", timeout=2.0) is None:
        pytest.skip("test IOC not reachable; skipping live-IOC ctrl-limit test")

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1_lim", "value": 5.0, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_e2e_ctrl_limit_opt_out_bypasses_gate(client):
    """The per-request escape hatch: ``check_limits=false`` bypasses the gate
    and the request reaches the IOC (HTTP 200). Whether the IOC then honors
    the out-of-range write is up to the record's LOPR/HOPR enforcement —
    caproto typically clamps or rejects — but the gate is provably out of
    the way (contrast with the 422 in the default-gate test above)."""
    from epics import caget

    if caget("IOC:m1_lim", timeout=2.0) is None:
        pytest.skip("test IOC not reachable; skipping live-IOC ctrl-limit test")

    r = client.post(
        "/api/v1/pv/set",
        json={
            "pv_name": "IOC:m1_lim",
            "value": 100.0,
            "check_limits": False,
            "wait": True,
            "timeout": 2.0,
        },
    )
    assert r.status_code == 200, r.text
