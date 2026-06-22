# Backend services

Three co-equal services, each with its own Dockerfile and README:

| Service | Role | Ports |
|---|---|---|
| `configuration_service/` | Device/PV registry + device locks | 8004 (REST) |
| `direct_control_service/` | Device commanding + PV monitoring | 8003 (REST + WS) |
| `queueserver_service/` | Plan queueing + execution — the merged bluesky-queueserver + bluesky-httpserver in unified mode | 60610 (HTTP + WS), 60615/60625 (0MQ) |

`queueserver_service/` is based on bluesky-queueserver + bluesky-httpserver
(imported from the `merge/httpserver` unification work) and is maintained
in-tree as an independent service — it does not track the upstream
bluesky-queueserver repo, and changes here are not merged back. Upstream
remains the community package; this service is free to diverge, with one
exception: the wire contracts consumed by the **bluesky-queueserver-api**
client library (0MQ protocol + HTTP/WS API) stay compatible — see the
service README.

All three run together in `integration/pods/with-queueserver/`. The root
`docker-compose.yml` inner loop runs only the first two.
