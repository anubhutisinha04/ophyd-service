"""Startup readiness probe against configuration_service.

configuration_service is direct_control's dependency for registry validation
(http backend) and the device-lock coordination gate. The probe
(``_probe_configuration_service``) makes a misconfigured / not-yet-started
config-service fail loudly at boot instead of surfacing later as per-request
503s. These tests drive the probe directly with an httpx MockTransport client
so no app boot / real socket is involved.
"""

from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", "http://localhost:0")

from direct_control.config import Settings  # noqa: E402
from direct_control.main import (  # noqa: E402
    _probe_configuration_service,
    _resolve_registry_backend,
)
from direct_control.registry_client import RegistryClient  # noqa: E402
from direct_control.registry_file import FileRegistryProvider  # noqa: E402


def _settings(
    *,
    registry_backend: str = "http",
    registry_file_path: str | None = None,
    coordination_check_enabled: bool = True,
    config_service_startup_probe: bool = True,
    config_service_startup_timeout: float = 10.0,
) -> Settings:
    return Settings(
        configuration_service_url="http://cs",
        registry_backend=registry_backend,
        registry_file_path=registry_file_path,
        coordination_check_enabled=coordination_check_enabled,
        config_service_startup_probe=config_service_startup_probe,
        config_service_startup_timeout=config_service_startup_timeout,
        config_service_startup_probe_interval=0.01,
    )


def _http(handler):
    """An AsyncClient whose requests are served by ``handler`` in-process.

    ``handler`` receives the count of prior calls and returns either an
    ``httpx.Response`` or an exception instance to raise (e.g. a ConnectError).
    Returns ``(client, calls)`` where ``calls`` is a single-element list whose
    [0] holds the number of /health polls made.
    """
    calls = [0]

    def transport_handler(request: httpx.Request) -> httpx.Response:
        result = handler(calls[0])
        calls[0] += 1
        if isinstance(result, Exception):
            raise result
        return result

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler), base_url="http://cs"
    )
    return client, calls


_CONNECT_ERR = httpx.ConnectError("refused", request=httpx.Request("GET", "http://cs/health"))


async def test_probe_passes_when_config_service_ready():
    client, calls = _http(lambda n: httpx.Response(200))
    await _probe_configuration_service(_settings(), client)
    assert calls[0] == 1
    await client.aclose()


async def test_probe_retries_then_succeeds():
    """A config-service that comes up on the 3rd poll is awaited, not aborted."""
    client, calls = _http(lambda n: httpx.Response(200) if n >= 2 else _CONNECT_ERR)
    await _probe_configuration_service(_settings(), client)
    assert calls[0] == 3
    await client.aclose()


async def test_probe_fails_hard_when_never_ready():
    """Unreachable past the deadline -> RuntimeError carrying the last detail."""
    client, calls = _http(lambda n: _CONNECT_ERR)
    with pytest.raises(RuntimeError, match="not reachable"):
        await _probe_configuration_service(_settings(config_service_startup_timeout=0.05), client)
    assert calls[0] >= 1
    await client.aclose()


async def test_probe_fails_hard_on_non_200():
    """A reachable config-service returning 503 still fails the probe."""
    client, _ = _http(lambda n: httpx.Response(503))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await _probe_configuration_service(_settings(config_service_startup_timeout=0.05), client)
    await client.aclose()


async def test_probe_skipped_when_disabled():
    """Disabled probe never touches the dependency."""
    client, calls = _http(lambda n: httpx.Response(503))
    await _probe_configuration_service(_settings(config_service_startup_probe=False), client)
    assert calls[0] == 0
    await client.aclose()


async def test_probe_skipped_when_config_service_not_required(tmp_path):
    """File-backed registry + coordination disabled => config-service unused."""
    reg = tmp_path / "registry.json"
    reg.write_text('{"devices": []}')
    client, calls = _http(lambda n: httpx.Response(503))
    await _probe_configuration_service(
        _settings(
            registry_backend="file",
            registry_file_path=str(reg),
            coordination_check_enabled=False,
        ),
        client,
    )
    assert calls[0] == 0
    await client.aclose()


async def test_probe_runs_for_file_registry_with_coordination_enabled(tmp_path):
    """Hybrid: file registry but coordination on still needs config-service."""
    reg = tmp_path / "registry.json"
    reg.write_text('{"devices": []}')
    client, calls = _http(lambda n: httpx.Response(200))
    await _probe_configuration_service(
        _settings(
            registry_backend="file",
            registry_file_path=str(reg),
            coordination_check_enabled=True,
        ),
        client,
    )
    assert calls[0] == 1
    await client.aclose()


# ----- registry_backend=auto resolution -----


async def test_auto_uses_http_when_config_service_up(tmp_path):
    """config-service reachable -> auto picks the HTTP registry, coordination kept."""
    reg = tmp_path / "registry.json"
    reg.write_text('{"devices": []}')
    settings = _settings(registry_backend="auto", registry_file_path=str(reg))
    client, _ = _http(lambda n: httpx.Response(200))
    resolution = await _resolve_registry_backend(settings, client)
    assert isinstance(resolution.provider, RegistryClient)
    assert resolution.coordination_enabled is True
    assert resolution.effective_backend == "http"
    await client.aclose()


async def test_auto_falls_back_to_file_and_disables_coordination(tmp_path):
    """config-service down + file configured -> file registry, coordination OFF.

    The decision is returned, not applied by mutating settings, so the caller
    is the single point that applies it.
    """
    reg = tmp_path / "registry.json"
    reg.write_text('{"devices": [{"name": "d", "pvs": []}]}')
    settings = _settings(
        registry_backend="auto",
        registry_file_path=str(reg),
        config_service_startup_timeout=0.05,
    )
    client, _ = _http(lambda n: _CONNECT_ERR)
    resolution = await _resolve_registry_backend(settings, client)
    assert isinstance(resolution.provider, FileRegistryProvider)
    assert resolution.coordination_enabled is False
    assert resolution.effective_backend == "file"
    # resolve does not mutate settings — the caller applies the decision.
    assert settings.coordination_check_enabled is True
    await client.aclose()


async def test_auto_honors_probe_optout_with_single_check(tmp_path):
    """auto + probe disabled -> one /health check, no retry/wait window."""
    reg = tmp_path / "registry.json"
    reg.write_text('{"devices": [{"name": "d", "pvs": []}]}')
    settings = _settings(
        registry_backend="auto",
        registry_file_path=str(reg),
        config_service_startup_probe=False,
        config_service_startup_timeout=30.0,  # would hang if the loop ran
    )
    client, calls = _http(lambda n: _CONNECT_ERR)
    resolution = await _resolve_registry_backend(settings, client)
    assert isinstance(resolution.provider, FileRegistryProvider)
    assert calls[0] == 1  # single probe, not the retry loop
    await client.aclose()


async def test_auto_fails_hard_when_down_and_no_file():
    """config-service down + no file -> hard failure (non-zero exit at startup)."""
    settings = _settings(registry_backend="auto", config_service_startup_timeout=0.05)
    client, _ = _http(lambda n: _CONNECT_ERR)
    with pytest.raises(RuntimeError, match="no .*REGISTRY_FILE_PATH"):
        await _resolve_registry_backend(settings, client)
    await client.aclose()
