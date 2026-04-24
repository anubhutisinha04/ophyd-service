"""
Unit tests for Configuration Service domain models.

Tests the core domain logic without external dependencies.
"""

import pytest
from configuration_service.models import (
    DeviceMetadata,
    PVMetadata,
    DeviceLabel,
    DeviceRegistry,
    DeviceNotFoundError,
    PVNotFoundError,
)


class TestDeviceRegistry:
    """Test DeviceRegistry domain model."""
    
    def test_add_device(self):
        """Test adding device to registry."""
        registry = DeviceRegistry()
        
        device = DeviceMetadata(
            name="sample_x",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={"user_readback": "BL01:SAMPLE:X.RBV"},
        )
        
        registry.add_device(device)
        
        assert "sample_x" in registry.devices
        assert registry.get_device("sample_x") == device
    
    def test_add_device_indexes_pvs(self):
        """Test that adding device automatically indexes PVs."""
        registry = DeviceRegistry()
        
        device = DeviceMetadata(
            name="sample_x",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={
                "user_readback": "BL01:SAMPLE:X.RBV",
                "user_setpoint": "BL01:SAMPLE:X",
            },
        )
        
        registry.add_device(device)
        
        # PVs should be automatically indexed
        assert "BL01:SAMPLE:X.RBV" in registry.pvs
        assert "BL01:SAMPLE:X" in registry.pvs
        
        pv_metadata = registry.get_pv("BL01:SAMPLE:X.RBV")
        assert pv_metadata is not None
        assert pv_metadata.device_name == "sample_x"
        assert pv_metadata.component_name == "user_readback"
    
    def test_list_devices_all(self):
        """Test listing all devices."""
        registry = DeviceRegistry()
        
        registry.add_device(DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="det1",
            device_label=DeviceLabel.DETECTOR,
            ophyd_class="EpicsDetector",
        ))
        
        devices = registry.list_devices()
        assert len(devices) == 2
        assert "motor1" in devices
        assert "det1" in devices
    
    def test_list_devices_by_type(self):
        """Test filtering devices by type."""
        registry = DeviceRegistry()
        
        registry.add_device(DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="motor2",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="det1",
            device_label=DeviceLabel.DETECTOR,
            ophyd_class="EpicsDetector",
        ))
        
        motors = registry.list_devices(device_label=DeviceLabel.MOTOR)
        assert len(motors) == 2
        assert "motor1" in motors
        assert "motor2" in motors
        assert "det1" not in motors
    
    def test_list_devices_by_pattern(self):
        """Test filtering devices by glob pattern."""
        registry = DeviceRegistry()
        
        registry.add_device(DeviceMetadata(
            name="sample_x",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="sample_y",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="det1",
            device_label=DeviceLabel.DETECTOR,
            ophyd_class="EpicsDetector",
        ))
        
        sample_devices = registry.list_devices(pattern="sample_*")
        assert len(sample_devices) == 2
        assert "sample_x" in sample_devices
        assert "sample_y" in sample_devices
        assert "det1" not in sample_devices
    
    def test_search_pvs(self):
        """Test PV search by glob pattern."""
        registry = DeviceRegistry()
        
        registry.add_device(DeviceMetadata(
            name="sample_x",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={
                "user_readback": "BL01:SAMPLE:X.RBV",
                "user_setpoint": "BL01:SAMPLE:X",
            },
        ))
        registry.add_device(DeviceMetadata(
            name="det1",
            device_label=DeviceLabel.DETECTOR,
            ophyd_class="EpicsDetector",
            pvs={
                "count": "BL01:DET1:CNT",
            },
        ))
        
        sample_pvs = registry.search_pvs("BL01:SAMPLE:*")
        assert len(sample_pvs) == 2
        assert "BL01:SAMPLE:X.RBV" in sample_pvs
        assert "BL01:SAMPLE:X" in sample_pvs
        assert "BL01:DET1:CNT" not in sample_pvs


class TestDeviceMetadata:
    """Test DeviceMetadata model."""
    
    def test_device_metadata_creation(self):
        """Test creating device metadata."""
        device = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            pvs={"readback": "BL01:M1.RBV"},
        )
        
        assert device.name == "motor1"
        assert device.device_label == DeviceLabel.MOTOR
        assert device.ophyd_class == "EpicsMotor"
        assert device.pvs == {"readback": "BL01:M1.RBV"}
    
    def test_device_metadata_defaults(self):
        """Test default values for optional fields."""
        device = DeviceMetadata(
            name="motor1",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
        )

        assert device.pvs == {}
        assert device.hints is None
        assert device.read_attrs == []
        assert device.configuration_attrs == []
        assert device.parent is None


class TestDeviceMetadataProtocolFlags:
    """Test extended protocol detection flags on DeviceMetadata."""

    def test_protocol_flags_default_false(self):
        """All 8 extended protocol flags default to False."""
        device = DeviceMetadata(
            name="dev",
            device_label=DeviceLabel.DEVICE,
            ophyd_class="Device",
        )
        assert device.is_triggerable is False
        assert device.is_stageable is False
        assert device.is_configurable is False
        assert device.is_pausable is False
        assert device.is_stoppable is False
        assert device.is_subscribable is False
        assert device.is_checkable is False
        assert device.writes_external_assets is False

    def test_protocol_flags_can_be_set_true(self):
        """All 8 extended protocol flags can be set to True."""
        device = DeviceMetadata(
            name="dev",
            device_label=DeviceLabel.MOTOR,
            ophyd_class="EpicsMotor",
            is_triggerable=True,
            is_stageable=True,
            is_configurable=True,
            is_pausable=True,
            is_stoppable=True,
            is_subscribable=True,
            is_checkable=True,
            writes_external_assets=True,
        )
        assert device.is_triggerable is True
        assert device.is_stageable is True
        assert device.is_configurable is True
        assert device.is_pausable is True
        assert device.is_stoppable is True
        assert device.is_subscribable is True
        assert device.is_checkable is True
        assert device.writes_external_assets is True


class TestListDevicesByOphydClass:
    """Test filtering devices by ophyd_class."""

    def _make_registry(self):
        registry = DeviceRegistry()
        registry.add_device(DeviceMetadata(
            name="motor1", device_label=DeviceLabel.MOTOR, ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="motor2", device_label=DeviceLabel.MOTOR, ophyd_class="EpicsMotor",
        ))
        registry.add_device(DeviceMetadata(
            name="det1", device_label=DeviceLabel.DETECTOR, ophyd_class="EpicsScaler",
        ))
        return registry

    def test_filter_by_ophyd_class_match(self):
        """Filtering by ophyd_class returns matching devices."""
        registry = self._make_registry()
        result = registry.list_devices(ophyd_class="EpicsMotor")
        assert result == ["motor1", "motor2"]

    def test_filter_by_ophyd_class_no_match(self):
        """Filtering by non-existent ophyd_class returns empty list."""
        registry = self._make_registry()
        result = registry.list_devices(ophyd_class="SynAxis")
        assert result == []

    def test_filter_by_ophyd_class_single(self):
        """Filtering by ophyd_class with one match."""
        registry = self._make_registry()
        result = registry.list_devices(ophyd_class="EpicsScaler")
        assert result == ["det1"]

    def test_filter_by_ophyd_class_combined_with_type(self):
        """ophyd_class and device_label filters can be combined."""
        registry = self._make_registry()
        # Both filters match
        result = registry.list_devices(
            device_label=DeviceLabel.MOTOR, ophyd_class="EpicsMotor"
        )
        assert result == ["motor1", "motor2"]
        # Filters contradict (type=detector but class=EpicsMotor)
        result = registry.list_devices(
            device_label=DeviceLabel.DETECTOR, ophyd_class="EpicsMotor"
        )
        assert result == []


