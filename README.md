# ophyd-service

Monorepo for the Bluesky ophyd-service. Two FastAPI backends + a caproto
simulated IOC + a React/Vite frontend, all wired together for local
development via `docker compose`.

## Layout

| Path | Role |
|---|---|
| `backend/configuration_service/` | Device/PV registry. REST on port 8004. |
| `backend/direct_control_service/` | Device commanding + PV monitoring. REST + WS on port 8003. |
| `frontend/` | React + Vite UI. |
| `ioc/` | Dockerfile for a containerized caproto `mini_beamline` IOC. |
| `seed/` | One-shot PV seed script used by the compose stack. |
| `shared-schema/` | OpenAPI schemas published by the backends. |
| `docker-compose.yml` | Backend + IOC stack (no frontend service — see below). |

## Running the backend stack

```bash
docker compose up --build
```

Starts four containers:

- `ioc` — caproto `mini_beamline` serving EPICS CA on 5064
- `configuration_service` — http://localhost:8004, Swagger UI at `/docs`
- `seed_registry` — one-shot sidecar: POSTs the mini_beamline PV list to
  `configuration_service` and exits. Required because
  `direct_control_service` 404s any read/write against an unregistered PV.
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
```

Generate types:

```bash
npx openapi-typescript shared-schema/configuration_service.openapi.json \
    -o frontend/src/api/configuration_service.d.ts
npx openapi-typescript shared-schema/direct_control.openapi.json \
    -o frontend/src/api/direct_control.d.ts
```

The committed JSON updates whenever a backend route changes and someone
re-exports. If you suspect it's stale, regenerate from a live backend
(below).

**From live backends** — after `docker compose up --build`:

```bash
curl http://localhost:8004/openapi.json > shared-schema/configuration_service.openapi.json
curl http://localhost:8003/openapi.json > shared-schema/direct_control.openapi.json
```

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
- http://localhost:8004/docs — configuration_service (device/PV registry, device locks, metadata)

## Notes

- **WebSocket endpoints on `direct_control_service` are not in the OpenAPI
  schema** (FastAPI limitation). Their contracts are documented separately
  in the service's own README.
- The `seed_registry` sidecar is a dev-only shortcut. Production
  deployments seed the registry via profile files or CRUD calls from
  an upstream Experiment Execution Service.
- `configuration_service` runs with `CONFIG_LOAD_STRATEGY=empty` against the
  compose `postgres` service for the demo; with no data volume the DB resets on
  every `docker compose down` and the seeder repopulates it on next `up`.
