# ophyd-service integration pods

Self-contained docker-compose test environments for exercising `configuration_service`, `direct_control_service`, and (eventually) the merged `bluesky-queueserver` against simulated IOCs. The pods are **reproducible by anyone with docker** — no facility accounts, no VPN, no hand-deployed systemd units.

These pods replace `xf31id1-tst-qs1` as the project's primary test/demo target.

## Two compose surfaces

| Surface | Use case |
|---|---|
| `ophyd-service/docker-compose.yml` (repo root) | **Inner loop.** Backend-only: IOC + 2 backends + curl seeder. Fast iteration when editing a single backend. |
| `ophyd-service/pods/<pod-name>/` (here) | **Integration.** Full-stack environments with realistic simulated beamlines and the full data/control pipeline. |

Pick by the blast radius of the change. Editing just `direct_control_service`? Inner loop. Touching contracts across services, or doing a demo? Pod.

## Current pods

### `minimal/`

Smallest shape that exercises the happi-seeded registry end-to-end. Three services:

- **`ioc`** — reuses `ophyd-service/ioc/` (caproto `mini_beamline`). Same image as the inner loop.
- **`configuration_service`** — loads devices from `pods/shared/happi/happi_db.json` via `CONFIG_LOAD_STRATEGY=happi`. No curl sidecar.
- **`direct_control_service`** — coordination check off, CA search limited to the pod network.

Run:

```sh
cd pods/minimal
docker compose up --build
```

Tear down with `docker compose down`. The config-service SQLite DB lives at `/tmp/config_service.db` inside the container and is recreated every `up` — happi reseeds on startup.

## Shared assets (`shared/`)

### `shared/happi/happi_db.json`

The pod's device database, ported from `bluesky/bluesky-pods`' `bluesky_config/happi/test_db.json` and trimmed to just the PVs our `caproto.ioc_examples.mini_beamline` IOC exposes.

**Key design choice — compound devices:** Real beamlines use compound device classes (`Spot`, `Det` here — AreaDetector, motor records, etc. in practice). The happi loader does pure JSON parsing; it doesn't enumerate sub-PVs of a compound device into `registry.pvs`. That's a problem for `direct_control_service`, which validates PVs at the leaf level (e.g. `mini:dot:img_sum`).

**Phase 1 solution — option (a):** for every leaf PV of a compound device, add an explicit `ophyd.signal.EpicsSignal` happi entry alongside the compound entry. The compound entry stays for device identity (`spot`, `pinhole1`); the leaf entries make the leaves discoverable. See the `spot_*` and `pinhole1_*` entries for the pattern.

**Source of truth for PV prefixes:** the running IOC's startup log (use `docker compose logs ioc | grep -A50 "PVs available"`). `caproto.ioc_examples.mini_beamline` publishes what it publishes — treat that as canonical and adjust `happi_db.json` to match, not the other way around. The repo-level `seed/seed-pvs.sh` (used by the backend-only inner-loop compose) registers `mini:ph1:*` PVs that don't exist on the IOC; it's only been satisfying the gate, not actual CA lookups. That's a known bug tracked in the tech debt ledger.

### `shared/localdevs/localdevs.py`

Minimal ophyd wrapper module (`Spot`, `Det`) referenced by the compound happi entries. **Not mounted into any container yet** — `HappiProfileLoader` does pure JSON parsing and never imports it. Shipped here now for Phase 3/4:

- When queueserver joins the pod and starts instantiating devices from happi, mount this directory into that container's PYTHONPATH.
- Already vanilla ophyd — no `nslsii` dependency. Keep it that way (see the `feedback_community_not_nsls` memory).

## Smoke tests

After `docker compose up --build`, from another terminal:

```sh
# config-service reachable, loaded devices from happi?
curl -s http://localhost:8004/health
curl -s http://localhost:8004/api/v1/devices | jq 'keys'

# direct-control can read a scalar PV (happi-registered as a leaf EpicsSignal)?
curl -s http://localhost:8003/api/v1/pv/mini:dot:mtrx/value

# direct-control can read a compound sub-PV?
# This exercises option (a) — spot_img_sum was added as a leaf entry
# alongside the compound `spot` device.
curl -s http://localhost:8003/api/v1/pv/mini:dot:img_sum/value
```

Both PV reads should succeed. If the compound-leaf read 404s but the scalar succeeds, the happi leaf-entry pattern isn't reaching `registry.pvs` — investigate before claiming option (a) works.

## Extension roadmap

**Phase 2 — `pods/full/`:** add sibling IOCs for `random_walk` (×3), `thermo_sim`, `simple`. Expand `shared/happi/happi_db.json` with matching entries and grow `shared/localdevs/localdevs.py` to include `RandomWalk`, `Thermo`, `Simple`.

**Phase 3 — data pipeline:** add `tiled`, `mongo` (databroker), `redis`, `kafka`. Lets us test the document streaming path.

**Phase 4 — queueserver/httpserver:** once the `merge/httpserver` branch lands, add a container building from `sligara7/bluesky-queueserver`. Mount `shared/localdevs/` into its PYTHONPATH so happi-defined compound devices instantiate.

## Why not just fork bluesky-pods?

`bluesky/bluesky-pods` is the reference for this kind of pod shape and we cannibalize it freely — but we don't track it upstream. Reasons:

- bluesky-pods doesn't include `configuration_service` or `direct_control_service` — they're our services, they have to live with us.
- We want to avoid inheriting NSLS-II-specific choices wholesale.
- The pod evolves in lockstep with our service code; keeping it in the same repo keeps the coupling visible.

If you want to see what bluesky-pods offers, clone `https://github.com/bluesky/bluesky-pods` separately — it's a useful reference, especially for the data-pipeline pieces when we build Phase 3.
