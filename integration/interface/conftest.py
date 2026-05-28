"""
Cross-service interface fixtures: direct_control's proxy wired to a REAL
configuration_service.

Both services are mounted in-process via ``httpx.ASGITransport`` and joined
through direct_control's ``get_config_http`` dependency, so direct_control's
``/api/v1/devices*`` proxy runs against the configuration service's real
response shapes — not a hand-written stub. This is the layer that catches
contract drift between the two services (e.g. one returning a list of name
strings while the other expects a list of objects).

The configuration service builds its registry inside its lifespan, so the
``config_client`` fixture enters the lifespan context before issuing
requests. direct_control's proxy endpoints don't touch ``app.state`` (only
the injected config client), so its lifespan is intentionally not run.
"""

from __future__ import annotations

import os

import httpx
import pytest_asyncio

# pyepics reads EPICS_CA_* at import time, and direct_control.main pulls in
# the monitoring stack (hence pyepics) on import. Set harmless values before
# any direct_control import so nothing broadcasts on a real network. The
# proxy endpoints under test never open a CA connection.
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")
os.environ.setdefault("EPICS_CA_ADDR_LIST", "127.0.0.1")
os.environ.setdefault("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", "http://config.invalid")


@pytest_asyncio.fixture
async def config_client(tmp_path):
    """An httpx client backed by a real configuration_service app (mock data).

    Mock data registers three devices: ``sample_x`` (motor, movable+readable),
    ``det1`` and ``cam1`` (detectors, readable). None are flyable.
    """
    from configuration_service.config import Settings
    from configuration_service.main import create_app

    settings = Settings(use_mock_data=True, db_path=tmp_path / "config.db")
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://config") as client:
            yield client


@pytest_asyncio.fixture
async def direct_client(config_client):
    """An httpx client for direct_control whose config backend IS ``config_client``."""
    from direct_control.main import app, get_config_http

    app.dependency_overrides[get_config_http] = lambda: config_client
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://direct") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_config_http, None)
