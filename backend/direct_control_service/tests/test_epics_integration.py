"""
End-to-end tests against the caproto test IOC.

Exercise the real service ↔ pyepics ↔ caproto IOC path. The `test_ioc`
session fixture (autouse via `_epics_env`) guarantees the IOC is up and
DIRECT_CONTROL_EPICS_CA_ADDR_LIST points at it.
"""

import time

import pytest


def test_get_scalar_returns_envelope(client):
    """GET /api/v1/pv/{name}/value returns tiled-style envelope for a scalar."""
    r = client.get("/api/v1/pv/IOC:counter/value")
    assert r.status_code == 200
    body = r.json()
    assert body["pv_name"] == "IOC:counter"
    assert body["shape"] == []
    assert body["ndim"] == 0
    assert isinstance(body["value"], int)


def test_get_waveform_returns_envelope_with_shape(client):
    """1-D waveform shows up with shape/dtype metadata."""
    r = client.get("/api/v1/pv/IOC:wf1/value")
    assert r.status_code == 200
    body = r.json()
    assert body["pv_name"] == "IOC:wf1"
    assert body["shape"] == [20]
    assert body["ndim"] == 1
    assert body["dtype"] is not None
    assert body["nbytes"] > 0
    assert len(body["value"]) == 20


def test_get_waveform_binary_mode(client):
    """Binary content negotiation returns raw bytes + X-PV-* headers."""
    r = client.get(
        "/api/v1/pv/IOC:wf1/value",
        headers={"accept": "application/octet-stream"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["X-PV-Name"] == "IOC:wf1"
    assert r.headers["X-PV-Shape"] == "20"
    assert r.headers["X-PV-Ndim"] == "1"
    nbytes = int(r.headers["X-PV-Nbytes"])
    assert len(r.content) == nbytes


def test_get_enum_as_string(client):
    """as_string=true returns the label, not the index."""
    r = client.get("/api/v1/pv/IOC:shutter/value?as_string=true")
    assert r.status_code == 200
    body = r.json()
    # caproto's enum PV exposes the label when requested as string.
    assert body["value"] in ("Closed", "Open", "Moving")


def test_set_scalar_pv(client):
    """POST /api/v1/pv/set with wait=true round-trips through caput + caget."""
    target = 3.14
    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": target, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True

    # Read it back. The writer path uses caput (no monitor); the reader
    # here bypasses the monitor cache via use_monitor=false.
    r = client.get("/api/v1/pv/IOC:m1/value?use_monitor=false&timeout=2.0")
    assert r.status_code == 200
    assert r.json()["value"] == pytest.approx(target)


def test_pv_socket_subscribe_receives_initial_value(client):
    """Subscribe via WebSocket; receive the subscribed ack and an initial update.

    Wire shape on `pv_update` follows finch's ``ValueUpdateResponse``: the
    field is ``pv`` (not ``pv_name``). See ``finch/src/api/ophyd/
    ophydPVSocketTypes.ts:13-20``.
    """
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:counter"})

        saw_subscribed = False
        saw_update = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (saw_subscribed and saw_update):
            msg = ws.receive_json()
            if msg.get("type") == "subscribed":
                saw_subscribed = True
                assert "IOC:counter" in msg["pv_names"]
            elif msg.get("event_type") == "pv_update":
                saw_update = True
                assert msg["pv"] == "IOC:counter"
                assert msg["connected"] is True
                # finch reads timestamp as unix epoch seconds (number); a
                # regression to ISO string would silently break arithmetic
                # in TableDeviceController.tsx flash-row check.
                assert isinstance(msg["timestamp"], (int, float))

        assert saw_subscribed, "never received 'subscribed' ack"
        assert saw_update, "never received initial pv_update"


def test_pv_socket_receives_update_on_caput(client):
    """After subscribing, a caput against the IOC drives a fresh pv_update."""
    with client.websocket_connect("/api/v1/pv-socket") as ws:
        ws.send_json({"action": "subscribe", "pv": "IOC:m1"})

        # Drain initial ack + initial value.
        deadline = time.monotonic() + 3.0
        initial_update_seen = False
        while time.monotonic() < deadline and not initial_update_seen:
            msg = ws.receive_json()
            if msg.get("event_type") == "pv_update":
                initial_update_seen = True
        assert initial_update_seen

        # Fire a caput via the service itself (simpler than bringing in
        # pyepics just for this assertion).
        new_value = 7.25
        r = client.post(
            "/api/v1/pv/set",
            json={"pv_name": "IOC:m1", "value": new_value, "wait": True, "timeout": 2.0},
        )
        assert r.status_code == 200

        # Expect a pv_update reflecting the new value.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("event_type") == "pv_update" and msg.get("value") == pytest.approx(
                new_value
            ):
                return
        pytest.fail(f"never saw pv_update with value={new_value}")


def test_oversize_response_rejected(client):
    """Bytesize cap returns 400 when a response would exceed the limit."""
    # Shrink the limit so even the 20-element waveform trips it.
    from direct_control.config import Settings

    # We can't easily resize the running Settings instance; overriding the
    # instance attribute is enough because _build_value_response reads
    # `settings.response_bytesize_limit` at request time.
    app = client.app
    app.state.settings.response_bytesize_limit = 10  # absurdly small

    r = client.get("/api/v1/pv/IOC:wf1/value")
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"].lower()

    # Restore so subsequent tests in the same session aren't affected.
    app.state.settings.response_bytesize_limit = Settings().response_bytesize_limit


def test_ws_oversize_update_delivers_error_envelope(client):
    """WS path enforces ``response_bytesize_limit``: oversize monitor updates
    are dropped and the client receives a typed error envelope rather than a
    silent drop or a dropped connection. Connection stays open for other PVs.

    Wire shape follows finch's contract — error envelope carries ``error``
    and ``pv`` fields (not ``message``/``pv_name``); pv_update messages use
    ``pv`` (not ``pv_name``). See ``finch/src/api/ophyd/
    ophydPVSocketTypes.ts``.
    """
    from direct_control.config import Settings

    app = client.app
    # Calibrated for the payload sizes with alarm fields on every update:
    #   scalar pv_update (IOC:counter): ~266 bytes
    #   waveform pv_update (IOC:wf1, 20 elements): ~331 bytes
    #   error envelope: ~130 bytes
    # 300 sits between scalar and waveform, leaves room for the error.
    # Re-tune if PVUpdate fields or the JSON serialization shape changes.
    app.state.settings.response_bytesize_limit = 300

    try:
        with client.websocket_connect("/api/v1/pv-socket") as ws:
            ws.send_json({"action": "subscribe", "pv": "IOC:wf1"})

            deadline = time.monotonic() + 3.0
            saw_error = False
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg.get("type") == "error" and msg.get("pv") == "IOC:wf1":
                    assert "size limit" in msg["error"].lower()
                    saw_error = True
                    break
            assert saw_error, "never received error envelope for oversize update"

            # Connection survives; a small-payload PV still flows.
            ws.send_json({"action": "subscribe", "pv": "IOC:counter"})
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                msg = ws.receive_json()
                if msg.get("event_type") == "pv_update" and msg.get("pv") == "IOC:counter":
                    return
            pytest.fail("connection should still deliver small-payload updates")
    finally:
        app.state.settings.response_bytesize_limit = Settings().response_bytesize_limit
