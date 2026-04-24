# How to Deploy the Configuration Service as a systemd Service

This guide walks through deploying the Bluesky Configuration Service on a
RHEL 8+ machine as a systemd-managed service. The steps were validated on
`xf31id1-tst-qs1.nsls2.bnl.gov` (RHEL 8.10).

## Prerequisites

- Root SSH access to the target machine
- The target machine must have network access to download packages
- A beamline service user (e.g. `xf31id`) that the service will run as

## 1. Install uv

[uv](https://docs.astral.sh/uv/) manages Python versions and dependencies.
It downloads its own Python, so the system Python version doesn't matter.

```bash
ssh root@<target-host>
curl -LsSf https://astral.sh/uv/install.sh | sh
```

This installs `uv` to `/root/.local/bin/uv`. Verify:

```bash
/root/.local/bin/uv --version
```

## 2. Create the installation directory

```bash
mkdir -p /opt/bs_config_svc
```

## 3. Copy the source code

From the machine where you have the repository cloned:

```bash
rsync -av \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    /path/to/configuration_service/ \
    root@<target-host>:/opt/bs_config_svc/
```

## 4. Install Python and dependencies

On the target machine, install Python and project dependencies using `uv`.
The key is to set `UV_PYTHON_INSTALL_DIR` so Python is installed under
`/opt/bs_config_svc/` rather than under `/root/` (which wouldn't be
accessible to the service user):

```bash
cd /opt/bs_config_svc

# Install Python to a shared location
UV_PYTHON_INSTALL_DIR=/opt/bs_config_svc/.python \
    /root/.local/bin/uv python install 3.14

# Create virtualenv and install all dependencies
UV_PYTHON_INSTALL_DIR=/opt/bs_config_svc/.python \
    /root/.local/bin/uv sync
```

Verify the CLI works:

```bash
/opt/bs_config_svc/.venv/bin/bluesky-configuration-service --help
```

## 5. Create the data directory

The service uses SQLite for persistence. Create a directory for the database:

```bash
mkdir -p /opt/bs_config_svc/data
```

## 6. Create the environment file

Create `/opt/bs_config_svc/.env` with the configuration for your beamline:

```bash
cat > /opt/bs_config_svc/.env << 'EOF'
# Bluesky Configuration Service environment
CONFIG_HOST=0.0.0.0
CONFIG_PORT=8004
CONFIG_LOG_LEVEL=info

# Profile collection path
CONFIG_PROFILE_PATH=/opt/bluesky/profile_collection

# Load strategy:
#   empty           - Start with zero devices (populated via CRUD by the EE service)
#   mock            - Built-in test data (good for initial deployment verification)
#   happi           - Parse happi_db.json from profile_path
#   bits            - Parse devices.yml + iconfig.yml from profile_path
#   auto            - Auto-detect based on files present in profile_path
CONFIG_LOAD_STRATEGY=empty

# SQLite database location
CONFIG_DB_PATH=/opt/bs_config_svc/data/config_service.db

# Enable device change history (CRUD operations and audit log)
CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=true

# Prometheus metrics
CONFIG_METRICS_ENABLED=true
CONFIG_METRICS_PORT=9004

# CORS - allow all origins during testing
CONFIG_CORS_ORIGINS=["*"]
EOF
```

### Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_HOST` | `0.0.0.0` | Bind address |
| `CONFIG_PORT` | `8004` | HTTP API port |
| `CONFIG_LOG_LEVEL` | `info` | Log level (`critical`, `error`, `warning`, `info`, `debug`, `trace`) |
| `CONFIG_PROFILE_PATH` | — | Path to beamline profile collection |
| `CONFIG_LOAD_STRATEGY` | `auto` | How to discover devices (see above) |
| `CONFIG_DB_PATH` | `/var/lib/bluesky/config_service.db` | SQLite database path |
| `CONFIG_DEVICE_CHANGE_HISTORY_ENABLED` | `true` | Enable persistent storage and CRUD |
| `CONFIG_METRICS_ENABLED` | `true` | Enable Prometheus metrics endpoint |
| `CONFIG_METRICS_PORT` | `9004` | Prometheus metrics port |
| `CONFIG_CORS_ORIGINS` | `["*"]` | Allowed CORS origins (JSON array) |

## 7. Set ownership

The service runs as the beamline user. Set ownership of the entire directory:

```bash
chown -R xf31id:xf31id /opt/bs_config_svc
```

Replace `xf31id:xf31id` with your beamline's service user and group.

## 8. Create the systemd unit file

Create `/usr/lib/systemd/system/bluesky-configuration-service.service`:

```ini
[Unit]
Description=Bluesky Configuration Service (SVC-004)
Requires=network.target
After=network.target

[Service]
Type=simple
User=xf31id
Group=xf31id
WorkingDirectory=/opt/bs_config_svc
EnvironmentFile=/opt/bs_config_svc/.env
ExecStart=/opt/bs_config_svc/.venv/bin/bluesky-configuration-service \
    --host ${CONFIG_HOST} \
    --port ${CONFIG_PORT} \
    --log-level ${CONFIG_LOG_LEVEL} \
    --load-strategy ${CONFIG_LOAD_STRATEGY} \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Replace `User=` and `Group=` with your beamline's service user if different.

## 9. Enable and start the service

```bash
systemctl daemon-reload
systemctl enable bluesky-configuration-service.service
systemctl start bluesky-configuration-service.service
```

## 10. Verify

Check the service status:

```bash
systemctl status bluesky-configuration-service
```

Test the API:

```bash
# Health check
curl -s http://localhost:8004/health

# Readiness check
curl -s http://localhost:8004/ready

# List devices
curl -s http://localhost:8004/api/v1/devices

# Swagger UI (open in browser)
# http://<target-host>:8004/docs
```

View logs:

```bash
journalctl -u bluesky-configuration-service -f
```

## Ports used

| Port | Purpose |
|------|---------|
| **8004** | Main HTTP API |
| **9004** | Prometheus metrics (if enabled) |

Ensure these ports are not used by other services. Check with:

```bash
ss -tlnp | grep -E '(8004|9004)'
```

## Updating the service

To deploy a new version:

```bash
# Stop the service
systemctl stop bluesky-configuration-service

# Sync updated source code
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='data' \
    /path/to/configuration_service/ \
    root@<target-host>:/opt/bs_config_svc/

# Re-install dependencies (on target)
cd /opt/bs_config_svc
UV_PYTHON_INSTALL_DIR=/opt/bs_config_svc/.python /root/.local/bin/uv sync

# Fix ownership if needed
chown -R xf31id:xf31id /opt/bs_config_svc

# Restart
systemctl start bluesky-configuration-service
```

Note: the `--exclude='data'` in rsync preserves the existing SQLite database.

## Troubleshooting

### Service fails with exit code 203/EXEC

The Python interpreter is not accessible to the service user. Ensure Python was
installed with `UV_PYTHON_INSTALL_DIR=/opt/bs_config_svc/.python` and that the
directory is owned by the service user.

### Service fails with exit code 2

An invalid CLI argument was passed. Check `journalctl -u bluesky-configuration-service`
for the specific error. Common issue: `CONFIG_LOG_LEVEL` must be lowercase
(`info`, not `INFO`).

### Port already in use

Check what is using the port: `ss -tlnp | grep 8004`. Change `CONFIG_PORT`
in `.env` if needed.

---

## Deploying with the Experiment Execution Service (SVC-001)

For profiles that use startup scripts (IPython-style), the Configuration
Service starts empty and is seeded by the Experiment Execution Service
(SVC-001) at runtime. The EE service executes the startup scripts, discovers
live device objects, and pushes them to the config service via the CRUD API.

### 1. Copy the EE service source

```bash
mkdir -p /opt/bs_ee_svc
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    /path/to/experiment_execution/ \
    root@<target-host>:/opt/bs_ee_svc/
```

### 2. Create a venv from the pixi environment

The EE service needs the full beamline stack (ophyd, bluesky, pyepics, etc.).
The pixi `qs` environment from the profile collection already has these.
Create a venv that inherits them via `--system-site-packages`:

```bash
/opt/bluesky/profile_collection/.pixi/envs/qs/bin/python \
    -m venv --system-site-packages /opt/bs_ee_svc/.venv
```

Then install the EE service (pulls in only its web framework deps):

```bash
/opt/bs_ee_svc/.venv/bin/pip install -e /opt/bs_ee_svc/
```

If `structlog` is missing (not in pixi env), install it into the venv:

```bash
/opt/bs_ee_svc/.venv/bin/pip install \
    --force-reinstall --no-deps structlog \
    -t /opt/bs_ee_svc/.venv/lib/python3.12/site-packages/
```

### 3. Create the environment file

```bash
cat > /opt/bs_ee_svc/.env << 'EOF'
# Bluesky Experiment Execution Service environment
EXEC_HOST=0.0.0.0
EXEC_PORT=8001
EXEC_LOG_LEVEL=info

# Profile collection — load devices from startup scripts
EXEC_PROFILE_COLLECTION_PATH=/opt/bluesky/profile_collection
EXEC_LOAD_STRATEGY=profile_collection

# Sync discovered devices to Configuration Service
EXEC_SYNC_TO_CONFIG_SERVICE=true
EXEC_CONFIG_SERVICE_URL=http://localhost:8004

# Queue storage — SQLite for persistence
EXEC_QUEUE_STORAGE=sqlite
EXEC_SQLITE_PATH=/opt/bs_ee_svc/data/queue.db

# Worker settings
EXEC_ENABLE_WATCHDOG=true
EXEC_WORKER_STARTUP_TIMEOUT=60.0

# Metrics
EXEC_METRICS_ENABLED=true
EXEC_METRICS_PORT=9001

# Disable optional services we do not have yet
EXEC_TILED_ENABLED=false
EXEC_GRAYLOG_ENABLED=false
EXEC_TRACING_ENABLED=false
EOF
```

### 4. Create data directory and set ownership

```bash
mkdir -p /opt/bs_ee_svc/data
chown -R xf31id:xf31id /opt/bs_ee_svc
```

### 5. Create the systemd unit file

Create `/usr/lib/systemd/system/bluesky-experiment-execution.service`:

```ini
[Unit]
Description=Bluesky Experiment Execution Service (SVC-001)
Requires=network.target
After=network.target bluesky-configuration-service.service

[Service]
Type=simple
User=xf31id
Group=xf31id
WorkingDirectory=/opt/bs_ee_svc
EnvironmentFile=/opt/bs_ee_svc/.env
ExecStart=/opt/bs_ee_svc/.venv/bin/bluesky-experiment-execution \
    --host ${EXEC_HOST} \
    --port ${EXEC_PORT} \
    --log-level ${EXEC_LOG_LEVEL} \
    --load-strategy ${EXEC_LOAD_STRATEGY} \
    --profile-path ${EXEC_PROFILE_COLLECTION_PATH} \
    --config-service-url ${EXEC_CONFIG_SERVICE_URL}
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 6. Enable and start

```bash
systemctl daemon-reload
systemctl enable bluesky-experiment-execution.service
systemctl start bluesky-experiment-execution.service
```

### 7. Verify the device sync

After the EE service finishes loading, check that devices were pushed:

```bash
# EE service health
curl -s http://localhost:8001/health

# Config service should now have devices
curl -s http://localhost:8004/api/v1/devices

# Detailed device info
curl -s http://localhost:8004/api/v1/devices-info | python3 -m json.tool
```

### EE service ports

| Port | Purpose |
|------|---------|
| **8001** | Main HTTP API |
| **9001** | Prometheus metrics (if enabled) |

---

## Deploying the Direct Control Service (SVC-003)

The Direct Control Service provides low-fidelity (caget/caput) and
high-fidelity (ophyd) channels for direct EPICS device control.

### 1. Copy the source

```bash
mkdir -p /opt/bs_dc_svc
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    /path/to/bluesky-direct-control-service/ \
    root@<target-host>:/opt/bs_dc_svc/
```

### 2. Create venv and install

Same pixi `--system-site-packages` approach as the EE service:

```bash
/opt/bluesky/profile_collection/.pixi/envs/qs/bin/python \
    -m venv --system-site-packages /opt/bs_dc_svc/.venv
/opt/bs_dc_svc/.venv/bin/pip install -e /opt/bs_dc_svc/
```

Install structlog if missing:

```bash
/opt/bs_dc_svc/.venv/bin/pip install \
    --force-reinstall --no-deps structlog \
    -t /opt/bs_dc_svc/.venv/lib/python3.12/site-packages/
```

### 3. Create environment file

```bash
cat > /opt/bs_dc_svc/.env << 'EOF'
DIRECT_CONTROL_HOST=0.0.0.0
DIRECT_CONTROL_PORT=8003
DIRECT_CONTROL_LOG_LEVEL=info
DIRECT_CONTROL_CONFIGURATION_SERVICE_URL=http://localhost:8004
DIRECT_CONTROL_EXPERIMENT_EXECUTION_URL=http://localhost:8001
DIRECT_CONTROL_REQUIRE_AUTH=false
DIRECT_CONTROL_COORDINATION_CHECK_ENABLED=true
DIRECT_CONTROL_EPICS_CA_AUTO_ADDR_LIST=true
DIRECT_CONTROL_COMMAND_TIMEOUT=30.0
DIRECT_CONTROL_ENABLE_METRICS=true
DIRECT_CONTROL_METRICS_PORT=9003
EOF
```

### 4. Create systemd unit

```ini
[Unit]
Description=Bluesky Direct Device Control Service (SVC-003)
Requires=network.target
After=network.target bluesky-configuration-service.service

[Service]
Type=simple
User=xf31id
Group=xf31id
WorkingDirectory=/opt/bs_dc_svc
EnvironmentFile=/opt/bs_dc_svc/.env
ExecStart=/opt/bs_dc_svc/.venv/bin/bluesky-direct-control \
    --host ${DIRECT_CONTROL_HOST} \
    --port ${DIRECT_CONTROL_PORT} \
    --log-level ${DIRECT_CONTROL_LOG_LEVEL} \
    --proxy-headers \
    --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5. Enable and start

```bash
chown -R xf31id:xf31id /opt/bs_dc_svc
systemctl daemon-reload
systemctl enable bluesky-direct-control.service
systemctl start bluesky-direct-control.service
```

### 6. Test with real EPICS PVs

Register PVs in the config service, then read/write via direct control:

```bash
# Register a PV
curl -X POST http://localhost:8004/api/v1/pvs \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "XF:31ID1-ES{SIM-Cam:2}cam1:GainX", "labels": ["tst"]}'

# Read via direct control
curl http://localhost:8003/api/v1/pv/XF:31ID1-ES%7BSIM-Cam:2%7Dcam1:GainX/value

# Write via direct control (checks A4 locks first)
curl -X POST http://localhost:8003/api/v1/pv/set \
  -H "Content-Type: application/json" \
  -d '{"pv_name": "XF:31ID1-ES{SIM-Cam:2}cam1:GainX", "value": 2.5}'
```

### DC service ports

| Port | Purpose |
|------|---------|
| **8003** | Main HTTP API |
| **9003** | Prometheus metrics (if enabled) |

---

## Tiled SimpleTiledServer Fix

The profile collection's `00-startup.py` creates a `SimpleTiledServer`.
In tiled <= 0.2.9, there is a race condition where `from_uri()` is called
before the server's FastAPI lifespan completes, causing 500 errors and
retries that appear as a hang.

### Fix

Patch `tiled/server/simple.py` to poll the server's metadata endpoint
until it returns 200 before the `SimpleTiledServer` constructor returns.
A patched copy is saved at `/opt/bs_ee_svc/tiled_simple_fix.py`.

To apply:

```bash
TILED_SIMPLE=$(python -c "import tiled.server.simple; print(tiled.server.simple.__file__)")
cp /opt/bs_ee_svc/tiled_simple_fix.py "$TILED_SIMPLE"
```

Key changes:
- Add trailing slash to health check URL (`/api/v1/` not `/api/v1`) to
  avoid 307 redirect loop
- Add `follow_redirects=True` as safety net
- Poll until 200 OK before returning from `__init__`

### Upgrading pixi packages

To get the latest tiled/bluesky/ophyd-async, copy the pixi.toml and
loosen version pins:

```bash
cp /opt/bluesky/profile_collection/pixi.toml /opt/bs_ee_svc/pixi.toml
# Edit pixi.toml: change pinned versions (==X.Y.Z) to wildcards (*)
pixi install --environment qs
```

---

## Testing blop Bayesian Optimization over HTTP

blop is a client library (not a service) that uses Ax/BoTorch for
Bayesian optimization. It can talk to the EE service over HTTP.

### 1. Install blop

```bash
mkdir -p /opt/blop
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    /path/to/blop/ root@<target-host>:/opt/blop/

# Create venv with uv-managed Python (avoids pixi library conflicts)
UV_PYTHON_INSTALL_DIR=/opt/blop/.python /root/.local/bin/uv venv \
    --python 3.12 /opt/blop/.venv

# Install CPU-only PyTorch + blop
export SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0
UV_PYTHON_INSTALL_DIR=/opt/blop/.python /root/.local/bin/uv pip install \
    --python /opt/blop/.venv/bin/python \
    --index-url https://download.pytorch.org/whl/cpu torch
UV_PYTHON_INSTALL_DIR=/opt/blop/.python /root/.local/bin/uv pip install \
    --python /opt/blop/.venv/bin/python -e "/opt/blop[qs]" httpx
```

### 2. Run optimization

```python
from blop.queueserver import create_http_client
client = create_http_client("http://localhost:8001", use_ee_service=True)
client.check_environment()
```

Or use `HTTPManagerAPI` directly with `AxOptimizer` for a full
Bayesian optimization loop (see test script at `/tmp/test_blop_optimize.py`
on the target).

---

## Service Summary

| Service | Port | Metrics | Directory | systemd unit |
|---------|------|---------|-----------|-------------|
| Configuration (SVC-004) | 8004 | 9004 | `/opt/bs_config_svc/` | `bluesky-configuration-service` |
| Experiment Execution (SVC-001) | 8001 | 9001 | `/opt/bs_ee_svc/` | `bluesky-experiment-execution` |
| Direct Control (SVC-003) | 8003 | 9003 | `/opt/bs_dc_svc/` | `bluesky-direct-control` |
| blop (client library) | — | — | `/opt/blop/` | — |
