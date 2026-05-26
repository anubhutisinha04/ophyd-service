#!/usr/bin/env bash
#
# IOS demo end-to-end exerciser.
#
# Walks the full periodic-table flow against the ios pod:
#   1. Verify config-service has the IOS happi DB loaded (pgm registered).
#   2. Resolve Ni_L preset friendly addresses → PV names via /api/v1/devices/resolve.
#   3. Apply Ni_L preset values via POST /api/v1/pv/set/batch on direct-control.
#   4. Verify readback on the simulated ios_pgm IOC.
#   5. Trigger a fly scan and verify Sts:Scan-Sts cycles.
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

# PGM prefix the ios_pgm IOC binds at.
PGM_PREFIX='XF:23ID2-OP{Mono}'

# URL-encode a single string (jq's @uri filter handles :, {, } correctly).
encode() { printf '%s' "$1" | jq -sRr @uri; }

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

# ─── Resolve Ni_L preset friendly addresses ──────────────────────────────
step "Resolve Ni_L preset paths"

body='{"addresses":["pgm.energy.setpoint","pgm.fly.start_sig","pgm.fly.stop_sig","pgm.fly.velocity"]}'
status=$(req POST "${CONFIG_URL}/api/v1/devices/resolve" "$body")
expect_status 200 "$status" "POST /api/v1/devices/resolve"

all_ok=$(jq -r '[.resolved[] | .ok] | all' < /tmp/exer_body)
if [ "$all_ok" != "true" ]; then
    note "resolve response: $(cat /tmp/exer_body)"
    fail "one or more addresses failed to resolve"
fi
pass "all four addresses resolved"

PV_ENRGY_SP=$(jq -r '.resolved[]|select(.address=="pgm.energy.setpoint")|.pv_name' < /tmp/exer_body)
PV_START_SP=$(jq -r '.resolved[]|select(.address=="pgm.fly.start_sig")|.pv_name' < /tmp/exer_body)
PV_STOP_SP=$(jq -r '.resolved[]|select(.address=="pgm.fly.stop_sig")|.pv_name' < /tmp/exer_body)
PV_FLY_VELO=$(jq -r '.resolved[]|select(.address=="pgm.fly.velocity")|.pv_name' < /tmp/exer_body)
pass "Enrgy-SP   = ${PV_ENRGY_SP}"
pass "Start-SP   = ${PV_START_SP}"
pass "Stop-SP    = ${PV_STOP_SP}"
pass "FlyVelo-SP = ${PV_FLY_VELO}"

# ─── Register resolved PVs as standalone (workaround) ────────────────────
# The happi loader only indexes top-level device prefixes, not sub-PVs
# derived from Component walks. Direct-control's PV-existence gate
# therefore rejects caputs to Enrgy-SP/Enrgy:Start-SP/etc. Until the
# loader (or the resolver) is taught to auto-register resolved PVs, the
# caller must POST them as standalone. Idempotent: 201 on first call,
# 409 on subsequent. Tracked as Phase 1 follow-up.
step "Register resolved PVs (workaround for loader gap)"

register_pv() {
    local pv=$1
    local body="{\"pv_name\":\"${pv}\",\"access_mode\":\"read-write\",\"description\":\"PGM sub-PV registered by IOS exerciser\"}"
    status=$(req POST "${CONFIG_URL}/api/v1/pvs" "$body")
    case "$status" in
        201) pass "registered ${pv}" ;;
        409) note "already registered ${pv}" ;;
        *)   fail "register ${pv} returned HTTP $status; body=$(cat /tmp/exer_body)" ;;
    esac
}
register_pv "${PV_ENRGY_SP}"
register_pv "${PV_START_SP}"
register_pv "${PV_STOP_SP}"
register_pv "${PV_FLY_VELO}"
# These are touched in the fly-scan step but never resolved through the
# resolver, so they need explicit registration too.
register_pv "${PGM_PREFIX}Enrgy-I"
register_pv "${PGM_PREFIX}Sts:Move-Sts"
register_pv "${PGM_PREFIX}Sts:Scan-Sts"
register_pv "${PGM_PREFIX}Cmd:FlyStart-Cmd.PROC"

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

check_pv() {
    local pv=$1 want=$2 label=$3 tol=${4:-0.05}
    status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$pv")/value")
    expect_status 200 "$status" "GET ${label}"
    got=$(jq -r '.value' < /tmp/exer_body)
    if awk -v g="$got" -v w="$want" -v t="$tol" \
       'BEGIN{d=g-w; exit !((d<=t)&&(d>=-t))}'; then
        pass "${label} = ${got} (≈ ${want})"
    else
        fail "${label} = ${got}, expected ≈${want} (±${tol})"
    fi
}

check_pv "${PV_ENRGY_SP}" "$NI_L_START"    "Enrgy-SP"
check_pv "${PV_START_SP}" "$NI_L_START"    "Enrgy:Start-SP"
check_pv "${PV_STOP_SP}"  "$NI_L_STOP"     "Enrgy:Stop-SP"
check_pv "${PV_FLY_VELO}" "$NI_L_VELOCITY" "Enrgy:FlyVelo-SP"
check_pv "${PGM_PREFIX}Enrgy-I" "$NI_L_START" "Enrgy-I (post-slew)"

# ─── Move-Sts cleared ────────────────────────────────────────────────────
step "Move status"

status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "${PGM_PREFIX}Sts:Move-Sts")/value")
expect_status 200 "$status" "GET Sts:Move-Sts"
got=$(jq -r '.value' < /tmp/exer_body)
case "$got" in
    Done|0)  pass "Sts:Move-Sts = ${got}" ;;
    *)       fail "Sts:Move-Sts = ${got}, expected Done/0" ;;
esac

# ─── Fly scan trigger ────────────────────────────────────────────────────
step "Fly scan (with smoke-test velocity override)"

# Real Ni_L velocity (0.2 eV/s) means 40 eV scan takes 200s — too slow for
# CI. Override to 1000 eV/s; scan completes in ~40ms.
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" \
    "{\"caputs\":[{\"pv_name\":\"${PV_FLY_VELO}\",\"value\":1000}]}")
expect_status 200 "$status" "set FlyVelo=1000"
pass "FlyVelo overridden to 1000 eV/s for smoke"

status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" \
    "{\"caputs\":[{\"pv_name\":\"${PGM_PREFIX}Cmd:FlyStart-Cmd.PROC\",\"value\":1}]}")
expect_status 200 "$status" "trigger FlyStart"
pass "fly scan triggered"

# Scan completes in ~0.04s; 0.5s margin.
sleep 0.5

status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "${PGM_PREFIX}Sts:Scan-Sts")/value")
expect_status 200 "$status" "GET Sts:Scan-Sts"
got=$(jq -r '.value' < /tmp/exer_body)
case "$got" in
    Idle|0)  pass "Sts:Scan-Sts = ${got} (fly scan completed)" ;;
    *)       fail "Sts:Scan-Sts = ${got}, expected Idle/0" ;;
esac

check_pv "${PGM_PREFIX}Enrgy-I" "$NI_L_STOP" "Enrgy-I (fly endpoint)" 0.5

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}configuration_service_ios: ALL CHECKS PASSED${RESET}\n"
