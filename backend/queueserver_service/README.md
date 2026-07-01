# queueserver_service

The bluesky **queueserver + httpserver** as a co-equal ophyd-service backend, alongside
`configuration_service` and `direct_control_service`.

## Package layout

One cohesive Python package, `queueserver_service` (based on bluesky-queueserver and
bluesky-httpserver, maintained here independently):

- `queueserver_service/manager/` — the RE manager, worker, CLI and plan/profile machinery
  (formerly `bluesky_queueserver.manager`);
- `queueserver_service/http/` — the FastAPI HTTP/WebSocket API server
  (formerly the `bluesky_httpserver` package);
- `queueserver_service/common/` — the 0MQ/JSON-RPC comms layer and logging glue shared by
  both halves (`comms`, `json_rpc`, `logging_setup`);
- `queueserver_service/profile_collection_sim/` — the simulated startup profile;
- `tests/manager/`, `tests/http/` — both test suites, collected by a single `pytest` run
  (the http fixtures build on the manager test harness in `tests/manager/common.py`).

## Compatibility contract

This service is maintained independently of upstream bluesky-queueserver, but the
surfaces consumed by the **bluesky-queueserver-api** client library are stable public
contracts and MUST keep working with it:

- the 0MQ CONTROL request/response protocol and INFO (PUB) message formats that
  `REManagerAPI` and its console/system-info monitors speak;
- the HTTP REST + WebSocket API that the api package's HTTP transport targets.

(The http half of this service itself imports `bluesky_queueserver_api`, so
breaking the api package breaks the service too.) Internal divergence — new endpoints,
new config sections, manager internals — is fine; changing or removing what
bluesky-queueserver-api consumes is not.

Two install-level consequences of that contract (see the notes in `pyproject.toml`
and `bluesky_queueserver/__init__.py`): the **distribution** is named
`bluesky-queueserver` so the api client's `Requires-Dist: bluesky-queueserver`
resolves to this package instead of pulling the upstream dist (which would shadow
the console scripts), and a one-module `bluesky_queueserver` package re-exports
the legacy top-level names the client imports. The import namespace for all code
in this repo is `queueserver_service`.

Enforced in CI: the `with-queueserver` integration job installs
`bluesky-queueserver-api` from PyPI and drives the running service over both
transports (`integration/exercise/queueserver_api_compat.py`).

## How the image is built

The `Dockerfile` builds from the **in-tree source** in this directory — it does not track
upstream, and nothing is pulled from an external git ref at build time. It installs
`pip install -e ".[all]"`, which installs the `queueserver_service` package and registers the
console scripts (`start-re-manager`, `qserver`, `start-bluesky-httpserver`, ...). The
console-script names are kept from upstream so existing deployments and docs keep working.

A few heavy, deployment-specific runtime deps are kept out of the base install and exposed as
**optional extras** (see `[project.optional-dependencies]` in `pyproject.toml`), so a lean or
standalone install can pick only what it needs:

- `spreadsheet` → `pandas` (`manager/conversions.py` spreadsheet→plan conversion);
- `sim` → `matplotlib` (the shipped `profile_collection_sim` startup);
- `epics` → `pyepics` (the ophyd EPICS layer for config-service consume-mode device injection);
- `all` → all three (what the Docker image installs).

Runtime dependencies are declared statically in `pyproject.toml` and pinned in the committed
`uv.lock` for reproducible resolution (`uv sync` / `uv lock`), mirroring the other backends.
Development/test dependencies remain in `requirements-dev.txt`.

```bash
docker build -t queueserver_service backend/queueserver_service
```

## How it runs

**Unified mode**: `start-re-manager` co-hosts the FastAPI httpserver in one process, serving
0MQ (60615 control / 60625 info) and HTTP (60610) together. This exercises the
process-unification work (U1–U3). It requires a Redis instance and, for backend integration,
a manager-config YAML pointing `config_service.url` at the configuration_service.

See `integration/pods/with-queueserver/` for the full pod (redis + the three backends + an IOC)
and the manager-config YAML.

**Queue storage** is pluggable (`queueserver_service/manager/queue_store.py`). Redis
is the default; the queue is stored in Redis at `--redis-addr` unless
`--queue-store-uri` (or `QSERVER_QUEUE_STORE_URI`, or the `network/queue_store_uri`
config key) is set. Supported URI schemes:

- `sqlite+aiosqlite:///path/to/queue.db` — store the queue in a local SQLite file
  (good for development, single-host deployments, or hosts without Redis).
- `postgresql+psycopg://user:pass@host:5432/dbname` — store the queue in
  PostgreSQL (shares the dual-backend pattern with `configuration_service`).
- `redis://host[:port]` — selects Redis explicitly; equivalent to leaving the
  option unset and using `--redis-addr`.

Any other scheme is an error (no silent fallback). Default behavior is unchanged
when the option is not set. The plan-queue test suite runs against both backends
(`tests/manager/test_plan_queue_ops.py` is parametrized over `["redis", "sqlite"]`).
See `docs/source/manager_config.rst` for the full config surface.

## Running the tests

One `pytest` run from this directory collects both suites (needs a local redis;
the LDAP authenticator tests also want `docker compose -f
docker-configs/ldap-docker-compose.yml up -d`).

The full suite boots real manager/worker/server processes and takes ~2 h, so
tests with a recorded duration ≥ 1 s are auto-marked `slow` from the committed
`.test_durations` (see `tests/conftest.py`):

```bash
pytest -m "not slow"     # development loop: ~2100 of the tests in ~7 min
pytest                   # everything
USE_IPYKERNEL=true pytest   # run the worker in IPython-kernel mode; required
                            # by the tests that skip otherwise
```

The IPython-kernel mode is a second test dimension: without `USE_IPYKERNEL`
the kernel-only tests skip (they are most of the skip count in a default run).

Refresh the durations data after the suite's shape changes significantly:

```bash
USE_IPYKERNEL=true pytest --store-durations
```
