# Simulated IOS IOCs + running the E_ramp plan

This branch (`simple-queueserver-ios-iocs`) builds on the generic
`queueserver-repro` setup by adding the simulated IOS EPICS IOCs from the
`demo/ios-nsls2` work and everything else needed to actually **run plans**
against the IOS profile collection — including the beamline's `E_ramp` plan.

`reproduce.sh up` now also:

- runs the six purpose-built IOS caproto IOCs (`iocs/ioc_ios_*.py`): `pgm`,
  `curramp`, `epu`, `vortex`, `scaler`, `feedback` — each a separate Channel
  Access server on its own port (`5064`, `5066`, …);
- runs a catch-all **blackhole** IOC (`iocs/blackhole_ioc.py`) for every other
  PV the profile's ~100 devices force-connect at startup;
- runs a **kafka** broker (RunEngine document publishing) and a minimal
  **mock Olog** server (`iocs/mock_olog.py`, the profile's logbook callback).

The RE worker reaches the IOCs through an explicit `EPICS_CA_ADDR_LIST` with
`EPICS_CA_AUTO_ADDR_LIST=NO` — the analog of the queueserver VM's dedicated,
broadcast-disabled EPICS NIC (the VM is dual-homed: a Data Services NIC for
redis/tiled/mongo/kafka/httpserver and an EPICS-only NIC to the IOC hosts).

## Realistic IOCs + catch-all, without a Channel Access race

The six realistic IOCs only cover their own device families; the profile
force-connects far more PVs than that. So a blackhole answers everything else.
To avoid two servers claiming the same PV (a non-deterministic CA race), each
realistic IOC is started with `--list-pvs`, its exact PV names are harvested,
and the blackhole is told to **defer on exactly those PVs** (and only those).
Result: realistic values where a real IOC exists (e.g. the scaler's `.TP`, the
mono energy), spoofed zeros everywhere else, and the whole profile opens fast.

## What it takes to RUN a plan (beyond opening the profile)

Opening the profile needs redis (×2), mongo, a tiled `ios` profile, a kafka
config, and `~/.pyOlog.conf`. **Running** a plan needs three more things, each
discovered by running `count` / `E_ramp` and watching where it stalled:

| Requirement | Why | Provided by |
|-------------|-----|-------------|
| Kafka **broker** on `:9092` | the RunEngine publishes documents to Kafka; the publisher blocks on a dead broker | `WITH_KAFKA=1` (KRaft broker container) |
| Responsive **Olog** server | the profile subscribes an Olog logbook callback that POSTs on every run start; no server → the run errors/blocks | `WITH_OLOG=1` (`iocs/mock_olog.py`) |
| Scaler `.CONT` record | bluesky stages the scaler by setting count-mode to one-shot; the set must confirm | added to `iocs/ioc_ios_scaler.py` |

## E_ramp-specific fidelity

`E_ramp` (in the profile's `98-ramp.py`) is a fly scan: it sets the mono fly
start/stop/velocity, triggers `pgm.fly.fly_start`, then `ramp_plan` reads the
detectors while the energy ramps, finishing when `pgm.fly.scan_status`
transitions to **`Ready`**. The stock `ioc_ios_pgm.py` only had `Idle`/`Scanning`
states, so `ramp_plan` never saw the done event. `scan_status` now includes a
`Ready` state and rests/returns there, so `E_ramp` completes.

## Run it

```bash
cd integration/queueserver-repro
./reproduce.sh up            # profile + infra + IOCs + kafka + mock Olog + services
```

Then drive a plan through the HTTP API (API key is printed by `up`, also in
`$QS_REPRO_HOME/config/secrets.env`):

```bash
KEY=$(sed -n 's/^HTTP_API_KEY=//p' "$HOME/qs-repro/config/secrets.env")
BASE=http://localhost:60610

# a simple count on the (realistic) scaler
curl -s -X POST $BASE/api/queue/item/execute \
  -H "Authorization: ApiKey $KEY" -H "Content-Type: application/json" \
  -d '{"item":{"name":"count","args":[["sclr"]],"kwargs":{"num":3},"item_type":"plan"}}'

# the beamline E_ramp fly scan: E_ramp(dets, start_eV, stop_eV, velocity_eV_s)
curl -s -X POST $BASE/api/queue/item/execute \
  -H "Authorization: ApiKey $KEY" -H "Content-Type: application/json" \
  -d '{"item":{"name":"E_ramp","args":[["sclr"],850,852,0.5],"item_type":"plan"}}'
```

Watch progress with `./reproduce.sh status` and `./reproduce.sh logs`.

Verified end to end: `E_ramp([sclr], 850, 852, 0.5)` completes with
`exit_status=success`, ~130 event documents captured, the mono energy readback
ramps 850 → 852 eV, and `Sts:Scan-Sts` returns to `Ready`.

## Toggles (environment variables)

`WITH_IOS_IOCS`, `WITH_REALISTIC_IOCS`, `WITH_BLACKHOLE`, `WITH_KAFKA`,
`WITH_OLOG`, `IOC_BASE_PORT`, `KAFKA_PORT`, `OLOG_PORT` — see the header of
`reproduce.sh` for the full list and defaults.

## Note on the IOC changes

`iocs/ioc_ios_pgm.py` (added a `Ready` scan-status state) and
`iocs/ioc_ios_scaler.py` (added the `.CONT` count-mode record) are lightly
adapted from the `demo/ios-nsls2` originals so the profile's plans stage and
complete under the queueserver. `iocs/blackhole_ioc.py` is ported from the
phase-1 `demo/ios-nsls2-queueserver` pod with exact-PV exclusion added.
