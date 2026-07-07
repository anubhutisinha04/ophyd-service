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

This service is maintained independently of upstream bluesky-queueserver. It is
moving to an **HTTP-only** design, so the compatibility surface has two tiers:

- **Stable, frozen contract** — the HTTP REST + WebSocket API consumed by the
  **bluesky-queueserver-api** client library's HTTP transport. This MUST keep
  working with the api package; anything behavior-visible against it is a
  breaking change.
- **Deprecated, pending removal** — the 0MQ CONTROL request/response protocol and
  INFO (PUB) message formats that `REManagerAPI`'s ZMQ transport and its
  console/system-info monitors speak. The 0MQ layer is being removed entirely
  (the manager already serves the control path in-process in unified mode); until
  it is deleted it still functions, but it is not a frozen contract and gets no
  new robustness investment. When it is removed, the api package's **ZMQ**
  transport, external PUB subscribers, and the legacy top-level
  `bluesky_queueserver` ZMQ shim names (e.g. `ZMQCommSendThreads`,
  `ZMQCommSendAsync`) stop being supported — the
  HTTP transport is the supported replacement.

(The http half of this service itself imports `bluesky_queueserver_api`, so
breaking the api package breaks the service too.) Internal divergence — new endpoints,
new config sections, manager internals — is fine; changing or removing what
bluesky-queueserver-api's HTTP transport consumes is not.

Two install-level consequences of that contract (see the notes in `pyproject.toml`
and `bluesky_queueserver/__init__.py`): the **distribution** is named
`bluesky-queueserver` so the api client's `Requires-Dist: bluesky-queueserver`
resolves to this package instead of pulling the upstream dist (which would shadow
the console scripts), and a one-module `bluesky_queueserver` package re-exports
the legacy top-level names the client imports. The import namespace for all code
in this repo is `queueserver_service`.

The dist-name trick only works if the resolver picks this distribution. Upstream
ships `0.0.x`; this fork is versioned `1.0.0` so it sorts newest and wins by
default. A pin that excludes `1.0.0` — `bluesky-queueserver<1.0`, `==0.0.*` — makes
the resolver take the real upstream dist instead, which shadows the
`qserver`/`start-re-manager` console scripts and the manager implementation. **Do
not pin `bluesky-queueserver` below `1.0` against this fork.** If some upstream
requirement forces a `<1.0` pin, the escape hatch is to re-version this fork as a
post-release of the newest upstream tag (e.g. `0.0.24.post1`, incrementing the
`.postN` number as needed), which satisfies the
pin while still sorting ahead of upstream — a deliberate release decision, noted in
`pyproject.toml`.

For the same reason, a second thin distribution — `bluesky-httpserver`, under
`bluesky-httpserver/` (see its `pyproject.toml` and `bluesky_httpserver/__init__.py`)
— claims the upstream `bluesky-httpserver` distribution name and provides the
`bluesky_httpserver` import namespace, lazily aliasing every submodule onto
`queueserver_service.http` (so e.g. `from bluesky_httpserver.server import start_server`
or a uvicorn `--factory bluesky_httpserver.server:app_factory` keep working). This
stops a third-party `Requires-Dist: bluesky-httpserver` from pulling upstream
httpserver in alongside and clobbering the `start-bluesky-httpserver` console script
(which the `bluesky-queueserver` dist above already owns — the shim deliberately does
NOT re-declare it). The shim pins `bluesky-queueserver==<same version>` so a
version-skewed pair is uninstallable. It is installed alongside the main package
(`pip install -e . -e ./bluesky-httpserver`), which the Dockerfile and the
queueserver-tests CI job both do.

Enforced in CI: the HTTP contract is exercised by the in-process side-C suite
(`tests/http/test_side_c_api_client_compat.py` / `tests/http/test_side_c_auth.py`), which
drives the PyPI `bluesky-queueserver-api` HTTP client against a live
manager+server, and by the `with-queueserver` integration job that installs the
api package from PyPI (`integration/exercise/queueserver_api_compat.py`). The
integration job still drives both transports today; its 0MQ leg is retained only
until the 0MQ layer is removed, after which HTTP is the sole exercised transport.

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

### Device locking (config_service integration)

When `config_service.enabled` is set, queueserver locks devices in
configuration_service around plan execution so Direct Control cannot command
them concurrently. Two settings shape this:

- `config_service.lock_scope` (default **`plan`**): lock exactly the registered
  devices a plan references, acquired at plan start and released when the plan
  ends — so an idle environment leaves devices free. The alternative
  `environment` locks the whole device set for the environment's lifetime
  (including while idle).
- Whether *other* (unused) devices are also blocked while a plan runs is chosen
  on the configuration_service side by its `CONFIG_LOCK_ALL` policy: `true`
  blocks every device while any plan-lock is held (Variant 1); `false` blocks
  only the plan's devices (Variant 2). Queueserver's acquisition is identical
  either way.

If configuration_service enables lock leases (`CONFIG_LOCK_LEASE_TTL_SECONDS`
> 0), the coordinator renews held locks on a timer and re-acquires them if the
authority reports the lease lost or its `lock_epoch` changed (a
configuration_service restart) — so a mid-plan restart doesn't leave devices
unprotected.

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
