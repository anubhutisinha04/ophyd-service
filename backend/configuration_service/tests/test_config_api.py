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
            "is_movable", "is_flyable", "is_readable",
            "is_triggerable", "is_stageable", "is_configurable",
            "is_pausable", "is_stoppable", "is_subscribable",
            "is_checkable", "writes_external_assets",
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
