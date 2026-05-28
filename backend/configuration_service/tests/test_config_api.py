"""
Integration tests for Configuration Service API endpoints.

Tests the REST API implementation with dependency injection.
"""

import pytest
from fastapi.testclient import TestClient
from configuration_service.main import create_app
from configuration_service.config import Settings
from configuration_service.models import DeviceLabel


@pytest.fixture
def client(tmp_path):
    """Create test client with mock data.

    Uses the lifespan context manager to properly initialize
    the ConfigurationState with mock loader.
    """
    settings = Settings(use_mock_data=True, db_path=tmp_path / "test.db")
    app = create_app(settings)

    # Use context manager to trigger lifespan events
    with TestClient(app) as test_client:
        yield test_client


class TestHealthEndpoints:
    """Test health check endpoints."""

    def test_health_check(self, client):
        """Test /health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "configuration_service"
        assert "devices_loaded" in data

    def test_readiness_check(self, client):
        """Test /ready endpoint."""
        response = client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"


class TestDeviceEndpoints:
    """Test device registry endpoints."""

    def test_list_devices(self, client):
        """Test GET /api/v1/devices."""
        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        devices = response.json()
        assert isinstance(devices, list)
        assert len(devices) > 0
        assert "sample_x" in devices

    def test_list_devices_by_type(self, client):
        """Test GET /api/v1/devices?device_label=motor."""
        response = client.get(f"/api/v1/devices?device_label={DeviceLabel.MOTOR.value}")
        assert response.status_code == 200
        devices = response.json()
        assert isinstance(devices, list)
        # Mock data has at least one motor
        assert len(devices) > 0

    def test_get_device(self, client):
        """Test GET /api/v1/devices/{device_name}."""
        response = client.get("/api/v1/devices/sample_x")
        assert response.status_code == 200
        device = response.json()
        assert device["name"] == "sample_x"
        assert device["device_label"] == DeviceLabel.MOTOR
        assert "pvs" in device

    def test_get_device_not_found(self, client):
        """Test GET /api/v1/devices/{nonexistent}."""
        response = client.get("/api/v1/devices/nonexistent")
        assert response.status_code == 404


class TestPVEndpoints:
    """Test PV registry endpoints."""

    def test_list_pvs(self, client):
        """Test GET /api/v1/pvs."""
        response = client.get("/api/v1/pvs")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "pvs" in data
        assert isinstance(data["pvs"], list)
        assert len(data["pvs"]) > 0

    def test_get_pv(self, client):
        """Test GET /api/v1/pvs/{pv_name}."""
        # First get list of PVs
        response = client.get("/api/v1/pvs")
        data = response.json()
        pvs = data["pvs"]

        if len(pvs) > 0:
            pv_name = pvs[0]
            response = client.get(f"/api/v1/pvs/{pv_name}")
            assert response.status_code == 200
            pv_data = response.json()
            assert pv_data["pv"] == pv_name


class TestDeviceProtocolFlags:
    """Test that device responses include all 11 protocol flag keys."""

    def test_device_response_includes_all_protocol_flags(self, client):
        """GET /api/v1/devices/{name} includes all protocol flag keys."""
        response = client.get("/api/v1/devices/sample_x")
        assert response.status_code == 200
        device = response.json()

        # Original 3 flags
        assert "is_movable" in device
        assert "is_flyable" in device
        assert "is_readable" in device
        # 8 new extended flags
        assert "is_triggerable" in device
        assert "is_stageable" in device
        assert "is_configurable" in device
        assert "is_pausable" in device
        assert "is_stoppable" in device
        assert "is_subscribable" in device
        assert "is_checkable" in device
        assert "writes_external_assets" in device

    def test_mock_motor_protocol_flags(self, client):
        """Mock motor has expected protocol flags set."""
        response = client.get("/api/v1/devices/sample_x")
        device = response.json()
        assert device["is_movable"] is True
        assert device["is_triggerable"] is True
        assert device["is_stageable"] is True
        assert device["is_stoppable"] is True
        assert device["is_pausable"] is False
        assert device["writes_external_assets"] is False

    def test_mock_detector_protocol_flags(self, client):
        """Mock detector has expected protocol flags set."""
        response = client.get("/api/v1/devices/det1")
        device = response.json()
        assert device["is_readable"] is True
        assert device["is_triggerable"] is True
        assert device["is_stageable"] is True
        assert device["is_configurable"] is True
        assert device["is_subscribable"] is True
        # Detector should not have motor-like flags
        assert device["is_movable"] is False
        assert device["is_stoppable"] is False
        assert device["is_checkable"] is False

    def test_devices_info_includes_protocol_flags(self, client):
        """GET /api/v1/devices-info bulk response includes all protocol flags."""
        response = client.get("/api/v1/devices-info")
        assert response.status_code == 200
        data = response.json()
        assert "sample_x" in data
        device = data["sample_x"]
        # Verify all 11 flag keys present in bulk response
        for flag in [
            "is_movable",
            "is_flyable",
            "is_readable",
            "is_triggerable",
            "is_stageable",
            "is_configurable",
            "is_pausable",
            "is_stoppable",
            "is_subscribable",
            "is_checkable",
            "writes_external_assets",
        ]:
            assert flag in device, f"Missing {flag} in devices-info response"


class TestOphydClassFilter:
    """Test filtering devices by ophyd_class query parameter."""

    def test_filter_by_ophyd_class(self, client):
        """GET /api/v1/devices?ophyd_class=EpicsMotor returns motors only."""
        response = client.get("/api/v1/devices?ophyd_class=EpicsMotor")
        assert response.status_code == 200
        devices = response.json()
        assert "sample_x" in devices
        assert "det1" not in devices

    def test_filter_by_ophyd_class_no_match(self, client):
        """GET /api/v1/devices?ophyd_class=SynAxis returns empty list."""
        response = client.get("/api/v1/devices?ophyd_class=SynAxis")
        assert response.status_code == 200
        devices = response.json()
        assert devices == []

    def test_filter_by_ophyd_class_combined_with_device_label(self, client):
        """ophyd_class and device_label filters can be combined."""
        # EpicsMotor + motor type should return sample_x
        response = client.get("/api/v1/devices?ophyd_class=EpicsMotor&device_label=motor")
        assert response.status_code == 200
        devices = response.json()
        assert "sample_x" in devices
        # EpicsMotor + detector type should return empty (class/type mismatch)
        response = client.get("/api/v1/devices?ophyd_class=EpicsMotor&device_label=detector")
        assert response.status_code == 200
        assert response.json() == []

    def test_filter_by_protocol_flags(self, client):
        """GET /api/v1/devices?{readable,movable,flyable}=... filters by protocol flag.

        Mock registry: sample_x (motor, movable+readable), det1/cam1
        (detectors, readable); none flyable.
        """
        # movable=true -> only the motor
        response = client.get("/api/v1/devices?movable=true")
        assert response.status_code == 200
        assert response.json() == ["sample_x"]
        # readable=true -> all three mock devices are readable
        response = client.get("/api/v1/devices?readable=true")
        assert response.status_code == 200
        assert sorted(response.json()) == ["cam1", "det1", "sample_x"]
        # flyable=true -> none in mock data
        response = client.get("/api/v1/devices?flyable=true")
        assert response.status_code == 200
        assert response.json() == []
        # combined with device_label
        response = client.get("/api/v1/devices?device_label=detector&readable=true")
        assert response.status_code == 200
        assert sorted(response.json()) == ["cam1", "det1"]


class TestDeviceClassesAndTypesEndpoints:
    """Test /devices/classes and /devices/types endpoints."""

    def test_list_device_classes(self, client):
        """GET /api/v1/devices/classes returns unique ophyd class names."""
        response = client.get("/api/v1/devices/classes")
        assert response.status_code == 200
        classes = response.json()
        assert isinstance(classes, list)
        assert "EpicsMotor" in classes
        assert "EpicsScaler" in classes
        # Should be sorted and unique
        assert classes == sorted(set(classes))

    def test_list_device_labels(self, client):
        """GET /api/v1/devices/types returns unique device type values."""
        response = client.get("/api/v1/devices/types")
        assert response.status_code == 200
        types = response.json()
        assert isinstance(types, list)
        assert "motor" in types
        assert "detector" in types
        assert types == sorted(set(types))


class TestMaxDepthParameter:
    """Test max_depth parameter on list_device_components.

    Uses cam1 (area detector) which has nested PV names:
      depth 1: image
      depth 2: cam.acquire, cam.acquire_time, cam.image_mode,
               stats.total, image.array_size.width (also depth 3)
      depth 3: stats.centroid.x, stats.centroid.y,
               image.array_size.width, image.array_size.height
    """

    def test_max_depth_none_returns_all(self, client):
        """Without max_depth, all components are returned."""
        response = client.get("/api/v1/devices/cam1/components")
        assert response.status_code == 200
        components = response.json()
        # cam1 has 9 PV entries
        assert len(components) == 9

    def test_max_depth_zero_returns_all(self, client):
        """max_depth=0 returns all components (same as no filter)."""
        all_resp = client.get("/api/v1/devices/cam1/components")
        depth_resp = client.get("/api/v1/devices/cam1/components?max_depth=0")
        assert depth_resp.status_code == 200
        assert len(depth_resp.json()) == len(all_resp.json())

    def test_max_depth_1_returns_only_top_level(self, client):
        """max_depth=1 returns only top-level components (no dots)."""
        response = client.get("/api/v1/devices/cam1/components?max_depth=1")
        assert response.status_code == 200
        components = response.json()
        names = [c["name"] for c in components]
        # Only "image" has depth 1 (no dots)
        for name in names:
            assert "." not in name, f"Component '{name}' should be filtered at depth 1"
        assert "image" in names

    def test_max_depth_2_excludes_depth_3(self, client):
        """max_depth=2 includes depth-1 and depth-2 but excludes depth-3."""
        response = client.get("/api/v1/devices/cam1/components?max_depth=2")
        assert response.status_code == 200
        components = response.json()
        names = [c["name"] for c in components]
        # depth-1 and depth-2 should be present
        assert "image" in names
        assert "cam.acquire" in names
        assert "stats.total" in names
        # depth-3 should be excluded
        assert "stats.centroid.x" not in names
        assert "stats.centroid.y" not in names
        assert "image.array_size.width" not in names
        assert "image.array_size.height" not in names

    def test_max_depth_3_returns_all_for_cam1(self, client):
        """max_depth=3 returns everything since cam1 max depth is 3."""
        all_resp = client.get("/api/v1/devices/cam1/components")
        depth_resp = client.get("/api/v1/devices/cam1/components?max_depth=3")
        assert depth_resp.status_code == 200
        assert len(depth_resp.json()) == len(all_resp.json())

    def test_max_depth_negative_rejected(self, client):
        """Negative max_depth is rejected with 422."""
        response = client.get("/api/v1/devices/cam1/components?max_depth=-1")
        assert response.status_code == 422

    def test_max_depth_on_flat_device_returns_all(self, client):
        """max_depth=1 on a flat device returns all components."""
        all_resp = client.get("/api/v1/devices/sample_x/components")
        depth_resp = client.get("/api/v1/devices/sample_x/components?max_depth=1")
        assert depth_resp.status_code == 200
        # sample_x has only flat PV names, so depth=1 returns everything
        assert len(depth_resp.json()) == len(all_resp.json())


class TestPlanEndpointsRemoved:
    """Verify plan catalog endpoints have been removed.

    Plan catalog is now the responsibility of Experiment Execution Service.
    Configuration Service only manages devices and PVs.
    """

    def test_plans_endpoint_removed(self, client):
        """Test GET /api/v1/plans returns 404 (removed)."""
        response = client.get("/api/v1/plans")
        assert response.status_code == 404

    def test_plan_detail_endpoint_removed(self, client):
        """Test GET /api/v1/plans/{name} returns 404 (removed)."""
        response = client.get("/api/v1/plans/count")
        assert response.status_code == 404


class TestDevicePVsEndpoint:
    """Test GET /api/v1/devices/{device_name}/pvs endpoint."""

    def test_get_device_pvs(self, client):
        """Returns PVs owned by a device."""
        response = client.get("/api/v1/devices/sample_x/pvs")
        assert response.status_code == 200
        data = response.json()
        assert data["device_name"] == "sample_x"
        assert data["device_label"] == "motor"
        assert data["count"] > 0
        # Mock motor has user_readback, user_setpoint, velocity PVs
        assert "user_readback" in data["pvs"]
        assert data["pvs"]["user_readback"]["pv_name"] == "BL01:SAMPLE:X.RBV"

    def test_get_device_pvs_not_found(self, client):
        """Returns 404 for nonexistent device."""
        response = client.get("/api/v1/devices/nonexistent/pvs")
        assert response.status_code == 404

    def test_get_device_pvs_detector(self, client):
        """Returns PVs for detector device."""
        response = client.get("/api/v1/devices/det1/pvs")
        assert response.status_code == 200
        data = response.json()
        assert data["device_name"] == "det1"
        assert data["device_label"] == "detector"
        assert "count" in data["pvs"]  # det1 has "count" component
        assert data["pvs"]["count"]["pv_name"] == "BL01:DET1:CNT"


class TestPVLookupEndpoint:
    """Test GET /api/v1/pvs/lookup endpoint."""

    def test_lookup_device_pvs_by_pv(self, client):
        """Given a PV, returns the owning device, prefix, and all its PVs."""
        response = client.get("/api/v1/pvs/lookup?pv_name=BL01:SAMPLE:X.RBV")
        assert response.status_code == 200
        data = response.json()
        assert data["pv_name"] == "BL01:SAMPLE:X.RBV"
        assert data["device_name"] == "sample_x"
        assert data["device_label"] == "motor"
        assert data["prefix"] == "BL01:SAMPLE:X"
        # Should include all sibling PVs from sample_x
        assert "user_readback" in data["sibling_pvs"]
        assert "user_setpoint" in data["sibling_pvs"]
        assert "velocity" in data["sibling_pvs"]
        assert data["count"] == len(data["sibling_pvs"])

    def test_lookup_pv_not_found(self, client):
        """Returns 404 for unknown PV."""
        response = client.get("/api/v1/pvs/lookup?pv_name=NONEXISTENT:PV")
        assert response.status_code == 404

    def test_lookup_returns_prefix_for_detector(self, client):
        """Prefix is derived from common PV prefix for detector."""
        response = client.get("/api/v1/pvs/lookup?pv_name=BL01:DET1:CNT")
        assert response.status_code == 200
        data = response.json()
        assert data["device_name"] == "det1"
        # det1 PVs are BL01:DET1:CNT and BL01:DET1:PRESET → prefix BL01:DET1:
        assert data["prefix"] == "BL01:DET1:"

    def test_lookup_finds_all_siblings(self, client):
        """Any PV from a device returns the same sibling set."""
        resp1 = client.get("/api/v1/pvs/lookup?pv_name=BL01:SAMPLE:X.RBV")
        resp2 = client.get("/api/v1/pvs/lookup?pv_name=BL01:SAMPLE:X")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # Both should return the same device, prefix, and sibling PVs
        assert resp1.json()["device_name"] == resp2.json()["device_name"]
        assert resp1.json()["prefix"] == resp2.json()["prefix"]
        assert resp1.json()["sibling_pvs"] == resp2.json()["sibling_pvs"]

    def test_lookup_dangling_device_reference_returns_500(self, client):
        """M6 regression: PV referencing a non-existent device must fail loudly.

        Pre-fix this returned 200 OK with sibling_pvs={} and count=0,
        masking referential-integrity corruption as "no siblings".
        """
        state = client.app.state.state_container["state"]
        # Delete the device but leave the PV → device_name link intact.
        assert "sample_x" in state.registry.devices
        del state.registry.devices["sample_x"]

        resp = client.get("/api/v1/pvs/lookup?pv_name=BL01:SAMPLE:X.RBV")
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "Registry inconsistency" in detail
        assert "sample_x" in detail
        assert "BL01:SAMPLE:X.RBV" in detail


class TestCorsOriginsRespectsSettings:
    """M2 regression: CORS middleware must honor `settings.cors_origins`.

    Pre-fix `allow_origins` was hardcoded to ``["*"]`` regardless of
    `CONFIG_CORS_ORIGINS`, so deployments that intended to lock down
    cross-origin access silently allowed everything.
    """

    @staticmethod
    def _client(tmp_path, allowed: list[str]) -> TestClient:
        settings = Settings(
            use_mock_data=True,
            db_path=tmp_path / "test.db",
            cors_origins=allowed,
        )
        app = create_app(settings)
        return TestClient(app)

    def test_allowed_origin_gets_cors_header(self, tmp_path):
        with self._client(tmp_path, ["http://allowed.example"]) as client:
            resp = client.get(
                "/health",
                headers={"Origin": "http://allowed.example"},
            )
            assert resp.status_code == 200
            assert (
                resp.headers.get("access-control-allow-origin") == "http://allowed.example"
            )

    def test_disallowed_origin_gets_no_cors_header(self, tmp_path):
        with self._client(tmp_path, ["http://allowed.example"]) as client:
            resp = client.get(
                "/health",
                headers={"Origin": "http://evil.example"},
            )
            # Endpoint still responds (CORS is browser-enforced), but the
            # ACA-Origin header is absent so a browser would block the read.
            assert resp.status_code == 200
            assert "access-control-allow-origin" not in {
                k.lower() for k in resp.headers.keys()
            }


class TestHealthEndpointDbProbe:
    """S5 regression: /health must surface DB unreachability as 503.

    Pre-fix /health returned 200 "healthy" the moment state was injected,
    without ever touching the DB. A mount-gone or permissions-revoked
    store would still pass health while every CRUD call 500'd. Now /health
    runs SELECT 1 against the registry store and flips to 503.
    """

    def test_health_503_when_registry_store_unreachable(self, client):
        import sqlite3

        class _BrokenStore:
            def ping(self) -> None:
                raise sqlite3.OperationalError("disk I/O error: simulated")

        container = client.app.state.registry_store_container
        original_store = container["store"]
        container["store"] = _BrokenStore()
        try:
            resp = client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert "disk I/O error" in data["detail"]
        finally:
            container["store"] = original_store

    def test_health_200_when_registry_store_healthy(self, client):
        """Sanity: with a real store, /health stays 200."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


class TestM3StandalonePVStoreInitFailureFailsStartup:
    """M3 regression: StandalonePVStore init failure must crash startup.

    Pre-fix the lifespan caught any init exception and continued; downstream
    /standalone-pvs/* endpoints then returned 501 with the misleading message
    "Set CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=true." even when the flag was set.
    """

    def test_lifespan_raises_when_pv_store_init_fails(self, mock_settings, monkeypatch):
        from fastapi.testclient import TestClient

        from configuration_service import standalone_pv_store as standalone_pv_module
        from configuration_service.main import create_app

        def _broken_initialize(self):
            raise RuntimeError("simulated PV store schema migration failure")

        monkeypatch.setattr(
            standalone_pv_module.StandalonePVStore, "initialize", _broken_initialize
        )

        app = create_app(mock_settings)
        with pytest.raises(RuntimeError, match="simulated PV store schema migration failure"):
            with TestClient(app):
                pass  # Entering the with-block runs lifespan; we expect it to raise.

    def test_lifespan_succeeds_when_flag_disabled_and_init_would_fail(
        self, tmp_path, monkeypatch
    ):
        """When the flag is OFF the broken initializer must never run.

        Pins the gate so an unrelated init breakage can't affect deployments
        that don't opt into the feature.
        """
        from fastapi.testclient import TestClient

        from configuration_service import standalone_pv_store as standalone_pv_module
        from configuration_service.config import Settings
        from configuration_service.main import create_app

        called = {"initialize": False}

        def _broken_initialize(self):
            called["initialize"] = True
            raise RuntimeError("should never run when flag is off")

        monkeypatch.setattr(
            standalone_pv_module.StandalonePVStore, "initialize", _broken_initialize
        )

        settings = Settings(
            use_mock_data=True,
            db_path=tmp_path / "test.db",
            device_change_history_enabled=False,
        )
        app = create_app(settings)
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
        assert called["initialize"] is False


class TestOpenApiExport:
    """M13 regression: OpenAPI schema export failures must fail loud.

    Pre-fix the helper logged a warning and continued, leaving the
    frontend's codegen watcher consuming a stale schema with no signal
    to operators. Now: if ``OPHYD_SERVICE_OPENAPI_EXPORT_PATH`` is set,
    the export must succeed or startup raises.
    """

    def test_export_writes_file_when_env_set(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from configuration_service.main import _maybe_export_openapi

        target = tmp_path / "out" / "openapi.json"
        monkeypatch.setenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", str(target))
        _maybe_export_openapi(FastAPI())
        assert target.exists()
        assert "openapi" in target.read_text()

    def test_export_raises_on_unwritable_path(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from configuration_service.main import _maybe_export_openapi

        # Block parent dir creation by planting a file where the parent should be.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        target = blocker / "openapi.json"
        monkeypatch.setenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", str(target))

        with pytest.raises(RuntimeError) as excinfo:
            _maybe_export_openapi(FastAPI())

        msg = str(excinfo.value)
        assert "OpenAPI schema export" in msg
        assert "OPHYD_SERVICE_OPENAPI_EXPORT_PATH" in msg
        assert str(target) in msg

    def test_no_op_when_env_unset(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from configuration_service.main import _maybe_export_openapi

        monkeypatch.delenv("OPHYD_SERVICE_OPENAPI_EXPORT_PATH", raising=False)
        # Should not raise and should not write anything.
        _maybe_export_openapi(FastAPI())
        assert list(tmp_path.iterdir()) == []
