# Configuration Service

Centralized device and PV registry for Bluesky beamline control systems. Loads device definitions from beamline profile collections and serves them over a REST API.

Other services query this registry to discover what devices exist, how to instantiate them, which PVs they own, and whether they are locked by a running experiment.

## Install

```bash
uv sync
```

## Quick start

```bash
# Run with built-in mock data (no profile collection needed)
uv run bluesky-configuration-service --use-mock-data

# Open http://localhost:8004/docs for the Swagger UI
```

## Run tests

```bash
uv run pytest tests/
```

## Documentation

| Section | Description |
|---------|-------------|
| [Getting Started](docs/tutorials/getting-started.md) | Hands-on walkthrough: start the service, query devices, add a device |
| [Run the Service](docs/how-to/run-the-service.md) | Start with mock data, a profile collection, or custom settings |
| [Manage Devices](docs/how-to/manage-devices.md) | Create, update, delete, enable/disable devices at runtime |
| [Manage PVs](docs/how-to/manage-pvs.md) | Register standalone PVs not tied to ophyd devices |
| [Load Profiles](docs/how-to/load-profiles.md) | Load from happi or BITS profiles, or start empty for CRUD-based registration |
| [API Reference](docs/reference/api.md) | Complete endpoint listing with methods, paths, and descriptions |
| [Configuration Reference](docs/reference/configuration.md) | All `CONFIG_` environment variables |
| [Data Models](docs/reference/models.md) | DeviceMetadata, DeviceInstantiationSpec, PVMetadata, and related types |
| [Architecture](docs/explanation/architecture.md) | Startup flow, DB-as-source-of-truth, loader design, dependency injection |
| [Device Locking](docs/explanation/device-locking.md) | Why locking exists and how A4 coordination works |

## Error Handling & Recovery

### HTTP Status Codes

| Status | Meaning | Common Causes |
|--------|---------|---------------|
| 200 OK | Operation succeeded | N/A |
| 201 Created | Resource created | Device/PV successfully added |
| 400 Bad Request | Invalid request format | Malformed JSON, missing required fields |
| 404 Not Found | Resource not found | Device/PV does not exist in registry |
| 409 Conflict | Device/PV already exists | Cannot create duplicate entries |
| 422 Unprocessable Entity | Validation failed | Device instantiation would fail, invalid configuration |
| 500 Internal Server Error | Service error | Database error, corrupted state |
| 503 Service Unavailable | Service not ready | Initializing profile loader or database |

### Startup Behavior

- **Cold start** (15s typical): Loading profile collection or initializing database
- **Happi loader** (5-10s): Parsing happi_db.json and instantiating device objects
- **CRUD mode** (1s): Starting empty with API-driven registration

Check `/health` endpoint during startup; returns 503 until ready.

### Recovery Procedures

**Service won't start:**
- Check logs: `docker compose logs configuration_service`
- Verify `CONFIG_PROFILE_PATH` points to valid directory (if using happi/BITS)
- Verify `CONFIG_DB_PATH` directory is writable
- Try `--use-mock-data` to bypass profile loading issues

**Health check failing:**
- Check `/health` endpoint: `curl http://localhost:8004/health`
- If response is 503, service is still initializing; wait a few seconds
- If response is 500, check database and profile paths

**Device instantiation fails:**
- Check device definition in happi_db.json or API POST body
- Verify all required ophyd imports are available
- Try querying the device via `/api/v1/devices/{name}` for detailed error
- Enable debug logging: `CONFIG_LOG_LEVEL=debug`
