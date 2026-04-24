# Manage Devices

## Create a device

POST a `metadata` and `instantiation_spec` with matching `name` fields.

```bash
curl -X POST http://localhost:8004/api/v1/devices \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {
      "name": "my_motor",
      "device_label": "motor",
      "ophyd_class": "EpicsMotor",
      "is_movable": true,
      "is_readable": true,
      "pvs": {
        "user_readback": "BL:MOT.RBV",
        "user_setpoint": "BL:MOT"
      }
    },
    "instantiation_spec": {
      "name": "my_motor",
      "device_class": "ophyd.EpicsMotor",
      "args": ["BL:MOT"],
      "kwargs": {"name": "my_motor"}
    }
  }'
```

Returns `201 Created`. Returns `409` if the name already exists.

## Update a device

PUT supports field-level partial updates. Only the fields you include are
changed — omitted fields keep their current values.

```bash
# Update just the documentation and labels
curl -X PUT http://localhost:8004/api/v1/devices/my_motor \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {
      "documentation": "Sample X translation stage",
      "labels": ["motors", "sample-stage"]
    }
  }'

# Update just the instantiation spec's active flag
curl -X PUT http://localhost:8004/api/v1/devices/my_motor \
  -H "Content-Type: application/json" \
  -d '{
    "instantiation_spec": {
      "active": false
    }
  }'
```

If `name` is included in the body, it must match the path parameter.

## Delete a device

```bash
curl -X DELETE http://localhost:8004/api/v1/devices/my_motor
```

Returns `200` on success, `404` if not found.

## Disable a device

A disabled device stays in the registry but is excluded from active instantiation spec listings. Other services pulling the device list will skip it.

```bash
curl -X PATCH http://localhost:8004/api/v1/devices/my_motor/disable
```

## Enable a device

```bash
curl -X PATCH http://localhost:8004/api/v1/devices/my_motor/enable
```

Both enable and disable are idempotent — calling them when already in that state returns success.

## View the audit log

Every mutation (seed, add, update, delete, reset, lock, unlock) is recorded.

```bash
# All entries
curl http://localhost:8004/api/v1/devices/history

# Filter to one device
curl "http://localhost:8004/api/v1/devices/history?device_name=my_motor"

# Limit results
curl "http://localhost:8004/api/v1/devices/history?limit=10"
```

## Reset the registry

Wipe all device data and re-seed from the profile collection. This erases all runtime changes (creates, updates, deletes).

```bash
curl -X POST http://localhost:8004/api/v1/registry/reset
```

Standalone PVs are preserved and re-applied after the reset.

## Export the registry

Export all devices in happi JSON format:

```bash
curl http://localhost:8004/api/v1/registry/export?format=happi > devices.json
```
