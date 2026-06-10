# queueserver_service

The bluesky **queueserver + httpserver** as a co-equal ophyd-service backend, alongside
`configuration_service` and `direct_control_service`.

## Compatibility contract

This service is maintained independently of upstream bluesky-queueserver, but the
surfaces consumed by the **bluesky-queueserver-api** client library are stable public
contracts and MUST keep working with it:

- the 0MQ CONTROL request/response protocol and INFO (PUB) message formats that
  `REManagerAPI` and its console/system-info monitors speak;
- the HTTP REST + WebSocket API that the api package's HTTP transport targets.

(The httpserver half of this service itself imports `bluesky_queueserver_api`, so
breaking the api package breaks the service too.) Internal divergence — new endpoints,
new config sections, manager internals — is fine; changing or removing what
bluesky-queueserver-api consumes is not.

## How the image is built

The `Dockerfile` builds from the **in-tree source** in this directory. The service is based
on the merged bluesky-queueserver + bluesky-httpserver (the `merge/httpserver` unification
work) and is maintained here independently — it does not track upstream. A single editable
`pip install -e .` installs both packages (the merged `setup.py` registers
`start-re-manager` and `start-bluesky-httpserver`). Nothing is pulled from an external git
ref at build time.

A few runtime deps the install doesn't pull are added explicitly: `pandas`
(`manager/conversions.py`), `matplotlib` (the shipped `profile_collection_sim` startup), and
`pyepics` (the ophyd EPICS layer for config-service consume-mode device injection).

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
