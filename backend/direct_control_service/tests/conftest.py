"""
Shared pytest fixtures.

`test_ioc` spins up the caproto test IOC in a subprocess and tears it down at
session end (borrowed from ophyd-websocket's conftest pattern). `client`
builds a FastAPI TestClient against the service with coordination and
registry validation stubbed so write paths don't require a real
configuration_service (which is the only HTTP backend direct_control talks
to — both the registry and the device-lock state live there).

Env setup happens *before* importing `direct_control.*` because pyepics
reads EPICS_CA_ADDR_LIST at import time.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

_IOC_PORT = 5064  # default EPICS CA
_IOC_ADDR = f"localhost:{_IOC_PORT}"

# NOTE: Hardcoded port 5064 (EPICS Channel Access default).
# This fixture supports two modes of parallel testing:
#   1. Single host, multiple containers: Each container gets its own isolated IOC
#      on 5064 (the compose network isolates ports).
#   2. Single machine, multiple processes: Parallel pytest runs on the same machine
#      will reuse an existing IOC if one is already bound to 5064 (line 42-45).
#      This assumes the IOC has compatible PVs (see test_ioc.py).
#      If you need truly parallel tests on the same machine without reuse, run in
#      separate containers or use pytest-xdist with worker isolation.


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


@pytest.fixture(scope="session")
def test_ioc() -> Iterator[None]:
    """Start the caproto test IOC for the session, or reuse one on :5064."""
    pytest.importorskip("caproto")

    if _port_in_use(_IOC_PORT):
        # Someone else is running an IOC on 5064; assume it has compatible PVs.
        yield
        return

    ioc_script = Path(__file__).parent / "test_ioc.py"
    proc = subprocess.Popen(
        [sys.executable, str(ioc_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the IOC to bind. caproto starts fast; 3s is plenty.
    for _ in range(30):
        if _port_in_use(_IOC_PORT):
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=2)
        raise RuntimeError(
            "Test IOC failed to start.\n"
            f"STDOUT: {stdout.decode(errors='replace')}\n"
            f"STDERR: {stderr.decode(errors='replace')}"
        )

    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session", autouse=True)
def _epics_env(test_ioc):
    """
    Point pyepics at the test IOC before any `direct_control` import.

    autouse so that importing `direct_control.main` (which triggers pyepics
    imports via the monitoring subpackage) always sees these values.
    """
    os.environ["DIRECT_CONTROL_EPICS_CA_ADDR_LIST"] = _IOC_ADDR
    os.environ["DIRECT_CONTROL_EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    # Point at a harmless URL; the `client` fixture swaps the real
    # configuration_service client for a stub after lifespan runs.
    os.environ["DIRECT_CONTROL_CONFIGURATION_SERVICE_URL"] = "http://localhost:0"
    # Skip the startup readiness probe: the unit-test app boots its lifespan
    # against the harmless URL above (no real config-service), so the probe
    # would block then fail. Integration tests that want the real probe drive
    # is_service_available() directly instead of booting the app.
    os.environ["DIRECT_CONTROL_CONFIG_SERVICE_STARTUP_PROBE"] = "false"
    # Enable control: read-only defaults to true, but the suite exercises the
    # write paths (PV set, device execute, WS set/stop). The read-only gate has
    # its own dedicated tests that flip this on explicitly.
    os.environ["DIRECT_CONTROL_GLOBAL_READ_ONLY"] = "false"
    yield


class _StubRegistry:
    """Registry client stub used by both `app` and `client` fixtures.

    Stateless — every method returns None — so it's safe to share a single
    class definition across fixtures even though each fixture instantiates
    its own. PVs are reported as standalone (no owning device) so the
    coord-gate falls through to the mock coordination client.
    """

    async def validate_pv(self, pv_name: str) -> None:
        return None

    async def validate_device(self, device_name: str) -> None:
        return None

    async def get_owning_device(self, pv_name: str):
        return None

    async def get_instantiation_spec(self, device_name: str):
        # No class info — device-level control paths get a clean 422; tests
        # that exercise live device control install a spec-bearing stub.
        return None

    async def cleanup(self) -> None:
        return None


@pytest.fixture
def app():
    """
    The FastAPI app with coordination + registry stubbed out.

    Uses dependency_overrides for REST endpoints, and monkey-patches
    `app.state` after lifespan has run so the WS managers (which captured
    the original refs at construction) also see the stubs.
    """
    from direct_control.main import (
        app as fastapi_app,
        get_coordination_client,
        get_registry_client,
    )
    from direct_control.protocols import MockCoordinationClient

    mock_coord = MockCoordinationClient(always_available=True)
    stub_registry = _StubRegistry()

    fastapi_app.dependency_overrides[get_coordination_client] = lambda: mock_coord
    fastapi_app.dependency_overrides[get_registry_client] = lambda: stub_registry

    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def install_config_http_stub(app):
    """Stub the configuration_service HTTP client the PVHealthReporter posts to.

    Pass a handler ``(httpx.Request) -> httpx.Response`` and the fixture
    returns the mock client. The reporter is constructed in lifespan with the
    real ``config_http`` and stored on ``app.state``; we rebind its underlying
    client so its fire-and-forget POSTs hit the MockTransport instead.

    Async so teardown can ``await aclose()``; httpx registers anyio state at
    construction, so GC-time close isn't safe.
    """
    import httpx

    mock_client: "httpx.AsyncClient | None" = None

    def install(handler):
        nonlocal mock_client
        mock_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://stub"
        )
        if hasattr(app.state, "pv_health_reporter"):
            app.state.pv_health_reporter._client = mock_client
        return mock_client

    try:
        yield install
    finally:
        if mock_client is not None:
            await mock_client.aclose()


@pytest.fixture
def client(app):
    """FastAPI TestClient. Entering the `with` block runs lifespan."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # Lifespan has constructed real clients; swap the ones captured by
        # WS managers and the device controller so their write paths use the
        # mocks too.
        from direct_control.protocols import MockCoordinationClient

        mock_coord = MockCoordinationClient(always_available=True)
        app.state.coordination_client = mock_coord
        if hasattr(app.state, "device_controller"):
            app.state.device_controller.coordination = mock_coord

        stub_registry = _StubRegistry()
        app.state.registry_client = stub_registry
        if hasattr(app.state, "ws_manager"):
            app.state.ws_manager.registry_client = stub_registry
        for image_mgr in ("camera_ws_manager", "tiff_ws_manager"):
            if hasattr(app.state, image_mgr):
                getattr(app.state, image_mgr).registry_client = stub_registry
        if hasattr(app.state, "device_controller"):
            # device_controller owns a registry_client for the PV→owning-device
            # lookup that drives the disabled/locked gate on PV-keyed writes.
            app.state.device_controller.registry_client = stub_registry

        yield c
