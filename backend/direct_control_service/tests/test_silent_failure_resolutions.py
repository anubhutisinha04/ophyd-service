"""
Regression tests for silent-failure-mode fixes from the 2026-05-01 audit.

Each test pins down a specific shape of the bug we just fixed: the API used
to report ``200 OK`` (or ``success=True``) on a path that was actually a
no-op or an error. A regression here would mean the silent-failure pattern
has crept back in.

Group by audit finding ID (S1, S2, ...) so it's clear what each test
guards. New tests added as we work through more findings — extend this
file rather than scattering them across other test files.
"""

from __future__ import annotations

import pytest

from direct_control.models import ServiceAvailability


# ─── S1: device-method placeholders no longer return 200-OK / success=False ──
#
# Pre-fix: ``/stop`` and ``/execute`` returned ``200 OK`` with
# ``success=False`` deep inside the body. Operators saw 200 and assumed
# the device stopped; it didn't. After the fix, the controller raises
# ``NotImplementedError`` and the HTTP layer maps it to ``501 Not
# Implemented`` with a clear "requires ophyd integration" message.


def test_s1_stop_endpoint_returns_501_not_200_with_success_false(client):
    """``POST /api/v1/device/{name}/stop`` must surface unimplemented as 501.

    Pre-fix bug: 200 OK with ``success=False`` in the JSON body meant a
    safety-critical /stop call looked successful while doing nothing.
    """
    r = client.post("/api/v1/device/some_device/stop")
    assert r.status_code == 501, (
        f"expected 501 Not Implemented, got {r.status_code} {r.text!r}. "
        "If this is 200 with success=False, the silent-failure pattern is back."
    )
    detail = r.json()["detail"].lower()
    assert "not yet implemented" in detail
    assert "ophyd" in detail or "configuration service" in detail


def test_s1_execute_device_method_returns_501(client):
    """``POST /api/v1/device/execute`` must surface unimplemented as 501."""
    r = client.post(
        "/api/v1/device/execute",
        json={
            "device_name": "some_device",
            "method": "trigger",
            "args": [],
            "kwargs": {},
        },
    )
    assert r.status_code == 501, (
        f"expected 501, got {r.status_code} {r.text!r}"
    )
    assert "not yet implemented" in r.json()["detail"].lower()


def test_s1_nested_device_set_returns_501(client):
    """``POST /api/v1/device/{path}`` with method=set must surface unimplemented as 501."""
    r = client.post(
        "/api/v1/device/some_device.user_setpoint",
        json={"method": "set", "value": 1.0, "timeout": None},
    )
    assert r.status_code == 501
    detail = r.json()["detail"].lower()
    assert "not yet implemented" in detail


def test_s1_nested_device_read_returns_501(client):
    """``POST /api/v1/device/{path}`` with method=read must surface unimplemented as 501.

    Pre-fix the read branch returned a placeholder dict via 200 OK; a
    careless frontend would render that as a real value. Failing the read
    loudly forces the integration question to surface immediately.
    """
    r = client.post(
        "/api/v1/device/some_device.user_readback",
        json={"method": "read", "value": None, "timeout": None},
    )
    assert r.status_code == 501
    assert "not yet implemented" in r.json()["detail"].lower()


def test_s1_get_nested_device_value_returns_501(client):
    """``GET /api/v1/device/{path}/value`` (read-only) must also surface unimplemented as 501."""
    r = client.get("/api/v1/device/some_device.user_readback/value")
    assert r.status_code == 501
    assert "not yet implemented" in r.json()["detail"].lower()


def test_s1_stop_lock_gate_still_fires_before_not_implemented(client):
    """The coord gate must run before the not-implemented placeholder.

    A disabled or locked device should produce 409/423 (so the operator
    knows to re-enable / wait), NOT 501. Pre-fix this worked by
    coincidence because the placeholder was unreachable for blocked
    devices; pin it down so a future refactor can't accidentally invert
    the order.
    """
    from datetime import datetime
    from direct_control.models import CoordinationStatus, DeviceLockStatus

    class _DisabledStub:
        async def check_device_available(self, device_name: str) -> CoordinationStatus:
            return CoordinationStatus(
                device_available=False,
                locked_by=None,
                status=DeviceLockStatus.DISABLED,
                timestamp=datetime.now(),
            )

        async def is_service_available(self) -> ServiceAvailability:
            return ServiceAvailability(available=True)

        async def cleanup(self) -> None:
            return None

    app = client.app
    stub = _DisabledStub()
    app.state.coordination_client = stub
    app.state.device_controller.coordination = stub

    r = client.post("/api/v1/device/some_device/stop")
    assert r.status_code == 409, (
        f"expected 409 (disabled gate fires before not-implemented), got "
        f"{r.status_code} {r.text!r}"
    )


# ─── S2: set_pv no longer returns 200-OK with success=False on EPICS errors ──
#
# Pre-fix: ``set_pv`` caught every exception, returned
# ``PVSetResponse(success=False, value_set=<requested>)`` and the HTTP
# layer returned that as 200 OK. ``value_set=<requested>`` actively
# misled callers — it advertised the requested value as if it had been
# written. After the fix, errors raise ``ControlError`` and the HTTP
# layer maps to 500 (or a more specific status from the typed handlers
# above it).


def test_s2_set_pv_eipcs_failure_returns_5xx_not_200_with_success_false(client):
    """An EPICS write failure must surface as a 5xx, not 200-with-success=False.

    Drives the failure by writing to a PV name that the test IOC does not
    serve, with a short connection_timeout so the test stays fast. Pre-fix
    this returned 200 OK with ``success=False`` in the body — exactly the
    "looks healthy but isn't" shape the user has been burnt by.
    """
    r = client.post(
        "/api/v1/pv/set",
        json={
            "pv_name": "NOPE:DOES:NOT:EXIST",
            "value": 1.0,
            "wait": True,
            "timeout": 0.5,
            "connection_timeout": 0.5,
        },
    )
    assert r.status_code >= 500, (
        f"expected 5xx for an EPICS failure, got {r.status_code} {r.text!r}. "
        "Returning 200 with success=False is the silent-failure pattern."
    )
    # And just to be explicit: the body must not be a PVSetResponse with
    # success=False masquerading as a 200.
    if r.status_code == 200:
        body = r.json()
        assert body.get("success") is True, (
            "200 OK with success=False is the audited silent-failure pattern."
        )


@pytest.mark.asyncio
async def test_s2_set_pv_controller_raises_control_error_on_put_false():
    """Direct unit test: when ``_execute_put`` returns False, ``set_pv`` raises.

    Bypasses HTTP so the contract on the controller method itself is
    pinned: it must NEVER return ``PVSetResponse(success=False, ...)``.
    Earlier, this exact shape was the bug — a failed write quietly
    returned a "result" envelope advertising the requested value.
    """
    from datetime import datetime
    from unittest.mock import AsyncMock

    from direct_control.config import Settings
    from direct_control.device_controller import DeviceController
    from direct_control.models import (
        ControlError,
        CoordinationStatus,
        DeviceLockStatus,
        PVSetRequest,
    )

    class _AvailableCoord:
        async def check_device_available(self, device_name: str) -> CoordinationStatus:
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )

        async def is_service_available(self) -> ServiceAvailability:
            return ServiceAvailability(available=True)

        async def cleanup(self) -> None:
            return None

    class _StubRegistry:
        async def get_owning_device(self, pv_name: str):
            return None

    settings = Settings()
    controller = DeviceController(settings, _AvailableCoord(), _StubRegistry())  # type: ignore[arg-type]
    # Force the put to "fail" without touching EPICS at all.
    controller._execute_put = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(ControlError, match="Failed to set PV"):
        await controller.set_pv(PVSetRequest(pv_name="ANY:PV", value=1.0, wait=True))


@pytest.mark.asyncio
async def test_s2_set_pv_controller_propagates_inner_exceptions():
    """If ``_execute_put`` itself raises, ``set_pv`` must let it propagate.

    Pre-fix the inner ``except Exception`` swallowed everything and
    returned ``success=False``; this test pins down that any inner
    failure now surfaces.
    """
    from datetime import datetime
    from unittest.mock import AsyncMock

    from direct_control.config import Settings
    from direct_control.device_controller import DeviceController
    from direct_control.models import (
        CoordinationStatus,
        DeviceLockStatus,
        PVSetRequest,
    )

    class _AvailableCoord:
        async def check_device_available(self, device_name: str) -> CoordinationStatus:
            return CoordinationStatus(
                device_available=True,
                locked_by=None,
                status=DeviceLockStatus.AVAILABLE,
                timestamp=datetime.now(),
            )

        async def is_service_available(self) -> ServiceAvailability:
            return ServiceAvailability(available=True)

        async def cleanup(self) -> None:
            return None

    class _StubRegistry:
        async def get_owning_device(self, pv_name: str):
            return None

    settings = Settings()
    controller = DeviceController(settings, _AvailableCoord(), _StubRegistry())  # type: ignore[arg-type]
    controller._execute_put = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("simulated EPICS write blew up"),
    )

    with pytest.raises(RuntimeError, match="simulated EPICS write blew up"):
        await controller.set_pv(PVSetRequest(pv_name="ANY:PV", value=1.0, wait=True))


# ─── S1: WebSocket path delivers error envelope (was stop_complete success=False) ──


def test_s1_ws_pv_socket_stop_emits_error_envelope(client):
    """``{action: stop}`` on the PV-socket must produce an error envelope.

    Pre-fix the WS handler called ``execute_device_method``, got back a
    placeholder ``DeviceCommandResponse(success=False, ...)`` and emitted
    a ``stop_complete`` event with ``success=False``. A WS client that
    only watched for ``type=="error"`` would miss the failure entirely.
    After the fix, ``execute_device_method`` raises and the WS handler
    routes the exception into ``send_error``.
    """
    import time

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "stop", "device": "some_device"})

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("type") == "error":
                # finch reads ``error`` (not ``message``) per
                # ``finch/src/api/ophyd/useOphydPVSocket.tsx:171``. A
                # regression to ``message`` would silently invisibilize
                # the error in the UI.
                error_text = (msg.get("error") or "").lower()
                assert "not yet implemented" in error_text, (
                    f"error envelope missing not-implemented message: {msg!r}"
                )
                return
            # Defensive: the old bug would have produced this event.
            if msg.get("type") == "stop_complete" and msg.get("success") is False:
                pytest.fail(
                    f"received stop_complete with success=False — "
                    f"the silent-failure pattern is back: {msg!r}"
                )

        pytest.fail("never received an error envelope on /stop unimplemented")


# ─── Finch contract: error envelope, pv field, timestamp number ──────────
#
# These tests pin down the wire shape the frontend depends on. A
# regression here would silently break finch (it would still parse the
# JSON but find no field it cares about, and either the value or the
# error would never reach the UI).


def test_finch_contract_error_envelope_uses_error_field(client):
    """WS error envelopes must carry the human-readable text in ``error``.

    finch's ``useOphydPVSocket.tsx:171`` literally checks
    ``if ('error' in message) console.error(...)``. A regression to
    ``message`` (our pre-2026-05-01 shape) makes every backend error
    invisible in the UI — the worst kind of silent failure.
    """
    import time

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "set"})  # missing pv + value → triggers send_error

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("type") == "error":
                assert "error" in msg, (
                    f"error envelope missing 'error' field — finch will "
                    f"silently drop this message. Got: {msg!r}"
                )
                assert "message" not in msg, (
                    f"error envelope still carries legacy 'message' field; "
                    f"finch reads 'error', so 'message' is dead weight: {msg!r}"
                )
                return

        pytest.fail("never received error envelope for malformed set request")


def test_finch_contract_pv_update_uses_pv_field(client):
    """pv_update messages must carry the PV name as ``pv``, not ``pv_name``.

    finch's ``useOphydPVSocket.tsx:160`` checks ``'pv' in message`` to
    discriminate value-update messages from meta messages. With ``pv_name``
    on the wire, finch's hook would simply drop the update.
    """
    import time

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:counter"})

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("event_type") == "pv_update":
                assert "pv" in msg, (
                    f"pv_update missing 'pv' field — finch's discriminator "
                    f"check fails: {msg!r}"
                )
                assert msg["pv"] == "IOC:counter"
                # The legacy `pv_name` shim should be gone — we removed it.
                assert "pv_name" not in msg, (
                    f"pv_update still carries legacy 'pv_name' shim: {msg!r}"
                )
                return

        pytest.fail("never received pv_update for IOC:counter")


def test_finch_contract_timestamp_is_unix_epoch_seconds(client):
    """Timestamps on pv_update must be unix epoch seconds (number).

    finch's ``TableDeviceController.tsx:31,35`` does
    ``Date.now() / 1000 - device.timestamp <= 0.03`` to drive a flash
    effect. With ISO strings the subtraction silently coerces to NaN
    and the feature dies without a peep. Numeric epoch seconds keep it
    working.
    """
    import time
    from datetime import datetime, timedelta

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:counter"})

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("event_type") == "pv_update" and msg.get("pv") == "IOC:counter":
                ts = msg["timestamp"]
                assert isinstance(ts, (int, float)), (
                    f"timestamp must be a number (unix epoch seconds); "
                    f"got {type(ts).__name__} {ts!r}"
                )
                # Sanity: should be within a day of "now" (sec since epoch).
                now = datetime.now().timestamp()
                assert abs(now - ts) < timedelta(days=1).total_seconds(), (
                    f"timestamp {ts} is implausibly far from now ({now})"
                )
                return

        pytest.fail("never received pv_update for IOC:counter")


def test_finch_contract_meta_message_emitted_on_subscribe(client):
    """A ``sub_type: meta`` message with units/limits/precision must arrive on subscribe.

    finch's ``useOphydPVSocket.tsx:146-159`` keys off ``sub_type === 'meta'``
    to populate device metadata (units, min/max). Without this message,
    every metadata field on the UI stays at its default-empty state.
    """
    import time

    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:counter"})

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("sub_type") == "meta" and msg.get("pv") == "IOC:counter":
                # finch reads these specifically; if any are missing the
                # frontend's min/max sliders silently default to nothing.
                assert "lower_ctrl_limit" in msg
                assert "upper_ctrl_limit" in msg
                assert "units" in msg
                assert "precision" in msg
                # And the timestamp must also be epoch seconds.
                assert isinstance(msg["timestamp"], (int, float))
                return

        pytest.fail("never received sub_type:meta envelope on subscribe")


def test_finch_contract_device_socket_emits_meta_message(client):
    """Device-socket subscribe must emit a ``sub_type: meta`` message per device PV.

    finch's ``useOphydDeviceSocket.ts:146-159`` keys on
    ``sub_type === 'meta'`` to populate device-level metadata
    (units/limits/precision). Pre-fix the device-socket emitted only
    ``DeviceUpdate`` value events, never meta — so the UI's metadata
    silently stayed empty.

    Stubs ``_fetch_device_info`` so the test doesn't need a live
    configuration_service; the WS manager then drives real EPICS
    subscriptions against the test IOC.
    """
    import time

    from direct_control.models import DeviceInfo

    app = client.app
    device_ws_manager = app.state.device_ws_manager

    async def _stub_fetch(device_name: str):
        # Map a single component to a real test-IOC PV so the subsequent
        # CA monitor + initial-value path produces a real value (and meta).
        return (
            DeviceInfo(
                name=device_name,
                device_type="motor",
                pvs={"readback": "IOC:counter"},
            ),
            None,
        )

    device_ws_manager._fetch_device_info = _stub_fetch  # type: ignore[method-assign]

    with client.websocket_connect("/api/v1/device-socket") as ws:
        ws.send_json({"action": "subscribe", "device": "fake_motor"})

        deadline = time.monotonic() + 3.0
        saw_meta = False
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("sub_type") == "meta" and msg.get("device") == "fake_motor":
                # Must include the metadata fields finch reads.
                assert "lower_ctrl_limit" in msg
                assert "upper_ctrl_limit" in msg
                assert "units" in msg
                assert "precision" in msg
                # Discriminator on the device socket is the `device` field
                # (per ophydDeviceSocketTypes.ts), not `pv`.
                assert "device" in msg
                # Timestamp must be epoch seconds, same contract as PV socket.
                assert isinstance(msg["timestamp"], (int, float))
                saw_meta = True
                break

        assert saw_meta, "device-socket never emitted sub_type:meta envelope"


def test_finch_contract_meta_oversize_emits_error_envelope(client):
    """When the meta message exceeds the size cap, send a structured error envelope.

    Pre-fix the meta-message send path used ``await ws.send_json(meta_msg)``
    directly with a ``try/except WebSocketResponseTooLarge: logger.warning``
    that swallowed the failure server-side. The client never knew.
    Post-fix, oversize meta delivers an ``{type: error, error: ..., pv: ...,
    sub_type: meta}`` envelope so finch can surface it.
    """
    import time

    app = client.app
    # Tight enough to block the meta envelope (~261 bytes for IOC:wf1) but
    # leave room for the error envelope (~150 bytes).
    app.state.settings.response_bytesize_limit = 200

    try:
        with client.websocket_connect("/api/v1/pv-socket") as ws:
            ws.send_json({"action": "subscribe", "pv": "IOC:wf1"})

            deadline = time.monotonic() + 3.0
            saw_meta_error = False
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if (
                    msg.get("type") == "error"
                    and msg.get("sub_type") == "meta"
                    and msg.get("pv") == "IOC:wf1"
                ):
                    assert "size limit" in (msg.get("error") or "").lower()
                    saw_meta_error = True
                    break
            assert saw_meta_error, (
                "never received structured error envelope for oversize meta — "
                "the silent-fallback pattern from the merge is back"
            )
    finally:
        from direct_control.config import Settings

        app.state.settings.response_bytesize_limit = Settings().response_bytesize_limit


# ─── S4: configuration_service_url must be required (no localhost default) ───
#
# Pre-fix: ``Settings.configuration_service_url`` defaulted to
# ``http://localhost:8004``. If a deployer forgot to set
# ``DIRECT_CONTROL_CONFIGURATION_SERVICE_URL``, the service would silently
# boot and start sending requests at localhost. The misconfig didn't
# surface until the first request — by then the service had already passed
# its readiness probe and been put in rotation. After the fix, the field
# has no default and ``Settings()`` raises at startup.


def test_s4_configuration_service_url_is_required(monkeypatch):
    """Settings() must fail if DIRECT_CONTROL_CONFIGURATION_SERVICE_URL is unset.

    Pre-fix bug: silent default to localhost:8004 hid forgotten config.
    Now: pydantic ValidationError at boot time.
    """
    from pydantic import ValidationError

    from direct_control.config import Settings

    monkeypatch.delenv("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings()
    msg = str(exc_info.value)
    assert "configuration_service_url" in msg.lower()


# ─── S5 + S6: /health surfaces real config-service availability + flips to 503 ──
#
# Pre-S5: /health always returned 200 even when configuration_service was
# unreachable; status flipped to "degraded" but LB readiness probes
# couldn't see anything wrong, so traffic kept routing here.
#
# Pre-S6: ``CoordinationClient.is_service_available()`` had a bare
# ``except Exception: return False`` with no logging and no detail. /health
# saw the bare bool and couldn't surface the actual failure mode.
#
# After: is_service_available() returns ``ServiceAvailability(available,
# detail)``; /health includes ``coordination_service_detail`` and flips
# to HTTP 503 when ``available=False``.


def test_s5_health_returns_503_when_coordination_unavailable(client):
    """Pre-fix: 200 + status="degraded" hid the failure from LB probes.

    Drive an unavailable coord stub through the same path the production
    client takes, then verify both the HTTP status code AND the body
    surface the failure.
    """
    from direct_control.main import get_coordination_client

    class _UnavailableCoord:
        async def is_service_available(self) -> ServiceAvailability:
            return ServiceAvailability(
                available=False,
                detail="cannot reach configuration_service /health: ConnectError",
            )

        async def cleanup(self) -> None:
            return None

    unavailable = _UnavailableCoord()
    app = client.app
    original = app.dependency_overrides.get(get_coordination_client)
    app.dependency_overrides[get_coordination_client] = lambda: unavailable
    try:
        r = client.get("/health")
        assert r.status_code == 503, (
            f"expected 503 (LB probes must see unhealthy), got {r.status_code}"
        )
        body = r.json()
        assert body["coordination_service_available"] is False
        assert body["coordination_service_detail"] is not None
        assert "configuration_service" in body["coordination_service_detail"]
        assert body["status"] == "unhealthy"
    finally:
        if original is None:
            app.dependency_overrides.pop(get_coordination_client, None)
        else:
            app.dependency_overrides[get_coordination_client] = original


def test_s5_health_200_when_coordination_available(client):
    """Sanity: the happy path stays 200 with detail=None."""
    # The default `client` fixture installs an always-available stub.
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["coordination_service_available"] is True
    assert body["coordination_service_detail"] is None
    assert body["status"] == "healthy"


def test_s6_is_service_available_returns_structured_detail_on_timeout():
    """Pre-S6: bare ``except Exception: return False``. Now: structured detail.

    Unit-tests the client directly with an httpx transport that raises
    TimeoutException so we don't depend on a live configuration_service.
    """
    import httpx

    from direct_control.config import Settings
    from direct_control.coordination_client import CoordinationClient

    settings = Settings()  # picks up DIRECT_CONTROL_CONFIGURATION_SERVICE_URL from env
    cc = CoordinationClient(settings)

    async def _run():
        # Inject a transport that always raises TimeoutException.
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        cc._client = httpx.AsyncClient(
            base_url=settings.configuration_service_url,
            transport=httpx.MockTransport(_handler),
        )
        result = await cc.is_service_available()
        assert result.available is False
        assert result.detail is not None
        assert "timeout" in result.detail.lower()
        await cc.cleanup()

    import asyncio

    asyncio.run(_run())


def test_s6_is_service_available_returns_structured_detail_on_non_200():
    """Non-2xx response from /health must produce a structured detail, not bare False."""
    import httpx

    from direct_control.config import Settings
    from direct_control.coordination_client import CoordinationClient

    settings = Settings()
    cc = CoordinationClient(settings)

    async def _run():
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "unhealthy"})

        cc._client = httpx.AsyncClient(
            base_url=settings.configuration_service_url,
            transport=httpx.MockTransport(_handler),
        )
        result = await cc.is_service_available()
        assert result.available is False
        assert result.detail is not None
        assert "503" in result.detail
        await cc.cleanup()

    import asyncio

    asyncio.run(_run())


# ─── S7: _fetch_device_info distinguishes 404 / non-2xx / network failure ────
#
# Pre-S7 ``_fetch_device_info`` returned ``None`` for all three failure
# classes. The caller in ``subscribe_device`` mapped that to
# ``reason="not_found"``, so a WS subscribe against a config_service that
# was *down* surfaced "Device 'X' not found" — operators ended up
# investigating phantom missing devices. After S7 the fetch result is
# ``(info, reason)`` and the WS handler emits an actionable
# upstream_error / upstream_unreachable envelope instead.


@pytest.mark.asyncio
async def test_s7_fetch_device_info_distinguishes_404_from_non2xx_from_network(client):
    """Pin the three failure-class mapping. No live config_service required."""
    import httpx

    from direct_control.models import DeviceInfo

    device_ws_manager = client.app.state.device_ws_manager

    async def _fake_get_404(_self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(404, json={"detail": "not found"})
            )
        )
        return client

    # 200 → (info, None) is exercised by the meta-message test above; here
    # we drive the three failure classes.
    async def _via_transport(handler):
        device_ws_manager._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return await device_ws_manager._fetch_device_info("any_device")

    # 404 → not_found (the device truly isn't registered)
    info, reason = await _via_transport(
        lambda req: httpx.Response(404, json={"detail": "missing"})
    )
    assert info is None
    assert reason == "not_found"

    # 500 → upstream_error (config_service is up but rejected)
    info, reason = await _via_transport(
        lambda req: httpx.Response(500, json={"detail": "kaboom"})
    )
    assert info is None
    assert reason == "upstream_error"

    # network failure → upstream_unreachable
    def _network_fail(req):
        raise httpx.ConnectError("simulated", request=req)

    info, reason = await _via_transport(_network_fail)
    assert info is None
    assert reason == "upstream_unreachable"


@pytest.mark.asyncio
async def test_s7_subscribe_device_propagates_fetch_reason():
    """``subscribe_device`` must surface the upstream reason verbatim.

    Pre-S7 it always returned ``"not_found"`` regardless of which failure
    fired in ``_fetch_device_info``. Drive each fetch reason via stubbing
    and assert the propagated tuple matches.
    """
    from direct_control.config import Settings
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()
    mgr = DeviceWebSocketManager(
        pv_monitor=object(),  # not exercised — fetch fails before subscribe
        device_controller=object(),
        settings=settings,
    )
    # Inject a fake connection so the early "unknown_client" arm is bypassed.
    mgr._connections["c"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["c"] = set()

    for fake_reason in ("not_found", "upstream_error", "upstream_unreachable"):

        async def _fake_fetch(_name, _reason=fake_reason):
            return None, _reason

        mgr._fetch_device_info = _fake_fetch  # type: ignore[method-assign]
        outcome = await mgr.subscribe_device("c", "dev")
        assert outcome.ok is False
        assert outcome.reason == fake_reason
        assert outcome.failed_pvs == []


# ─── S8: failed PV subscribes are cleaned up + surfaced via error envelope ───
#
# Pre-S8: when a PV failed to subscribe, ``_pv_callbacks[pv_name]`` and
# ``_device_pvs[device_name][component]`` were left dangling — callbacks
# bound to no live CA monitor — and the WS client got a "subscribed"
# event with no indication that some components would be silent forever.


@pytest.mark.asyncio
async def test_s8_failed_pv_subscribes_purged_from_bookkeeping():
    """Stale callbacks for failed PVs must be removed."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()

    class _PVMonitorStub:
        def __init__(self):
            self.subscribed: list[str] = []

        def subscribe(self, pv_name: str, callback, *, on_error=None):
            # Simulate: "good" PVs subscribe fine; "bad" PVs raise.
            if "bad" in pv_name:
                raise RuntimeError(f"CA timeout for {pv_name}")
            self.subscribed.append(pv_name)

        def unsubscribe(self, pv_name: str, callback):
            pass

    pv_monitor = _PVMonitorStub()
    mgr = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["c"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["c"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(
                name="dev",
                device_type="motor",
                pvs={"good_signal": "IOC:good", "bad_signal": "IOC:bad"},
            ),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    async def _noop_send_current_values(*_args, **_kwargs):
        return None

    mgr._send_current_values = _noop_send_current_values  # type: ignore[method-assign]

    outcome = await mgr.subscribe_device("c", "dev")
    assert outcome.ok is True
    assert outcome.reason is None
    assert len(outcome.failed_pvs) == 1
    assert outcome.failed_pvs[0].pv == "IOC:bad"
    assert outcome.failed_pvs[0].signal == "bad_signal"

    # Bookkeeping must reflect the actual subscribed-vs-failed split.
    assert "IOC:good" in mgr._pv_callbacks
    assert "IOC:bad" not in mgr._pv_callbacks, (
        "failed PV must be removed from _pv_callbacks (S8 regression)"
    )
    assert mgr._device_pvs["dev"] == {"good_signal": "IOC:good"}, (
        "failed component must be removed from _device_pvs (S8 regression)"
    )


@pytest.mark.asyncio
async def test_s8_require_connection_rolls_back_on_partial_failure():
    """With require_connection=True any PV failure must roll back the whole subscription."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()

    class _PVMonitorStub:
        def __init__(self):
            self.subscribed: list[str] = []
            self.unsubscribed: list[str] = []

        def subscribe(self, pv_name: str, callback, *, on_error=None):
            if "bad" in pv_name:
                raise RuntimeError("CA timeout")
            self.subscribed.append(pv_name)

        def unsubscribe(self, pv_name: str, callback):
            self.unsubscribed.append(pv_name)

    pv_monitor = _PVMonitorStub()
    mgr = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["c"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["c"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(
                name="dev",
                device_type="motor",
                pvs={"good": "IOC:good", "bad": "IOC:bad"},
            ),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    outcome = await mgr.subscribe_device(
        "c", "dev", require_connection=True
    )
    assert outcome.ok is False
    assert outcome.reason == "not_connected"
    assert outcome.failed_pvs == []  # rolled-back path returns empty per the contract

    # Rollback teardown: the GOOD PV that subscribed must be unsubscribed
    # so we don't leak a CA monitor.
    assert "IOC:good" in pv_monitor.unsubscribed, (
        "successful PVs must be torn down on rollback (S8 require_connection)"
    )
    # All bookkeeping for this device gone.
    assert "dev" not in mgr._device_clients
    assert "dev" not in mgr._device_pvs
    assert "IOC:good" not in mgr._pv_callbacks


# ─── C2: failed device PVs visible to every subscriber + retried on resubscribe ──
#
# Pre-fix: when client A subscribed to a device and component PV `bad` failed,
# `_purge_failed_pvs` removed it from `_device_pvs[device]`. When client B
# later subscribed to the same device, `subscribe_device`'s fast path skipped
# `new_subscriptions` entirely (since `device_name in _device_clients`), so B
# saw no fail envelope and the PV was never retried — silent forever until the
# last subscriber left. Surfaced by Copilot in PR #5 review.


@pytest.mark.asyncio
async def test_c2_subsequent_subscriber_sees_failures_and_retry_recovers():
    """Failures must be reported to every subscriber AND retried on resubscribe."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()

    class _PVMonitorStub:
        def __init__(self):
            self.subscribed: list[str] = []
            # Map pv_name -> remaining failures before it succeeds. "bad" PVs
            # fail forever unless we flip ``healed``. ``subscribe_calls`` lets
            # us assert the retry actually fired.
            self.subscribe_calls: list[str] = []
            self.healed = False

        def subscribe(self, pv_name: str, callback, *, on_error=None):
            self.subscribe_calls.append(pv_name)
            if "bad" in pv_name and not self.healed:
                raise RuntimeError(f"CA timeout for {pv_name}")
            self.subscribed.append(pv_name)

        def unsubscribe(self, pv_name: str, callback):
            pass

    pv_monitor = _PVMonitorStub()
    mgr = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._connections["b"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()
    mgr._device_subscriptions["b"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(
                name="dev",
                device_type="motor",
                pvs={"good": "IOC:good", "bad": "IOC:bad"},
            ),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    async def _noop(*_args, **_kwargs):
        return None

    mgr._send_current_values = _noop  # type: ignore[method-assign]

    # ── Client A: first subscriber. ``bad`` fails. ───────────────────────
    outcome_a = await mgr.subscribe_device("a", "dev")
    assert outcome_a.ok is True
    assert {f.signal for f in outcome_a.failed_pvs} == {"bad"}
    assert mgr._device_pv_failures["dev"]["bad"].pv == "IOC:bad"

    # ── Client B: subsequent subscriber. Must see the existing failure
    # AND trigger a retry of just the failed component (not the good one,
    # which is already live and shared). ────────────────────────────────
    pv_monitor.subscribe_calls.clear()
    outcome_b = await mgr.subscribe_device("b", "dev")
    assert outcome_b.ok is True
    assert {f.signal for f in outcome_b.failed_pvs} == {"bad"}, (
        "subsequent subscriber must see all currently-broken signals (C2 visibility)"
    )
    assert pv_monitor.subscribe_calls == ["IOC:bad"], (
        "subsequent subscribe must retry only the failed component (C2 retry)"
    )

    # ── IOC heals; client C subscribes; retry recovers. ──────────────────
    pv_monitor.healed = True
    pv_monitor.subscribe_calls.clear()
    mgr._connections["c"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["c"] = set()
    outcome_c = await mgr.subscribe_device("c", "dev")
    assert outcome_c.ok is True
    assert outcome_c.failed_pvs == [], (
        "recovered failure must be cleared from the device's failure set (C2 recovery)"
    )
    assert pv_monitor.subscribe_calls == ["IOC:bad"]
    assert mgr._device_pv_failures["dev"] == {}
    assert mgr._device_pvs["dev"] == {"good": "IOC:good", "bad": "IOC:bad"}


@pytest.mark.asyncio
async def test_c2_pv_failures_cleared_on_last_client_unsubscribe():
    """``_device_pv_failures[device]`` must be torn down with the device."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()

    class _PVMonitorStub:
        def subscribe(self, pv_name, callback, *, on_error=None):
            if "bad" in pv_name:
                raise RuntimeError("CA timeout")

        def unsubscribe(self, pv_name, callback):
            pass

    mgr = DeviceWebSocketManager(
        pv_monitor=_PVMonitorStub(),
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(name="dev", device_type="motor", pvs={"bad": "IOC:bad"}),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    async def _noop(*_a, **_kw):
        return None

    mgr._send_current_values = _noop  # type: ignore[method-assign]

    await mgr.subscribe_device("a", "dev")
    assert mgr._device_pv_failures["dev"] != {}

    await mgr.unsubscribe_device("a", "dev")
    assert "dev" not in mgr._device_pv_failures, (
        "last-client unsubscribe must drop _device_pv_failures[device] (C2 cleanup)"
    )

    # Same for disconnect.
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()
    await mgr.subscribe_device("a", "dev")
    assert mgr._device_pv_failures["dev"] != {}

    await mgr.disconnect("a")
    assert "dev" not in mgr._device_pv_failures, (
        "last-client disconnect must drop _device_pv_failures[device] (C2 cleanup)"
    )


# ─── C2 follow-ups (PR #6 review) ─────────────────────────────────────────────
#
# Two issues Copilot caught after the initial C2 fix landed:
#   * partial recovery during a require_connection retry was discarded along
#     with the rolled-back client — leaking the CA monitor and leaving the
#     component listed as "still broken" forever.
#   * a second client subscribing to the same device while the first
#     subscriber's gather was in-flight took the "subsequent subscriber" path,
#     saw an empty failure set, and got a clean ack — masking the failures the
#     first subscribe was about to surface.


@pytest.mark.asyncio
async def test_c2_partial_recovery_preserved_through_require_connection_rollback():
    """When subscribe_safely partial-recovers, recoveries stick for other clients."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    settings = Settings()

    class _PVMonitorStub:
        def __init__(self):
            self.subscribe_calls: list[str] = []
            # bad1 heals on second attempt; bad2 is permanently broken.
            self.bad1_calls = 0

        def subscribe(self, pv_name, callback, *, on_error=None):
            self.subscribe_calls.append(pv_name)
            if pv_name == "IOC:bad1":
                self.bad1_calls += 1
                if self.bad1_calls == 1:
                    raise RuntimeError("CA timeout (transient)")
                return  # heals
            if pv_name == "IOC:bad2":
                raise RuntimeError("CA timeout (permanent)")

        def unsubscribe(self, pv_name, callback):
            pass

    pv_monitor = _PVMonitorStub()
    mgr = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._connections["b"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()
    mgr._device_subscriptions["b"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(
                name="dev",
                device_type="motor",
                pvs={"good": "IOC:good", "bad1": "IOC:bad1", "bad2": "IOC:bad2"},
            ),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    async def _noop(*_a, **_kw):
        return None

    mgr._send_current_values = _noop  # type: ignore[method-assign]

    # Client A holds the device; bad1 + bad2 fail on first attempt.
    outcome_a = await mgr.subscribe_device("a", "dev")
    assert outcome_a.ok is True
    assert {f.signal for f in outcome_a.failed_pvs} == {"bad1", "bad2"}

    # Client B does subscribe_safely. bad1 recovers on retry, bad2 still fails.
    # require_connection rolls B back, but the recovery must persist for A.
    outcome_b = await mgr.subscribe_device("b", "dev", require_connection=True)
    assert outcome_b.ok is False
    assert outcome_b.reason == "not_connected"

    # The recovery is the load-bearing assertion: bad1 left _device_pv_failures
    # and joined _device_pvs, so A keeps receiving its updates and the eventual
    # last-client teardown unsubscribes the live CA monitor.
    assert "bad1" in mgr._device_pvs["dev"], (
        "recovered component must persist in _device_pvs after rollback (C2 follow-up A)"
    )
    assert mgr._device_pv_failures["dev"].keys() == {"bad2"}, (
        "recovered component must drop out of _device_pv_failures even on rollback"
    )
    # B is not on the device; A still is.
    assert mgr._device_clients["dev"] == {"a"}
    assert "dev" not in mgr._device_subscriptions["b"]


@pytest.mark.asyncio
async def test_c2_concurrent_subscribers_to_same_device_serialize():
    """A second subscribe in-flight with the first must wait + see consistent failures."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager
    import asyncio as _asyncio
    import threading

    settings = Settings()

    # The first subscribe call (whichever client gets there first) blocks on
    # `release` until the test fires it; the second client must serialize
    # behind the per-device lock and observe the same failure set.
    started = threading.Event()
    release = threading.Event()

    class _PVMonitorStub:
        def subscribe(self, pv_name, callback, *, on_error=None):
            if not started.is_set():
                started.set()
                release.wait(timeout=2.0)
            if "bad" in pv_name:
                raise RuntimeError("CA timeout")

        def unsubscribe(self, pv_name, callback):
            pass

    pv_monitor = _PVMonitorStub()
    mgr = DeviceWebSocketManager(
        pv_monitor=pv_monitor,
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._connections["b"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()
    mgr._device_subscriptions["b"] = set()

    async def _fetch(_name):
        return (
            DeviceInfo(
                name="dev",
                device_type="motor",
                pvs={"good": "IOC:good", "bad": "IOC:bad"},
            ),
            None,
        )

    mgr._fetch_device_info = _fetch  # type: ignore[method-assign]

    async def _noop(*_a, **_kw):
        return None

    mgr._send_current_values = _noop  # type: ignore[method-assign]

    # Kick off A; it will block inside the gather waiting for the release.
    a_task = _asyncio.create_task(mgr.subscribe_device("a", "dev"))
    await _asyncio.to_thread(started.wait, 2.0)

    # While A is mid-gather, fire B. Without the per-device lock, B would
    # take the "subsequent subscriber" path, see an empty failure set, and
    # return ok=True with no failed_pvs. The lock must make B wait for A.
    b_task = _asyncio.create_task(mgr.subscribe_device("b", "dev"))
    # Give B a moment to enter and block on the per-device lock.
    await _asyncio.sleep(0.05)
    assert not b_task.done(), (
        "B must block on the per-device subscribe lock while A is in-flight "
        "(C2 follow-up B)"
    )

    # Release A; both should now complete with consistent state.
    release.set()
    outcome_a = await a_task
    outcome_b = await b_task

    assert outcome_a.ok is True
    assert {f.signal for f in outcome_a.failed_pvs} == {"bad"}
    assert outcome_b.ok is True
    assert {f.signal for f in outcome_b.failed_pvs} == {"bad"}, (
        "B must observe A's failure set, not an empty one (C2 follow-up B)"
    )


@pytest.mark.asyncio
async def test_c2_disconnect_during_subscribe_returns_unknown_client():
    """A disconnect mid-subscribe must not raise KeyError on _device_subscriptions."""
    from direct_control.config import Settings
    from direct_control.models import DeviceInfo
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager
    import asyncio as _asyncio
    import threading

    settings = Settings()

    # Block subscribe inside _fetch_device_info — that's an await point
    # AFTER the initial _connections check but BEFORE the inner self._lock
    # re-check. Disconnecting in this window is the precise race.
    fetch_started = threading.Event()
    fetch_release = _asyncio.Event()

    mgr = DeviceWebSocketManager(
        pv_monitor=object(),
        device_controller=object(),
        settings=settings,
    )
    mgr._connections["a"] = object()  # type: ignore[assignment]
    mgr._device_subscriptions["a"] = set()

    async def _fetch_blocking(_name):
        fetch_started.set()
        await fetch_release.wait()
        return (
            DeviceInfo(name="dev", device_type="motor", pvs={"good": "IOC:good"}),
            None,
        )

    mgr._fetch_device_info = _fetch_blocking  # type: ignore[method-assign]

    subscribe_task = _asyncio.create_task(mgr.subscribe_device("a", "dev"))

    # Wait for the subscribe to enter _fetch_device_info, then disconnect.
    await _asyncio.to_thread(fetch_started.wait, 2.0)
    await mgr.disconnect("a")

    # Release the fetch so subscribe_device proceeds past the await.
    fetch_release.set()
    outcome = await subscribe_task

    # Pre-fix: KeyError on self._device_subscriptions["a"].add() because
    # disconnect popped the entry.
    assert outcome.ok is False
    assert outcome.reason == "unknown_client", (
        "subscribe must surface unknown_client (not KeyError) when the client "
        "disconnects mid-await (C2 follow-up D)"
    )


# ─── M7: PV access bits must default to False on extraction failure ──────────
#
# Three sites pre-fix told a UI "you can write this PV" when we hadn't
# confirmed it: `_signal_to_pv_value` defaulted access bits to True before
# its try/except metadata block; `_handle_refresh` and `_send_current_values`
# overrode the real bits with hardcoded True. Now extraction defaults to
# False and the two override sites propagate the real PVValue bits.


def _make_fake_signal(read_pv=None, value=0):
    """Stub of an ophyd EpicsSignal exposing only what `_signal_to_pv_value` reads."""

    class _Signal:
        connected = True
        timestamp = None

        def get(self):
            return value

    if read_pv is not None:
        _Signal._read_pv = read_pv
    return _Signal()


def _locked_pvvalue(pv_name: str):
    """A PVValue with both access bits False — the input the override-site tests need."""
    from datetime import datetime

    from direct_control.models import PVValue

    return PVValue(
        pv_name=pv_name,
        value=0,
        timestamp=datetime.now(),
        status=0,
        severity=0,
        connected=True,
        read_access=False,
        write_access=False,
    )


def test_m7_extraction_defaults_to_no_access_when_read_pv_missing():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    pv_value = PVMonitorManager(Settings())._signal_to_pv_value(
        "FAKE:pv", _make_fake_signal()
    )

    assert pv_value.read_access is False, (
        "missing _read_pv must default read_access=False — pre-fix this was True"
    )
    assert pv_value.write_access is False, (
        "missing _read_pv must default write_access=False — pre-fix this was True"
    )


def test_m7_extraction_keeps_no_access_when_metadata_read_raises():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    class _ExplodingPV:
        def __getattr__(self, name):
            raise RuntimeError(f"simulated EPICS metadata read failure on {name}")

    pv_value = PVMonitorManager(Settings())._signal_to_pv_value(
        "FAKE:pv", _make_fake_signal(read_pv=_ExplodingPV())
    )

    assert pv_value.read_access is False
    assert pv_value.write_access is False


def test_m7_extraction_propagates_real_access_bits():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    class _ReadOnlyPV:
        units = "mm"
        precision = 3
        enum_strs = None
        lower_ctrl_limit = 0.0
        upper_ctrl_limit = 100.0
        lower_disp_limit = 0.0
        upper_disp_limit = 100.0
        read_access = True
        write_access = False  # caput access denied

    pv_value = PVMonitorManager(Settings())._signal_to_pv_value(
        "FAKE:pv", _make_fake_signal(read_pv=_ReadOnlyPV(), value=42.0)
    )

    assert pv_value.read_access is True
    assert pv_value.write_access is False, (
        "extraction must surface write_access=False — if True, default is masking real value"
    )


@pytest.mark.asyncio
async def test_m7_handle_refresh_propagates_pvvalue_access_bits():
    from direct_control.config import Settings
    from direct_control.models import PVUpdate
    from direct_control.monitoring.websocket_manager import WebSocketManager

    locked_value = _locked_pvvalue("LOCKED:pv")

    class _PVMonitorStub:
        def get_value(self, pv_name: str):
            return locked_value

    class _StubWS:
        async def send_json(self, payload):
            return None

    sent: list[PVUpdate] = []

    mgr = WebSocketManager(
        pv_monitor=_PVMonitorStub(),  # type: ignore[arg-type]
        device_controller=object(),  # type: ignore[arg-type]
        settings=Settings(),
    )
    mgr._subscriptions["c"] = {"LOCKED:pv"}

    async def _capture_send(client_id: str, update, websocket=None):
        sent.append(update)

    mgr._send_to_client = _capture_send  # type: ignore[method-assign]

    await mgr._handle_refresh("c", websocket=_StubWS(), data={"pv": "LOCKED:pv"})  # type: ignore[arg-type]

    assert len(sent) == 1
    assert sent[0].read_access is False, (
        "refresh must propagate read_access=False — pre-fix override is back (Site C)"
    )
    assert sent[0].write_access is False, (
        "refresh must propagate write_access=False — pre-fix override is back (Site C)"
    )


@pytest.mark.asyncio
async def test_m7_send_current_values_propagates_pvvalue_access_bits():
    from direct_control.config import Settings
    from direct_control.models import DeviceUpdate
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    locked_value = _locked_pvvalue("IOC:locked")

    class _PVMonitorStub:
        def get_value(self, pv_name: str):
            return locked_value

    sent: list[DeviceUpdate] = []

    mgr = DeviceWebSocketManager(
        pv_monitor=_PVMonitorStub(),  # type: ignore[arg-type]
        device_controller=object(),  # type: ignore[arg-type]
        settings=Settings(),
    )
    mgr._device_pvs["dev"] = {"motor": "IOC:locked"}
    mgr._connections["c"] = object()  # type: ignore[assignment]

    async def _capture_send(client_id, update, websocket=None):
        if isinstance(update, DeviceUpdate):
            sent.append(update)

    async def _noop_send_meta(*_args, **_kwargs):
        return None

    mgr._send_to_client = _capture_send  # type: ignore[method-assign]
    mgr._send_meta_to_client = _noop_send_meta  # type: ignore[method-assign]

    await mgr._send_current_values("c", "dev")

    assert len(sent) == 1
    assert sent[0].read_access is False, (
        "device-socket current-values must propagate read_access=False — "
        "pre-fix override is back (Site E)"
    )
    assert sent[0].write_access is False, (
        "device-socket current-values must propagate write_access=False — "
        "pre-fix override is back (Site E)"
    )


# ─── M9: PV callback exceptions surface to the WS subscriber ─────────────────
#
# Pre-fix: a callback raising in ``_handle_value_update`` /
# ``_handle_meta_update`` was logged at error and dropped on the floor.
# WS subscribers saw nothing change and would assume the broken PV was
# just quiet. Now ``subscribe(on_error=...)`` lets the subscriber
# translate the failure into a ``pv_error`` envelope.


def test_m9_subscribe_on_error_fires_when_value_callback_raises():
    """``_dispatch_subscriber`` must invoke ``on_error`` after callback raise.

    Calls ``_dispatch_subscriber`` directly (rather than going through
    the CA listener thread) so the test is fast and deterministic.
    """
    from datetime import datetime
    from direct_control.config import Settings
    from direct_control.models import PVUpdate
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    captured: list[BaseException] = []

    def failing_cb(_update):
        raise RuntimeError("simulated callback failure")

    def on_error(exc):
        captured.append(exc)

    mgr = PVMonitorManager(Settings())
    sub = _Subscriber(callback=failing_cb, on_error=on_error)
    update = PVUpdate(
        pv="IOC:x",
        value=1.0,
        timestamp=datetime.now(),
        status=0,
        severity=0,
        connected=True,
    )

    mgr._dispatch_subscriber("IOC:x", sub, update, source="value")

    assert len(captured) == 1
    assert isinstance(captured[0], RuntimeError)
    assert "simulated callback failure" in str(captured[0])


def test_m9_dispatch_swallows_on_error_failures_so_other_subscribers_run():
    """An on_error that itself raises must not poison the dispatch loop.

    Pre-M9 the bare ``logger.error`` ate the callback exception; M9
    surfaces it. The on_error handler (running on the CA thread) must
    in turn be defensive — if it raises, log and move on, otherwise a
    bug in one subscriber's error handling would break fan-out for
    every other subscriber on the same PV.
    """
    from datetime import datetime
    from direct_control.config import Settings
    from direct_control.models import PVUpdate
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    def failing_cb(_update):
        raise RuntimeError("primary callback failure")

    def failing_on_error(_exc):
        raise RuntimeError("on_error itself blew up")

    mgr = PVMonitorManager(Settings())
    sub = _Subscriber(callback=failing_cb, on_error=failing_on_error)
    update = PVUpdate(
        pv="IOC:x",
        value=1.0,
        timestamp=datetime.now(),
        status=0,
        severity=0,
        connected=True,
    )

    # Should not raise — both layers are caught and logged.
    mgr._dispatch_subscriber("IOC:x", sub, update, source="value")


def test_m9_unsubscribe_finds_subscriber_by_callback_identity():
    """``unsubscribe(pv, callback)`` must locate the entry by callback identity.

    Pre-M9 ``_callbacks`` stored bare callables and used ``list.remove``
    (equality match). M9 stores ``(callback, on_error)`` tuples; the
    new ``unsubscribe`` path filters by ``sub.callback is not callback``
    so the on_error doesn't strand the entry.
    """
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    mgr = PVMonitorManager(Settings())

    def cb1(_u):
        pass

    def cb2(_u):
        pass

    def on_err(_exc):
        pass

    mgr._callbacks["IOC:x"].append(_Subscriber(cb1, on_err))
    mgr._callbacks["IOC:x"].append(_Subscriber(cb2, None))

    # Without _signals[pv], unsubscribe early-exits the destroy path —
    # we only care about _callbacks bookkeeping here.
    mgr.unsubscribe("IOC:x", cb1)

    remaining = [sub.callback for sub in mgr._callbacks["IOC:x"]]
    assert remaining == [cb2]


@pytest.mark.asyncio
async def test_m9_pv_socket_error_handler_broadcasts_envelope_to_clients():
    """``WebSocketManager._broadcast_pv_callback_error`` sends to all subs.

    Builds the manager state directly (bypassing the WS handshake),
    registers a stub LockedWS for two clients on the same PV, then
    invokes the broadcast helper and verifies both got an ``error``
    envelope mentioning the PV.
    """
    from direct_control.config import Settings
    from direct_control.monitoring.websocket_manager import WebSocketManager

    sent: dict[str, list] = {"a": [], "b": []}

    class _StubWS:
        def __init__(self, client_id: str):
            self.client_id = client_id

        async def send_json(self, payload):
            sent[self.client_id].append(payload)

    mgr = WebSocketManager(
        pv_monitor=object(),  # type: ignore[arg-type]
        device_controller=object(),  # type: ignore[arg-type]
        settings=Settings(),
    )
    mgr._connections["a"] = _StubWS("a")  # type: ignore[assignment]
    mgr._connections["b"] = _StubWS("b")  # type: ignore[assignment]
    mgr._pv_clients["IOC:x"] = {"a", "b"}

    await mgr._broadcast_pv_callback_error("IOC:x", RuntimeError("kaboom"))

    for client_id in ("a", "b"):
        assert len(sent[client_id]) == 1, (
            f"client {client_id} should have received exactly one error envelope"
        )
        msg = sent[client_id][0]
        assert msg["type"] == "error"
        assert msg["pv"] == "IOC:x"
        assert "kaboom" in msg["error"]


@pytest.mark.asyncio
async def test_m9_device_socket_error_handler_broadcasts_envelope_to_clients():
    """Parallel of the PV-socket test for ``DeviceWebSocketManager``."""
    from direct_control.config import Settings
    from direct_control.monitoring.device_websocket_manager import DeviceWebSocketManager

    sent: dict[str, list] = {"c1": [], "c2": []}

    class _StubWS:
        def __init__(self, client_id: str):
            self.client_id = client_id

        async def send_json(self, payload):
            sent[self.client_id].append(payload)

    mgr = DeviceWebSocketManager(
        pv_monitor=object(),  # type: ignore[arg-type]
        device_controller=object(),  # type: ignore[arg-type]
        settings=Settings(),
    )
    mgr._connections["c1"] = _StubWS("c1")  # type: ignore[assignment]
    mgr._connections["c2"] = _StubWS("c2")  # type: ignore[assignment]
    mgr._device_clients["dev"] = {"c1", "c2"}

    await mgr._broadcast_device_callback_error(
        "dev", "motor", "IOC:motor", RuntimeError("kaboom")
    )

    for client_id in ("c1", "c2"):
        assert len(sent[client_id]) == 1
        msg = sent[client_id][0]
        assert msg["type"] == "error"
        assert msg["device"] == "dev"
        assert msg["signal"] == "motor"
        assert msg["pv"] == "IOC:motor"
        assert "kaboom" in msg["error"]


# ─── M10: get_value distinguishes "not subscribed" from "read error" ─────────
#
# Pre-fix: get_value swallowed read exceptions to None, indistinguishable
# from "PV is not in our subscription cache". The REST endpoint mapped
# both to HTTP 404 "PV not found", so a transient EPICS read failure
# looked like the PV didn't exist. Now get_value raises PVReadError on
# read failure; the REST layer maps it to 503 (transient).


def test_m10_get_value_returns_none_when_pv_not_subscribed():
    """Genuine "PV not in our cache" still returns None — caller maps to 404."""
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    # No subscription, no signal, no cached value.
    assert mgr.get_value("IOC:never_subscribed") is None


def test_m10_get_value_raises_pv_read_error_when_signal_read_fails():
    """Subscribed PV with broken read raises PVReadError, not None.

    Pre-fix this returned None and the REST endpoint mapped it to 404
    "not found" — wrong: the PV *is* known, EPICS just failed to read.
    """
    from direct_control.config import Settings
    from direct_control.models import PVReadError
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())

    class _FailingSignal:
        """A stub ophyd signal whose read fails."""

        connected = True

        def get(self, *_args, **_kwargs):
            raise RuntimeError("CA timeout")

    # Plant a fake "subscribed" entry without going through CA.
    mgr._signals["IOC:flaky"] = _FailingSignal()  # type: ignore[assignment]
    # _latest_values is empty, so get_value falls through to the on-demand read.

    with pytest.raises(PVReadError) as excinfo:
        mgr.get_value("IOC:flaky")

    assert "IOC:flaky" in str(excinfo.value)
    assert "CA timeout" in str(excinfo.value)


def test_m10_rest_get_monitored_pv_returns_503_on_read_error(client):
    """REST `/api/v1/pvs/{pv}/value` returns 503, not 404, on PVReadError.

    Drives the real endpoint with a pv_monitor whose `get_value` raises
    PVReadError. Pre-fix the same condition surfaced as 404 "not found".
    """
    from direct_control.models import PVReadError
    from direct_control.protocols import PVMonitor

    class _FailingMonitor:
        def subscribe(self, *_args, **_kwargs):
            return None

        def get_value(self, pv_name: str):
            raise PVReadError(f"read failed for {pv_name}: simulated CA timeout")

        # Methods unused on this code path — provide stubs for the
        # protocol's runtime_checkable surface in case anything probes.
        def unsubscribe(self, *_args, **_kwargs):
            pass

        def get_buffer(self, _pv_name):
            return []

        def is_connected(self, _pv_name):
            return True

        def get_connected_pvs(self):
            return []

        async def cleanup(self):
            pass

    from direct_control.main import app, get_pv_monitor

    app.dependency_overrides[get_pv_monitor] = lambda: _FailingMonitor()
    try:
        resp = client.get("/api/v1/pvs/IOC:counter/value")
    finally:
        app.dependency_overrides.pop(get_pv_monitor, None)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "IOC:counter" in detail
    assert "simulated CA timeout" in detail


# ─── M13: OpenAPI export must fail loudly when explicitly requested ──────────
#
# Pre-fix the helper logged a warning and continued, leaving the
# frontend's codegen watcher consuming a stale schema with no signal
# to operators. Now: if `OPHYD_SERVICE_OPENAPI_EXPORT_PATH` is set,
# the export must succeed or startup raises.


def test_m13_openapi_export_writes_file_when_env_set(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from direct_control.main import _maybe_export_openapi

    target = tmp_path / "out" / "openapi.json"
    monkeypatch.setenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", str(target))
    _maybe_export_openapi(FastAPI())
    assert target.exists()
    assert "openapi" in target.read_text()


def test_m13_openapi_export_raises_on_unwritable_path(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from direct_control.main import _maybe_export_openapi

    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    target = blocker / "openapi.json"
    monkeypatch.setenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", str(target))

    with pytest.raises(RuntimeError) as excinfo:
        _maybe_export_openapi(FastAPI())

    msg = str(excinfo.value)
    assert "OpenAPI schema export" in msg
    assert "OPHYD_SERVICE_OPENAPI_EXPORT_PATH" in msg
    assert str(target) in msg


def test_m13_openapi_export_no_op_when_env_unset(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from direct_control.main import _maybe_export_openapi

    monkeypatch.delenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", raising=False)
    _maybe_export_openapi(FastAPI())
    assert list(tmp_path.iterdir()) == []


# ─── M11: int-array → ASCII heuristic removed from _convert_value ────────────
#
# Pre-fix: any uint/int ndarray whose values were all <256 was rendered
# as a string after dropping zero entries. Legitimate uint8 status bytes
# came through as garbled chars (and lost the zero values entirely). Now
# arrays pass through as lists; clients that need DBR_CHAR-as-string
# semantics decode based on the dtype/shape metadata on PVValue.


def test_m11_uint8_array_passes_through_as_list_not_ascii_string():
    import numpy as np

    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    # Status-byte array — pre-fix this became "\x01\x02\x03" (and dropped any zero).
    raw = np.array([1, 2, 3, 0, 4], dtype=np.uint8)

    converted = mgr._convert_value(raw)

    assert converted == [1, 2, 3, 0, 4], (
        "uint8 array must come through as list — pre-M11 the ASCII heuristic "
        "rendered it as a string and silently dropped zeros"
    )


def test_m11_int_array_with_small_values_passes_through_as_list():
    import numpy as np

    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    # Pre-fix any int array with all values <256 triggered the ASCII path.
    raw = np.array([10, 20, 30], dtype=np.int32)

    converted = mgr._convert_value(raw)

    assert converted == [10, 20, 30]


def test_m11_float_array_unchanged():
    import numpy as np

    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    raw = np.array([1.5, 2.5], dtype=np.float64)

    converted = mgr._convert_value(raw)

    assert converted == [1.5, 2.5]


def test_m11_bytes_still_decode_to_string():
    """Bytes-typed scalar values (e.g. DBR_CHAR scalar) keep their string decode.

    The M11 fix only removes the *array*-to-ASCII heuristic; the scalar
    bytes branch is the right place for a known-string EPICS value.
    """
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())

    assert mgr._convert_value(b"hello") == "hello"


# ─── M14: streaming PV callbacks must populate access bits ───────────────────
#
# Pre-fix: ``_handle_value_update`` and ``_handle_meta_update`` built
# PVUpdate (and the cached PVValue) without setting read/write_access.
# The pydantic defaults silently produced ``read_access=True`` /
# ``write_access=False`` regardless of CA reality. M14 plumbs the real
# bits through ``_extract_access_bits`` and flips the model defaults to
# False so a future construction-site miss surfaces as locked-out
# (under-permissive) instead of fully-readable (over-permissive).


def test_m14_pvvalue_default_access_bits_are_locked_out():
    from datetime import datetime

    from direct_control.models import PVValue

    pv_value = PVValue(
        pv_name="IOC:x",
        value=1.0,
        timestamp=datetime.now(),
    )

    assert pv_value.read_access is False, (
        "PVValue must default read_access=False — pre-M14 was True"
    )
    assert pv_value.write_access is False, (
        "PVValue must default write_access=False — pre-M14 was True"
    )


def test_m14_pvupdate_default_access_bits_are_locked_out():
    from datetime import datetime

    from direct_control.models import PVUpdate

    update = PVUpdate(
        pv="IOC:x",
        value=1.0,
        timestamp=datetime.now(),
    )

    assert update.read_access is False, (
        "PVUpdate must default read_access=False — pre-M14 was True"
    )
    assert update.write_access is False


def test_m14_pvinfo_default_access_bits_are_locked_out():
    from datetime import datetime

    from direct_control.models import PVInfo

    info = PVInfo(
        pv_name="IOC:x",
        connected=True,
        timestamp=datetime.now(),
    )

    assert info.read_access is False, "PVInfo defaults flipped to False post-M14"
    assert info.write_access is False


def test_m14_pvvalueresponse_default_access_bits_are_locked_out():
    from datetime import datetime

    from direct_control.models import PVValueResponse

    resp = PVValueResponse(pv_name="IOC:x", value=1.0, timestamp=datetime.now())

    assert resp.read_access is False
    assert resp.write_access is False


def _install_signal_with_access(mgr, pv_name: str, *, read_access: bool, write_access: bool):
    """Install a stub EpicsSignal in the manager's registry exposing access bits.

    The streaming callbacks read access bits from
    ``self._signals[pv_name]._read_pv``; fixing them up bypasses the
    real pyepics subscribe path and lets us drive the value/meta
    handlers directly with deterministic access state.
    """

    class _ReadPV:
        pass

    rpv = _ReadPV()
    rpv.read_access = read_access  # type: ignore[attr-defined]
    rpv.write_access = write_access  # type: ignore[attr-defined]

    class _Signal:
        _read_pv = rpv

    mgr._signals[pv_name] = _Signal()  # type: ignore[assignment]


def test_m14_extract_access_bits_returns_real_values_from_read_pv():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    _install_signal_with_access(mgr, "IOC:x", read_access=True, write_access=False)

    assert mgr._extract_access_bits("IOC:x") == (True, False)


def test_m14_extract_access_bits_locks_out_when_signal_missing():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())

    assert mgr._extract_access_bits("IOC:never_subscribed") == (False, False)


def test_m14_extract_access_bits_locks_out_when_read_pv_missing():
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())

    class _SignalNoReadPV:
        pass

    mgr._signals["IOC:x"] = _SignalNoReadPV()  # type: ignore[assignment]

    assert mgr._extract_access_bits("IOC:x") == (False, False)


def test_m14_streaming_value_update_propagates_access_bits():
    from direct_control.config import Settings
    from direct_control.models import PVUpdate
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    mgr = PVMonitorManager(Settings())
    _install_signal_with_access(mgr, "IOC:x", read_access=True, write_access=False)

    captured: list[PVUpdate] = []

    def cb(update):
        captured.append(update)

    mgr._callbacks["IOC:x"] = [_Subscriber(callback=cb, on_error=None)]

    mgr._handle_value_update("IOC:x", 42.0, timestamp=None)

    assert len(captured) == 1, "callback must fire on value update"
    assert captured[0].read_access is True, (
        "streaming PVUpdate must carry the real read_access — pre-M14 it "
        "fell back to the model default and silently asserted True regardless"
    )
    assert captured[0].write_access is False, (
        "streaming PVUpdate must carry the real write_access — pre-M14 it "
        "fell back to write_access=False (here that's accidentally correct, "
        "but only because the default happened to match the locked state)"
    )


def test_m14_streaming_value_update_caches_access_bits_in_pvvalue():
    """Pre-M14 the cached PVValue inherited PVValue's permissive True/True.

    A subsequent ``_handle_refresh`` or ``_send_current_values`` reads
    that cache; if the streaming path doesn't populate access bits, the
    cached value silently misrepresents the PV's CA access right after
    the very first update — the M7 propagation tests then become moot.
    """
    from direct_control.config import Settings
    from direct_control.monitoring.pv_monitor import PVMonitorManager

    mgr = PVMonitorManager(Settings())
    _install_signal_with_access(mgr, "IOC:locked", read_access=False, write_access=False)

    mgr._handle_value_update("IOC:locked", 7.0, timestamp=None)

    cached = mgr._latest_values["IOC:locked"]
    assert cached.read_access is False
    assert cached.write_access is False, (
        "cached PVValue must reflect the real CA bits — pre-M14 the "
        "streaming path inherited PVValue's True/True defaults"
    )


def test_m14_streaming_meta_update_propagates_access_bits():
    from direct_control.config import Settings
    from direct_control.models import PVUpdate
    from direct_control.monitoring.pv_monitor import PVMonitorManager, _Subscriber

    mgr = PVMonitorManager(Settings())
    _install_signal_with_access(mgr, "IOC:x", read_access=True, write_access=True)
    # Prime the connection-tracking so the meta handler treats this as
    # a real transition (not the first-and-connected dedupe).
    mgr._connection_status["IOC:x"] = True

    captured: list[PVUpdate] = []

    def cb(update):
        captured.append(update)

    mgr._callbacks["IOC:x"] = [_Subscriber(callback=cb, on_error=None)]

    # Disconnect transition.
    mgr._handle_meta_update("IOC:x", connected=False)

    assert len(captured) == 1
    assert captured[0].connected is False
    assert captured[0].read_access is True, (
        "meta-update PVUpdate must carry real access bits — pre-M14 it "
        "would have silently reported the pydantic defaults instead"
    )
    assert captured[0].write_access is True


def test_m14_streaming_value_update_locks_out_when_signal_already_unregistered():
    """If the value handler fires after a teardown race, defaults must be locked-out.

    The handler returns early when ``pv_name not in self._signals`` so
    no callback fires — but if a future refactor re-orders that guard,
    the default access bits must still be safe.
    """
    from datetime import datetime

    from direct_control.models import PVUpdate

    update = PVUpdate(pv="IOC:x", value=1.0, timestamp=datetime.now())

    # Construction without explicit access bits ⇒ post-M14 defaults
    # ⇒ both False ⇒ a UI driven by this would refuse to write.
    assert update.read_access is False
    assert update.write_access is False
