# Configuration Reference

All settings are read from environment variables with the `CONFIG_` prefix. A `.env` file in the working directory is also loaded automatically.

## Server

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CONFIG_HOST` | str | `0.0.0.0` | Bind address |
| `CONFIG_PORT` | int | `8004` | HTTP port |
| `CONFIG_LOG_LEVEL` | str | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `CONFIG_CORS_ORIGINS` | list | `["*"]` | Allowed CORS origins (JSON array) |

## Profile Loading

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CONFIG_PROFILE_PATH` | path | — | Path to a beamline profile collection directory |
| `CONFIG_LOAD_STRATEGY` | str | `auto` | `auto`, `empty`, `happi`, `bits`, or `mock` |

### Load strategies

| Value | Description |
|-------|-------------|
| `auto` | Detect format from files in `CONFIG_PROFILE_PATH` (happi → bits) |
| `empty` | Start with zero devices. Devices are added via the CRUD API, typically by the Experiment Execution Service (SVC-001) |
| `happi` | Parse `happi_db.json` |
| `bits` | Parse `configs/devices.yml` + `configs/iconfig.yml` |
| `mock` | Built-in test data: `sample_x` (motor), `det1` (scaler), `cam1` (area detector) |

## Persistence

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CONFIG_DB_PATH` | path | `/var/lib/bluesky/config_service.db` | SQLite database for device registry and standalone PVs |
| `CONFIG_DEVICE_CHANGE_HISTORY_ENABLED` | bool | `true` | Enable DB persistence and CRUD endpoints. When `false`, devices load from profile on every startup with no persistence |

## Metrics

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `CONFIG_METRICS_ENABLED` | bool | `true` | Enable Prometheus metrics |
| `CONFIG_METRICS_PORT` | int | `9004` | Metrics endpoint port |

## CLI Arguments

The `bluesky-configuration-service` CLI accepts arguments that map to the same settings:

```
--host HOST              CONFIG_HOST
--port PORT              CONFIG_PORT
--log-level LEVEL        CONFIG_LOG_LEVEL
--profile-path PATH      CONFIG_PROFILE_PATH
--load-strategy STRAT    CONFIG_LOAD_STRATEGY
--use-mock-data          CONFIG_LOAD_STRATEGY=mock
--reload                 Enable uvicorn auto-reload
--workers N              Number of uvicorn workers
--ssl-keyfile PATH       SSL private key
--ssl-certfile PATH      SSL certificate
--proxy-headers          Enable X-Forwarded-For/Proto
--forwarded-allow-ips    Trusted proxy IPs
```

CLI arguments take precedence over environment variables.
