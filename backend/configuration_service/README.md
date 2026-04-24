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
| [Manage Metadata](docs/how-to/manage-metadata.md) | Store and retrieve arbitrary JSON metadata for sharing between services |
| [Load Profiles](docs/how-to/load-profiles.md) | Load from happi or BITS profiles, or start empty for CRUD-based registration |
| [API Reference](docs/reference/api.md) | Complete endpoint listing with methods, paths, and descriptions |
| [Configuration Reference](docs/reference/configuration.md) | All `CONFIG_` environment variables |
| [Data Models](docs/reference/models.md) | DeviceMetadata, DeviceInstantiationSpec, PVMetadata, and related types |
| [Architecture](docs/explanation/architecture.md) | Startup flow, DB-as-source-of-truth, loader design, dependency injection |
| [Device Locking](docs/explanation/device-locking.md) | Why locking exists and how A4 coordination works |
