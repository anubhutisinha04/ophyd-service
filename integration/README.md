# ophyd-service integration environments

Self-contained docker-compose test/demo environments for exercising `configuration_service`, `direct_control_service`, and (eventually) the merged `bluesky-queueserver` against simulated IOCs. Reproducible by anyone with docker — no facility accounts, no VPN, no hand-deployed systemd units.

These pods are the project's primary test target.

## Layout

```
integration/
├── ioc/                       # caproto Dockerfile (mini_beamline by default)
├── happi/happi_db.json        # canonical device DB, used by every compose surface
├── localdevs/localdevs.py     # vanilla-ophyd shim (Spot, Det, RandomWalk, Thermo, …)
├── pods/
│   ├── minimal/docker-compose.yaml   # 1 IOC + 2 backends
│   └── full/docker-compose.yaml      # 5 IOCs + 2 backends
└── exercise/
    ├── configuration_service.sh      # bash + curl + jq
    ├── direct_control.sh             # bash + curl + jq (HTTP only)
    └── direct_control_ws.py          # python + websockets (closes the WS gap)
```

## Two compose surfaces under `ophyd-service/`

| Surface | Use case |
|---|---|
| `ophyd-service/docker-compose.yml` (repo root) | **Inner loop.** 1 IOC + 2 backends, happi-seeded. Fast iteration on a single backend. |
| `ophyd-service/integration/pods/minimal/` | **Smoke test.** 1 IOC + 2 backends, no hot-reload. Quick "does the wiring still work?" |
| `ophyd-service/integration/pods/full/` | **Integration / demo.** 5 IOCs + 2 backends, no hot-reload. More device shapes for end-to-end testing. |
| `ophyd-service/integration/pods/dev/` | **Joint backend + frontend dev.** 2 IOCs + 2 backends in hot-reload + frontend dev server. |

Both surfaces mount the same `integration/happi/happi_db.json` as the device registry source. Devices that have no live IOC in the chosen surface are still listed (the registry is a *catalog*); reads against them will fail at the CA layer.

## Pods

### `pods/minimal/`

Smallest shape that exercises the happi-seeded registry end-to-end. Three services: `ioc` (caproto `mini_beamline`), `configuration_service`, `direct_control_service`. Coordination check off, CA search limited to the pod network.

```sh
cd integration/pods/minimal
docker compose up --build
```

### `pods/full/`

Five IOCs + the two backends. Adds `random_walk` ×3 (different prefixes) and `thermo_sim` to the mini_beamline base. Lets you exercise the registry against a more realistic device mix and run the WebSocket exerciser against a live, ticking PV.

```sh
cd integration/pods/full
docker compose up --build
```

All five IOCs build from the same `integration/ioc/Dockerfile`; per-service `command:` overrides choose the caproto module + prefix. Health is gated on every IOC reaching `service_healthy`.

### `pods/dev/`

**The recommended environment for joint backend + frontend development.** Runs two IOCs (caproto `mini_beamline` + `fake_motor_record` for real motor records with `.RBV`/`.VAL`/`.VELO`/etc.), both backends in **uvicorn `--reload` hot-reload mode** with bind-mounted source, and a **`node:20` frontend container** running `npm run dev` against bind-mounted `frontend/`.

```sh
cd integration/pods/dev
docker compose up --build
```

What's published to the host:

| Port | Service | Notes |
|---|---|---|
| `5173` | frontend | vite dev server, HMR over WS |
| `8003` | direct_control_service | uvicorn with `--reload` |
| `8004` | configuration_service | uvicorn with `--reload`, happi-loaded |

**Backend hot-reload:** edit anything under `backend/configuration_service/src/` or `backend/direct_control_service/src/` on the host — uvicorn detects within ~1s and restarts the worker. Confirmed by tailing `docker compose logs configuration_service`.

**Frontend hot-reload:** edit anything under `frontend/src/` on the host — vite HMR pushes the change to the browser. (`CHOKIDAR_USEPOLLING=true` is set so file events propagate cleanly across the bind mount.)

**Frontend node_modules isolation:** the container uses an anonymous volume on `/app/node_modules` so the host's modules don't bleed in. First `up` populates it via `npm install` (~9s); subsequent ups reuse the volume.

**Frontend boundary respected:** this compose is provided by the backend team. The frontend container's behavior is fully controlled by `frontend/`'s own `package.json` + `vite.config.ts` — we don't override anything inside `frontend/`. The frontend team can swap node version, change scripts, add proxies, etc., without touching this compose file.

The dev pod's registry has 44 devices (41 from base happi + 3 motor records: `motor1`, `motor2`, `motor3` at `sim:mtr1`/`sim:mtr2`/`sim:mtr3`).

## Shared assets

### `happi/happi_db.json`

Canonical device DB. Currently 41 entries: 8 compound devices (`spot`, `pinhole`, `edge`, `slit`, `thermo`, `random_walk[_h|_v]`) and 33 leaf scalars. Ported from `bluesky/bluesky-pods`' `test_db.json`, modified to match what our IOCs actually publish.

**Compound devices and the leaf-entry pattern (option (a)):** real beamlines use compound device classes; the happi loader does pure JSON parsing and does **not** enumerate a compound device's sub-PVs into `registry.pvs`. `direct_control_service` validates at the leaf level (`mini:dot:img_sum`, not `mini:dot`). Solution: alongside each compound entry, add explicit `ophyd.signal.EpicsSignal` entries for the leaf PVs. See `spot` + `spot_*` for the pattern.

**Source of truth for PV prefixes:** the running IOC's `--list-pvs` log (`docker compose logs ioc | grep -A50 "PVs available"`). caproto modules publish what they publish; adjust `happi_db.json` to match, not the other way around.

### `localdevs/localdevs.py`

Minimal vanilla-ophyd shim. Defines `Det`, `Spot`, `RandomWalk`, `Eurotherm`, `Thermo` — all the compound classes referenced by `happi_db.json`. **Not mounted into any container yet** (the loader doesn't import it). Shipped here for the future queueserver pod, where actual device instantiation happens.

Vanilla ophyd only — no `nslsii` dependency, even though `Eurotherm` originated there. The bluesky-pods upstream extracted it specifically to drop that dep; we keep it that way (see the `feedback_community_not_nsls` memory).

## Endpoint exercisers (`exercise/`)

These walk every public endpoint family on each backend as a first-time user would, exit non-zero on any assertion miss, and are CI-ready. They replace the old `seed/seed-pvs.sh` curl sidecar — instead of pre-populating test data, they *verify* the service from a fresh happi-seeded state.

```sh
# pod must be up first
./integration/exercise/configuration_service.sh         # ~30 endpoint checks
./integration/exercise/direct_control.sh                # HTTP-only walk
uv run --with websockets integration/exercise/direct_control_ws.py
# (or `pip install websockets` and run directly)
```

Override targets via env vars: `CONFIG_URL`, `DIRECT_URL`, `DIRECT_WS_URL`. Override the WS exerciser's PV with `PV_NAME` (defaults to `random_walk:x`, which ticks frequently when the full pod is up).

## Extension roadmap

**Phase 3 — data pipeline:** add `tiled`, `mongo` (databroker), `redis`, `kafka`. Lets us test the document-streaming path end-to-end.

**Phase 4 — queueserver/httpserver:** once the `merge/httpserver` branch lands, add a container built from `sligara7/bluesky-queueserver`. Mount `integration/localdevs/` into its PYTHONPATH so happi-defined compound devices instantiate.

**Other gaps:** MADSIM (AreaDetector simulator) — bluesky-pods references `simcam1` but doesn't ship the IOC. Add when we have one.

## Why not fork bluesky-pods?

`bluesky/bluesky-pods` is the reference for this kind of pod shape and we cannibalize it freely — but we don't track it upstream. Reasons:

- bluesky-pods doesn't include `configuration_service` or `direct_control_service` — they're our services, they have to live with us.
- We want to avoid inheriting NSLS-II-specific choices wholesale.
- The pod evolves in lockstep with our service code; keeping it in the same repo keeps the coupling visible.

Clone `https://github.com/bluesky/bluesky-pods` separately if you want the full reference — useful especially for the data-pipeline pieces when we build Phase 3.
