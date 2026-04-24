# Direct Device Control + Monitoring Service (SVC-003)

Combined service: A4-coordinated device commanding **and** real-time EPICS PV
monitoring via WebSocket, running on a single port.

## Features

- **A4 Device Coordination**: Checks with experiment_execution before any write; returns `423 Locked` if a plan holds the device.
- **PV Control**: Low-fidelity channel for EPICS PV set/get (fire-and-forget or put-completion).
- **Device Method Execution**: High-fidelity channel for Ophyd device methods, always confirmed.
- **Nested Device Access**: Navigate device component hierarchies (ophyd-websocket compatible).
- **EPICS PV Monitoring**: Channel Access + PVAccess subscriptions via ophyd (pyepics, p4p).
- **WebSocket Streaming**: Real-time PV and device updates; writes route through coordination.
- **ophyd-websocket compatible**: `pv-socket`, `device-socket`, `control-socket` endpoints.
- **No in-service auth**: Authorization is handled by upstream middleware.

## Deployment

### Installation

```bash
pip install -e .
```

### Running the Service

```bash
# Basic startup (port 8003)
bluesky-direct-control

# With custom experiment_execution URL
export DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL=http://localhost:8001
bluesky-direct-control

# With EPICS configuration
export EPICS_CA_ADDR_LIST="10.0.0.255"
bluesky-direct-control

# Disable coordination checks (testing only)
DIRECT_CONTROL_COORDINATION_CHECK_ENABLED=false bluesky-direct-control

# Development mode with auto-reload
bluesky-direct-control --reload --log-level debug
```

### Docker

```bash
docker build -t bluesky-direct-control .
docker run -p 8003:8003 \
  -e DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL=http://host.docker.internal:8001 \
  bluesky-direct-control
```

## Service Dependencies

| Dependency | Interface | Purpose |
|------------|-----------|---------|
| `experiment_execution` | `/api/v1/coordination/devices/{name}/status` | A4 device lock status |
| `configuration_service` | `/api/v1/devices/{name}`, `/api/v1/pvs` | Device + PV registry |

## Configuration

All settings use the `DIRECT_CONTROL_` environment variable prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `DIRECT_CONTROL_HOST` | `0.0.0.0` | Bind address |
| `DIRECT_CONTROL_PORT` | `8003` | HTTP port |
| `DIRECT_CONTROL_LOG_LEVEL` | `info` | Log level |
| `DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL` | `http://localhost:8001` | Experiment Execution URL |
| `DIRECT_CONTROL_CONFIGURATION_SERVICE_URL` | `http://localhost:8004` | Configuration Service URL |
| `DIRECT_CONTROL_COORDINATION_CHECK_ENABLED` | `true` | Enable A4 coordination checks |
| `DIRECT_CONTROL_COORDINATION_TIMEOUT` | `5.0` | Coordination check timeout (s) |
| `DIRECT_CONTROL_COMMAND_TIMEOUT` | `30.0` | Command execution timeout (s) |
| `DIRECT_CONTROL_WS_MAX_CONNECTIONS` | `100` | Max WebSocket connections |
| `DIRECT_CONTROL_WS_HEARTBEAT_INTERVAL` | `30` | Heartbeat interval (s) |
| `DIRECT_CONTROL_WS_MESSAGE_QUEUE_SIZE` | `1000` | Message queue size |
| `DIRECT_CONTROL_PV_BUFFER_SIZE` | `100` | PV value buffer size |
| `DIRECT_CONTROL_PV_UPDATE_RATE_LIMIT` | `0.1` | Min seconds between updates |
| `DIRECT_CONTROL_RESPONSE_BYTESIZE_LIMIT` | `100000000` | Max bytesize for PV value responses (400 if exceeded) |
| `DIRECT_CONTROL_MAX_SUBSCRIPTIONS_PER_CLIENT` | `1000` | Max PVs (pv-socket) or devices (device-socket) one WS client may subscribe to. 0 disables the cap. |
| `DIRECT_CONTROL_ENABLE_METRICS` | `true` | Enable Prometheus metrics |
| `DIRECT_CONTROL_METRICS_PORT` | `9003` | Metrics port |
| `EPICS_CA_ADDR_LIST` | — | EPICS Channel Access address list |
| `EPICS_CA_AUTO_ADDR_LIST` | `YES` | Auto-discover EPICS addresses |

### CLI Options

```
bluesky-direct-control --help
```

## API Endpoints

### Health & Status
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (coordination + monitoring stats) |
| GET | `/api/v1/stats` | Combined control + monitoring statistics |

### PV Control (Low Fidelity, coordination-checked)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/pv/set` | Set PV value (pyepics caput knobs) |
| GET | `/api/v1/pv/{pv_name}/value` | One-shot CA get (pyepics caget knobs as query params) |

**`POST /api/v1/pv/set` body fields** (pyepics `caput` knobs):

| Field | Type | Default | pyepics mapping |
|---|---|---|---|
| `pv_name` | string (required) | — | `caput(pvname=…)` |
| `value` | any (required) | — | `caput(value=…)` |
| `wait` | bool | `false` | `caput(wait=…)` — block CA thread until done |
| `timeout` | float \| null | `command_timeout` (30s) | `caput(timeout=…)` |
| `connection_timeout` | float \| null | pyepics default (5s) | `caput(connection_timeout=…)` |
| `use_complete` | bool | `false` | Routes to `PV.put(use_complete=True)`; service awaits put-callback without holding a CA thread. Overrides `wait`. |
| `ftype` | int \| null | native | Forces non-native DBR type via `ca.put(ftype=…)` (power-user) |

Completion modes:
- `wait=false, use_complete=false` — fire-and-forget.
- `wait=true, use_complete=false` — blocking wait (ties up a CA thread for up to `timeout`).
- `use_complete=true` — put-with-callback; preferred for long puts over HTTP since no worker thread is held.

**`GET /api/v1/pv/{pv_name}/value` query params** (pyepics `caget` knobs):

| Param | Type | Default | Meaning |
|---|---|---|---|
| `format` | string \| null | — | Override `Accept` header: `json` or `binary` (octet-stream) |
| `as_string` | bool | `false` | Return string representation (enum labels, char-waveform decoded) |
| `count` | int \| null | native | Cap waveform elements returned |
| `as_numpy` | bool | `true` | Return arrays as numpy (JSON-serialized to list either way) |
| `use_monitor` | bool | `false` | Force fresh CA get. Set `true` to share a monitor with an existing subscription — note pyepics will install a permanent auto-monitor on first such call for a PV. |
| `timeout` | float | `5.0` | CA get timeout (seconds) |
| `connection_timeout` | float | `5.0` | CA connection timeout (seconds) |
| `ftype` | int \| null | native | Force non-native DBR type via `ca.get(ftype=…)` (power-user) |

**Response envelope** (tiled-style — applies to both `/api/v1/pv/{name}/value`
and `/api/v1/pvs/{name}/value`):

JSON mode (default, or `Accept: application/json`, or `?format=json`):
```json
{
  "pv_name": "IOC:image",
  "value": [[...], [...]],
  "timestamp": "2026-04-20T12:00:00",
  "shape": [1024, 1024],
  "dtype": "<u2",
  "ndim": 2,
  "nbytes": 2097152
}
```
For scalars: `shape=[]`, `dtype=null`, `ndim=0`, `nbytes=0`, `value` is a
native JSON number/bool/string. The monitored endpoint additionally
includes `connected`, `status`, `severity`, `units`, `precision`,
`enum_strs`, `lower_ctrl_limit`, `upper_ctrl_limit`, `lower_disp_limit`,
`upper_disp_limit`, `read_access`, `write_access`.

Binary mode (`Accept: application/octet-stream` or `?format=binary`):
- Body: raw bytes of a C-contiguous numpy array.
- Headers: `X-PV-Name`, `X-PV-Shape` (csv), `X-PV-Dtype` (numpy
  `dtype.str`, e.g. `<u2`), `X-PV-Ndim`, `X-PV-Nbytes`, `X-PV-Timestamp`.
- Binary mode only serves numeric dtypes (int/uint/float/bool/complex).
  Strings or enum-as-string return `406 Not Acceptable`.

**Response size cap.** Any value whose `nbytes` exceeds
`DIRECT_CONTROL_RESPONSE_BYTESIZE_LIMIT` (default 100 MB) returns
`400 Bad Request` with a "slice or raise the limit" message.

### PV Monitoring (subscription-backed)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/pvs/{pv_name}/value` | Current value from subscription cache (full metadata) |
| GET | `/api/v1/pvs/connected` | List currently connected PVs |

### Device Control (High Fidelity, coordination-checked)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/device/execute` | Execute Ophyd device method |
| POST | `/api/v1/device/{device_name}/stop` | Stop a device |

### Device Metadata (proxied from configuration_service)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/devices` | List devices (with class / protocol filters) |
| GET | `/api/v1/devices/{device_name}` | Get device metadata |
| GET | `/api/v1/devices/{device_name}/bundle` | Hierarchical component tree |

### Nested Device Access (ophyd-websocket compatible)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/device/{device_path}` | Access nested component (read/set) |
| GET | `/api/v1/device/{device_path}/value` | Get nested component value |

### WebSockets
| Method | Endpoint | Description |
|--------|----------|-------------|
| WS | `/ws/pv/monitor` | PV monitoring (legacy path) |
| WS | `/api/v1/pv-socket` | PV monitoring (ophyd-websocket compatible) |
| WS | `/api/v1/device-socket` | Device-level monitoring (ophyd-websocket compatible) |
| WS | `/api/v1/control-socket` | Combined PV + device control |

All write actions over WebSocket (`set`, `stop`) route through `DeviceControl`
and inherit the A4 coordination check.

## Example curl Commands

### Health Check
```bash
curl http://localhost:8003/health
```

### Set PV Value (Fire-and-Forget)
```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "IOC:motor1", "value": 10.0, "wait": false}'
```

### Set PV Value (Put-Completion, blocking)
```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "IOC:motor1", "value": 10.0, "wait": true, "timeout": 5.0}'
```

### Set PV Value (Put-with-Callback — no CA thread held)
```bash
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "IOC:motor1", "value": 10.0, "use_complete": true, "timeout": 30.0}'
```

### One-shot Get with Knobs
```bash
# Enum label instead of index, bounded connection timeout
curl "http://localhost:8003/api/v1/pv/IOC:valve1.VAL/value?as_string=true&connection_timeout=2.0"

# Waveform truncated to first 100 samples (default is a fresh CA get)
curl "http://localhost:8003/api/v1/pv/IOC:wf1/value?count=100"
```

### Binary Retrieval of a 2D Image
```bash
# Raw bytes via Accept header; shape/dtype in X-PV-* response headers.
curl -i -H "Accept: application/octet-stream" \
  "http://localhost:8003/api/v1/pvs/IOC:camera1:image/value" \
  -o image.bin

# Or force via query param (handy when clients can't easily set Accept).
curl "http://localhost:8003/api/v1/pv/IOC:camera1:image/value?format=binary" \
  -o image.bin

# Python reconstruction:
#   import numpy as np
#   shape = tuple(int(s) for s in resp.headers['X-PV-Shape'].split(','))
#   dtype = np.dtype(resp.headers['X-PV-Dtype'])
#   img = np.frombuffer(resp.content, dtype=dtype).reshape(shape)
```

### Get PV Value (subscription-backed, with metadata)
```bash
curl http://localhost:8003/api/v1/pvs/IOC:motor1/value
```

### List Connected PVs
```bash
curl http://localhost:8003/api/v1/pvs/connected
```

### Execute Device Method
```bash
curl -X POST http://localhost:8003/api/v1/device/execute \
  -H "Content-Type: application/json" \
  -d '{"device_name": "det", "method": "trigger", "args": [], "kwargs": {}}'
```

### Stop a Device
```bash
curl -X POST http://localhost:8003/api/v1/device/motor1/stop
```

### Access Nested Device Component (Read)
```bash
curl -X POST http://localhost:8003/api/v1/device/motor.user_readback \
  -H "Content-Type: application/json" \
  -d '{"method": "read"}'
```

### Access Nested Device Component (Set)
```bash
curl -X POST http://localhost:8003/api/v1/device/motor.user_setpoint \
  -H "Content-Type: application/json" \
  -d '{"method": "set", "value": 5.0}'
```

## WebSocket Protocols

### PV Monitoring (`/api/v1/pv-socket`, ophyd-websocket compatible)

**Client → Server:**
```json
{"action": "subscribe", "pv": "IOC:m1"}
{"action": "unsubscribe", "pv": "IOC:m1"}
{"action": "subscribeSafely", "pv": "IOC:m1"}
{"action": "subscribeReadOnly", "pv": "IOC:m1"}
{"action": "refresh", "pv": "IOC:m1"}
{"action": "set", "pv": "IOC:m1", "value": 10, "timeout": 5}
{"action": "stop", "device": "motor1"}
{"action": "ping"}
```

**Server → Client:**
```json
{"type": "subscribed", "pv_names": ["IOC:m1"], "timestamp": "..."}
{"type": "set_complete", "pv": "IOC:m1", "success": true, "value": 10}
{"type": "error", "message": "Device locked by plan count", "locked": true}
{"type": "heartbeat", "timestamp": "..."}
{"event_type": "pv_update", "pv_name": "IOC:m1", "value": 10.5, "connected": true, ...}
{"event_type": "pv_update", "pv_name": "IOC:m1", "value": null, "connected": false, ...}
```

**Connection lifecycle events**

- The server emits `{"type": "heartbeat"}` every `DIRECT_CONTROL_WS_HEARTBEAT_INTERVAL` seconds (default 30s). Primarily to keep NAT/proxy TCP connections warm and to surface dead peers early. No response required; clients can ignore it.
- When a PV's CA connection goes down or comes back, subscribed clients receive a synthetic `pv_update` with the new `connected` flag. Value-updates stop while the PV is disconnected; the client gets a fresh value-update when the IOC reconnects.
- When the last WS client unsubscribes from a PV, the service also disconnects the underlying pyepics PV object, freeing the IOC-side monitor. A subsequent subscribe re-pays the UDP search + TCP setup (~30ms).
- Per-client subscription cap: `DIRECT_CONTROL_MAX_SUBSCRIPTIONS_PER_CLIENT` (default 1000). Attempts to exceed it return a WS error; the subscribe is rejected atomically (no partial subscribes).

### Device-Level Monitoring (`/api/v1/device-socket`)

Subscribes to all PVs of a device from configuration_service. Emits
`device_update` events. See the device_monitoring docs in the original service
for the full shape.

### Combined Control (`/api/v1/control-socket`)

Same protocol as `pv-socket`. Use when the client wants one socket for both
monitoring and commanding; all writes are coordination-checked.

### Python Client Example (PV monitoring)

```python
import asyncio
import json
import websockets

async def monitor():
    uri = "ws://localhost:8003/api/v1/pv-socket"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"action": "subscribe", "pv": "IOC:motor1"}))
        async for message in ws:
            data = json.loads(message)
            if data.get("event_type") == "pv_update":
                print(f"{data['pv_name']}: {data['value']}")

asyncio.run(monitor())
```

## A4 Coordination

Every write operation flows through the same check:

```
Request → Registry validate → Coordination check → EPICS write
                                    ↓
                            Device locked?
                            Yes → 423 Locked
                            No  → Execute
```

If the coordination service is unreachable: `503 Service Unavailable`.
If the PV/device is not registered in configuration_service: `404 Not Found`.

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests live under `tests/`. The `test_ioc` session fixture (see
`tests/conftest.py`) spawns a caproto-backed soft-IOC in a subprocess on
port 5064 for the duration of the test session; if port 5064 is already
in use, the fixture assumes an IOC is already running and reuses it.
`tests/test_ioc.py` defines the PV set (`IOC:m1`, `IOC:counter`, `IOC:wf1`,
`IOC:shutter`). Coordination and registry validation are stubbed out in
`conftest.py` so tests don't require the real experiment_execution or
configuration services.

## Architecture

```
src/direct_control/
├── main.py                 # FastAPI app, all endpoints, lifespan
├── config.py               # Settings (DIRECT_CONTROL_ env prefix)
├── models.py               # Pydantic models (control + monitoring)
├── protocols.py            # CoordinationService, DeviceControl, PVMonitor
├── cli.py                  # bluesky-direct-control entry point
├── coordination_client.py  # A4 HTTP client
├── device_controller.py    # EPICS/ophyd command execution
├── registry_client.py      # Config-service validation with TTL cache
└── monitoring/             # Monitoring subpackage (lazy-imported)
    ├── pv_monitor.py              # ophyd EpicsSignal subscription manager
    ├── websocket_manager.py       # /api/v1/pv-socket + legacy + control-socket
    ├── device_websocket_manager.py # /api/v1/device-socket
    └── describers.py              # ophyd device describer plugins
```

Writes through WebSocket are never direct EPICS writes — they always go
through `DeviceControl.set_pv` / `.execute_device_method` / `.access_nested_device`,
which perform the coordination check.
