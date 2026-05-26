#!/usr/bin/env bash
#
# IOS demo end-to-end exerciser.
#
# Walks the full periodic-table flow against the ios pod:
#   1. Verify config-service has the IOS happi DB loaded (pgm registered).
#   2. Resolve every PGM address (setpoint, readback, fly trigger, status,
#      fly scan params) via /api/v1/devices/resolve — no hardcoded prefixes.
#   3. Register the resolved PVs as standalone (workaround for the happi
#      loader gap, see tech-debt ledger). Cleanup deletes them on exit.
#   4. Apply Ni_L preset values via POST /api/v1/pv/set/batch on direct-control.
#   5. Verify readback on the simulated ios_pgm IOC.
#   6. Trigger a fly scan and verify Sts:Scan-Sts cycles.
#
# Pairs with integration/pods/ios/docker-compose.yaml. Ni_L values come from
# integration/happi/sites/ios/edge_map.json.
#
# Usage:
#   ./configuration_service_ios.sh
#   DIRECT_URL=http://remote:8003 CONFIG_URL=http://remote:8004 ./configuration_service_ios.sh

set -euo pipefail

. "$(dirname "$0")/_exerciser_lib.sh"

DIRECT_URL="${DIRECT_URL:-http://localhost:8003}"
CONFIG_URL="${CONFIG_URL:-http://localhost:8004}"

# Ni_L preset (must match edge_map.json Ni_L entry).
NI_L_START=845
NI_L_STOP=885
NI_L_VELOCITY=0.2

# URL-encode (jq's @uri handles :, {, } correctly for path segments).
encode() { printf '%s' "$1" | jq -sRr @uri; }

# Cleanup: DELETE every standalone PV we successfully registered, so the
# registry stays clean across runs and a failed run doesn't leak entries
# into /api/v1/registry/export. Runs on EXIT (success or failure path).
REGISTERED_PVS=()

cleanup() {
    if [ ${#REGISTERED_PVS[@]} -eq 0 ]; then
        return
    fi
    printf "  (cleanup) deleting %d registered standalone PVs\n" \
        "${#REGISTERED_PVS[@]}"
    for pv in "${REGISTERED_PVS[@]}"; do
        curl -s -o /dev/null -X DELETE \
            "${CONFIG_URL}/api/v1/pvs/standalone/$(encode "$pv")" || true
    done
}
CLEANUP_FN=cleanup
trap cleanup EXIT

# register_pv PV ACCESS_MODE
# POST a standalone PV with the given access mode. Tracks successful
# registrations in REGISTERED_PVS so cleanup can DELETE them.
register_pv() {
    local pv=$1 access_mode=$2
    local body status
    body=$(jq -n --arg pv "$pv" --arg am "$access_mode" '{
        pv_name: $pv,
        access_mode: $am,
        description: "PGM sub-PV registered by IOS exerciser"
    }')
    status=$(req POST "${CONFIG_URL}/api/v1/pvs" "$body")
    case "$status" in
        201) REGISTERED_PVS+=("$pv")
             pass "registered ${pv} (${access_mode})" ;;
        409) note "already registered ${pv} (not tracked for cleanup — likely residue from prior run; restart pod to clear)" ;;
        *)   fail "register ${pv} returned HTTP $status; body=$(cat /tmp/exer_body)" ;;
    esac
}

# check_pv PV WANT LABEL [TOL]
# Read a PV and assert numeric equality within tolerance. Bails hard if the
# value isn't numeric (jq null / empty / enum string) so a misconfigured
# check can't silently coerce-to-zero in awk.
check_pv() {
    local pv=$1 want=$2 label=$3 tol=${4:-0.05}
    local status got
    status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$pv")/value")
    expect_status 200 "$status" "GET ${label}"
    got=$(jq -r '.value' < /tmp/exer_body)
    if ! [[ "$got" =~ ^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$ ]]; then
        fail "${label} = '${got}' is not numeric (cannot compare to ${want})"
    fi
    if awk -v g="$got" -v w="$want" -v t="$tol" \
       'BEGIN{d=g-w; exit !((d<=t)&&(d>=-t))}'; then
        pass "${label} = ${got} (≈ ${want})"
    else
        fail "${label} = ${got}, expected ≈${want} (±${tol})"
    fi
}

printf "${BOLD}configuration_service_ios exerciser${RESET}  direct=${DIRECT_URL} config=${CONFIG_URL}\n"

# ─── Health ──────────────────────────────────────────────────────────────
step "Health"

status=$(req GET "${DIRECT_URL}/health")
expect_status 200 "$status" "direct-control /health"
pass "direct-control /health"

status=$(req GET "${CONFIG_URL}/health")
expect_status 200 "$status" "config-service /health"
pass "config-service /health"

# ─── IOS registry sanity ─────────────────────────────────────────────────
step "IOS registry sanity"

status=$(req GET "${CONFIG_URL}/api/v1/devices/pgm")
expect_status 200 "$status" "device pgm registered"
device_class=$(jq -r '.ophyd_class // .device_class // "?"' < /tmp/exer_body)
pass "pgm registered  class=${device_class}"

# ─── Resolve every PGM address via config-service ────────────────────────
# All eight PVs are declared as Components on ios_devs.PGM, so they all
# resolve through the same endpoint. No hardcoded prefix — a future
# prefix change in happi_db.json would automatically flow through.
step "Resolve all PGM addresses"

body='{"addresses":[
  "pgm.energy.setpoint",
  "pgm.energy.readback",
  "pgm.fly.start_sig",
  "pgm.fly.stop_sig",
  "pgm.fly.velocity",
  "pgm.fly.fly_start",
  "pgm.fly.scan_status",
  "pgm.move_status"
]}'
status=$(req POST "${CONFIG_URL}/api/v1/devices/resolve" "$body")
expect_status 200 "$status" "POST /api/v1/devices/resolve"

all_ok=$(jq -r '[.resolved[] | .ok] | all' < /tmp/exer_body)
if [ "$all_ok" != "true" ]; then
    note "resolve response: $(cat /tmp/exer_body)"
    fail "one or more addresses failed to resolve"
fi
# `all` over zero rows is true — verify the server actually returned 8 rows
# so a dropped/missing entry can't slip past as "all ok" of nothing.
EXPECTED_RESOLVED=8
actual_resolved=$(jq -r '.resolved | length' < /tmp/exer_body)
if [ "$actual_resolved" != "$EXPECTED_RESOLVED" ]; then
    note "resolve response: $(cat /tmp/exer_body)"
    fail "resolve returned $actual_resolved rows, expected $EXPECTED_RESOLVED"
fi
pass "all $EXPECTED_RESOLVED addresses resolved"

resolve_body=$(cat /tmp/exer_body)
# pv_for fails hard if the address didn't match a row or the pv_name is null.
# Returning "" or the literal "null" silently would propagate downstream into
# register_pv / check_pv and bury the cause far from the typo.
pv_for() {
    local out
    out=$(printf '%s' "$resolve_body" \
        | jq -r --arg a "$1" '.resolved[]|select(.address==$a)|.pv_name')
    if [ -z "$out" ] || [ "$out" = "null" ]; then
        fail "pv_for: '$1' did not resolve to a non-null PV name"
    fi
    printf '%s' "$out"
}

PV_ENRGY_SP=$(pv_for "pgm.energy.setpoint")
PV_ENRGY_I=$(pv_for "pgm.energy.readback")
PV_START_SP=$(pv_for "pgm.fly.start_sig")
PV_STOP_SP=$(pv_for "pgm.fly.stop_sig")
PV_FLY_VELO=$(pv_for "pgm.fly.velocity")
PV_FLY_START=$(pv_for "pgm.fly.fly_start")
PV_SCAN_STS=$(pv_for "pgm.fly.scan_status")
PV_MOVE_STS=$(pv_for "pgm.move_status")

pass "Enrgy-SP   = ${PV_ENRGY_SP}"
pass "Enrgy-I    = ${PV_ENRGY_I}"
pass "Start-SP   = ${PV_START_SP}"
pass "Stop-SP    = ${PV_STOP_SP}"
pass "FlyVelo-SP = ${PV_FLY_VELO}"
pass "FlyStart   = ${PV_FLY_START}"
pass "Scan-Sts   = ${PV_SCAN_STS}"
pass "Move-Sts   = ${PV_MOVE_STS}"

# ─── Register resolved PVs (workaround for loader gap) ───────────────────
# Happi loader indexes only top-level device prefixes, not sub-PVs derived
# from Component walks; direct-control's existence gate therefore 404s
# Enrgy-SP/etc. Register them as standalone with the access mode the IOC
# actually serves (so the registry doesn't lie). Cleanup deletes on exit.
# Tracked as tech-debt: real fix is loader.py or resolver auto-registration.
step "Register resolved PVs (workaround for loader gap)"

register_pv "$PV_ENRGY_SP"  read-write
register_pv "$PV_START_SP"  read-write
register_pv "$PV_STOP_SP"   read-write
register_pv "$PV_FLY_VELO"  read-write
register_pv "$PV_FLY_START" read-write

# Read-only at the IOC (declared with read_only=True). Register accordingly
# so a future access-mode-gating wireup doesn't see a writability lie here.
register_pv "$PV_ENRGY_I"   read-only
register_pv "$PV_SCAN_STS"  read-only
register_pv "$PV_MOVE_STS"  read-only

# ─── Apply Ni_L preset via batch caput ───────────────────────────────────
step "Apply Ni_L preset (batch caput)"

body=$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_ENRGY_SP}","value":${NI_L_START}},
  {"pv_name":"${PV_START_SP}","value":${NI_L_START}},
  {"pv_name":"${PV_STOP_SP}","value":${NI_L_STOP}},
  {"pv_name":"${PV_FLY_VELO}","value":${NI_L_VELOCITY}}
]}
EOF
)
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$body")
expect_status 200 "$status" "POST /api/v1/pv/set/batch"

ok=$(jq -r '.ok' < /tmp/exer_body)
applied=$(jq -r '.applied' < /tmp/exer_body)
if [ "$ok" != "true" ] || [ "$applied" != "4" ]; then
    note "batch response: $(cat /tmp/exer_body)"
    fail "batch caput ok=${ok} applied=${applied} (expected ok=true applied=4)"
fi
pass "batch caput applied=${applied}/4"

# ios_pgm slews at 50 eV/s; pre-position from 850→845 finishes in ~0.1s.
sleep 1

# ─── Readback verification ───────────────────────────────────────────────
step "Readback verification"

check_pv "$PV_ENRGY_SP" "$NI_L_START"    "Enrgy-SP"
check_pv "$PV_START_SP" "$NI_L_START"    "Enrgy:Start-SP"
check_pv "$PV_STOP_SP"  "$NI_L_STOP"     "Enrgy:Stop-SP"
check_pv "$PV_FLY_VELO" "$NI_L_VELOCITY" "Enrgy:FlyVelo-SP"
check_pv "$PV_ENRGY_I"  "$NI_L_START"    "Enrgy-I (post-slew)"

# ─── Move-Sts cleared ────────────────────────────────────────────────────
step "Move status"

status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$PV_MOVE_STS")/value")
expect_status 200 "$status" "GET Sts:Move-Sts"
got=$(jq -r '.value' < /tmp/exer_body)
case "$got" in
    Done|0)  pass "Sts:Move-Sts = ${got}" ;;
    *)       fail "Sts:Move-Sts = ${got}, expected Done/0" ;;
esac

# ─── Fly scan trigger ────────────────────────────────────────────────────
step "Fly scan (with smoke-test velocity override)"

# Real Ni_L velocity (0.2 eV/s) means 40 eV scan takes 200s — too slow for
# CI. Override to 1000 eV/s; one TICK_S iteration covers the whole 40 eV
# range so the scan finishes in ~100ms.
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" \
    "{\"caputs\":[{\"pv_name\":\"${PV_FLY_VELO}\",\"value\":1000}]}")
expect_status 200 "$status" "set FlyVelo=1000"
pass "FlyVelo overridden to 1000 eV/s for smoke"

status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" \
    "{\"caputs\":[{\"pv_name\":\"${PV_FLY_START}\",\"value\":1}]}")
expect_status 200 "$status" "trigger FlyStart"
pass "fly scan triggered"

# Scan completes in ~100ms; 0.5s margin.
sleep 0.5

status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$PV_SCAN_STS")/value")
expect_status 200 "$status" "GET Sts:Scan-Sts"
got=$(jq -r '.value' < /tmp/exer_body)
case "$got" in
    Idle|0)  pass "Sts:Scan-Sts = ${got} (fly scan completed)" ;;
    *)       fail "Sts:Scan-Sts = ${got}, expected Idle/0" ;;
esac

check_pv "$PV_ENRGY_I" "$NI_L_STOP" "Enrgy-I (fly endpoint)" 0.5

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}configuration_service_ios: ALL CHECKS PASSED${RESET}\n"
