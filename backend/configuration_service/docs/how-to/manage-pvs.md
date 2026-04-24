# Manage Standalone PVs

Standalone PVs are EPICS PVs that are not associated with any ophyd device. They appear in the unified PV registry alongside device-bound PVs but are managed separately.

Use cases: ring current, beamline status PVs, vacuum gauges, or any PV you want to track without creating a full device definition.

## Register a standalone PV

```bash
curl -X POST http://localhost:8004/api/v1/pvs \
  -H "Content-Type: application/json" \
  -d '{
    "pv_name": "SR:C01:RING:CURR",
    "description": "Storage ring beam current",
    "protocol": "ca",
    "access_mode": "read-only",
    "labels": ["machine", "beam-diagnostics"]
  }'
```

Returns `201 Created`. Returns `409` if the PV name already exists (either as a device-bound PV or another standalone PV).

Only `pv_name` is required. All other fields are optional.

## List standalone PVs

```bash
# All standalone PVs
curl http://localhost:8004/api/v1/pvs/standalone

# Filter by labels (comma-separated, AND logic)
curl "http://localhost:8004/api/v1/pvs/standalone?labels=machine,beam-diagnostics"
```

## List standalone PV labels

```bash
curl http://localhost:8004/api/v1/pvs/labels
```

Returns a sorted list of all unique labels across all standalone PVs.

## Update a standalone PV

Partial update — only provided fields are changed, others keep their current values.

```bash
curl -X PUT http://localhost:8004/api/v1/pvs/standalone/SR:C01:RING:CURR \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Storage ring beam current (averaged)",
    "labels": ["machine", "beam-diagnostics", "averaging"]
  }'
```

## Delete a standalone PV

```bash
curl -X DELETE http://localhost:8004/api/v1/pvs/standalone/SR:C01:RING:CURR
```

Removes from both the persistent store and the in-memory PV registry.

## Standalone PVs in the unified PV registry

Once registered, standalone PVs appear in the main PV endpoints alongside device-bound PVs:

```bash
# Appears in the full PV list
curl http://localhost:8004/api/v1/pvs

# Resolvable by name (device_name will be null)
curl http://localhost:8004/api/v1/pvs/SR:C01:RING:CURR
```

## Persistence

Standalone PVs are stored in the same SQLite database as the device registry. They survive service restarts and are re-applied to the in-memory registry on startup. They are not affected by `POST /api/v1/registry/reset`.
