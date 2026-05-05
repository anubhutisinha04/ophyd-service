# API Reference

Base URL: `http://localhost:8003`

Interactive documentation: `http://localhost:8003/docs` (Swagger UI)

## Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health, coordination and auth service availability |

## Statistics

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/stats` | Runtime stats: coordination status, command timeout |

## PV Control (Low-Fidelity Channel)

Direct EPICS Channel Access reads and writes. PVs must be registered
in the Configuration Service before access is allowed.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/pv/{pv_name}/value` | Read a PV value via caget. Returns `{pv_name, value, timestamp}` |
| POST | `/api/v1/pv/set` | Write a PV value via caput. Body: `{pv_name, value}`. Checks A4 device locks before writing |

### Example: Read a PV

```bash
curl http://localhost:8003/api/v1/pv/XF:31ID1-ES%7BSIM-Cam:2%7Dcam1:AcquireTime_RBV/value
```

### Example: Write a PV

```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "XF:31ID1-ES{SIM-Cam:2}cam1:GainX", "value": 2.5}'
```

## Device Control (High-Fidelity Channel)

Ophyd device-level operations. Devices must be registered in the
Configuration Service. A4 coordination locks are checked before
any write operation.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/device/{device_path}/value` | Read a device or nested component value |
| POST | `/api/v1/device/{device_path}` | Access/control a nested device component |
| POST | `/api/v1/device/execute` | Execute a device method. Body: `{device_name, method, args?, kwargs?}` |
| POST | `/api/v1/device/{device_name}/stop` | Emergency stop a device |

## Configuration

Environment variables (prefix `DIRECT_CONTROL_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8003` | HTTP port |
| `LOG_LEVEL` | `info` | Log level |
| `CONFIGURATION_SERVICE_URL` | **required** | configuration_service URL — owns PV/device registry AND device-lock state read by the A4 coordination check |
| `COORDINATION_CHECK_ENABLED` | `true` | Check device locks before writes |
| `COORDINATION_TIMEOUT` | `5.0` | Coordination check timeout (seconds) |
| `COMMAND_TIMEOUT` | `30.0` | EPICS command timeout (seconds) |
| `EPICS_CA_AUTO_ADDR_LIST` | `true` | Auto-discover EPICS IOCs |
| `EPICS_CA_ADDR_LIST` | — | Explicit EPICS address list |
| `ENABLE_METRICS` | `true` | Enable Prometheus metrics |
| `METRICS_PORT` | `9003` | Prometheus metrics port |
