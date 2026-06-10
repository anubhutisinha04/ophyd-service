"""
Side-A integration, Direction 2: config-service's ``DirectControlClient`` against
the REAL direct-control enrichment endpoint.

Companion to ``test_config_service_integration.py`` (Direction 1, direct-control
-> config). Here the arrow reverses: configuration_service's
``DirectControlClient`` (``POST /api/v1/devices/enrich``) is driven against a
real, running direct-control FastAPI app -- including live ophyd introspection
against the caproto test IOC -- so the test pins the actual enrich wire contract
(request shape, the ``{ok, pv_name, error_type, message}`` result rows, 1:1
ordering, and per-item error categories) that config-service's MockTransport unit
test (``configuration_service/tests/test_direct_control_client.py``) cannot.

This lives in direct-control's suite, not config-service's, because the enrich
endpoint needs the caproto test IOC (conftest.py's autouse fixtures) for the
instantiated device's components to connect -- config-service's own test env has
no IOC.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("configuration_service")

from ophyd import Component as Cpt, Device, EpicsSignal  # noqa: E402

from configuration_service.direct_control_client import (  # noqa: E402
    DirectControlClient,
    EnrichmentSpec,
)


class _EnrichTestDevice(Device):
    """Module-scope so the ophyd cache's ``importlib.import_module`` can resolve
    it by ``__module__ + __qualname__``.

    ``counter`` resolves to ``IOC:counter`` on the caproto test IOC when the
    device is instantiated with prefix ``IOC:``.
    """

    counter = Cpt(EpicsSignal, "counter")


# Path the enrich endpoint imports the class from (mirrors test_enrich_endpoint).
_CLASS_PATH = f"{__name__}._EnrichTestDevice"


@pytest_asyncio.fixture
async def dc_enrich_client(client) -> AsyncIterator[DirectControlClient]:
    """config-service's DirectControlClient wired to the real direct-control app.

    ``client`` (conftest) is a TestClient whose ``__enter__`` already ran the
    direct-control lifespan, so ``app.state.ophyd_cache`` is live. We point
    config's async client at the same app via ASGITransport. The eagerly-created
    real httpx client is closed first and replaced with the in-process one.
    """
    dc = DirectControlClient(base_url="http://direct-control")
    await dc.aclose()
    # timeout matches DirectControlClient's own default (10s). The injected client
    # otherwise falls back to httpx's 5s default, which is too tight for the first
    # enrich call: it does live ophyd wait_for_connection against the IOC on a cold
    # cache, which can exceed 5s on slow CI and flake.
    dc._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://direct-control",
        timeout=10.0,
    )
    try:
        yield dc
    finally:
        await dc.aclose()


async def test_enrich_resolves_pv_through_config_client(dc_enrich_client):
    """Happy path: config's client resolves (class, prefix, sub_path) to a PV.

    The canonical use: config-service's static resolver returns needs_enrichment,
    calls this, and gets the live-introspected PV back.
    """
    results = await dc_enrich_client.enrich(
        [EnrichmentSpec(device_class_path=_CLASS_PATH, prefix="IOC:", sub_path="counter")]
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].pv_name == "IOC:counter"
    assert results[0].error_type is None


async def test_enrich_per_item_error_maps_through_config_client(dc_enrich_client):
    """A bad sub_path comes back as a parsed EnrichmentResult(ok=False, ...).

    Per-item failure is a NORMAL 200 response (not DirectControlUnavailable);
    the client must surface the real error_type/message, not collapse it.
    """
    results = await dc_enrich_client.enrich(
        [EnrichmentSpec(device_class_path=_CLASS_PATH, prefix="IOC:", sub_path="does_not_exist")]
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error_type == "NoSuchAttr"
    assert "does_not_exist" in (results[0].message or "")


async def test_enrich_batch_order_and_mixed_outcomes(dc_enrich_client):
    """Batch returns 1:1 ordered results with mixed ok/fail.

    Exercises the client's count-match guard (len(results) == len(specs)) and
    order preservation against the real endpoint, not a shaped mock.
    """
    results = await dc_enrich_client.enrich(
        [
            EnrichmentSpec(device_class_path=_CLASS_PATH, prefix="IOC:", sub_path="counter"),
            EnrichmentSpec(device_class_path=_CLASS_PATH, prefix="IOC:", sub_path="bogus"),
        ]
    )
    assert [r.ok for r in results] == [True, False]
    assert results[0].pv_name == "IOC:counter"
    assert results[1].error_type == "NoSuchAttr"
