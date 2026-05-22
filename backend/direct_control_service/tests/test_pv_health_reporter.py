"""Tests for direct-control's fire-and-forget PV-health reporter.

Mocks configuration_service via ``install_config_http_stub`` and verifies
that successful + failed caputs trigger background POSTs to the
``/api/v1/pvs/{pv_name}/{success|failure}`` endpoints. Threading-aware:
the mock handler runs on the TestClient's internal event-loop thread,
so the assertions wait on a ``threading.Event`` rather than racing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


@dataclass
class _CapturedReport:
    method: str
    path: str
    body: dict


def _make_capturing_handler(captured: List[_CapturedReport], seen: threading.Event):
    """Build a MockTransport handler that records POSTs to the PV-health
    endpoints. The Event lets the test thread wait for the background
    task to finish rather than racing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/pvs/" in str(request.url) and (
            str(request.url).endswith("/success")
            or str(request.url).endswith("/failure")
        ):
            try:
                body = request.read()
                parsed = httpx.Response(200, content=body).json() if body else {}
            except Exception:
                parsed = {}
            captured.append(
                _CapturedReport(
                    method=request.method,
                    path=request.url.path,
                    body=parsed,
                )
            )
            seen.set()
            return httpx.Response(
                200,
                json={
                    "pv_name": request.url.path.split("/")[4],
                    "consecutive_failures": 0,
                    "last_failure_at": None,
                    "last_failure_message": None,
                    "last_success_at": None,
                    "state": "healthy",
                },
            )
        # Any other call (registry-validate side trips, etc.) — return a
        # benign success. Tests in this file don't depend on those paths.
        return httpx.Response(200, json={})

    return handler


def _wait_for_report(seen: threading.Event, timeout: float = 2.0) -> None:
    assert seen.wait(timeout=timeout), (
        "no PV-health report observed within "
        f"{timeout}s — fire-and-forget task may have failed to run"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_successful_caput_reports_success(install_config_http_stub, client):
    """A successful /api/v1/pv/set fires POST /api/v1/pvs/{pv}/success."""
    captured: List[_CapturedReport] = []
    seen = threading.Event()
    install_config_http_stub(_make_capturing_handler(captured, seen))

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 3.14, "wait": True, "timeout": 2.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    _wait_for_report(seen)
    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].path == "/api/v1/pvs/IOC:m1/success"


def test_failed_caput_reports_failure(install_config_http_stub, client, monkeypatch):
    """A pyepics-side caput failure should fire POST /failure with the
    error message."""
    captured: List[_CapturedReport] = []
    seen = threading.Event()
    install_config_http_stub(_make_capturing_handler(captured, seen))

    # Patch device_controller.set_pv to raise so we exercise the
    # exception-side reporting path without needing a misbehaving IOC.
    from direct_control.main import get_device_controller

    class _RaisingController:
        async def set_pv(self, request):
            raise RuntimeError("simulated CA timeout")

    client.app.dependency_overrides[get_device_controller] = lambda: _RaisingController()
    try:
        r = client.post(
            "/api/v1/pv/set",
            json={"pv_name": "IOC:m1", "value": 1.0},
        )
        assert r.status_code == 500
    finally:
        client.app.dependency_overrides.pop(get_device_controller, None)

    _wait_for_report(seen)
    assert len(captured) == 1
    assert captured[0].path == "/api/v1/pvs/IOC:m1/failure"
    assert captured[0].body.get("message") == "simulated CA timeout"


def test_batch_caput_reports_success_per_item(install_config_http_stub, client):
    """Each successful item in a batch should produce one report."""
    captured: List[_CapturedReport] = []
    seen = threading.Event()

    # Reset seen on each capture so multiple reports each trigger it;
    # tests below count via len(captured).
    raw_handler = _make_capturing_handler(captured, seen)

    def handler(request):
        # Re-arm the event each time so a single .wait() releases for
        # the first report; we then poll until we have what we expect.
        seen.clear()
        resp = raw_handler(request)
        seen.set()
        return resp

    install_config_http_stub(handler)

    r = client.post(
        "/api/v1/pv/set/batch",
        json={
            "caputs": [
                {"pv_name": "IOC:m1", "value": 1.0, "wait": True, "timeout": 2.0},
                {"pv_name": "IOC:counter", "value": 2, "wait": True, "timeout": 2.0},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Wait for both reports.
    import time
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(captured) >= 2:
            break
        time.sleep(0.05)
    assert len(captured) == 2
    paths = [r.path for r in captured]
    assert "/api/v1/pvs/IOC:m1/success" in paths
    assert "/api/v1/pvs/IOC:counter/success" in paths


def test_reporter_swallows_config_service_errors(install_config_http_stub, client):
    """If configuration_service is down, the caput response should still
    return 200 — the reporter is fire-and-forget and never propagates
    errors back to the caller."""

    def handler(_req: httpx.Request) -> httpx.Response:
        # Pretend config-service is down — but ONLY for the health
        # endpoint. Anything else returns a benign 200 so RegistryClient
        # (which also goes through this transport in some paths) doesn't
        # also fail.
        if str(_req.url).endswith("/success") or str(_req.url).endswith("/failure"):
            raise httpx.ConnectError("simulated config-service outage")
        return httpx.Response(200, json={})

    install_config_http_stub(handler)

    r = client.post(
        "/api/v1/pv/set",
        json={"pv_name": "IOC:m1", "value": 1.0, "wait": True, "timeout": 2.0},
    )
    # The caput itself succeeded; the reporter swallowed its error.
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_gate_failures_skip_reporting(install_config_http_stub, client):
    """DeviceLockedError / DeviceDisabledError / CoordinationCheckError
    reflect orchestration policy, not PV health — they must NOT trigger
    a health report."""
    captured: List[_CapturedReport] = []
    seen = threading.Event()
    install_config_http_stub(_make_capturing_handler(captured, seen))

    from direct_control.main import get_device_controller
    from direct_control.models import DeviceLockedError

    class _LockedController:
        async def set_pv(self, request):
            raise DeviceLockedError("IOC:m1 locked by plan_xyz")

    client.app.dependency_overrides[get_device_controller] = lambda: _LockedController()
    try:
        r = client.post("/api/v1/pv/set", json={"pv_name": "IOC:m1", "value": 1.0})
        assert r.status_code == 423
    finally:
        client.app.dependency_overrides.pop(get_device_controller, None)

    # Give any spurious background task a chance to run.
    import time
    time.sleep(0.1)
    assert captured == [], (
        f"Expected no PV-health reports for a gate failure, got: {captured}"
    )
