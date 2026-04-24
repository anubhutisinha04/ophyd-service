# API Reference

Base URL: `http://localhost:8004`

Interactive documentation: `http://localhost:8004/docs` (Swagger UI)

## Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Returns `{"status": "healthy", "service": "configuration_service", "devices_loaded": N}` |
| GET | `/ready` | Returns `{"status": "ready"}` or 503 if the registry is not yet loaded |

## Device Registry

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/devices` | List device names. Query params: `device_label`, `pattern` (glob), `ophyd_class` |
| GET | `/api/v1/devices-info` | All devices with full metadata as `{name: DeviceMetadata}` |
| GET | `/api/v1/devices/classes` | Sorted unique ophyd class names |
| GET | `/api/v1/devices/types` | Sorted unique device label values (`motor`, `detector`, etc.) |
| GET | `/api/v1/devices/{device_name}` | Single device metadata. 404 if not found |
| GET | `/api/v1/devices/{device_name}/pvs` | PVs owned by a device, with component mapping |

## Device Instantiation

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/devices/instantiation` | All instantiation specs. Query param: `active_only` (default `true`) |
| GET | `/api/v1/devices/{device_name}/instantiation` | Single device instantiation spec. 404 if not found |

## Device Management (CRUD)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/devices` | Create device. Body: `{metadata, instantiation_spec}`. 201 on success, 409 if exists |
| PUT | `/api/v1/devices/{device_name}` | Update device. Body: `{metadata?, instantiation_spec?}`. Partial merge |
| DELETE | `/api/v1/devices/{device_name}` | Delete device. 404 if not found |
| PATCH | `/api/v1/devices/{device_name}/enable` | Set `active=true` on instantiation spec. Idempotent |
| PATCH | `/api/v1/devices/{device_name}/disable` | Set `active=false` on instantiation spec. Idempotent |
| GET | `/api/v1/devices/history` | Audit log. Query params: `device_name`, `limit` (default 1000) |

## Registry Admin

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/registry/reset` | Wipe DB and re-seed from profile. Preserves standalone PVs |
| POST | `/api/v1/registry/clear` | Wipe DB to empty without re-seeding. Preserves standalone PVs |
| GET | `/api/v1/registry/export` | Export in happi format. Query param: `format` (only `happi` supported) |

## PV Registry

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/pvs` | All PVs (device-bound + standalone). Query param: `pattern` (glob) |
| GET | `/api/v1/pvs/detailed` | PVs organized by device: `{devices: {name: {component: pv}}}` |
| GET | `/api/v1/pvs/lookup` | Find owning device from PV name. Query param: `pv_name` (required) |
| GET | `/api/v1/pvs/{pv_name}` | Single PV metadata. Path uses `:path` converter for PV names with dots |

## Standalone PVs

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/pvs` | Register standalone PV. Body: `{pv_name, description?, protocol?, access_mode?, labels?}`. 409 if exists |
| GET | `/api/v1/pvs/standalone` | List standalone PVs. Query param: `labels` (comma-separated) |
| GET | `/api/v1/pvs/labels` | Unique labels across standalone PVs |
| PUT | `/api/v1/pvs/standalone/{pv_name}` | Update standalone PV. Partial merge. 404 if not found |
| DELETE | `/api/v1/pvs/standalone/{pv_name}` | Delete standalone PV. 404 if not found |

## Device Locking

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/devices/lock` | Acquire locks atomically. Body: `{device_names, item_id, plan_name}`. 409 on conflict, 404 if not found, 422 if disabled |
| POST | `/api/v1/devices/unlock` | Release locks. Body: `{device_names, item_id}`. 403 if wrong owner |
| POST | `/api/v1/devices/force-unlock` | Admin override. Body: `{device_names, reason}`. 404 if not found |
| GET | `/api/v1/devices/{device_name}/status` | Lock and enabled state. Returns `{available, enabled, lock_status}` |
| GET | `/api/v1/pvs/status` | PV availability via owning device. Query param: `pv_name` (required) |

## Metadata

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/metadata` | List all metadata entries, sorted by key |
| GET | `/api/v1/metadata/{key}` | Get a single metadata entry. 404 if not found |
| POST | `/api/v1/metadata/{key}` | Create a metadata entry. Body: `{value: {...}}`. 409 if key exists |
| PUT | `/api/v1/metadata/{key}` | Create or replace a metadata entry (upsert). Body: `{value: {...}}` |
| DELETE | `/api/v1/metadata/{key}` | Delete a metadata entry. 404 if not found |

## Device Components

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/devices/{device_name}/components` | List components. Query param: `max_depth` (0 = all) |
| GET | `/api/v1/devices/{device_path}/component` | Single component by dot-path (e.g., `cam1.cam.acquire`) |
