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

## Pointing at PostgreSQL

The service persists to PostgreSQL. Provide a connection string (required unless
persistence is disabled with `CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=false`):

```bash
CONFIG_DATABASE_URL=postgresql+psycopg://bluesky:bluesky@localhost:5432/config_service \
    CONFIG_LOAD_STRATEGY=mock bluesky-configuration-service
```

The fastest way to get a local PostgreSQL:

```bash
docker run --rm -d -p 5432:5432 \
    -e POSTGRES_USER=bluesky -e POSTGRES_PASSWORD=bluesky -e POSTGRES_DB=config_service \
    postgres:16
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
CONFIG_DATABASE_URL=postgresql+psycopg://bluesky:bluesky@localhost:5432/config_service
CONFIG_LOG_LEVEL=DEBUG
```

Then start without any environment variables:

```bash
bluesky-configuration-service
```

The service reads `.env` automatically via pydantic-settings.
