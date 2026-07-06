"""
Domain models for Configuration Service (SVC-004).

These models represent the core entities for the device/PV registry.
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# Re-exported so the path-resolver response model has a single source of
# truth for outcome values. Lazy to avoid pulling path_resolver's optional
# ophyd / ophyd-async lazy imports into this module's import time.
from .path_resolver import Outcome as PathResolveOutcome

logger = logging.getLogger(__name__)


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
    args: list[Any] = Field(
        default_factory=list,
        description="Positional arguments for device constructor (e.g., ['BL01:DET1:'])",
    )
    kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments for device constructor (e.g., {'name': 'det1'})",
    )
    active: bool = Field(default=True, description="Whether this device should be instantiated")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "det1",
                "device_class": "ophyd.EpicsScaler",
                "args": ["BL01:DET1:"],
                "kwargs": {"name": "det1"},
                "active": True,
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
    module: str | None = Field(
        default=None, description="Python module containing the device class"
    )
    # Capability flags (from ophyd protocol introspection)
    is_movable: bool = Field(default=False, description="Implements Movable protocol")
    is_flyable: bool = Field(default=False, description="Implements Flyable protocol")
    is_readable: bool = Field(default=False, description="Implements Readable protocol")
    # Extended protocol flags (blueapi Device union protocols)
    is_triggerable: bool = Field(
        default=False, description="Implements Triggerable protocol (has trigger)"
    )
    is_stageable: bool = Field(
        default=False, description="Implements Stageable protocol (has stage/unstage)"
    )
    is_configurable: bool = Field(
        default=False,
        description="Implements Configurable protocol (has read_configuration/describe_configuration)",
    )
    is_pausable: bool = Field(
        default=False, description="Implements Pausable protocol (has pause/resume)"
    )
    is_stoppable: bool = Field(
        default=False, description="Implements Stoppable protocol (has stop)"
    )
    is_subscribable: bool = Field(
        default=False, description="Implements Subscribable protocol (has subscribe/clear_sub)"
    )
    is_checkable: bool = Field(
        default=False, description="Implements Checkable protocol (has check_value)"
    )
    writes_external_assets: bool = Field(
        default=False, description="Writes external assets (has collect_asset_docs)"
    )
    # PV and attribute info
    pvs: dict[str, str] = Field(default_factory=dict, description="Component name to PV mapping")
    hints: dict[str, Any] | None = Field(
        default=None, description="Bluesky hints for plotting/display"
    )
    read_attrs: list[str] = Field(default_factory=list, description="Readable attributes")
    configuration_attrs: list[str] = Field(
        default_factory=list, description="Configuration attributes"
    )
    parent: str | None = Field(default=None, description="Parent device if this is a component")
    # Labels for device grouping (BITS format)
    labels: list[str] = Field(
        default_factory=list,
        description="Device labels for grouping (e.g., 'motors', 'detectors', 'baseline')",
    )
    # Extended metadata (happi format)
    beamline: str | None = Field(default=None, description="Beamline identifier (from happi)")
    location_group: str | None = Field(default=None, description="Location grouping (from happi)")
    functional_group: str | None = Field(
        default=None, description="Functional grouping (from happi)"
    )
    documentation: str | None = Field(default=None, description="Device documentation/description")

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
                    "velocity": "BL01:SAMPLE:X.VELO",
                },
                "hints": {"fields": ["sample_x"]},
                "read_attrs": ["user_readback", "user_setpoint"],
                "configuration_attrs": ["velocity", "acceleration"],
                "parent": None,
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
    dtype: str | None = Field(default=None, description="EPICS data type")
    units: str | None = Field(default=None, description="Engineering units")
    precision: int | None = Field(default=None, description="Display precision")
    enum_strs: list[str] | None = Field(
        default=None, description="Enumeration strings for enum PVs"
    )
    upper_ctrl_limit: float | None = Field(default=None, description="Upper control limit")
    lower_ctrl_limit: float | None = Field(default=None, description="Lower control limit")
    device_name: str | None = Field(default=None, description="Owning device name if known")
    component_name: str | None = Field(default=None, description="Component name within device")

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
                "component_name": "user_readback",
            }
        }


class DeviceRegistry(BaseModel):
    """
    In-memory device registry.

    Loaded from beamline profile collection at startup.
    Provides fast lookup for device metadata and instantiation specs.
    """

    devices: dict[str, DeviceMetadata] = Field(
        default_factory=dict, description="Device name to metadata mapping"
    )
    pvs: dict[str, PVMetadata] = Field(
        default_factory=dict, description="PV name to metadata mapping"
    )
    instantiation_specs: dict[str, DeviceInstantiationSpec] = Field(
        default_factory=dict, description="Device name to instantiation specification mapping"
    )
    standalone_pv_names: set[str] = Field(
        default_factory=set,
        description=(
            "PVs registered as standalone (no owning device). Tracked so a "
            "device that later claims one of these PVs can be removed without "
            "destroying the standalone registration."
        ),
    )

    def get_device(self, name: str) -> DeviceMetadata | None:
        """Get device by name."""
        return self.devices.get(name)

    def list_devices(
        self,
        device_label: DeviceLabel | None = None,
        pattern: str | None = None,
        labels: list[str] | None = None,
        ophyd_class: str | None = None,
        readable: bool | None = None,
        movable: bool | None = None,
        flyable: bool | None = None,
    ) -> list[str]:
        """List device names with optional filtering.

        Args:
            device_label: Filter by device type
            pattern: Glob pattern for name matching
            labels: Filter by labels (device must have ALL specified labels)
            ophyd_class: Filter by ophyd device class name
            readable: Filter by the Readable protocol flag
            movable: Filter by the Movable protocol flag
            flyable: Filter by the Flyable protocol flag
        """
        names = list(self.devices.keys())

        if device_label:
            names = [name for name in names if self.devices[name].device_label == device_label]

        if pattern:
            # Simple glob pattern matching (* and ? supported)
            import fnmatch

            names = [name for name in names if fnmatch.fnmatch(name, pattern)]

        if labels:
            names = [
                name
                for name in names
                if all(label in self.devices[name].labels for label in labels)
            ]

        if ophyd_class:
            names = [name for name in names if self.devices[name].ophyd_class == ophyd_class]

        if readable is not None:
            names = [name for name in names if self.devices[name].is_readable == readable]

        if movable is not None:
            names = [name for name in names if self.devices[name].is_movable == movable]

        if flyable is not None:
            names = [name for name in names if self.devices[name].is_flyable == flyable]

        return sorted(names)

    def list_labels(self) -> list[str]:
        """Get all unique labels from devices."""
        all_labels: set = set()
        for device in self.devices.values():
            all_labels.update(device.labels)
        return sorted(all_labels)

    def get_pv(self, pv_name: str) -> PVMetadata | None:
        """Get PV metadata by name."""
        return self.pvs.get(pv_name)

    def search_pvs(self, pattern: str) -> list[str]:
        """Search PVs by glob pattern."""
        import fnmatch

        return sorted([pv for pv in self.pvs.keys() if fnmatch.fnmatch(pv, pattern)])

    def add_device(
        self, device: DeviceMetadata, instantiation_spec: DeviceInstantiationSpec | None = None
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
                    pv=pv_name, device_name=device.name, component_name=component_name
                )
            else:
                # Update existing PV with device ownership info. Shared PVs
                # are legitimate (compound devices + leaf-PV entries index the
                # same PV); the index points at the most recent registrant.
                # remove_device() re-homes the entry instead of deleting it,
                # so the earlier owner's registration survives removal.
                prior_owner = self.pvs[pv_name].device_name
                if prior_owner is not None and prior_owner != device.name:
                    logger.info(
                        "PV %s ownership reassigned from device %s to %s",
                        pv_name,
                        prior_owner,
                        device.name,
                    )
                self.pvs[pv_name].device_name = device.name
                self.pvs[pv_name].component_name = component_name

    def add_standalone_pv(self, pv_name: str) -> None:
        """Register a PV with no owning device.

        If a device already owns the PV in the index, the device's ownership
        is preserved (the device-level lock/disable gate stays in force) and
        the PV is only marked standalone — removal of that device then
        reverts the entry to standalone instead of deleting it.
        """
        self.standalone_pv_names.add(pv_name)
        if pv_name not in self.pvs:
            self.pvs[pv_name] = PVMetadata(pv=pv_name, device_name=None)
        elif self.pvs[pv_name].device_name is not None:
            logger.info(
                "PV %s registered standalone but is owned by device %s; device ownership preserved",
                pv_name,
                self.pvs[pv_name].device_name,
            )

    def remove_standalone_pv(self, pv_name: str) -> None:
        """Unregister a standalone PV.

        Drops the index entry only when no device owns it — deleting a
        device-owned entry here would destroy that device's registration.
        """
        self.standalone_pv_names.discard(pv_name)
        meta = self.pvs.get(pv_name)
        if meta is not None and meta.device_name is None:
            del self.pvs[pv_name]

    def _release_pv_ownership(self, name: str) -> None:
        """Release the PVs owned by ``name`` in the index without destroying
        entries that other devices or standalone registrations still need.

        A PV another device also lists is REASSIGNED to that device; a PV
        registered as standalone reverts to standalone (unowned); only PVs that
        nobody else claims are dropped. Shared by ``remove_device`` and
        ``update_device`` so that updating a device which drops a shared or
        standalone PV re-homes that PV instead of deleting the surviving entry.
        """
        owned = [pv_name for pv_name, pv_meta in self.pvs.items() if pv_meta.device_name == name]
        for pv_name in owned:
            new_owner = None
            for other_name, other_device in self.devices.items():
                if other_name == name:
                    continue
                for component_name, other_pv in other_device.pvs.items():
                    if other_pv == pv_name:
                        new_owner = (other_name, component_name)
                        break
                if new_owner:
                    break
            if new_owner is not None:
                self.pvs[pv_name].device_name = new_owner[0]
                self.pvs[pv_name].component_name = new_owner[1]
            elif pv_name in self.standalone_pv_names:
                self.pvs[pv_name].device_name = None
                self.pvs[pv_name].component_name = None
            else:
                del self.pvs[pv_name]

    def remove_device(self, name: str) -> bool:
        """Remove device from registry including its instantiation spec and indexed PVs.

        Args:
            name: Device name to remove

        Returns:
            True if device was found and removed, False if not found
        """
        if name not in self.devices:
            return False

        # Re-home or drop the PVs this device owns before removing it.
        self._release_pv_ownership(name)

        # Remove instantiation spec
        self.instantiation_specs.pop(name, None)

        # Remove device
        del self.devices[name]

        return True

    def update_device(
        self, device: DeviceMetadata, instantiation_spec: DeviceInstantiationSpec | None = None
    ) -> bool:
        """Update an existing device by re-homing old PV indexes and re-adding.

        Args:
            device: Updated device metadata
            instantiation_spec: Optional updated instantiation specification

        Returns:
            True if device existed and was updated, False if not found
        """
        if device.name not in self.devices:
            return False

        # Release the device's current PV ownership (re-homing shared/standalone
        # entries rather than destroying them), then re-add. add_device re-claims
        # the PVs this device still lists; any PV dropped by the update stays with
        # its surviving owner or reverts to standalone.
        self._release_pv_ownership(device.name)

        # Re-add with updated data
        self.add_device(device, instantiation_spec)
        return True

    def get_instantiation_spec(self, name: str) -> DeviceInstantiationSpec | None:
        """Get device instantiation specification by name."""
        return self.instantiation_specs.get(name)

    def list_instantiation_specs(
        self, active_only: bool = True
    ) -> dict[str, DeviceInstantiationSpec]:
        """Get all device instantiation specifications.

        Args:
            active_only: If True, only return active devices

        Returns:
            Dictionary mapping device name to instantiation spec
        """
        if active_only:
            return {name: spec for name, spec in self.instantiation_specs.items() if spec.active}
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


def _partial_field(src_cls: type[BaseModel], name: str) -> Any:
    """Optional Field whose description mirrors the canonical class.

    Keeps per-field OpenAPI docs in sync without duplication. Parity is
    enforced by TestPartialUpdateModelFieldParity.
    """
    return Field(default=None, description=src_cls.model_fields[name].description)


class DeviceInstantiationSpecUpdate(BaseModel):
    """Partial of DeviceInstantiationSpec for PATCH/PUT updates.

    Every field is Optional with default None so callers can send only
    the fields they want to change. Pair with ``model_dump(exclude_unset=True)``
    to distinguish "not sent" from "sent as None". Field set must mirror
    DeviceInstantiationSpec — enforced by test_partial_models_field_parity.
    """

    name: str | None = _partial_field(DeviceInstantiationSpec, "name")
    device_class: str | None = _partial_field(DeviceInstantiationSpec, "device_class")
    args: list[Any] | None = _partial_field(DeviceInstantiationSpec, "args")
    kwargs: dict[str, Any] | None = _partial_field(DeviceInstantiationSpec, "kwargs")
    active: bool | None = _partial_field(DeviceInstantiationSpec, "active")


class DeviceMetadataUpdate(BaseModel):
    """Partial of DeviceMetadata for PATCH/PUT updates.

    Every field is Optional with default None so callers can send only
    the fields they want to change. Pair with ``model_dump(exclude_unset=True)``
    to distinguish "not sent" from "sent as None". Field set must mirror
    DeviceMetadata — enforced by test_partial_models_field_parity.
    """

    name: str | None = _partial_field(DeviceMetadata, "name")
    device_label: DeviceLabel | None = _partial_field(DeviceMetadata, "device_label")
    ophyd_class: str | None = _partial_field(DeviceMetadata, "ophyd_class")
    module: str | None = _partial_field(DeviceMetadata, "module")
    is_movable: bool | None = _partial_field(DeviceMetadata, "is_movable")
    is_flyable: bool | None = _partial_field(DeviceMetadata, "is_flyable")
    is_readable: bool | None = _partial_field(DeviceMetadata, "is_readable")
    is_triggerable: bool | None = _partial_field(DeviceMetadata, "is_triggerable")
    is_stageable: bool | None = _partial_field(DeviceMetadata, "is_stageable")
    is_configurable: bool | None = _partial_field(DeviceMetadata, "is_configurable")
    is_pausable: bool | None = _partial_field(DeviceMetadata, "is_pausable")
    is_stoppable: bool | None = _partial_field(DeviceMetadata, "is_stoppable")
    is_subscribable: bool | None = _partial_field(DeviceMetadata, "is_subscribable")
    is_checkable: bool | None = _partial_field(DeviceMetadata, "is_checkable")
    writes_external_assets: bool | None = _partial_field(DeviceMetadata, "writes_external_assets")
    pvs: dict[str, str] | None = _partial_field(DeviceMetadata, "pvs")
    hints: dict[str, Any] | None = _partial_field(DeviceMetadata, "hints")
    read_attrs: list[str] | None = _partial_field(DeviceMetadata, "read_attrs")
    configuration_attrs: list[str] | None = _partial_field(DeviceMetadata, "configuration_attrs")
    parent: str | None = _partial_field(DeviceMetadata, "parent")
    labels: list[str] | None = _partial_field(DeviceMetadata, "labels")
    beamline: str | None = _partial_field(DeviceMetadata, "beamline")
    location_group: str | None = _partial_field(DeviceMetadata, "location_group")
    functional_group: str | None = _partial_field(DeviceMetadata, "functional_group")
    documentation: str | None = _partial_field(DeviceMetadata, "documentation")


class DeviceUpdateRequest(BaseModel):
    """Request model for updating a device.

    Supports field-level partial updates: only the fields you include
    in ``metadata`` or ``instantiation_spec`` are changed.  Omitted fields
    keep their current values.
    """

    metadata: DeviceMetadataUpdate | None = Field(
        default=None,
        description="Partial device metadata — only included fields are updated",
    )
    instantiation_spec: DeviceInstantiationSpecUpdate | None = Field(
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
    details: str | None = Field(default=None, description="Optional JSON details")


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
    metadata: DeviceMetadata | None = Field(default=None)
    spec: DeviceInstantiationSpec | None = Field(default=None)


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
    changes: list[DeviceChangeEntry] = Field(default_factory=list)


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
    description: str | None = Field(default=None, description="Human-readable description")
    protocol: PVProtocol = Field(default=PVProtocol.CA, description="EPICS protocol")
    access_mode: PVAccessMode = Field(default=PVAccessMode.READ_ONLY, description="Access mode")
    labels: list[str] = Field(default_factory=list, description="Labels for RBAC grouping")
    source: str = Field(default="runtime", description="Source of registration")
    created_by: str | None = Field(default=None, description="User who registered this PV")
    created_at: float | None = Field(default=None, description="Unix timestamp of creation")
    updated_at: float | None = Field(default=None, description="Unix timestamp of last update")


class StandalonePVCreateRequest(BaseModel):
    """Request model for registering a standalone PV."""

    # min_length=1 keeps an empty pv_name out of the registry. An empty key
    # is unremovable via DELETE /api/v1/pvs/standalone/{pv_name:path} since
    # an empty path segment doesn't match the route — once it's in the
    # PostgreSQL store the only way to clear it is to recreate the container.
    #
    # pattern=^[\x21-\x7e]+$ requires printable ASCII only — no whitespace,
    # no ASCII controls (NUL/BEL/ESC), no high-bit Unicode (ZWSP, NBSP,
    # BOM). All three classes hit the same unrecoverable-registry-entry
    # failure mode through different input shapes:
    #   - whitespace / newline: URL-encodes inconsistently
    #   - NUL: silently terminates C strings in downstream consumers
    #     (CA name comparisons, epicsString*), making "foo\x00bar" present
    #     as "foo" in some layers and as the full string in others
    #   - zero-width Unicode (U+200B, U+FEFF, etc.): visually identical to
    #     a different name, so the typed delete-by-name doesn't match
    # NSLS-II PV names like "XF:23ID2-OP{Mono}Enrgy-SP" are entirely within
    # printable ASCII; the constraint matches real EPICS CA naming.
    pv_name: str = Field(
        min_length=1,
        pattern=r"^[\x21-\x7e]+$",
        description="EPICS PV name (non-empty, printable ASCII, no whitespace)",
    )
    description: str | None = Field(default=None, description="Human-readable description")
    protocol: PVProtocol = Field(default=PVProtocol.CA, description="EPICS protocol")
    access_mode: PVAccessMode = Field(default=PVAccessMode.READ_ONLY, description="Access mode")
    labels: list[str] = Field(default_factory=list, description="Labels for RBAC grouping")

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

    description: str | None = Field(default=None, description="Human-readable description")
    protocol: PVProtocol | None = Field(default=None, description="EPICS protocol")
    access_mode: PVAccessMode | None = Field(default=None, description="Access mode")
    labels: list[str] | None = Field(default=None, description="Labels for RBAC grouping")

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

    device_names: list[str] = Field(description="Devices to lock")
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
    locked_by_plan: str | None = Field(default=None, description="Plan holding the lock")
    locked_at: str | None = Field(default=None, description="ISO timestamp of lock acquisition")


class LockPolicy(BaseModel):
    """Global device-lock availability policy.

    ``lock_all=True``: while ANY device lock is held (a plan is running),
    every registered device reports locked/unavailable — not just the
    devices the plan named. Lock acquisition/release semantics are
    unchanged. Boot default comes from CONFIG_LOCK_ALL; runtime value is
    read/changed via GET/PUT /api/v1/devices/lock/policy.
    """

    lock_all: bool


class DeviceLockResponse(BaseModel):
    """Response model for successful lock acquisition."""

    success: bool = Field(description="Whether locks were acquired")
    locked_devices: list[str] = Field(default_factory=list, description="Devices that were locked")
    locked_pvs: list[str] = Field(default_factory=list, description="PVs implicitly locked")
    lock_id: str | None = Field(default=None, description="Lock group identifier")
    registry_version: int = Field(description="Lock version counter")
    lock_epoch: str = Field(
        description=(
            "Lock-authority generation id. Changes when configuration_service "
            "restarts (in-memory lock state is rebuilt). Holders compare this "
            "across calls to detect that their locks were dropped and must be "
            "re-acquired."
        ),
    )
    expires_at: str | None = Field(
        default=None,
        description=(
            "ISO timestamp when the lease lapses if not renewed, or null when "
            "leases are disabled (CONFIG_LOCK_LEASE_TTL_SECONDS=0)."
        ),
    )
    lease_ttl_seconds: float = Field(
        default=0.0,
        description="Configured lease TTL in seconds (0 = leases disabled).",
    )


class DeviceLockConflictResponse(BaseModel):
    """Response model for lock conflict (409/404/422)."""

    success: bool = Field(default=False)
    message: str = Field(description="Human-readable error message")
    conflicting_devices: list[DeviceLockConflict] = Field(
        default_factory=list, description="Devices that caused the conflict"
    )


class DeviceUnlockRequest(BaseModel):
    """Request model for releasing device locks."""

    device_names: list[str] = Field(description="Devices to unlock")
    item_id: str = Field(description="Queue item ID that holds the lock")


class DeviceLockRenewRequest(BaseModel):
    """Request model for renewing (heartbeating) held device locks."""

    device_names: list[str] = Field(description="Devices whose lease to extend")
    item_id: str = Field(description="Queue item ID that holds the lock")

    class Config:
        json_schema_extra = {
            "example": {
                "device_names": ["sample_x", "det1"],
                "item_id": "550e8400-e29b-41d4-a716-446655440000",
            }
        }


class DeviceLockRenewResponse(BaseModel):
    """Response model for a lock-renewal (heartbeat).

    ``success`` is True only when every requested device was renewed. When
    ``lost`` is non-empty the holder no longer owns those locks here (they
    expired, were released, or the authority restarted) and must re-acquire;
    ``lock_epoch`` lets the holder confirm an authority reset.
    """

    success: bool = Field(description="True only if every device was renewed")
    renewed_devices: list[str] = Field(
        default_factory=list, description="Devices whose lease was extended"
    )
    lost_devices: list[str] = Field(
        default_factory=list,
        description="Requested devices no longer held here — re-acquire needed",
    )
    conflict_devices: list[str] = Field(
        default_factory=list,
        description="Requested devices currently held by a different item_id",
    )
    lock_epoch: str = Field(description="Lock-authority generation id")
    expires_at: str | None = Field(
        default=None, description="New lease expiry (null when leases disabled)"
    )


class DeviceUnlockResponse(BaseModel):
    """Response model for unlock operations."""

    success: bool = Field(description="Whether locks were released")
    unlocked_devices: list[str] = Field(
        default_factory=list, description="Devices that were unlocked"
    )
    registry_version: int = Field(description="Lock version counter")
    lock_epoch: str = Field(description="Lock-authority generation id")


class DeviceForceUnlockRequest(BaseModel):
    """Request model for administrative force-unlock."""

    device_names: list[str] = Field(description="Devices to force-unlock")
    reason: str = Field(description="Reason for force-unlock (for audit log)")

    class Config:
        json_schema_extra = {
            "example": {
                "device_names": ["sample_x"],
                "reason": "EE crashed during rel_scan, clearing stale locks",
            }
        }


class DeviceStatusResponse(BaseModel):
    """Combined device availability check (lock + enabled state + PV health)."""

    device_name: str = Field(description="Device name")
    available: bool = Field(description="True only when enabled AND unlocked")
    enabled: bool = Field(description="Whether the device is enabled for instantiation")
    lock_status: str = Field(description="Lock state: 'locked' or 'unlocked'")
    locked_by_plan: str | None = Field(default=None, description="Plan holding the lock")
    locked_by_item: str | None = Field(default=None, description="Queue item ID holding the lock")
    locked_at: str | None = Field(default=None, description="ISO timestamp of lock acquisition")
    locked_until: str | None = Field(
        default=None,
        description=(
            "ISO timestamp when the current lock's lease lapses if not "
            "renewed, or null when unlocked or leases are disabled."
        ),
    )
    lock_epoch: str = Field(
        description=(
            "Lock-authority generation id. Changes on a configuration_service "
            "restart (in-memory lock state is rebuilt). Readers (direct-control) "
            "can detect that the lock table was reset — every device will report "
            "unlocked until holders re-acquire."
        ),
    )
    pv_health: dict[str, "PVHealthRecord"] = Field(
        default_factory=dict,
        description=(
            "PV-level health records keyed by PV name. Only PVs that have "
            "failed at least once since service start appear here — PVs "
            "with no failures (or only successes) are intentionally not "
            "persisted to keep the store bounded by 'PVs needing attention' "
            "rather than 'PVs ever caput'd'. Absence therefore means "
            "'no failures observed, assume healthy'."
        ),
    )


class PVStatusResponse(BaseModel):
    """PV availability check (resolves PV to owning device lock state)."""

    pv_name: str = Field(description="EPICS PV name")
    available: bool = Field(
        description="True when owning device is enabled and unlocked (or standalone)"
    )
    device_name: str | None = Field(
        default=None, description="Owning device name (null for standalone PVs)"
    )
    device_enabled: bool | None = Field(
        default=None, description="Whether the owning device is enabled"
    )
    device_lock_status: str | None = Field(default=None, description="Owning device lock state")
    locked_by_plan: str | None = Field(
        default=None, description="Plan holding the lock on the owning device"
    )
    locked_by_item: str | None = Field(default=None, description="Queue item ID holding the lock")
    locked_at: str | None = Field(default=None, description="ISO timestamp of lock acquisition")


# ===== PV Health Tracking =====


class PVHealthState(str, Enum):
    """Per-PV health state derived from recent caput outcomes.

    Direct-control reports each caput's outcome (success or failure) back
    to configuration_service. The state is computed from the consecutive-
    failure counter so a single successful caput always resets to healthy.

    Frontends should color-code by state and surface ``unresponsive`` PVs
    prominently — they typically mean the IOC is down or a PV has been
    deleted from it.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNRESPONSIVE = "unresponsive"


# Threshold flipping degraded → unresponsive. Kept as a module-level
# constant for now; can be moved to Settings if a deployment argues for
# tuning it per-beamline.
PV_HEALTH_UNRESPONSIVE_THRESHOLD = 3


class PVHealthRecord(BaseModel):
    """Per-PV health record, returned by the health endpoints + embedded
    in the device-status response.

    ``state`` is a computed_field derived from ``consecutive_failures``;
    callers should never construct a record with an inconsistent state.
    """

    model_config = ConfigDict(extra="forbid")

    pv_name: str
    consecutive_failures: int = 0
    last_failure_at: datetime | None = None
    last_failure_message: str | None = None
    last_success_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def state(self) -> PVHealthState:
        if self.consecutive_failures == 0:
            return PVHealthState.HEALTHY
        if self.consecutive_failures < PV_HEALTH_UNRESPONSIVE_THRESHOLD:
            return PVHealthState.DEGRADED
        return PVHealthState.UNRESPONSIVE


class PVHealthReport(BaseModel):
    """Request body for the failure/success report endpoints from direct-control.

    Only failure reports carry a message; success reports are zero-content
    beyond the URL path identifying the PV.
    """

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(
        None,
        description="Diagnostic message for failure reports (EPICS error, timeout reason, etc.).",
    )


class PVHealthClearResponse(BaseModel):
    """Response from the admin clear endpoints.

    ``cleared`` is the count of records actually removed: 0 or 1 for the
    single-PV endpoint (idempotent on missing records), N for the
    clear-all endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    cleared: int = Field(..., ge=0, description="Number of records removed (never negative).")


class PVHealthStateCounts(BaseModel):
    """Per-state PV-health record counts.

    Three explicit fields (one per :class:`PVHealthState` value) rather
    than ``Dict[str, int]`` so the OpenAPI schema actually enforces the
    "every state is always present" contract — generated SDK clients
    get proper accessors and can't drift on a future state-machine
    addition without updating this model in lockstep.
    """

    model_config = ConfigDict(extra="forbid")

    healthy: int = Field(0, ge=0, description="PVs in the ``healthy`` state.")
    degraded: int = Field(0, ge=0, description="PVs in the ``degraded`` state.")
    unresponsive: int = Field(0, ge=0, description="PVs in the ``unresponsive`` state.")


class PVHealthStats(BaseModel):
    """At-a-glance count of PV-health records grouped by state.

    ``tracked_pvs`` is a ``computed_field`` derived from ``by_state``,
    so the two can never drift out of sync.
    """

    model_config = ConfigDict(extra="forbid")

    by_state: PVHealthStateCounts = Field(
        ...,
        description=(
            "Count of records grouped by state. All three states are "
            "always present as fields, zero if no records match."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tracked_pvs(self) -> int:
        """Total number of PVs with at least one health record."""
        return self.by_state.healthy + self.by_state.degraded + self.by_state.unresponsive


class NestedDeviceComponent(BaseModel):
    """Information about a nested device component."""

    name: str = Field(description="Component name")
    device_path: str = Field(description="Full path to component")
    parent_device: str = Field(description="Parent device name")
    component_type: str | None = Field(None, description="Component type")
    pv: str | None = Field(None, description="Associated EPICS PV")
    is_readable: bool = Field(default=True, description="Whether component is readable")
    is_settable: bool = Field(default=False, description="Whether component is settable")


# ===== Path Resolver Models =====


class PathResolveRequest(BaseModel):
    """Batch request to resolve dotted device addresses to PV names.

    Each address is one of:
    - ``"<device>"`` — top-level happi entry. For classic-ophyd entries
      whose class is a single ``EpicsSignal`` / ``EpicsMotor`` this
      resolves to the device's prefix. ophyd-async devices always need a
      sub-attribute and return ``no_such_attr`` for top-level addressing.
    - ``"<device>.<attr>.<attr>..."`` — walks the device class structure.

    Resolution never opens EPICS connections, but the per-framework
    introspection differs:

    - **Classic ophyd**: configuration_service walks the class-level
      ``Component`` / ``DynamicDeviceComponent`` / ``FormattedComponent``
      declarations. No instantiation. ``FormattedComponent`` suffixes
      with real ``{}`` placeholders return ``needs_enrichment`` since
      they need a live parent to evaluate.
    - **ophyd-async**: signals are created in ``__init__``, so
      configuration_service instantiates the device locally (without
      calling ``.connect()``) and reads each leaf signal's
      ``.source`` URI.
    """

    addresses: list[str] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Dotted device addresses to resolve.",
    )


class PathResolveResultItem(BaseModel):
    """One row in the batch response.

    ``ok`` is derived from ``outcome`` (``True`` iff
    ``outcome is Outcome.RESOLVED``); declared as a ``computed_field`` so
    the JSON output carries it for client convenience but it cannot drift
    from ``outcome``. Other outcomes carry their reason in ``message`` so
    the frontend can show the operator why a particular address couldn't
    be resolved.
    """

    address: str
    outcome: PathResolveOutcome = Field(
        description=(
            "Per-address result kind. See ``PathResolveOutcome`` for the full set of values."
        )
    )
    pv_name: str | None = None
    message: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.outcome is PathResolveOutcome.RESOLVED


class PathResolveResponse(BaseModel):
    """Aggregate response for a batch resolve.

    Always 200 — per-address outcomes are in the rows. Resolution is
    read-only with no state change, so "best effort with per-item errors"
    is the right semantic; unlike batch caput there's no halt-on-failure.
    """

    resolved: list[PathResolveResultItem]
