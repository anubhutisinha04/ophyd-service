"""Pin the committed standalone-registry examples against the parser.

The example registry (``examples/standalone_registry.example.yaml``) and the
standalone pod's live registry (``integration/pods/standalone/registry.json``)
are documentation that users copy. Loading them through FileRegistryProvider
here means any future schema change that would break them breaks CI instead
of a user's deployment.

Also covers the standalone-mode Settings contract: configuration_service_url
is optional ONLY with registry_backend=file, and PV-health reporting becomes
an explicit no-op when there is no config-service to report to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from direct_control.config import Settings
from direct_control.registry_file import FileRegistryProvider

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE_YAML = (
    _REPO_ROOT
    / "backend"
    / "direct_control_service"
    / "examples"
    / "standalone_registry.example.yaml"
)
_POD_REGISTRY = _REPO_ROOT / "integration" / "pods" / "standalone" / "registry.json"


def test_example_yaml_parses_and_carries_specs():
    provider = FileRegistryProvider(str(_EXAMPLE_YAML))

    # PV-gateway-only device: present, but no instantiation spec.
    import asyncio

    async def check():
        await provider.validate_device("slit_det")
        assert await provider.get_instantiation_spec("slit_det") is None

        spec = await provider.get_instantiation_spec("m1")
        assert spec is not None
        assert spec.device_class == "ophyd.EpicsMotor"
        assert spec.framework == "ophyd-sync"

        spec2 = await provider.get_instantiation_spec("m2")
        assert spec2 is not None
        assert spec2.framework == "ophyd-async"

        await provider.validate_pv("BL01:RING:CURRENT")  # standalone_pvs entry
        assert await provider.get_owning_device("BL01:RING:CURRENT") is None

    asyncio.run(check())


def test_standalone_pod_registry_parses_and_carries_specs():
    provider = FileRegistryProvider(str(_POD_REGISTRY))

    import asyncio

    async def check():
        for name in ("beam_current", "motor_spotx", "pinhole", "slit_det"):
            await provider.validate_device(name)
        spec = await provider.get_instantiation_spec("pinhole")
        assert spec is not None
        assert spec.device_class == "localdevs.Det"
        assert await provider.get_instantiation_spec("slit_det") is None
        await provider.validate_pv("mini:dot:exp")

    asyncio.run(check())


def test_settings_url_optional_only_in_file_mode(monkeypatch, tmp_path):
    # The suite's conftest exports DIRECT_CONTROL_CONFIGURATION_SERVICE_URL;
    # remove it so we exercise the unset-URL arms.
    monkeypatch.delenv("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", raising=False)
    registry = tmp_path / "r.json"
    registry.write_text('{"devices": [{"name": "d", "pvs": ["X:PV"]}]}')

    # file mode: no URL needed.
    s = Settings(registry_backend="file", registry_file_path=str(registry))
    assert s.configuration_service_url is None

    # http / auto: URL required, fail hard.
    with pytest.raises(ValueError, match="CONFIGURATION_SERVICE_URL"):
        Settings(registry_backend="http")
    with pytest.raises(ValueError, match="CONFIGURATION_SERVICE_URL"):
        Settings(registry_backend="auto", registry_file_path=str(registry))


async def test_pv_health_reporter_noops_without_config_service():
    from direct_control.pv_health_reporter import PVHealthReporter

    reporter = PVHealthReporter(None)
    assert reporter.report("X:PV", success=True) is None
    assert reporter.inflight_count() == 0
    await reporter.drain()  # no-op, must not raise


def test_app_boots_standalone_without_config_service_url(monkeypatch, tmp_path):
    """Full lifespan boot in standalone mode with NO configuration_service
    URL: registry from file, coordination auto-off, healthy /health, and
    the no-op PV-health reporter wired in."""
    from fastapi.testclient import TestClient

    from direct_control.main import app

    registry = tmp_path / "r.json"
    registry.write_text('{"devices": [{"name": "d", "pvs": ["X:PV"]}]}')
    monkeypatch.delenv("DIRECT_CONTROL_CONFIGURATION_SERVICE_URL", raising=False)
    monkeypatch.setenv("DIRECT_CONTROL_REGISTRY_BACKEND", "file")
    monkeypatch.setenv("DIRECT_CONTROL_REGISTRY_FILE_PATH", str(registry))

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["registry_backend"] == "file"

        r = c.get("/api/v1/stats")
        assert r.status_code == 200
        assert r.json()["coordination_enabled"] is False

        assert app.state.pv_health_reporter._client is None
