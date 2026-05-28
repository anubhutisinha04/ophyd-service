"""
Contract tests for the device endpoints shared by configuration_service and
direct_control_service.

direct_control's ``/api/v1/devices`` and ``/api/v1/devices/{name}`` are
proxies to the configuration service. These tests run the proxy against the
REAL configuration service (see conftest) and assert the two agree on:

  - the LIST shape (a list of device-name strings, never objects),
  - filter behaviour (every filter is forwarded and produces identical
    results whether you hit the configuration service directly or via the
    proxy),
  - the single-device passthrough (identical body, ``pvs`` preserved),
  - error parity (404 on unknown device),
  - tolerance of unknown query params (forwarded, ignored, never a 500).

Backed by the configuration service's mock registry:
  sample_x — motor,    movable + readable
  det1     — detector, readable
  cam1     — detector, readable
(no device is flyable)
"""

from __future__ import annotations

import pytest

ALL_DEVICES = ["cam1", "det1", "sample_x"]  # sorted, as the registry returns


async def _json(client, url):
    r = await client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text}"
    return r.json()


# ── LIST shape: the string-vs-object contract ──────────────────────────


async def test_list_returns_name_strings_not_objects(direct_client):
    """The exact bug guard: the proxy must return a list of *strings*.

    The previous proxy assumed the configuration service returned a list of
    dicts and called ``d.get(...)`` on each item. The configuration service
    actually returns name strings, so any filtered call raised
    ``'str' object has no attribute 'get'``. This pins the real shape.
    """
    body = await _json(direct_client, "/api/v1/devices")
    assert isinstance(body, list)
    assert body, "registry should be non-empty with mock data"
    assert all(isinstance(name, str) for name in body), f"expected name strings, got {body!r}"


async def test_list_unfiltered_parity(direct_client, config_client):
    """Unfiltered list is identical through the proxy and direct."""
    via_proxy = await _json(direct_client, "/api/v1/devices")
    direct = await _json(config_client, "/api/v1/devices")
    assert via_proxy == direct == ALL_DEVICES


# ── Filter forwarding + parity ──────────────────────────────────────────

FILTER_QUERIES = [
    "device_label=motor",
    "device_label=detector",
    "ophyd_class=EpicsMotor",
    "ophyd_class=EpicsScaler",
    "movable=true",
    "movable=false",
    "readable=true",
    "readable=false",
    "flyable=true",
    "flyable=false",
    "pattern=cam*",
    "pattern=sample*",
    "device_label=detector&readable=true",
    "device_label=motor&movable=true",
]


@pytest.mark.parametrize("query", FILTER_QUERIES)
async def test_filter_parity_proxy_matches_config(query, direct_client, config_client):
    """Every filter is forwarded; proxy result == configuration-service result.

    This is what would have caught the silently-dropped ``device_label``:
    the proxy used to not forward it at all, so a filtered proxy call
    returned every device while the direct call returned the filtered set.
    """
    via_proxy = await _json(direct_client, f"/api/v1/devices?{query}")
    direct = await _json(config_client, f"/api/v1/devices?{query}")
    assert via_proxy == direct, f"proxy/config disagree for ?{query}"
    assert all(isinstance(n, str) for n in via_proxy)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("device_label=motor", ["sample_x"]),
        ("device_label=detector", ["cam1", "det1"]),
        ("ophyd_class=EpicsMotor", ["sample_x"]),
        ("movable=true", ["sample_x"]),
        ("movable=false", ["cam1", "det1"]),
        ("readable=true", ALL_DEVICES),
        ("readable=false", []),
        ("flyable=true", []),
        ("pattern=cam*", ["cam1"]),
        ("device_label=detector&readable=true", ["cam1", "det1"]),
    ],
)
async def test_filter_narrows_to_expected_set(query, expected, direct_client):
    """Filtering produces the right device set through the proxy (anchors behaviour).

    Parity alone would pass even if both sides were equally broken; this
    pins the concrete expected results against the known mock registry.
    """
    body = await _json(direct_client, f"/api/v1/devices?{query}")
    assert body == expected


async def test_unknown_query_param_is_tolerated(direct_client, config_client):
    """An unrecognised filter is forwarded and ignored, never a 500."""
    via_proxy = await _json(direct_client, "/api/v1/devices?bogus=whatever")
    direct = await _json(config_client, "/api/v1/devices?bogus=whatever")
    assert via_proxy == direct == ALL_DEVICES


# ── Single-device passthrough ───────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_DEVICES)
async def test_single_device_parity(name, direct_client, config_client):
    """``GET /devices/{name}`` body is identical through the proxy and direct."""
    via_proxy = await _json(direct_client, f"/api/v1/devices/{name}")
    direct = await _json(config_client, f"/api/v1/devices/{name}")
    assert via_proxy == direct


async def test_single_device_preserves_pvs(direct_client):
    """The proxy preserves the ``pvs`` map the frontend reads (DeviceMotorController)."""
    body = await _json(direct_client, "/api/v1/devices/sample_x")
    assert isinstance(body.get("pvs"), dict)
    assert "user_readback" in body["pvs"]
    assert body["ophyd_class"] == "EpicsMotor"


async def test_single_device_404_parity(direct_client, config_client):
    """Unknown device is a 404 on both sides; the proxy names the device."""
    proxy_r = await direct_client.get("/api/v1/devices/does_not_exist")
    config_r = await config_client.get("/api/v1/devices/does_not_exist")
    assert proxy_r.status_code == 404
    assert config_r.status_code == 404
    assert "does_not_exist" in proxy_r.json()["detail"]


# ── Bundle: a reshaping proxy exercised against real config data ────────


async def test_bundle_builds_component_tree_from_real_device(direct_client):
    """``/devices/{name}/bundle`` reshapes the real device's pvs into a tree."""
    body = await _json(direct_client, "/api/v1/devices/sample_x/bundle")
    assert body["name"] == "sample_x"
    assert body["class"] == "EpicsMotor"
    assert body["components"], "component tree should be non-empty"
    # total_signals mirrors the number of pvs the configuration service holds.
    device = await _json(direct_client, "/api/v1/devices/sample_x")
    assert body["total_signals"] == len(device["pvs"])
