# Data Models

All models are Pydantic v2 `BaseModel` subclasses defined in `src/configuration_service/models.py`.

## DeviceLabel

Enum classifying devices by their ophyd base class.

| Value | Ophyd Class |
|-------|-------------|
| `motor` | `EpicsMotor`, `Motor` |
| `detector` | `DetectorBase`, `StandardDetector` |
| `signal` | `Signal`, `EpicsSignal` |
| `flyer` | `FlyerInterface`, `StandardFlyer` |
| `readable` | `StandardReadable` (readable but not motor/detector) |
| `device` | `Device` (generic base) |

## DeviceMetadata

Full device description.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | required | Device name |
| `device_label` | DeviceLabel | required | Classification |
| `ophyd_class` | str | required | Class name (e.g., `EpicsMotor`) |
| `module` | str | `None` | Python module (e.g., `ophyd.epics_motor`) |
| `is_movable` | bool | `False` | Movable protocol |
| `is_flyable` | bool | `False` | Flyable protocol |
| `is_readable` | bool | `False` | Readable protocol |
| `is_triggerable` | bool | `False` | Triggerable protocol |
| `is_stageable` | bool | `False` | Stageable protocol |
| `is_configurable` | bool | `False` | Configurable protocol |
| `is_pausable` | bool | `False` | Pausable protocol |
| `is_stoppable` | bool | `False` | Stoppable protocol |
| `is_subscribable` | bool | `False` | Subscribable protocol |
| `is_checkable` | bool | `False` | Checkable protocol |
| `writes_external_assets` | bool | `False` | WritesExternalAssets protocol |
| `pvs` | dict[str, str] | `{}` | Component name → PV name mapping |
| `hints` | dict | `None` | Bluesky hints for plotting |
| `read_attrs` | list[str] | `[]` | Readable attribute names |
| `configuration_attrs` | list[str] | `[]` | Configuration attribute names |
| `parent` | str | `None` | Parent device name (if component) |
| `labels` | list[str] | `[]` | User-defined labels (e.g., `["motors", "baseline"]`) |
| `beamline` | str | `None` | Beamline identifier |
| `location_group` | str | `None` | Location grouping (from happi) |
| `functional_group` | str | `None` | Functional grouping (from happi) |
| `documentation` | str | `None` | Device description |

## DeviceInstantiationSpec

Everything needed to recreate a device in another service.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | str | required | Device name |
| `device_class` | str | required | Fully qualified class path (e.g., `ophyd.EpicsMotor`) |
| `args` | list | `[]` | Positional constructor args (e.g., `["BL01:SAMPLE:X"]`) |
| `kwargs` | dict | `{}` | Keyword constructor args (e.g., `{"name": "sample_x"}`) |
| `active` | bool | `True` | Whether the device should be instantiated |

## PVMetadata

EPICS PV description.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pv` | str | required | PV name |
| `connected` | bool | `False` | Connection status |
| `dtype` | str | `None` | EPICS data type |
| `units` | str | `None` | Engineering units |
| `precision` | int | `None` | Display precision |
| `enum_strs` | list[str] | `None` | Enum strings |
| `upper_ctrl_limit` | float | `None` | Upper control limit |
| `lower_ctrl_limit` | float | `None` | Lower control limit |
| `device_name` | str | `None` | Owning device (null for standalone PVs) |
| `component_name` | str | `None` | Component within device |

## DeviceRegistry

In-memory container holding all devices, PVs, and instantiation specs.

| Field | Type | Description |
|-------|------|-------------|
| `devices` | dict[str, DeviceMetadata] | Name → metadata |
| `pvs` | dict[str, PVMetadata] | PV name → metadata |
| `instantiation_specs` | dict[str, DeviceInstantiationSpec] | Name → spec |

Methods: `get_device()`, `list_devices()`, `add_device()`, `remove_device()`, `update_device()`, `get_pv()`, `search_pvs()`, `get_instantiation_spec()`, `list_instantiation_specs()`, `list_labels()`.

## StandalonePV

A PV not associated with any ophyd device.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pv_name` | str | required | PV name |
| `description` | str | `None` | Human-readable description |
| `protocol` | `ca` or `pva` | `ca` | EPICS protocol |
| `access_mode` | `read-only` or `read-write` | `read-only` | Access mode |
| `labels` | list[str] | `[]` | Labels for grouping |
| `source` | str | `runtime` | Source identifier |
| `created_by` | str | `None` | Registering user |
| `created_at` | float | `None` | Unix timestamp |
| `updated_at` | float | `None` | Unix timestamp |

## DeviceAuditEntry

Append-only change log entry.

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Auto-incrementing ID |
| `device_name` | str | Device name (`*` for registry-wide operations) |
| `operation` | str | `seed`, `add`, `update`, `delete`, `reset`, `lock`, `unlock`, `force_unlock` |
| `timestamp` | float | Unix timestamp |
| `details` | str | Optional JSON with context |

## MetadataEntry

A stored metadata key-value entry.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `key` | str | required | Unique string key |
| `value` | dict[str, Any] | required | Arbitrary JSON dictionary |
| `created_at` | float | `None` | Unix timestamp of creation |
| `updated_at` | float | `None` | Unix timestamp of last update |

## Request/Response Models

**DeviceCreateRequest**: `{metadata: DeviceMetadata, instantiation_spec: DeviceInstantiationSpec}`

**DeviceUpdateRequest**: `{metadata?: dict[str, Any], instantiation_spec?: dict[str, Any]}` — field-level partial update; only included fields are changed, omitted fields keep existing values

**DeviceCRUDResponse**: `{success: bool, device_name: str, operation: str, message: str}`

**DeviceLockRequest**: `{device_names: list[str], item_id: str, plan_name: str, locked_by_service?: str}`

**DeviceLockResponse**: `{success: bool, locked_devices: list[str], locked_pvs: list[str], lock_id: str, registry_version: int}`

**DeviceStatusResponse**: `{device_name: str, available: bool, enabled: bool, lock_status: str, locked_by_plan?: str, locked_by_item?: str, locked_at?: str}`

**PVStatusResponse**: `{pv_name: str, available: bool, device_name?: str, device_enabled?: bool, device_lock_status?: str, locked_by_plan?: str}`

**MetadataWriteRequest**: `{value: dict[str, Any]}`

**MetadataCRUDResponse**: `{success: bool, key: str, operation: str, message: str}`
