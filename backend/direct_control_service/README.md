# Direct Device Control + Monitoring Service (SVC-003)

Combined service: A4-coordinated device commanding **and** real-time EPICS PV
monitoring via WebSocket, running on a single port.

## Features

- **A4 Device Coordination**: Checks the device-lock state in `configuration_service` before any write; returns `423 Locked` if a plan holds the device.
- **PV Control**: Low-fidelity channel for EPICS PV set/get (fire-and-forget or put-completion).
- **Device Method Execution**: High-fidelity channel — instantiates the registry's device class as a live ophyd object and runs the verb with Status completion awaited. Supports **both classic ophyd and ophyd-async** device classes, dispatched per device by framework detection.
- **Nested Device Access**: Navigate live device component hierarchies (ophyd-websocket compatible).
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
# Basic startup (port 8003) — DIRECT_CONTROL_CONFIGURATION_SERVICE_URL is required.
export DIRECT_CONTROL_CONFIGURATION_SERVICE_URL=http://localhost:8004
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
  -e DIRECT_CONTROL_CONFIGURATION_SERVICE_URL=http://host.docker.internal:8004 \
  bluesky-direct-control
```

## Service Dependencies

direct_control talks to **only** `configuration_service`. It never reaches
EE or queueserver directly — coordination is mediated through device-lock
state held in configuration_service's registry. EE/queueserver writes
locks via `POST /api/v1/devices/lock`; direct_control reads them via
`GET /api/v1/devices/{name}/status`.

| Dependency | Interface | Purpose |
|------------|-----------|---------|
| `configuration_service` | `/api/v1/devices/{name}`, `/api/v1/pvs` | Device + PV registry |
| `configuration_service` | `/api/v1/devices/{name}/status` | A4 device lock status (read-only) |
| `configuration_service` | `/api/v1/devices/{name}/instantiation` | Instantiation spec (class path + ctor args) for device-level control |

## Configuration

All settings use the `DIRECT_CONTROL_` environment variable prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `DIRECT_CONTROL_HOST` | `0.0.0.0` | Bind address |
| `DIRECT_CONTROL_PORT` | `8003` | HTTP port |
| `DIRECT_CONTROL_LOG_LEVEL` | `info` | Log level |
| `DIRECT_CONTROL_CONFIGURATION_SERVICE_URL` | **required** | Configuration Service URL (registry + lock state) |
| `DIRECT_CONTROL_COORDINATION_CHECK_ENABLED` | `true` | Enable A4 coordination checks |
| `DIRECT_CONTROL_COORDINATION_TIMEOUT` | `5.0` | Coordination check timeout (s) |
| `DIRECT_CONTROL_COMMAND_TIMEOUT` | `30.0` | Command execution timeout (s) |
| `DIRECT_CONTROL_DEVICE_CONNECT_TIMEOUT` | `10.0` | Connect timeout (s) when instantiating a live device for device-level control |
| `DIRECT_CONTROL_GLOBAL_READ_ONLY` | `true` | Monitor-only by default: all control/write operations return 403 until set to `false` |
| `DIRECT_CONTROL_REGISTRY_BACKEND` | `http` | `http` (configuration_service) \| `file` (standalone, local registry file) \| `auto` |
| `DIRECT_CONTROL_REGISTRY_FILE_PATH` | — | Registry file path; required for `file`, optional fallback for `auto` |
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
| POST | `/api/v1/device/execute` | Execute a device method on a live ophyd / ophyd-async object |
| POST | `/api/v1/device/{device_name}/stop` | Stop a device (`stop()` with completion) |

The service instantiates the device from its registry **instantiation spec**
(class path + constructor args), connects it (cached after first use), and
runs the method via a framework-matched driver — classic ophyd (pyepics,
`Status.wait` on a worker thread) or ophyd-async (aioca/p4p, awaited
`AsyncStatus`) — detected per device from the imported class.

Allowed methods: `set`, `put`, `get`, `read`, `describe`,
`read_configuration`, `describe_configuration`, `trigger`, `stop`.
`use_put=true` returns right after initiating a Status-returning method
instead of awaiting completion.

Status codes: `400` method outside the allowlist or unsupported by the
device class · `404` device not in registry / unknown nested component ·
`409` disabled · `422` device has no instantiation spec (device-level
control unavailable; PV-level operations still work) · `423` locked by a
plan · `503` registry unreachable.

In standalone (`file`) registry mode, a device entry opts into device-level
control by carrying class info:

```json
{
  "devices": [
    {
      "name": "m1",
      "pvs": ["BL01:M1.RBV"],
      "device_class": "ophyd.EpicsMotor",
      "args": ["BL01:M1"],
      "kwargs": {},
      "framework": "ophyd-sync"
    }
  ]
}
```

`framework` (`ophyd-sync` | `ophyd-async`) is optional and advisory — the
imported class is classified authoritatively, and a mismatching tag is a
hard error. Entries without `device_class` stay PV-gateway-only.

### Device metadata
Device metadata (the device list, per-device records, component trees) is **not**
served here — read it directly from `configuration_service`, the registry of
record. direct_control consumes the registry internally (PV/device validation,
lock state) but does not re-expose it as HTTP endpoints.

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
| WS | `/api/v1/camera-socket` | AreaDetector image streaming (finch `ophydSocketCameraPath`) |
| WS | `/api/v1/tiff-socket` | TIFF-detector image streaming (finch `ophydSocketTIFFPath`) |

All write actions over WebSocket (`set`, `stop`) route through `DeviceControl`
and inherit the A4 coordination check. The image sockets are read-only EPICS
monitors (no writes, no coordination check).

### Image streaming (`camera-socket` / `tiff-socket`)

Both stream binary **JPEG** frames plus JSON metadata, matching finch's
`useCameraCanvas` / `useTIFFCanvas` hooks:

- **Client → server (subscribe):**
  - camera: `{"imageArray_PV": "...", "startX": "...", "sizeX": "...", "colorMode": "...", "dataType": "...", ...}` — any omitted setting PV is inferred from the `imageArray_PV` prefix (`<prefix>:cam1:<suffix>`); an omitted `imageArray_PV` falls back to `DIRECT_CONTROL_CAMERA_DEFAULT_IMAGE_ARRAY_PV`.
  - tiff: `{"prefix": "13PIL1"}` — expands to `<prefix>:image1:ArrayData` + `<prefix>:cam1:*` (tiff is camera-with-prefix-inference).
- **Client → server (optional):** `{"toggleLogNormalization": true|false}`.
- **Server → client:** binary JPEG frames; JSON `{"x":int,"y":int,...}` on dimension change; JSON `{"logNormalization":bool}` on toggle. No heartbeat is sent on these sockets (finch interprets any non-`logNormalization` JSON text frame as a dimension message).

The **image array PV** is validated against the configuration_service registry before connecting — the same gate as `pv-socket`/`device-socket`. An unregistered array PV (or an unreachable config-service) refuses the connection with an `error` envelope. The `cam1:*` setting PVs are *not* registry-validated: AreaDetector devices register the image-data PV but not each scalar setting as a standalone registry entry, and the settings ride on the same validated detector prefix.

The wire encoding is pluggable via `DIRECT_CONTROL_IMAGE_ENCODING` (`jpeg`|`png`|`webp`,
see `monitoring/image_encoders.py`), but stays `jpeg` by default because finch
decodes frames as `image/jpeg`. TIFF is intentionally **not** an option — browsers
cannot decode TIFF; "TIFF" names the detector class, not the wire format.

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
`conftest.py` so tests don't require a real `configuration_service` instance
(coordination state lives in `configuration_service`; direct_control reads
device-lock status from there).

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

## Error Handling & Recovery

### HTTP Status Codes

| Status | Meaning | Common Causes |
|--------|---------|---------------|
| 200 OK | Operation succeeded | N/A |
| 400 Bad Request | Invalid request format or value | Malformed JSON, invalid device/PV name |
| 404 Not Found | Device or PV not registered | Typo in device/PV name, device not in registry |
| 406 Not Acceptable | Unsupported accept header | Client requested unsupported media type |
| 423 Locked | Device is locked by coordination | Experiment running, plan holds the device |
| 500 Internal Server Error | Service error | EPICS unavailable, configuration error |
| 503 Service Unavailable | Service unhealthy or coordination check failed | Configuration service down, service initializing or shutting down |

### Timeout Behavior

- **Command timeout** (30s default): Long-running ophyd methods or EPICS operations timeout after 30s
  - Set via `DIRECT_CONTROL_COMMAND_TIMEOUT`
- **EPICS CA timeout** (5s typical): Individual channel access operations timeout; not user-configurable
- **Coordination check timeout** (5s default): timeout for the HTTP read of device-lock state from `configuration_service`; configurable via `DIRECT_CONTROL_COORDINATION_TIMEOUT`

If a timeout occurs, the PV/device may be in an indeterminate state. Query the device state endpoint to verify.

### Recovery Procedures

**Service won't start:**
- Check logs: `docker compose logs direct_control_service`
- Verify `DIRECT_CONTROL_CONFIGURATION_SERVICE_URL` points to a running configuration_service
- Verify EPICS network connectivity: `caget` from host should work

**Health check failing:**
- Check `/health` endpoint: `curl http://localhost:8003/health`
- If coordination_service_available is false, check coordination service status
- Service reports `status="unhealthy"` (HTTP 503) when configuration_service is unreachable; reads against already-subscribed PVs may still work, but coordination-checked operations will fail until it recovers

**Devices are locked:**
- Inspect a specific device's lock state: `curl http://localhost:8004/api/v1/devices/{name}/status`
- Wait for the holding plan to complete (the lock-holding service — typically queueserver — releases it on env-close)
- Return 423 is expected during active experiments
