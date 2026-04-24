# Getting Started

This tutorial walks through starting the Configuration Service, exploring its API, and adding a device at runtime. By the end you will understand the core workflow: load devices, query them, and modify the registry.

## Prerequisites

Install the service:

```bash
cd configuration_service
uv sync
```

## Start the service

We will use mock mode, which loads three built-in devices (`sample_x`, `det1`, `cam1`) so we can explore the API without needing a real beamline profile.

```bash
uv run bluesky-configuration-service --use-mock-data
```

You should see output like:

```
Starting Configuration Service (SVC-004)
  Host: 0.0.0.0
  Port: 8004
  Load Strategy: mock

API Documentation: http://0.0.0.0:8004/docs
```

Open http://localhost:8004/docs in a browser to see the Swagger UI. Leave the service running and open a second terminal for the next steps.

## Check health

```bash
curl -s http://localhost:8004/health | python -m json.tool
```

```json
{
    "status": "healthy",
    "service": "configuration_service",
    "devices_loaded": 3
}
```

The service is running with 3 mock devices.

## List devices

```bash
curl -s http://localhost:8004/api/v1/devices | python -m json.tool
```

```json
["cam1", "det1", "sample_x"]
```

These are the names of all registered devices, sorted alphabetically.

## Get device metadata

Pick a device and fetch its full metadata:

```bash
curl -s http://localhost:8004/api/v1/devices/sample_x | python -m json.tool
```

```json
{
    "name": "sample_x",
    "device_label": "motor",
    "ophyd_class": "EpicsMotor",
    "module": "ophyd.epics_motor",
    "is_movable": true,
    "is_readable": true,
    "is_triggerable": true,
    "is_stageable": true,
    "is_configurable": true,
    "is_stoppable": true,
    "is_subscribable": true,
    "is_checkable": true,
    "pvs": {
        "user_readback": "BL01:SAMPLE:X.RBV",
        "user_setpoint": "BL01:SAMPLE:X",
        "velocity": "BL01:SAMPLE:X.VELO"
    },
    "hints": {"fields": ["sample_x"]},
    "read_attrs": ["user_readback", "user_setpoint"],
    "configuration_attrs": ["velocity", "acceleration"]
}
```

This tells us everything about `sample_x`: it is a motor, it has three PVs, it is movable and stoppable, and its ophyd class is `EpicsMotor`.

## Filter devices

List only motors:

```bash
curl -s "http://localhost:8004/api/v1/devices?device_label=motor"
```

```json
["sample_x"]
```

List devices matching a glob pattern:

```bash
curl -s "http://localhost:8004/api/v1/devices?pattern=det*"
```

```json
["det1"]
```

## Browse PVs

List all PVs across all devices:

```bash
curl -s http://localhost:8004/api/v1/pvs | python -m json.tool
```

```json
{
    "success": true,
    "pvs": [
        "BL01:CAM1:Stats1:CentroidX_RBV",
        "BL01:CAM1:Stats1:CentroidY_RBV",
        "BL01:CAM1:Stats1:Total_RBV",
        "BL01:CAM1:cam1:Acquire",
        "BL01:CAM1:cam1:AcquireTime",
        "BL01:CAM1:cam1:ImageMode",
        "BL01:CAM1:image1:ArrayData",
        "BL01:CAM1:image1:ArraySize0",
        "BL01:CAM1:image1:ArraySize1",
        "BL01:DET1:CNT",
        "BL01:DET1:PRESET",
        "BL01:SAMPLE:X",
        "BL01:SAMPLE:X.RBV",
        "BL01:SAMPLE:X.VELO"
    ],
    "count": 14
}
```

Given a PV, find which device owns it:

```bash
curl -s "http://localhost:8004/api/v1/pvs/lookup?pv_name=BL01:SAMPLE:X.RBV" | python -m json.tool
```

```json
{
    "pv_name": "BL01:SAMPLE:X.RBV",
    "device_name": "sample_x",
    "device_label": "motor",
    "prefix": "BL01:SAMPLE:X",
    "sibling_pvs": {
        "user_readback": "BL01:SAMPLE:X.RBV",
        "user_setpoint": "BL01:SAMPLE:X",
        "velocity": "BL01:SAMPLE:X.VELO"
    },
    "count": 3
}
```

## Add a device at runtime

Create a new motor by posting its metadata and instantiation spec:

```bash
curl -s -X POST http://localhost:8004/api/v1/devices \
  -H "Content-Type: application/json" \
  -d '{
    "metadata": {
      "name": "sample_y",
      "device_label": "motor",
      "ophyd_class": "EpicsMotor",
      "is_movable": true,
      "is_readable": true,
      "pvs": {
        "user_readback": "BL01:SAMPLE:Y.RBV",
        "user_setpoint": "BL01:SAMPLE:Y"
      }
    },
    "instantiation_spec": {
      "name": "sample_y",
      "device_class": "ophyd.EpicsMotor",
      "args": ["BL01:SAMPLE:Y"],
      "kwargs": {"name": "sample_y"}
    }
  }' | python -m json.tool
```

```json
{
    "success": true,
    "device_name": "sample_y",
    "operation": "create",
    "message": "Device 'sample_y' created successfully"
}
```

Verify it exists:

```bash
curl -s http://localhost:8004/api/v1/devices
```

```json
["cam1", "det1", "sample_x", "sample_y"]
```

The new device is persisted to SQLite and will survive service restarts.

## Delete a device

```bash
curl -s -X DELETE http://localhost:8004/api/v1/devices/sample_y | python -m json.tool
```

```json
{
    "success": true,
    "device_name": "sample_y",
    "operation": "delete",
    "message": "Device 'sample_y' deleted successfully"
}
```

## View the audit log

Every mutation is recorded:

```bash
curl -s "http://localhost:8004/api/v1/devices/history?limit=5" | python -m json.tool
```

You will see `seed`, `add`, and `delete` entries with timestamps.

## Run the tests

Stop the service (Ctrl+C) and run the test suite:

```bash
uv run pytest tests/
```

All 151 tests use mock data and run in about 12 seconds.

## Next steps

- [Manage Devices](../how-to/manage-devices.md) — update, enable/disable, reset the registry
- [Load Profiles](../how-to/load-profiles.md) — load real beamline devices from happi or BITS profiles
- [API Reference](../reference/api.md) — complete endpoint listing
- [Architecture](../explanation/architecture.md) — how the startup flow and persistence work
