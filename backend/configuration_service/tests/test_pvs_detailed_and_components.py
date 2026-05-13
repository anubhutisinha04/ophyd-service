"""
Coverage for two GET endpoints not exercised in other test files:

  - GET /api/v1/pvs/detailed
  - GET /api/v1/devices/{device_path:path}/component

Driven by the ``client`` fixture (mock-data create_app), which seeds
``sample_x`` and ``det1`` per MockProfileLoader.
"""


def test_get_pvs_detailed_nominal(client):
    """Returns devices→signal-path→PV mapping with consistent counts."""
    r = client.get("/api/v1/pvs/detailed")
    assert r.status_code == 200

    body = r.json()
    assert body["success"] is True
    assert isinstance(body["devices"], dict)
    assert body["device_count"] == len(body["devices"])
    assert body["pv_count"] == sum(len(pvs) for pvs in body["devices"].values())

    # MockProfileLoader ships sample_x with three PVs; sanity-check the shape.
    assert "sample_x" in body["devices"]
    assert "user_readback" in body["devices"]["sample_x"]


def test_get_nested_device_component_top_level_nominal(client):
    """Bare device name (no dot) returns top-level device metadata."""
    r = client.get("/api/v1/devices/sample_x/component")
    assert r.status_code == 200

    body = r.json()
    assert body["parent_device"] == "sample_x"
    assert body["device_path"] == "sample_x"
    assert body["component_type"] == "EpicsMotor"


def test_get_nested_device_component_signal_nominal(client):
    """Dotted path resolves to the signal's PV mapping."""
    r = client.get("/api/v1/devices/sample_x.user_readback/component")
    assert r.status_code == 200

    body = r.json()
    assert body["parent_device"] == "sample_x"
    assert body["device_path"] == "sample_x.user_readback"
    assert body["pv"] == "BL01:SAMPLE:X.RBV"


def test_get_nested_device_component_unknown_device_returns_404(client):
    r = client.get("/api/v1/devices/nonexistent/component")
    assert r.status_code == 404
    assert "nonexistent" in r.json()["detail"]
