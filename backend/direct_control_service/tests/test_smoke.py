"""
Smoke tests: the service imports, lifespan runs, /health and /api/v1/stats
are reachable. These don't talk to EPICS, but they do run under the
`test_ioc` session fixture so every test file sees the same CA address.
"""


def test_imports():
    """Package imports without executing the app."""
    import direct_control  # noqa: F401
    import direct_control.config  # noqa: F401
    import direct_control.models  # noqa: F401
    import direct_control.protocols  # noqa: F401


def test_health_endpoint_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    # Mock coordination reports available → healthy. (Pre-S5 the unhealthy
    # branch was "degraded"; S5 also flips status code to 503 in that case
    # so this code path is the only one that yields 200.)
    assert body["status"] == "healthy"
    assert body["coordination_service_available"] is True


def test_stats_endpoint_returns_200(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "direct_control"
    assert "pv_socket" in body
    assert "device_socket" in body


def test_unsupported_accept_returns_406(client):
    r = client.get("/api/v1/pv/IOC:m1/value", headers={"accept": "image/png"})
    assert r.status_code == 406


# ===== LockedWS size cap (pure unit, no IOC) =====


from unittest.mock import AsyncMock

import pytest

from direct_control.monitoring._envelopes import LockedWS, WebSocketResponseTooLarge


async def test_locked_ws_passes_small_payload_when_cap_set():
    fake_ws = AsyncMock()
    locked = LockedWS(fake_ws, max_message_bytes=1024)
    await locked.send_json({"type": "heartbeat", "ts": "2026"})
    fake_ws.send_text.assert_awaited_once()


async def test_locked_ws_raises_on_oversize_json():
    fake_ws = AsyncMock()
    locked = LockedWS(fake_ws, max_message_bytes=20)
    with pytest.raises(WebSocketResponseTooLarge, match="exceeds"):
        await locked.send_json({"value": "x" * 100})
    fake_ws.send_text.assert_not_awaited()


async def test_locked_ws_no_cap_allows_any_size():
    fake_ws = AsyncMock()
    locked = LockedWS(fake_ws)  # no max_message_bytes
    await locked.send_json({"value": "x" * 10_000})
    fake_ws.send_text.assert_awaited_once()
