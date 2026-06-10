# ophyd-service

Monorepo for the Bluesky ophyd-service. Three backend services + a caproto
simulated IOC, wired together for a local backend inner loop via
`docker compose`. The React/Vite frontend lives here too but is run
separately (see below).

## Layout

| Path | Role |
|---|---|
| `backend/configuration_service/` | Device/PV registry. REST on port 8004. Optional persistence to PostgreSQL or SQLite (see below). |
| `backend/direct_control_service/` | Device commanding + PV monitoring. REST + WS on port 8003. |
| `backend/queueserver_service/` | Plan queueing + execution, based on bluesky-queueserver + bluesky-httpserver, run in unified mode (one process serves 0MQ on 60615/60625 and HTTP + WS on 60610). Maintained in-tree; see its README. Runs in `integration/pods/with-queueserver/`, not the inner-loop compose. |
| `frontend/` | React + Vite UI (not part of `docker-compose.yml`). |
| `integration/` | Richer multi-service pods (`pods/{minimal,full,dev}/`), the caproto IOC image (`ioc/`), happi seed data (`happi/`), and local device classes (`localdevs/`). |
| `shared-schema/` | OpenAPI schemas published by the backends. |
| `docker-compose.yml` | Backend inner-loop stack: IOC + Postgres + both backends (no frontend service — see below). |

## Running the backend stack

```bash
docker compose up --build
```

Starts four containers:

- `ioc` — caproto `mini_beamline` serving EPICS CA on 5064
- `postgres` — `postgres:16`, the persistence backend for `configuration_service`
- `configuration_service` — http://localhost:8004, Swagger UI at `/docs`.
  Seeds its registry on startup from `integration/happi/happi_db.json`
  (`CONFIG_LOAD_STRATEGY=happi`), so the mini_beamline devices/PVs are
  registered without a separate seed step. (`direct_control_service` 404s any
  read/write against a PV the registry doesn't know about.)
- `direct_control_service` — http://localhost:8003, Swagger UI at `/docs`

On startup each backend writes its live OpenAPI schema to
`./shared-schema/<service>.openapi.json` via the bind-mounted volume.
Those files are also committed to the repo so schema-dependent tooling
works without running docker.

Quick smoke test once everything is up:

```bash
curl http://localhost:8003/api/v1/pv/mini:current/value
curl -X POST http://localhost:8003/api/v1/pv/set \
    -H 'Content-Type: application/json' \
    -d '{"pv_name":"mini:dot:mtrx","value":3.14}'
```

## For frontend developers

The frontend is not part of `docker-compose.yml` — run it separately.

### Consuming the OpenAPI schemas

Each backend publishes its full OpenAPI spec. You can generate TypeScript
types from either the committed JSON artifacts (no docker needed) or from
a running backend.

**From committed artifacts** — works on a fresh clone:

```bash
cat shared-schema/configuration_service.openapi.json
cat shared-schema/direct_control.openapi.json
cat shared-schema/queueserver_service.openapi.json
```

Generate types:

```bash
npx openapi-typescript shared-schema/configuration_service.openapi.json \
    -o frontend/src/api/configuration_service.d.ts
npx openapi-typescript shared-schema/direct_control.openapi.json \
    -o frontend/src/api/direct_control.d.ts
npx openapi-typescript shared-schema/queueserver_service.openapi.json \
    -o frontend/src/api/queueserver_service.d.ts
```

The committed JSON updates whenever a backend route changes and someone
re-exports. If you suspect it's stale, regenerate from a live backend
(below).

**From live backends** — after `docker compose up --build`:

```bash
curl http://localhost:8004/openapi.json > shared-schema/configuration_service.openapi.json
curl http://localhost:8003/openapi.json > shared-schema/direct_control.openapi.json
```

The queueserver schema is regenerated with its export script instead (the
committed artifact is the bare server — deployment-specific auth-provider
routes are intentionally excluded):

```bash
cd backend/queueserver_service/subprojects/bluesky-httpserver
python scripts/export_openapi.py -o ../../../../shared-schema/queueserver_service.openapi.json
```

The queueserver's WebSocket endpoints (`/api/status/ws`, `/api/info/ws`,
`/api/console_output/ws`) don't appear in OpenAPI (FastAPI limitation) — see
the API description header and `backend/queueserver_service/README.md`.

Or mount `./shared-schema` into your own frontend container; the backends
will overwrite the files on every `docker compose up` via their startup
lifespan hooks.

### Running the frontend

```bash
cd frontend
npm install
npm run dev
```

Vite dev server starts at http://localhost:5173. Both backends have CORS
`allow_origins="*"` in development so fetches from localhost:5173 to
localhost:8003 / localhost:8004 work without proxy setup.

## Interactive API exploration

Both backends expose Swagger UI:

- http://localhost:8003/docs — direct_control_service (device commanding, PV read/write, task status)
- http://localhost:8004/docs — configuration_service (device/PV registry, device locks)

## Notes

- **WebSocket endpoints on `direct_control_service` are not in the OpenAPI
  schema** (FastAPI limitation). Their contracts are documented separately
  in the service's own README.
- **Persistence backend** — when persistence is enabled (the default;
  `CONFIG_DEVICE_CHANGE_HISTORY_ENABLED=false` disables it and loads from the
  profile each start), `configuration_service` uses SQLAlchemy against either
  PostgreSQL or SQLite, selected by the `CONFIG_DATABASE_URL` scheme
  (`postgresql+psycopg://user:pass@host:5432/config_service` or
  `sqlite+pysqlite:////var/lib/config_service/config.db`). PostgreSQL is
  recommended for production / multi-writer deploys; SQLite suits single-node /
  dev use. This compose stack uses the bundled `postgres` service.
- Startup happi-seeding (`CONFIG_LOAD_STRATEGY=happi`) is the dev shortcut here.
  Production deployments seed the registry via profile files or CRUD calls from
  an upstream Experiment Execution Service.
- The compose `postgres` service has no data volume, so the registry resets on
  every `docker compose down` and is re-seeded from happi on the next `up`.
- For a richer full-stack environment (more IOCs, queueserver, etc.), use
  `integration/pods/<name>/` instead of this inner-loop compose.
