# IOS demo pod

**NSLS-II-specific development/demonstration asset.** Lives on the
`demo/ios-nsls2` branch only — never merged to upstream community
`main`. See the project memo for the why (`feedback_ios_demo_not_upstream`
in the workspace memory dir).

This pod brings up six simulated caproto IOCs serving the PVs that the
IOS happi database references, behind the standard ophyd-service stack
(configuration_service + direct_control_service). The exerciser walks
the full periodic-table → batch caput → IOC dynamics → readback
pipeline against the simulated hardware, so the IOS Ni_L preset (and
others) can be tested end-to-end without a real beamline.

## Quick start

From the repo root:

```bash
./integration/pods/ios/run_demo.sh
```

That builds the images, brings the pod up, waits for everything healthy,
runs the exerciser, leaves the pod running. ~30s on a warm cache, ~2 min
cold.

Common flags:

```bash
./integration/pods/ios/run_demo.sh --rebuild      # force --build
./integration/pods/ios/run_demo.sh --tear-down    # exerciser then down -v
./integration/pods/ios/run_demo.sh --skip-exerciser  # just up the pod
./integration/pods/ios/run_demo.sh --logs         # tail direct-control logs after
```

To tear down by hand:

```bash
docker compose -f integration/pods/ios/docker-compose.yaml down
```

## Architecture

```
                         ┌──────────────────┐
                         │  configuration_  │  loads IOS happi DB
                         │  service :8004   │  (sites/ios/happi_db.json)
                         └────────┬─────────┘
                                  │ /api/v1/devices/resolve
                                  ▼
  exerciser  ──HTTP──▶  ┌──────────────────┐
  (Ni_L     ──HTTP──▶   │  direct_control_ │  pyepics CA caput/caget
   preset)              │  service :8003   │
                        └────────┬─────────┘
                                 │ Channel Access (UDP 5064 + TCP 5064)
                                 ▼
              ┌───────┬──────────┬─────────┬───────┬─────────┬──────────┐
              │ pgm   │ curramp  │ epu     │ vortex│ scaler  │ feedback │
              │       │          │         │       │         │          │
              │ slew  │ enum     │ echo +  │ Poisson│ preset-│ P-loop  │
              │ +fly  │ echo     │ FLT calc│ MCA + │ time    │ converge│
              │       │          │         │ ROI    │ count   │          │
              └───────┴──────────┴─────────┴───────┴─────────┴──────────┘
              caproto IOCs serving the IOS PV prefixes
```

Six IOCs, all from the same `integration/ioc/Dockerfile`, distinguished
by the script-per-service `command:` override in
`docker-compose.yaml`.

## What each IOC simulates

| IOC | Prefix | Behaviour | Phase added |
|---|---|---|---|
| `ios_pgm` | `XF:23ID2-OP{Mono}` | Enrgy-I slews to Enrgy-SP at 50 eV/s; fly-scan sweeps Start→Stop at FlyVelo; Move-Sts / Scan-Sts reflect motion state; Cmd:Stop-Cmd aborts (slow-move + fly) | 1 |
| `ios_curramp` | `XF:23ID2-ES{CurrAmp:N}` (N=1,2,3) | Three SR570-style channels with Gain:Val-SP + Gain:Decade-SP enum PVs (echo) | 2 |
| `ios_epu` | `XF:23ID-ID{EPU:1\|2}` | Gap/Phase positioners (echo), EPU1 also serves FLT/RLT interpolators (Out1-I = Inp1-SP + InpOff1-SP) and Val:Table-Sel | 2+3 |
| `ios_vortex` | `XF:23ID2-ES{Vortex}mca1` | 2048-channel spectrum (Poisson-noise template with two simulated peaks at ch 300 and 600); 8 ROIs (R0..R7 LO/HI + summed scalars); spectrum re-rolls on PRTM write | 3 |
| `ios_scaler` | `XF:23ID2-ES{Sclr:1}` | synApps scaler with .CNT/.TP preset-time counting; S1=10 MHz clock, S2-S4 at simulated rates; CNT auto-clears when T reaches TP | 3 |
| `ios_feedback` | `XF:23ID2-OP{FBck}` | M1B1 PID — when Sts:FB-Sel='On', a P-only loop closes ~16% of (PID-SP − CVAL) per 100 ms tick until error ≤ deadband | 3 |

All prefixes match the literal PV names declared in
`integration/happi/sites/ios/ios_devs.py` (the ophyd device-class shim
ported from `ios-profile-collection`).

## What the exerciser proves

`integration/exercise/configuration_service_ios.sh` runs 38 checks:

| Block | Verifies |
|---|---|
| Health + registry sanity | both backends up; `pgm` device registered from happi DB |
| Resolve all 29 addresses | config-service walks ios_devs.PGM/CurrAmp/EPU/Vortex/Scaler classes and returns the right PV names |
| Apply Ni_L preset (PGM) | batch caput Start/Stop/FlyVelo + slow-move pre-position |
| Readback (PGM) | values stored; Enrgy-I slewed to setpoint |
| Fly scan | trigger Cmd:FlyStart-Cmd.PROC, watch Sts:Scan-Sts cycle, verify Enrgy-I reached endpoint |
| Apply Ni_L (CurrAmp + EPU) | string-enum caputs for gain/decade; numeric caputs for EPU1 table/offset/deadband |
| EPU FLT calc | caput Inp1-SP + InpOff1-SP, verify Out1-I = sum (Phase 3 dynamic) |
| Vortex ROI | caput PRTM + R2 bounds spanning the simulated peak, verify R2 sum non-trivially integrated |
| Scaler | caput TP + CNT=1, wait, verify CNT auto-cleared + S1 ≈ 1e7 × TP |
| Feedback PID | caput SP + enable=On, wait, verify CVAL converged to SP |

Sub-PVs (`Enrgy-SP`, `Cmd:FlyStart-Cmd.PROC`, MCA ROI scalars, etc.)
are indexed automatically by the loader, which walks every happi
entry's Device class on cold start. No standalone-PV CRUD dance
required — direct-control's existence gate sees them as soon as the
pod is healthy.

## Quick tour (poke at it by hand)

After `./run_demo.sh --skip-exerciser` (or after a regular run leaves
the pod up), you can hit the services directly:

```bash
# Where things are
curl -s http://localhost:8004/health | jq '.devices_loaded'
curl -s http://localhost:8004/api/v1/devices/pgm | jq '.ophyd_class'
docker compose -f integration/pods/ios/docker-compose.yaml ps

# Resolve a friendly happi address to its PV name
curl -s -X POST http://localhost:8004/api/v1/devices/resolve \
  -H 'Content-Type: application/json' \
  -d '{"addresses":["pgm.energy.setpoint","vortex.mca.rois.roi2.lo_chan"]}' \
  | jq '.resolved'

# Watch the PGM IOC slew Enrgy-I toward Enrgy-SP in real time
# (sub-PVs are auto-indexed by the loader from the happi entry's Device
# class — no manual registration needed)
ENC='XF%3A23ID2-OP%7BMono%7DEnrgy-I'
while sleep 0.5; do
    curl -s "http://localhost:8003/api/v1/pv/${ENC}/value" | jq -r '.value'
done

# Watch the vortex spectrum + ROI sums after triggering acquisition

# Tail the IOC logs
docker compose -f integration/pods/ios/docker-compose.yaml logs -f ios_pgm
docker compose -f integration/pods/ios/docker-compose.yaml logs -f ios_vortex
```

For a different edge, the values come from
`integration/happi/sites/ios/edge_map.json`. The exerciser hardcodes
Ni_L; to test another edge by hand, look up its `start`/`stop`/
`velocity`/`epu_table`/`deadband` and caput the relevant PVs.

## Known caveats

These are real bugs in the surrounding services that the exerciser
works around. They're tracked in the workspace's `project_technical_debt`
memo with concrete fix recipes.

1. **direct-control fire-and-forget masks IOC putter rejections.**
   The default `POST /api/v1/pv/set/batch` mode is fire-and-forget;
   direct-control returns `success=true` even when caproto rejects
   the CA write (e.g., when an IOC putter raises ValueError or
   CannotExceedLimits). The IOC IS rejecting (visible in
   `docker compose logs`), but HTTP says OK. Per-PV `success=true`
   in the batch response should be treated as "issued", not
   "applied". Verify via readback or via `wait=true` mode.

## Troubleshooting

- **All 6 IOCs healthy but direct-control fails to start**: usually
  means one IOC took too long to bind the CA port. Check
  `docker compose ps` — if any IOC is still "starting", give it more
  time. `docker compose logs <ioc>` shows startup progress.
- **Exerciser fails at "scaler S1"** with a tiny value: the count
  may not have completed within the `TP + margin` sleep. Bump
  `PHASE3_TP` in the script or extend the sleep margin.

## Why this exists separately from `main`

ophyd-service is a community-aimed project (configuration_service /
direct_control / pods/minimal/full/dev / frontend are for any bluesky
facility worldwide). The IOS demo bakes in NSLS-II conventions (literal
`XF:23ID*` PV prefixes, beamline-specific gain steps, IOS happi
catalog) that don't generalize.

The demo informs design decisions but doesn't ship to the community
codebase. Backend fixes the demo surfaces (e.g., the pv_name
validator that became PR #22 upstream) are extracted to focused PRs
against `main`; the demo itself stays here.

When the demo stops being useful, `git branch -D demo/ios-nsls2`
clears it.
