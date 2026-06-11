"""Tests for ``POST /api/v1/devices/enrich``.

The endpoint instantiates ophyd device classes and walks their attribute
trees against a live EPICS endpoint. We exercise it with small test
classes wired to the caproto test IOC (``IOC:counter``, ``IOC:m1``) so
each test class's components actually connect.

Two helper classes live at module scope so importlib can resolve them
by ``__module__ + __qualname__`` (the cache uses ``importlib.import_module``).
"""

from __future__ import annotations

from ophyd import Component as Cpt, Device, EpicsSignal, FormattedComponent as FmtCpt


# ---------------------------------------------------------------------------
# Test ophyd classes wired to the caproto test IOC PVs
# ---------------------------------------------------------------------------


class _TestDeviceWithCpt(Device):
    """Simple device whose components are direct EpicsSignals on the test IOC.

    The instantiation prefix is ``IOC:`` so each component resolves to a
    real PV (``IOC:counter``, ``IOC:m1``) that the caproto test IOC
    serves. Without that, ophyd's lazy Component access would block on
    ``wait_for_connection``.
    """

    counter = Cpt(EpicsSignal, "counter")
    m1 = Cpt(EpicsSignal, "m1")


class _TestInnerWithFmtCpt(Device):
    """Sub-device whose FmtCpt references its parent's prefix.

    Mirrors the IOS pattern (``MirrorAxis.actuate``): the formatted
    suffix uses ``{self.parent.prefix}``, which only has meaning when
    the device is instantiated as a *child* of another Device — so we
    nest it inside ``_TestDeviceWithFmtCpt`` below.
    """

    # Default add_prefix=("suffix",) on FormattedComponent enables format
    # interpolation; {self.parent.prefix} resolves to the outer device's
    # prefix at instantiation time. (Adding add_prefix=() would disable
    # the formatting entirely — the placeholder string would reach pyepics
    # raw and trip BadPVName.)
    counter_via_fmt = FmtCpt(EpicsSignal, "{self.parent.prefix}counter")


class _TestDeviceWithFmtCpt(Device):
    """Outer device that contains the FmtCpt-bearing inner.

    This is the canonical case the enrichment endpoint exists to handle:
    config-service's static resolver can't fill in
    ``{self.parent.prefix}`` from the class alone and returns
    ``needs_enrichment``. Once instantiated, ophyd materializes the
    formatted suffix into the real PV (``IOC:counter``).
    """

    inner = Cpt(_TestInnerWithFmtCpt, "")


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


def test_enrich_simple_cpt_walk(client):
    """Direct Component on a top-level device class — happy path."""
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "counter",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["ok"] is True, row
    assert row["pv_name"] == "IOC:counter"


def test_enrich_fmt_cpt_with_runtime_placeholder(client):
    """The case the endpoint exists for: FmtCpt with {self.parent.prefix}.

    Static resolution can't fill this in; the live device materializes
    the suffix and we read pvname off the leaf signal.
    """
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithFmtCpt",
                    "prefix": "IOC:",
                    "sub_path": "inner.counter_via_fmt",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    row = r.json()["results"][0]
    assert row["ok"] is True, row
    assert row["pv_name"] == "IOC:counter"


def test_enrich_intermediate_device_returns_not_a_pv_leaf(client):
    """sub_path landing on an intermediate Device (not a leaf signal)
    should return NotAPVLeaf — the walked attr exists but exposes neither
    ``pvname`` (classic ophyd EpicsSignal) nor ``.source`` (ophyd-async
    Signal), so there's no PV to caput against.
    """
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithFmtCpt",
                    "prefix": "IOC:",
                    "sub_path": "inner",  # lands on the inner Device, not a leaf
                }
            ]
        },
    )
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["ok"] is False
    assert row["error_type"] == "NotAPVLeaf"
    assert "not a PV-bearing signal" in row["message"]


def test_enrich_unknown_sub_path_returns_no_such_attr(client):
    """A typo'd sub_path should fail per-item with NoSuchAttr."""
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "does_not_exist",
                }
            ]
        },
    )
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["ok"] is False
    assert row["error_type"] == "NoSuchAttr"
    assert "does_not_exist" in row["message"]


def test_enrich_unknown_device_class_returns_instantiation_failed(client):
    """Bad import paths are caught and reported, not raised."""
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": "nonexistent_module.SomeClass",
                    "prefix": "IOC:",
                    "sub_path": "x",
                }
            ]
        },
    )
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["ok"] is False
    assert row["error_type"] == "InstantiationFailed"
    assert "nonexistent_module" in row["message"]


def test_enrich_batch_with_mixed_outcomes(client):
    """Best-effort per item — one bad row doesn't fail the others."""
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "counter",
                },
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "bogus",
                },
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "m1",
                },
            ]
        },
    )
    assert r.status_code == 200
    rows = r.json()["results"]
    assert [row["ok"] for row in rows] == [True, False, True]
    assert rows[0]["pv_name"] == "IOC:counter"
    assert rows[1]["error_type"] == "NoSuchAttr"
    assert rows[2]["pv_name"] == "IOC:m1"


def test_enrich_cache_hit_reuses_device_instance(client):
    """Second call with the same (class, prefix) reuses the cached device.

    Verified by checking ``ophyd_cache.size()`` is 1 after two calls
    against the same key. (We don't directly assert call-count savings
    because ophyd lazy-instantiates sub-components; the test class makes
    that observable via the cache size.)
    """
    spec = {
        "device_class_path": f"{__name__}._TestDeviceWithCpt",
        "prefix": "IOC:",
        "sub_path": "counter",
    }
    r1 = client.post("/api/v1/devices/enrich", json={"items": [spec]})
    r2 = client.post("/api/v1/devices/enrich", json={"items": [spec]})
    assert r1.status_code == 200 and r2.status_code == 200

    cache = client.app.state.ophyd_cache
    assert cache.size() == 1


def test_enrich_empty_request_rejected(client):
    """Empty items list is a pydantic validation error."""
    r = client.post("/api/v1/devices/enrich", json={"items": []})
    assert r.status_code == 422


def test_enrich_max_length_enforced(client):
    """201-item request is rejected by the pydantic max_length guard."""
    spec = {
        "device_class_path": f"{__name__}._TestDeviceWithCpt",
        "prefix": "IOC:",
        "sub_path": "counter",
    }
    r = client.post(
        "/api/v1/devices/enrich",
        json={"items": [spec for _ in range(201)]},
    )
    assert r.status_code == 422


def test_enrich_extra_field_rejected(client):
    """extra='forbid' rejects unknown top-level keys."""
    r = client.post(
        "/api/v1/devices/enrich",
        json={
            "items": [
                {
                    "device_class_path": f"{__name__}._TestDeviceWithCpt",
                    "prefix": "IOC:",
                    "sub_path": "counter",
                }
            ],
            "spurious": True,
        },
    )
    assert r.status_code == 422


# Cache teardown is intentionally NOT done here anymore. The service lifespan
# clears app.state.ophyd_cache on shutdown (see direct_control.main), and the
# `client` fixture re-runs the lifespan per test — so every test starts with a
# fresh, empty cache and instantiated devices' CA channels are released on
# teardown, with no per-file fixture to keep in sync.
