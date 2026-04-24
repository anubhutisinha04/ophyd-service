# Run the Service

## With mock data

No profile collection needed. Loads three built-in devices (`sample_x`, `det1`, `cam1`).

```bash
bluesky-configuration-service --use-mock-data
```

Or via environment variable:

```bash
CONFIG_LOAD_STRATEGY=mock bluesky-configuration-service
```

## With a profile collection

Point to a profile directory. The service auto-detects the format (happi or BITS).

```bash
CONFIG_PROFILE_PATH=/path/to/profile bluesky-configuration-service
```

To force a specific format:

```bash
CONFIG_PROFILE_PATH=/path/to/profile CONFIG_LOAD_STRATEGY=happi bluesky-configuration-service
```

## With a custom database path

The default SQLite path is `/var/lib/bluesky/config_service.db`. Override it for development:

```bash
CONFIG_DB_PATH=/tmp/config.db CONFIG_LOAD_STRATEGY=mock bluesky-configuration-service
```

## Development mode

Auto-reload on code changes:

```bash
bluesky-configuration-service --use-mock-data --reload --log-level debug
```

## Custom host and port

```bash
bluesky-configuration-service --host 127.0.0.1 --port 9000 --use-mock-data
```

## With SSL

```bash
bluesky-configuration-service --ssl-keyfile key.pem --ssl-certfile cert.pem --use-mock-data
```

## Behind a reverse proxy

Enable proxy header forwarding:

```bash
bluesky-configuration-service --proxy-headers --forwarded-allow-ips="10.0.0.0/8" --use-mock-data
```

## Using a `.env` file

Create a `.env` file in the working directory:

```
CONFIG_LOAD_STRATEGY=mock
CONFIG_DB_PATH=/tmp/config.db
CONFIG_LOG_LEVEL=DEBUG
```

Then start without any environment variables:

```bash
bluesky-configuration-service
```

The service reads `.env` automatically via pydantic-settings.
