"""
Domain models for Configuration Service (SVC-004).

These models represent the core entities for the device/PV registry.
"""

from typing import Dict, List, Literal, Optional, Any, Type, Union, get_args
from enum import Enum
from pydantic import BaseModel, Field, create_model


def make_partial_model(
    model: Type[BaseModel], name: str = None
) -> Type[BaseModel]:
    """Create a copy of *model* where every field is Optional with default None.

    Used to generate partial-update request models from canonical models.
    Fields added to the base model are automatically available in the
    partial variant, eliminating field-drift risk.

    Works with ``model_dump(exclude_unset=True)`` to distinguish
    "not sent" from "sent as None".
    """
    fields = {}
    for field_name, field_info in model.model_fields.items():
        ann = field_info.annotation
        if type(None) not in get_args(ann):
            ann = Optional[ann]
        fields[field_name] = (ann, Field(default=None, description=field_info.description))
    return create_model(name or f"{model.__name__}Update", **fields)


class DeviceLabel(str, Enum):
    """Device classification derived from ophyd/ophyd-async class hierarchy.

    Each value maps directly to a concrete base class in ophyd or ophyd-async:
      MOTOR     — ophyd.EpicsMotor, ophyd_async.epics.motor.Motor
      DETECTOR  — ophyd.areadetector.DetectorBase, ophyd_async.core.StandardDetector
      SIGNAL    — ophyd.Signal/EpicsSignal, ophyd_async.core.Signal
      FLYER     — ophyd.FlyerInterface, ophyd_async.core.StandardFlyer
      READABLE  — ophyd_async.core.StandardReadable (readable but not motor/detector)
      DEVICE    — ophyd.Device, ophyd_async.core.Device (generic base)
    """
    MOTOR = "motor"
    DETECTOR = "detector"
    SIGNAL = "signal"
    FLYER = "flyer"
    READABLE = "readable"
    DEVICE = "device"


class DeviceInstantiationSpec(BaseModel):
    """
    Device instantiation specification for remote device creation.

    This model contains all information needed to recreate a device instance
    in another service (e.g., Experiment Execution Service). By providing
    the class path and constructor arguments, remote services can dynamically
    import and instantiate identical device objects.

    This enables Configuration Service to be the single source of truth for
    device definitions, ensuring PV names and configurations are consistent
    across all services.
    """
    name: str = Field(description="Device name from profile collection")
    device_class: str = Field(
        description="Fully qualified class path (e.g., 'ophyd.EpicsMotor', 'ophyd.EpicsScaler')"
    )
    args: List[Any] = Field(
        default_factory=list,
        description="Positional arguments for device constructor (e.g., ['BL01:DET1:'])"
    )
    kwargs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments for device constructor (e.g., {'name': 'det1'})"
    )
    active: bool = Field(
        default=True,
        description="Whether this device should be instantiated"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "det1",
                "device_class": "ophyd.EpicsScaler",
                "args": ["BL01:DET1:"],
                "kwargs": {"name": "det1"},
                "active": True
            }
        }


class DeviceMetadata(BaseModel):
    """
    Device metadata model.

    Represents device information loaded from profile collections.
    Maps to ProvidesDeviceRegistry.get_device() return type.

    Compatible with ophyd/ophyd-async device introspection and profile collection formats.
    """
    name: str = Field(description="Device name from profile collection")
    device_label: DeviceLabel = Field(description="Classification of device")
    ophyd_class: str = Field(description="Ophyd device class name")
    module: Optional[str] = Field(
        default=None,
        description="Python module containing the device class"
    )
    # Capability flags (from ophyd protocol introspection)
    is_movable: bool = Field(default=False, description="Implements Movable protocol")
    is_flyable: bool = Field(default=False, description="Implements Flyable protocol")
    is_readable: bool = Field(default=False, description="Implements Readable protocol")
    # Extended protocol flags (blueapi Device union protocols)
    is_triggerable: bool = Field(default=False, description="Implements Triggerable protocol (has trigger)")
    is_stageable: bool = Field(default=False, description="Implements Stageable protocol (has stage/unstage)")
    is_configurable: bool = Field(default=False, description="Implements Configurable protocol (has read_configuration/describe_configuration)")
    is_pausable: bool = Field(default=False, description="Implements Pausable protocol (has pause/resume)")
    is_stoppable: bool = Field(default=False, description="Implements Stoppable protocol (has stop)")
    is_subscribable: bool = Field(default=False, description="Implements Subscribable protocol (has subscribe/clear_sub)")
    is_checkable: bool = Field(default=False, description="Implements Checkable protocol (has check_value)")
    writes_external_assets: bool = Field(default=False, description="Writes external assets (has collect_asset_docs)")
    # PV and attribute info
    pvs: Dict[str, str] = Field(
        default_factory=dict,
        description="Component name to PV mapping"
    )
    hints: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Bluesky hints for plotting/display"
    )
    read_attrs: List[str] = Field(
        default_factory=list,
        description="Readable attributes"
    )
    configuration_attrs: List[str] = Field(
        default_factory=list,
        description="Configuration attributes"
    )
    parent: Optional[str] = Field(
        default=None,
        description="Parent device if this is a component"
    )
    # Labels for device grouping (BITS format)
    labels: List[str] = Field(
        default_factory=list,
        description="Device labels for grouping (e.g., 'motors', 'detectors', 'baseline')"
    )
    # Extended metadata (happi format)
    beamline: Optional[str] = Field(
        default=None,
        description="Beamline identifier (from happi)"
    )
    location_group: Optional[str] = Field(
        default=None,
        description="Location grouping (from happi)"
    )
    functional_group: Optional[str] = Field(
        default=None,
        description="Functional grouping (from happi)"
    )
    documentation: Optional[str] = Field(
        default=None,
        description="Device documentation/description"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "sample_x",
                "device_label": "motor",
                "ophyd_class": "EpicsMotor",
                "module": "ophyd.epics_motor",
                "is_movable": True,
                "is_flyable": False,
                "is_readable": True,
                "is_triggerable": True,
                "is_stageable": True,
                "is_configurable": True,
                "is_pausable": False,
                "is_stoppable": True,
                "is_subscribable": True,
                "is_checkable": True,
                "writes_external_assets": False,
                "pvs": {
                    "user_readback": "BL01:SAMPLE:X.RBV",
                    "user_setpoint": "BL01:SAMPLE:X",
                    "velocity": "BL01:SAMPLE:X.VELO"
                },
                "hints": {"fields": ["sample_x"]},
                "read_attrs": ["user_readback", "user_setpoint"],
                "configuration_attrs": ["velocity", "acceleration"],
                "parent": None
            }
        }


class PVMetadata(BaseModel):
    """
    EPICS PV metadata model.
    
    Represents PV information from EPICS network discovery.
    Maps to ProvidesDeviceRegistry.get_pv_metadata() return type.
    """
    pv: str = Field(description="EPICS PV name")
    connected: bool = Field(default=False, description="Connection status")
    dtype: Optional[str] = Field(default=None, description="EPICS data type")
    units: Optional[str] = Field(default=None, description="Engineering units")
    precision: Optional[int] = Field(default=None, description="Display precision")
    enum_strs: Optional[List[str]] = Field(
        default=None,
        description="Enumeration strings for enum PVs"
    )
    upper_ctrl_limit: Optional[float] = Field(
        default=None,
        description="Upper control limit"
    )
    lower_ctrl_limit: Optional[float] = Field(
        default=None,
        description="Lower control limit"
    )
    device_name: Optional[str] = Field(
        default=None,
        description="Owning device name if known"
    )
    component_name: Optional[str] = Field(
        default=None,
        description="Component name within device"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "pv": "BL01:SAMPLE:X.RBV",
                "connected": True,
                "dtype": "double",
                "units": "mm",
                "precision": 3,
                "enum_strs": None,
                "upper_ctrl_limit": 100.0,
                "lower_ctrl_limit": -100.0,
                "device_name": "sample_x",
                "component_name": "user_readback"
            }
        }



class DeviceRegistry(BaseModel):
    """
    In-memory device registry.

    Loaded from beamline profile collection at startup.
    Provides fast lookup for device metadata and instantiation specs.
    """
    devices: Dict[str, DeviceMetadata] = Field(
        default_factory=dict,
        description="Device name to metadata mapping"
    )
    pvs: Dict[str, PVMetadata] = Field(
        default_factory=dict,
        description="PV name to metadata mapping"
    )
    instantiation_specs: Dict[str, DeviceInstantiationSpec] = Field(
        default_factory=dict,
        description="Device name to instantiation specification mapping"
    )
    
    def get_device(self, name: str) -> Optional[DeviceMetadata]:
        """Get device by name."""
        return self.devices.get(name)
    
    def list_devices(
        self,
        device_label: Optional[DeviceLabel] = None,
        pattern: Optional[str] = None,
        labels: Optional[List[str]] = None,
        ophyd_class: Optional[str] = None,
    ) -> List[str]:
        """List device names with optional filtering.

        Args:
            device_label: Filter by device type
            pattern: Glob pattern for name matching
            labels: Filter by labels (device must have ALL specified labels)
            ophyd_class: Filter by ophyd device class name
        """
        names = list(self.devices.keys())

        if device_label:
            names = [
                name for name in names
                if self.devices[name].device_label == device_label
            ]

        if pattern:
            # Simple glob pattern matching (* and ? supported)
            import fnmatch
            names = [name for name in names if fnmatch.fnmatch(name, pattern)]

        if labels:
            names = [
                name for name in names
                if all(label in self.devices[name].labels for label in labels)
            ]

        if ophyd_class:
            names = [
                name for name in names
                if self.devices[name].ophyd_class == ophyd_class
            ]

        return sorted(names)

    def list_labels(self) -> List[str]:
        """Get all unique labels from devices."""
        all_labels: set = set()
        for device in self.devices.values():
            all_labels.update(device.labels)
        return sorted(all_labels)
    
    def get_pv(self, pv_name: str) -> Optional[PVMetadata]:
        """Get PV metadata by name."""
        return self.pvs.get(pv_name)
    
    def search_pvs(self, pattern: str) -> List[str]:
        """Search PVs by glob pattern."""
        import fnmatch
        return sorted([
            pv for pv in self.pvs.keys()
            if fnmatch.fnmatch(pv, pattern)
        ])
    
    def add_device(
        self,
        device: DeviceMetadata,
        instantiation_spec: Optional[DeviceInstantiationSpec] = None
    ) -> None:
        """Add or update device in registry.

        Args:
            device: Device metadata
            instantiation_spec: Optional instantiation specification for remote creation
        """
        self.devices[device.name] = device

        # Add instantiation spec if provided
        if instantiation_spec is not None:
            self.instantiation_specs[device.name] = instantiation_spec

        # Index PVs for this device
        for component_name, pv_name in device.pvs.items():
            if pv_name not in self.pvs:
                self.pvs[pv_name] = PVMetadata(
                    pv=pv_name,
                    device_name=device.name,
                    component_name=component_name
                )
            else:
                # Update existing PV with device ownership info
                self.pvs[pv_name].device_name = device.name
                self.pvs[pv_name].component_name = component_name

    def remove_device(self, name: str) -> bool:
        """Remove device from registry including its instantiation spec and indexed PVs.

        Args:
            name: Device name to remove

        Returns:
            True if device was found and removed, False if not found
        """
        if name not in self.devices:
            return False

        device = self.devices[name]

        # Remove indexed PVs owned by this device
        pv_names_to_remove = [
            pv_name for pv_name, pv_meta in self.pvs.items()
            if pv_meta.device_name == name
        ]
        for pv_name in pv_names_to_remove:
            del self.pvs[pv_name]

        # Remove instantiation spec
        self.instantiation_specs.pop(name, None)

        # Remove device
        del self.devices[name]

        return True

    def update_device(
        self,
        device: DeviceMetadata,
        instantiation_spec: Optional[DeviceInstantiationSpec] = None
    ) -> bool:
        """Update an existing device by removing old PV indexes and re-adding.

        Args:
            device: Updated device metadata
            instantiation_spec: Optional updated instantiation specification

        Returns:
            True if device existed and was updated, False if not found
        """
        if device.name not in self.devices:
            return False

        # Remove old PV indexes for this device
        pv_names_to_remove = [
            pv_name for pv_name, pv_meta in self.pvs.items()
            if pv_meta.device_name == device.name
        ]
        for pv_name in pv_names_to_remove:
            del self.pvs[pv_name]

        # Re-add with updated data
        self.add_device(device, instantiation_spec)
        return True

    def get_instantiation_spec(self, name: str) -> Optional[DeviceInstantiationSpec]:
        """Get device instantiation specification by name."""
        return self.instantiation_specs.get(name)

    def list_instantiation_specs(
        self,
        active_only: bool = True
    ) -> Dict[str, DeviceInstantiationSpec]:
        """Get all device instantiation specifications.

        Args:
            active_only: If True, only return active devices

        Returns:
            Dictionary mapping device name to instantiation spec
        """
        if active_only:
            return {
                name: spec
                for name, spec in self.instantiation_specs.items()
                if spec.active
            }
        return dict(self.instantiation_specs)


# Exceptions for registry operations
class DeviceNotFoundError(Exception):
    """Raised when device not found in registry."""
    def __init__(self, device_name: str):
        self.device_name = device_name
        super().__init__(f"Device not found: {device_name}")


class PVNotFoundError(Exception):
    """Raised when PV not found in registry."""
    def __init__(self, pv_name: str):
        self.pv_name = pv_name
        super().__init__(f"PV not found: {pv_name}")


# ===== Device CRUD Request/Response Models =====

class DeviceCreateRequest(BaseModel):
    """Request model for creating a runtime device."""
    metadata: DeviceMetadata = Field(description="Device metadata")
    instantiation_spec: DeviceInstantiationSpec = Field(
        description="Device instantiation specification"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "metadata": {
                    "name": "new_motor",
                    "device_label": "motor",
                    "ophyd_class": "EpicsMotor",
                    "is_movable": True,
                    "is_readable": True,
                    "pvs": {"user_readback": "NEW:MOTOR.RBV", "user_setpoint": "NEW:MOTOR"},
                },
                "instantiation_spec": {
                    "name": "new_motor",
                    "device_class": "ophyd.EpicsMotor",
                    "args": ["NEW:MOTOR"],
                    "kwargs": {"name": "new_motor"},
                },
            }
        }


DeviceMetadataUpdate = make_partial_model(DeviceMetadata)
DeviceInstantiationSpecUpdate = make_partial_model(DeviceInstantiationSpec)


class DeviceUpdateRequest(BaseModel):
    """Request model for updating a device.

    Supports field-level partial updates: only the fields you include
    in ``metadata`` or ``instantiation_spec`` are changed.  Omitted fields
    keep their current values.
    """
    metadata: Optional[DeviceMetadataUpdate] = Field(
        default=None,
        description="Partial device metadata — only included fields are updated",
    )
    instantiation_spec: Optional[DeviceInstantiationSpecUpdate] = Field(
        default=None,
        description="Partial instantiation spec — only included fields are updated",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "metadata": {
                    "documentation": "Sample X translation stage",
                    "labels": ["motors", "sample-stage"],
                },
            }
        }


class DeviceCRUDResponse(BaseModel):
    """Response model for device CRUD operations."""
    success: bool = Field(description="Whether the operation succeeded")
    device_name: str = Field(description="Name of the device")
    operation: str = Field(description="Operation performed (create/update/delete)")
    message: str = Field(description="Human-readable status message")


class DeviceAuditEntry(BaseModel):
    """Entry in the device audit log (append-only change history)."""
    id: int = Field(description="Auto-incrementing audit log entry ID")
    device_name: str = Field(description="Device name (or '*' for registry-wide ops)")
    operation: str = Field(description="Operation (seed/add/update/delete/reset)")
    timestamp: float = Field(description="Unix timestamp")
    details: Optional[str] = Field(default=None, description="Optional JSON details")


class DeviceChangeEntry(BaseModel):
    """
    One device's current state, reported in a delta response.

    ``op`` is "upsert" if the device currently exists (create or update), or
    "delete" if it no longer exists. For "upsert", ``metadata`` and ``spec``
    reflect the current state; for "delete" both are null.
    """
    device_name: str = Field(description="Device name")
    op: Literal["upsert", "delete"] = Field(description="Either 'upsert' or 'delete'")
    version: int = Field(description="Audit log id of the change that produced this state")
    metadata: Optional[DeviceMetadata] = Field(default=None)
    spec: Optional[DeviceInstantiationSpec] = Field(default=None)


class DeviceChangesResponse(BaseModel):
    """
    Delta response: every device whose state changed after ``since_version``.

    If ``reset_occurred`` is true, a registry-wide reset happened in the
    requested range and the caller should discard its local state and
    re-fetch the full registry instead of applying ``changes`` incrementally.
    ``service_epoch`` is a stable identifier that changes only on manual
    re-seed or DB wipe; if it differs from the value the caller saw last,
    treat the cursor as invalid.
    """
    current_version: int = Field(description="Latest audit log id at query time")
    service_epoch: str = Field(description="Stable service-instance identifier")
    reset_occurred: bool = Field(description="True if a registry-wide reset happened in the range")
    changes: List[DeviceChangeEntry] = Field(default_factory=list)


# ===== Nested Device Models =====

# ===== Standalone PV Models =====

class PVProtocol(str, Enum):
    """EPICS protocol for standalone PV access."""
    CA = "ca"
    PVA = "pva"


class PVAccessMode(str, Enum):
    """Access mode for standalone PVs."""
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


class StandalonePV(BaseModel):
    """A standalone PV not associated with any ophyd device."""
    pv_name: str = Field(description="EPICS PV name")
    description: Optional[str] = Field(default=None, description="Human-readable description")
    protocol: PVProtocol = Field(default=PVProtocol.CA, description="EPICS protocol")
    access_mode: PVAccessMode = Field(default=PVAccessMode.READ_ONLY, description="Access mode")
    labels: List[str] = Field(default_factory=list, description="Labels for RBAC grouping")
    source: str = Field(default="runtime", description="Source of registration")
    created_by: Optional[str] = Field(default=None, description="User who registered this PV")
    created_at: Optional[float] = Field(default=None, description="Unix timestamp of creation")
    updated_at: Optional[float] = Field(default=None, description="Unix timestamp of last update")


class StandalonePVCreateRequest(BaseModel):
    """Request model for registering a standalone PV."""
    pv_name: str = Field(description="EPICS PV name")
    description: Optional[str] = Field(default=None, description="Human-readable description")
    protocol: PVProtocol = Field(default=PVProtocol.CA, description="EPICS protocol")
    access_mode: PVAccessMode = Field(default=PVAccessMode.READ_ONLY, description="Access mode")
    labels: List[str] = Field(default_factory=list, description="Labels for RBAC grouping")

    class Config:
        json_schema_extra = {
            "example": {
                "pv_name": "BL01:RING:CURRENT",
                "description": "Storage ring beam current",
                "protocol": "ca",
                "access_mode": "read-only",
                "labels": ["machine", "beam-diagnostics"],
            }
        }


class StandalonePVUpdateRequest(BaseModel):
    """Request model for updating a standalone PV.

    All fields optional.  Only fields included in the request body are
    applied; omitted fields keep their current values.
    """
    description: Optional[str] = Field(default=None, description="Human-readable description")
    protocol: Optional[PVProtocol] = Field(default=None, description="EPICS protocol")
    access_mode: Optional[PVAccessMode] = Field(default=None, description="Access mode")
    labels: Optional[List[str]] = Field(default=None, description="Labels for RBAC grouping")

    class Config:
        json_schema_extra = {
            "example": {
                "description": "Updated: storage ring beam current (averaged)",
                "labels": ["machine", "beam-diagnostics", "averaging"],
            }
        }


class StandalonePVCRUDResponse(BaseModel):
    """Response model for standalone PV CRUD operations."""
    success: bool = Field(description="Whether the operation succeeded")
    pv_name: str = Field(description="PV name")
    operation: str = Field(description="Operation performed (create/update/delete)")
    message: str = Field(description="Human-readable status message")


# ===== Device Locking Request/Response Models =====


class DeviceLockRequest(BaseModel):
    """Request model for acquiring device locks (bulk atomic)."""
    device_names: List[str] = Field(description="Devices to lock")
    item_id: str = Field(description="Queue item ID holding the lock")
    plan_name: str = Field(description="Name of the plan acquiring devices")
    locked_by_service: str = Field(
        default="experiment_execution",
        description="Service requesting the lock",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "device_names": ["sample_x", "det1"],
                "item_id": "550e8400-e29b-41d4-a716-446655440000",
                "plan_name": "count",
                "locked_by_service": "experiment_execution",
            }
        }


class DeviceLockConflict(BaseModel):
    """A single device that could not be locked."""
    device_name: str = Field(description="Device name")
    reason: str = Field(description="Why the lock failed (not_found, disabled, already_locked)")
    locked_by_plan: Optional[str] = Field(default=None, description="Plan holding the lock")
    locked_at: Optional[str] = Field(default=None, description="ISO timestamp of lock acquisition")


class DeviceLockResponse(BaseModel):
    """Response model for successful lock acquisition."""
    success: bool = Field(description="Whether locks were acquired")
    locked_devices: List[str] = Field(default_factory=list, description="Devices that were locked")
    locked_pvs: List[str] = Field(default_factory=list, description="PVs implicitly locked")
    lock_id: Optional[str] = Field(default=None, description="Lock group identifier")
    registry_version: int = Field(description="Lock version counter")


class DeviceLockConflictResponse(BaseModel):
    """Response model for lock conflict (409/404/422)."""
    success: bool = Field(default=False)
    message: str = Field(description="Human-readable error message")
    conflicting_devices: List[DeviceLockConflict] = Field(
        default_factory=list, description="Devices that caused the conflict"
    )


class DeviceUnlockRequest(BaseModel):
    """Request model for releasing device locks."""
    device_names: List[str] = Field(description="Devices to unlock")
    item_id: str = Field(description="Queue item ID that holds the lock")


class DeviceUnlockResponse(BaseModel):
    """Response model for unlock operations."""
    success: bool = Field(description="Whether locks were released")
    unlocked_devices: List[str] = Field(default_factory=list, description="Devices that were unlocked")
    registry_version: int = Field(description="Lock version counter")


class DeviceForceUnlockRequest(BaseModel):
    """Request model for administrative force-unlock."""
    device_names: List[str] = Field(description="Devices to force-unlock")
    reason: str = Field(description="Reason for force-unlock (for audit log)")

    class Config:
        json_schema_extra = {
            "example": {
                "device_names": ["sample_x"],
                "reason": "EE crashed during rel_scan, clearing stale locks",
            }
        }


class DeviceStatusResponse(BaseModel):
    """Combined device availability check (lock + enabled state)."""
    device_name: str = Field(description="Device name")
    available: bool = Field(description="True only when enabled AND unlocked")
    enabled: bool = Field(description="Whether the device is enabled for instantiation")
    lock_status: str = Field(description="Lock state: 'locked' or 'unlocked'")
    locked_by_plan: Optional[str] = Field(default=None, description="Plan holding the lock")
    locked_by_item: Optional[str] = Field(default=None, description="Queue item ID holding the lock")
    locked_at: Optional[str] = Field(default=None, description="ISO timestamp of lock acquisition")


class PVStatusResponse(BaseModel):
    """PV availability check (resolves PV to owning device lock state)."""
    pv_name: str = Field(description="EPICS PV name")
    available: bool = Field(description="True when owning device is enabled and unlocked (or standalone)")
    device_name: Optional[str] = Field(default=None, description="Owning device name (null for standalone PVs)")
    device_enabled: Optional[bool] = Field(default=None, description="Whether the owning device is enabled")
    device_lock_status: Optional[str] = Field(default=None, description="Owning device lock state")
    locked_by_plan: Optional[str] = Field(default=None, description="Plan holding the lock on the owning device")
    locked_by_item: Optional[str] = Field(default=None, description="Queue item ID holding the lock")
    locked_at: Optional[str] = Field(default=None, description="ISO timestamp of lock acquisition")


class NestedDeviceComponent(BaseModel):
    """Information about a nested device component."""
    name: str = Field(description="Component name")
    device_path: str = Field(description="Full path to component")
    parent_device: str = Field(description="Parent device name")
    component_type: Optional[str] = Field(None, description="Component type")
    pv: Optional[str] = Field(None, description="Associated EPICS PV")
    is_readable: bool = Field(default=True, description="Whether component is readable")
    is_settable: bool = Field(default=False, description="Whether component is settable")


# ===== Generic Metadata Store Models =====


class MetadataEntry(BaseModel):
    """A stored metadata key-value entry."""
    key: str = Field(description="Unique string key")
    value: Dict[str, Any] = Field(description="Arbitrary JSON dictionary")
    created_at: Optional[float] = Field(default=None, description="Unix timestamp of creation")
    updated_at: Optional[float] = Field(default=None, description="Unix timestamp of last update")


class MetadataWriteRequest(BaseModel):
    """Request model for creating or updating a metadata entry."""
    value: Dict[str, Any] = Field(description="Arbitrary JSON dictionary to store")


class MetadataCRUDResponse(BaseModel):
    """Response model for metadata CRUD operations."""
    success: bool = Field(description="Whether the operation succeeded")
    key: str = Field(description="Metadata key")
    operation: str = Field(description="Operation performed (create/update/delete)")
    message: str = Field(description="Human-readable status message")

