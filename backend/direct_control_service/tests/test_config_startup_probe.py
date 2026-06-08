"""Startup readiness probe against configuration_service.

direct_control's only HTTP backend is configuration_service; every registry-
validated read/write and the device-lock coordination gate need it. The probe
(``_probe_configuration_service``) makes a misconfigured / not-yet-started
config-service fail loudly at boot instead of surfacing later as per-request
503s. These tests drive the probe directly with a fake coordination client so
no app boot / real socket is involved.
"""

from __future__ import annotations

import os

import pytest

# Probe import pulls in direct_control.main (and pyepics via monitoring); the
# autouse _epics_env fixture in conftest has already set EPICS_CA_* by the time
# a test runs, but importing at module load is fine because we only need the
# function object, not a live IOC.
os.environ.setdefault("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", "http://localhost:0")

from direct_control.config import Settings  # noqa: E402
from direct_control.main import _probe_configuration_service  # noqa: E402
from direct_control.models import ServiceAvailability  # noqa: E402


class _FakeCoord:
    """Coordination client stub exposing only is_service_available.

    ``results`` is a list of availability outcomes returned in order; the last
    entry is repeated once exhausted so a never-ready run keeps failing.
    ``calls`` records how many probes happened.
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def is_service_available(self) -> ServiceAvailability:
        self.calls += 1
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]

    # The probe only calls is_service_available; these satisfy the
    # CoordinationService protocol so the double type-checks cleanly.
    async def check_device_available(self, device_name: str):  # pragma: no cover
        raise NotImplementedError

    async def cleanup(self) -> None:  # pragma: no cover
        return None


def _settings(
    *,
    config_service_startup_probe: bool = True,
    config_service_startup_timeout: float = 10.0,
) -> Settings:
    return Settings(
        configuration_service_url="http://localhost:0",
        config_service_startup_probe=config_service_startup_probe,
        config_service_startup_timeout=config_service_startup_timeout,
        config_service_startup_probe_interval=0.01,
    )


async def test_probe_passes_when_config_service_ready():
    coord = _FakeCoord([ServiceAvailability(available=True)])
    await _probe_configuration_service(_settings(), coord)
    assert coord.calls == 1


async def test_probe_retries_then_succeeds():
    """A config-service that comes up on the 3rd poll is awaited, not aborted."""
    coord = _FakeCoord(
        [
            ServiceAvailability(available=False, detail="connect refused"),
            ServiceAvailability(available=False, detail="connect refused"),
            ServiceAvailability(available=True),
        ]
    )
    await _probe_configuration_service(_settings(), coord)
    assert coord.calls == 3


async def test_probe_fails_hard_when_never_ready():
    """Unreachable past the deadline -> RuntimeError carrying the last detail."""
    coord = _FakeCoord([ServiceAvailability(available=False, detail="connect refused")])
    with pytest.raises(RuntimeError, match="connect refused"):
        await _probe_configuration_service(
            _settings(config_service_startup_timeout=0.05), coord
        )
    assert coord.calls >= 1


async def test_probe_skipped_when_disabled():
    """Disabled probe never touches the dependency."""
    coord = _FakeCoord([ServiceAvailability(available=False, detail="should not be hit")])
    await _probe_configuration_service(
        _settings(config_service_startup_probe=False), coord
    )
    assert coord.calls == 0
