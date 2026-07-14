# queueserver-repro — reproduce the real queueserver on any machine

`reproduce.sh` stands up the **real upstream** `bluesky-queueserver` (RE Manager)
and `bluesky-httpserver` running an NSLS-II beamline **profile collection**, on a
fresh machine, with one command. It provisions the external services the
profile's startup code expects, then launches the two services from the
profile's own `pixi` `qs` environment — exactly how the production ansible
`bsqs` role runs them.

Default target is the **IOS** profile
(`github.com/NSLS2/ios-profile-collection`); it is parameterized for any
beamline (see *Other beamlines*).

```bash
./reproduce.sh up        # clone + build env + provision deps + launch + open + verify
./reproduce.sh status    # containers, services, environment state
./reproduce.sh logs      # tail the RE Manager + HTTP server logs
./reproduce.sh verify    # re-print plan/device counts + HTTP API/key
./reproduce.sh down       # stop services + remove containers (keeps clone + env)
./reproduce.sh nuke       # down + delete the whole work directory
```

## Requirements

- `git`, `curl`, `openssl`
- **Docker** (or `podman`) — runs redis ×2 and mongodb
- `pixi` — auto-installed to `~/.pixi` if missing
- Internet access (clone the profile, solve the pixi env, pull container images)

## What it provisions and why

Opening a beamline profile under the queueserver requires several services that
the profile's `00-startup.py` (`nslsii.configure_base(...)`) reaches out to. The
script supplies each, mapped to how production deploys it:

| Dependency | What the profile needs it for | Script provides | Production role |
|------------|-------------------------------|-----------------|-----------------|
| Redis (TLS, :6380) | `RE.md` metadata store (`nslsii.configure_base`, `redis_ssl=True`) | `redis:7` container, TLS + password, self-signed cert | `redis6` (+ ACME cert) |
| Redis (plain, :60590) | the RE Manager's own queue/history store | `redis:7` container, no auth | `redis` role (`bluesky-queueserver-redis`) |
| MongoDB (:27017) | `databroker.Broker.named('ios')` catalog (via tiled) | `mongo:6` container | beamline mongo |
| tiled profile `ios` | resolves `Broker.named('ios')` → the mongo catalog | generated, on `TILED_PROFILES` path | beamline tiled profiles |
| `kafka.yml` | `nslsii` Kafka publisher config (file only) | generated, broker-less, `abort=false` | `bluesky_kafka_config` |
| `~/.pyOlog.conf` | stops `SimpleOlogClient()` prompting for a password | generated if absent | `ansible-epics-tools` olog roles |

**Not started:** a Kafka broker and an EPICS IOC. The IOS profile opens without
them — Kafka publishing is non-fatal, and its devices don't force PV
connections at environment-open. If a profile you try *does* force connections,
run with `WITH_IOC=1` to add a caproto catch-all IOC.

## The services are the real thing

The RE Manager and HTTP server are launched with:

```
pixi run --manifest-path <profile>/pixi.toml -e qs start-re-manager --config ...
pixi run --manifest-path <profile>/pixi.toml -e qs uvicorn ... bluesky_httpserver.server:app
```

i.e. the upstream binaries from the profile collection's own `qs` environment —
the same commands the `bsqs` systemd units run. The RE Manager is configured by
a YAML (`config/queueserver-config.yml`) whose `startup.startup_dir` points at
the cloned profile's `startup/` directory, and it talks to the HTTP server over
0MQ IPC sockets under the work directory.

## Configuration

Override via environment variables (defaults shown):

```
ENDSTATION=ios
PROFILE_REPO=https://github.com/NSLS2/ios-profile-collection.git
PROFILE_BRANCH=main
QS_REPRO_HOME=$HOME/qs-repro        # clone, configs, sockets, logs, secrets
PROFILE_DIR=$QS_REPRO_HOME/profile_collection
HTTP_PORT=60610                     # host port for the HTTP API
REDIS_QUEUE_PORT=60590
REDIS_TLS_PORT=6380
MONGO_PORT=27017
REDIS_TLS_PASSWORD=<generated hex>  # persisted in $QS_REPRO_HOME/config/secrets.env
HTTP_API_KEY=<generated hex>        # httpserver single-user key (alphanumeric)
WITH_IOC=0                          # 1 = also run a caproto catch-all IOC
```

Generated secrets persist in `$QS_REPRO_HOME/config/secrets.env`, so re-running
`up` reuses the same Redis password and API key.

## Using it

```bash
./reproduce.sh up
# ... reports: allowed plans: 145, allowed devices: 100, and the API key

KEY=$(sed -n 's/^HTTP_API_KEY=//p' "$HOME/qs-repro/config/secrets.env")
curl -s http://localhost:60610/api/status        -H "Authorization: ApiKey $KEY"
curl -s http://localhost:60610/api/plans/allowed  -H "Authorization: ApiKey $KEY" | head -c 300
```

Swagger UI: <http://localhost:60610/docs>. Point a client
(e.g. `bluesky-widgets` `queue_monitor`) at the same base URL + API key.

## Other beamlines

Any NSLS-II profile collection with a `qs` pixi environment should work:

```bash
ENDSTATION=tst \
  PROFILE_REPO=https://github.com/NSLS2/tst-profile-collection.git \
  PROFILE_BRANCH=main \
  ./reproduce.sh up
```

The tiled profile is named after `ENDSTATION`. Profiles that reach additional
services (extra Redis DBs, a real Tiled API key, live PVs) may surface new
startup errors — `./reproduce.sh logs` shows exactly where startup stopped, the
same way each IOS dependency above was discovered.

## Relation to production

This is the **developer / any-machine** path: it spoofs the beamline
infrastructure so a profile can be opened anywhere. Production deploys the same
two services natively under **systemd**, launched with `pixi` against the
profile collection's `qs` environment, via the NSLS-II ansible `bsqs` role
(with the `redis`/`redis6`, `bluesky_kafka_config`, and nginx roles providing
the surrounding services). The dependency table above maps each spoofed service
to its production role.
