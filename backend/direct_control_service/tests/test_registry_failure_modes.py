"""Registry-outage failure modes must fail CLOSED and truthfully.

A configuration_service 5xx during registry
lookups used to be misreported or, worse, silently bypassed the device-lock
gate:

- ``get_owning_device`` returned None on ANY non-200, which ``set_pv`` read
  as "standalone PV, no lock concept" — a write to a plan-LOCKED device's PV
  could proceed during a transient registry outage.
- ``validate_pv``/``validate_device`` raised RegistryValidationError on
  unexpected statuses, which the HTTP layer maps to 404 "not found" — a
  registry outage masqueraded as a missing PV/device.

Now: 404 means not-found; everything else raises RuntimeError (503 at the
endpoints), and the owner-lookup failure surfaces as a coordination-gate
failure without issuing the EPICS write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock

import httpx
import pytest

from direct_control.config import Settings
from direct_control.models import (
    CoordinationCheckError,
    CoordinationStatus,
    DeviceLockStatus,
    PVSetRequest,
    ServiceAvailability,
)
from direct_control.registry_client import RegistryClient, RegistryValidationError


def _client_with(handler) -> RegistryClient:
    client = RegistryClient(Settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://stub"
    )
    return client


def _status_handler(status_code: int, json_body: Optional[dict] = None):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(status_code, json=json_body if json_body is not None else {})

    return handler, calls


# ===== validate_pv / validate_device: 5xx is 503, never 404 ==================


async def test_validate_pv_5xx_raises_unavailable_not_notfound():
    handler, calls = _status_handler(503)
    client = _client_with(handler)
    try:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await client.validate_pv("X:PV")
        # The outage is NOT cached as nonexistence: recovery is immediate.
        assert "X:PV" not in client._pv_cache
        with pytest.raises(RuntimeError):
            await client.validate_pv("X:PV")
        assert calls["count"] == 2
    finally:
        await client.cleanup()


async def test_validate_pv_404_still_raises_validation_error():
    handler, _ = _status_handler(404)
    client = _client_with(handler)
    try:
        with pytest.raises(RegistryValidationError):
            await client.validate_pv("X:PV")
    finally:
        await client.cleanup()


async def test_validate_device_500_raises_unavailable_not_notfound():
    handler, _ = _status_handler(500)
    client = _client_with(handler)
    try:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await client.validate_device("motor1")
        assert "motor1" not in client._device_cache
    finally:
        await client.cleanup()


# ===== get_owning_device: fail closed on 5xx =================================


async def test_get_owning_device_404_is_standalone_none():
    handler, _ = _status_handler(404)
    client = _client_with(handler)
    try:
        assert await client.get_owning_device("X:PV") is None
    finally:
        await client.cleanup()


async def test_get_owning_device_5xx_raises_instead_of_none():
    handler, _ = _status_handler(502)
    client = _client_with(handler)
    try:
        with pytest.raises(RuntimeError, match="HTTP 502"):
            await client.get_owning_device("X:PV")
        # Not cached: a later healthy lookup must see the real owner.
        assert "X:PV" not in client._pv_owner_cache
    finally:
        await client.cleanup()


# ===== set_pv: lock gate fails closed, no EPICS write issued =================


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


class _OutageRegistry:
    """Owner lookup hits a registry outage (what RegistryClient raises)."""

    async def validate_pv(self, pv_name: str) -> None:
        return None

    async def validate_device(self, device_name: str) -> None:
        return None

    async def get_owning_device(self, pv_name: str):
        raise RuntimeError("Configuration service registry error: HTTP 503")

    async def get_instantiation_spec(self, device_name: str):
        return None

    async def cleanup(self) -> None:
        return None


async def test_set_pv_fails_closed_when_owner_lookup_errors():
    """The EPICS write must NOT be issued when the owning
    device can't be determined — pre-fix the None fallback skipped the
    device-lock gate and wrote to a possibly plan-locked device."""
    from direct_control.device_controller import DeviceController
    from direct_control.device_manager import DeviceManager

    settings = Settings()
    controller = DeviceController(
        settings, _AvailableCoord(), _OutageRegistry(), DeviceManager(settings)  # type: ignore[arg-type]
    )
    controller._execute_put = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(CoordinationCheckError, match="owning device"):
        await controller.set_pv(PVSetRequest(pv_name="X:PV", value=1.0))

    controller._execute_put.assert_not_called()


async def test_set_pv_endpoint_returns_503_on_owner_lookup_outage(client):
    """End to end through the HTTP layer: registry outage during the owner
    lookup → 503 (coordination failure), not 200 and not 404."""
    app = client.app
    app.state.device_controller.registry_client = _OutageRegistry()

    r = client.post("/api/v1/pv/set", json={"pv_name": "X:PV", "value": 1.0})
    assert r.status_code == 503
    assert "owning device" in r.json()["detail"]
