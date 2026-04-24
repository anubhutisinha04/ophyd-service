"""
Shared pytest fixtures.

`test_ioc` spins up the caproto test IOC in a subprocess and tears it down at
session end (borrowed from ophyd-websocket's conftest pattern). `client`
builds a FastAPI TestClient against the service with coordination and
registry validation stubbed so write paths don't require the real
experiment_execution / configuration services.

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
    # Keep coordination / registry pointed at harmless URLs; the `client`
    # fixture swaps real clients for stubs after lifespan runs.
    os.environ["DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL"] = "http://localhost:0"
    os.environ["DIRECT_CONTROL_CONFIGURATION_SERVICE_URL"] = "http://localhost:0"
    yield


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

    class _StubRegistry:
        async def validate_pv(self, pv_name: str) -> None:
            return None

        async def validate_device(self, device_name: str) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    mock_coord = MockCoordinationClient(always_available=True)
    stub_registry = _StubRegistry()

    fastapi_app.dependency_overrides[get_coordination_client] = lambda: mock_coord
    fastapi_app.dependency_overrides[get_registry_client] = lambda: stub_registry

    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


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

        class _StubRegistry:
            async def validate_pv(self, pv_name: str) -> None:
                return None

            async def validate_device(self, device_name: str) -> None:
                return None

            async def cleanup(self) -> None:
                return None

        stub_registry = _StubRegistry()
        app.state.registry_client = stub_registry
        if hasattr(app.state, "ws_manager"):
            app.state.ws_manager.registry_client = stub_registry

        yield c
