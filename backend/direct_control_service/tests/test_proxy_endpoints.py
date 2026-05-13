"""
Coverage for four GET endpoints:

  - GET /api/v1/devices                       (proxies to configuration_service)
  - GET /api/v1/devices/{name}                (proxies to configuration_service)
  - GET /api/v1/devices/{name}/bundle         (proxies to configuration_service)
  - GET /api/v1/pvs/connected                 (no proxy — uses pv_monitor directly)

The proxy endpoint tests use the ``install_config_http_stub`` fixture
(conftest.py) to swap the configuration_service-facing httpx client for
one backed by an ``httpx.MockTransport``, so the proxy logic runs
end-to-end without a live config service.
"""

import httpx


# ─── GET /api/v1/devices ────────────────────────────────────────────────


def test_list_devices_nominal(install_config_http_stub, client):
    """Proxy returns the list configuration_service sent."""
    payload = [
        {"name": "m1", "ophyd_class": "EpicsMotor", "is_readable": True, "is_movable": True},
        {"name": "det1", "ophyd_class": "EpicsScaler", "is_readable": True, "is_movable": False},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/devices"
        return httpx.Response(200, json=payload)

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices")
    assert r.status_code == 200
    assert r.json() == payload


def test_list_devices_filters_by_movable(install_config_http_stub, client):
    """Query-string filters are applied client-side after the proxy fetch."""
    payload = [
        {"name": "m1", "ophyd_class": "EpicsMotor", "is_movable": True},
        {"name": "det1", "ophyd_class": "EpicsScaler", "is_movable": False},
    ]

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices?movable=true")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()]
    assert names == ["m1"]


def test_list_devices_upstream_unreachable_returns_503(install_config_http_stub, client):
    """Network failure to config service surfaces as 503."""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices")
    assert r.status_code == 503
    assert "Configuration service unavailable" in r.json()["detail"]


# ─── GET /api/v1/devices/{name} ─────────────────────────────────────────


def test_get_device_nominal(install_config_http_stub, client):
    """Proxy returns the device JSON unchanged."""
    payload = {"name": "m1", "ophyd_class": "EpicsMotor", "pvs": {"user_readback": "IOC:M1.RBV"}}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/devices/m1"
        return httpx.Response(200, json=payload)

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices/m1")
    assert r.status_code == 200
    assert r.json() == payload


def test_get_device_not_found_returns_404(install_config_http_stub, client):
    """Upstream 404 surfaces as a 404 whose detail is the proxy-side ``not_found_msg``.

    ``_config_get`` doesn't forward the upstream's ``detail``; it raises with
    its own message (``f"Device not found: {device_name}"``), so the
    assertion below checks for the device name rather than the upstream text.
    """

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such device"})

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices/missing")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]


# ─── GET /api/v1/devices/{name}/bundle ─────────────────────────────────


def test_get_device_bundle_nominal(install_config_http_stub, client):
    """Bundle re-shapes the device's pvs into a grouped component tree."""
    payload = {
        "name": "m1",
        "ophyd_class": "EpicsMotor",
        "prefix": "IOC:M1",
        "is_readable": True,
        "is_movable": True,
        "pvs": {
            "user_readback": "IOC:M1.RBV",
            "velocity.value": "IOC:M1.VELO",
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/devices/m1"
        return httpx.Response(200, json=payload)

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices/m1/bundle")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "m1"
    assert body["class"] == "EpicsMotor"
    assert body["prefix"] == "IOC:M1"
    assert body["total_signals"] == 2
    assert body["components"], "component tree should be non-empty"


def test_get_device_bundle_not_found_returns_404(install_config_http_stub, client):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    install_config_http_stub(handler)

    r = client.get("/api/v1/devices/missing/bundle")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]


# ─── GET /api/v1/pvs/connected ──────────────────────────────────────────


def test_list_connected_pvs_nominal(client):
    """Pure getter on ``pv_monitor`` — empty list is the legitimate fresh-service shape."""
    r = client.get("/api/v1/pvs/connected")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    for item in body:
        assert isinstance(item, str)
