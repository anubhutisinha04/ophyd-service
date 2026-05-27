#!/usr/bin/env bash
#
# IOS demo end-to-end exerciser.
#
# Walks the full periodic-table flow against the ios pod (Phases 1 + 2 + 3):
#   1. Verify config-service has the IOS happi DB loaded (pgm registered).
#   2. Resolve every PGM + CurrAmp + EPU + Vortex + Scaler + Feedback
#      address via /api/v1/devices/resolve.
#   3. Apply Ni_L preset values via POST /api/v1/pv/set/batch on direct-control:
#        - PGM scan params (Phase 1, slew dynamics)
#        - CurrAmp gain/decade (Phase 2, echo)
#        - EPU1 table / offset / deadband (Phase 2, echo)
#   4. Verify readback against ios_pgm + ios_curramp + ios_epu IOCs.
#   5. Trigger a fly scan and verify Sts:Scan-Sts cycles.
#   6. Phase 3 dynamics:
#        - EPU FLT calc: write input + offset, verify output = sum
#        - Vortex MCA: write ROI bounds + PRTM, verify ROI sum > 0
#        - Scaler: write TP, trigger CNT, verify auto-clear + channel counts
#        - Feedback PID: enable + setpoint, verify CVAL converges
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

# Ni_L preset values (PGM scan params come from edge_map.json Ni_L entry;
# detector+EPU values are representative — det_settings.xlsx isn't loaded).
NI_L_START=845
NI_L_STOP=885
NI_L_VELOCITY=0.2
NI_L_EPU_TABLE=4
NI_L_EPU_OFFSET=100.0
NI_L_EPU_DEADBAND=12
NI_L_SAMPLE_GAIN="1"
NI_L_SAMPLE_DECADE="1 nA/V"
NI_L_AUMESH_GAIN="1"
NI_L_AUMESH_DECADE="100 pA/V"
NI_L_PD_GAIN="1"
NI_L_PD_DECADE="1 nA/V"

# Phase 3 fixture values. Vortex ROI2 covers the simulated peak at channel
# 300 (see ios_vortex.py's _mean_rates). FLT calc verifies linear-sum
# dynamic. Feedback test waits ~2s for the PID loop to converge.
PHASE3_R2_LO=250
PHASE3_R2_HI=350
PHASE3_PRTM=2.0
PHASE3_TP=0.5
PHASE3_FLT_INPUT=5.0
PHASE3_FLT_OFFSET=10.0
PHASE3_FLT_OUTPUT=15.0   # = input + offset
PHASE3_PID_SP=5.0

# URL-encode (jq's @uri handles :, {, } correctly for path segments).
encode() { printf '%s' "$1" | jq -sRr @uri; }

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

# check_str_pv PV WANT LABEL
# Like check_pv but for enum/string PVs (e.g. CurrAmp gain/decade). Accepts
# either the string form or the numeric enum index — direct-control's value
# endpoint reports the integer index for ENUM PVs by default, so the want
# value may need to be the index rather than the string.
check_str_pv() {
    local pv=$1 want=$2 label=$3
    local status got
    status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$pv")/value")
    expect_status 200 "$status" "GET ${label}"
    got=$(jq -r '.value' < /tmp/exer_body)
    if [ "$got" = "$want" ]; then
        pass "${label} = ${got}"
    else
        fail "${label} = ${got}, expected ${want}"
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

# ─── Resolve every PGM + CurrAmp + EPU address via config-service ────────
# PGM addresses are dotted paths on ios_devs.PGM. CurrAmp + EPU
# (epu1table, epu1offset) are top-level happi entries (EpicsSignal at the
# specified prefix). epu1.flt.output_deadband is a sub-component path.
# No hardcoded prefixes anywhere — a happi_db.json prefix change flows
# through automatically.
step "Resolve all PGM + CurrAmp + EPU addresses"

body='{"addresses":[
  "pgm.energy.setpoint",
  "pgm.energy.readback",
  "pgm.fly.start_sig",
  "pgm.fly.stop_sig",
  "pgm.fly.velocity",
  "pgm.fly.fly_start",
  "pgm.fly.scan_status",
  "pgm.move_status",
  "sample_sclr_gain",
  "sample_sclr_decade",
  "aumesh_sclr_gain",
  "aumesh_sclr_decade",
  "pd_sclr_gain",
  "pd_sclr_decade",
  "epu1table",
  "epu1offset",
  "epu1.flt.output_deadband",
  "epu1.flt.input",
  "epu1.flt.input_offset",
  "epu1.flt.output",
  "vortex.mca.preset_real_time",
  "vortex.mca.rois.roi2.lo_chan",
  "vortex.mca.rois.roi2.hi_chan",
  "vortex.mca.rois.roi2.count",
  "sclr.count",
  "sclr.time",
  "sclr.channels.chan1",
  "sclr_time",
  "m1b1_setpoint"
]}'
# m1b1.fbl.* paths intentionally omitted: the resolver doesn't honor
# `add_prefix=""` on Components (FeedbackLoop's literal prefix gets
# concatenated onto M1bMirror's). Hardcoded below as a workaround;
# tracked as resolver tech debt.
status=$(req POST "${CONFIG_URL}/api/v1/devices/resolve" "$body")
expect_status 200 "$status" "POST /api/v1/devices/resolve"

all_ok=$(jq -r '[.resolved[] | .ok] | all' < /tmp/exer_body)
if [ "$all_ok" != "true" ]; then
    note "resolve response: $(cat /tmp/exer_body)"
    fail "one or more addresses failed to resolve"
fi
# `all` over zero rows is true — verify the server actually returned the
# expected row count so a dropped/missing entry can't slip past as
# "all ok" of nothing.
EXPECTED_RESOLVED=29
actual_resolved=$(jq -r '.resolved | length' < /tmp/exer_body)
if [ "$actual_resolved" != "$EXPECTED_RESOLVED" ]; then
    note "resolve response: $(cat /tmp/exer_body)"
    fail "resolve returned $actual_resolved rows, expected $EXPECTED_RESOLVED"
fi
pass "all $EXPECTED_RESOLVED addresses resolved"

resolve_body=$(cat /tmp/exer_body)
# pv_for fails hard if the address didn't match a row, the pv_name is null,
# OR jq emitted multiple rows. The multi-row case would otherwise produce a
# newline-joined string that flows into batch caput bodies, which would
# either be silently malformed JSON or pass an unaddressable PV name to
# direct-control — either way the failure surfaces far from the cause.
pv_for() {
    local out
    out=$(printf '%s' "$resolve_body" \
        | jq -r --arg a "$1" '.resolved[]|select(.address==$a)|.pv_name')
    if [ -z "$out" ] || [ "$out" = "null" ]; then
        fail "pv_for: '$1' did not resolve (no row matched or pv_name is null)"
    fi
    if [[ "$out" == *$'\n'* ]]; then
        fail "pv_for: '$1' resolved to multiple rows: $(printf '%s' "$out" | tr '\n' '|')"
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

# CurrAmp (top-level EpicsSignals — resolver returns the prefix directly)
PV_SAMPLE_GAIN=$(pv_for "sample_sclr_gain")
PV_SAMPLE_DECADE=$(pv_for "sample_sclr_decade")
PV_AUMESH_GAIN=$(pv_for "aumesh_sclr_gain")
PV_AUMESH_DECADE=$(pv_for "aumesh_sclr_decade")
PV_PD_GAIN=$(pv_for "pd_sclr_gain")
PV_PD_DECADE=$(pv_for "pd_sclr_decade")

# EPU1
PV_EPU_TABLE=$(pv_for "epu1table")
PV_EPU_OFFSET=$(pv_for "epu1offset")
PV_EPU_DEADBAND=$(pv_for "epu1.flt.output_deadband")

# Phase 3: EPU FLT calc inputs/output
PV_EPU_FLT_INPUT=$(pv_for "epu1.flt.input")
PV_EPU_FLT_OFFSET=$(pv_for "epu1.flt.input_offset")
PV_EPU_FLT_OUTPUT=$(pv_for "epu1.flt.output")

# Phase 3: Vortex MCA (PRTM + ROI2 bounds and sum)
PV_VORTEX_PRTM=$(pv_for "vortex.mca.preset_real_time")
PV_VORTEX_R2_LO=$(pv_for "vortex.mca.rois.roi2.lo_chan")
PV_VORTEX_R2_HI=$(pv_for "vortex.mca.rois.roi2.hi_chan")
PV_VORTEX_R2_SUM=$(pv_for "vortex.mca.rois.roi2.count")

# Phase 3: Scaler
PV_SCLR_CNT=$(pv_for "sclr.count")
PV_SCLR_T=$(pv_for "sclr.time")
PV_SCLR_S1=$(pv_for "sclr.channels.chan1")
PV_SCLR_TP=$(pv_for "sclr_time")

# Phase 3: Feedback. m1b1_setpoint resolves via the top-level happi entry.
# Sts:FB-Sel and PID.CVAL are hardcoded — the resolver doesn't honor
# `add_prefix=""` on the FeedbackLoop Component, so walking m1b1.fbl.*
# produces garbage prefixes. Resolver fix tracked separately; hardcoding
# matches the literal FBck prefix that the IOC actually serves.
PV_FB_SP=$(pv_for "m1b1_setpoint")
PV_FB_ENABLE='XF:23ID2-OP{FBck}Sts:FB-Sel'
PV_FB_CVAL='XF:23ID2-OP{FBck}PID.CVAL'

pass "Enrgy-SP        = ${PV_ENRGY_SP}"
pass "Enrgy-I         = ${PV_ENRGY_I}"
pass "Start-SP        = ${PV_START_SP}"
pass "Stop-SP         = ${PV_STOP_SP}"
pass "FlyVelo-SP      = ${PV_FLY_VELO}"
pass "FlyStart        = ${PV_FLY_START}"
pass "Scan-Sts        = ${PV_SCAN_STS}"
pass "Move-Sts        = ${PV_MOVE_STS}"
pass "sample_gain     = ${PV_SAMPLE_GAIN}"
pass "sample_decade   = ${PV_SAMPLE_DECADE}"
pass "aumesh_gain     = ${PV_AUMESH_GAIN}"
pass "aumesh_decade   = ${PV_AUMESH_DECADE}"
pass "pd_gain         = ${PV_PD_GAIN}"
pass "pd_decade       = ${PV_PD_DECADE}"
pass "epu1_table      = ${PV_EPU_TABLE}"
pass "epu1_offset     = ${PV_EPU_OFFSET}"
pass "epu1_deadband   = ${PV_EPU_DEADBAND}"

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

# ─── Apply Ni_L CurrAmp + EPU preset (batch caput) ───────────────────────
step "Apply Ni_L CurrAmp + EPU preset (batch caput)"

body=$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_SAMPLE_GAIN}",  "value":"${NI_L_SAMPLE_GAIN}"},
  {"pv_name":"${PV_SAMPLE_DECADE}","value":"${NI_L_SAMPLE_DECADE}"},
  {"pv_name":"${PV_AUMESH_GAIN}",  "value":"${NI_L_AUMESH_GAIN}"},
  {"pv_name":"${PV_AUMESH_DECADE}","value":"${NI_L_AUMESH_DECADE}"},
  {"pv_name":"${PV_PD_GAIN}",      "value":"${NI_L_PD_GAIN}"},
  {"pv_name":"${PV_PD_DECADE}",    "value":"${NI_L_PD_DECADE}"},
  {"pv_name":"${PV_EPU_TABLE}",    "value":${NI_L_EPU_TABLE}},
  {"pv_name":"${PV_EPU_OFFSET}",   "value":${NI_L_EPU_OFFSET}},
  {"pv_name":"${PV_EPU_DEADBAND}", "value":${NI_L_EPU_DEADBAND}}
]}
EOF
)
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$body")
expect_status 200 "$status" "POST /api/v1/pv/set/batch (CurrAmp+EPU)"

ok=$(jq -r '.ok' < /tmp/exer_body)
applied=$(jq -r '.applied' < /tmp/exer_body)
if [ "$ok" != "true" ] || [ "$applied" != "9" ]; then
    note "batch response: $(cat /tmp/exer_body)"
    fail "CurrAmp+EPU batch caput ok=${ok} applied=${applied} (expected ok=true applied=9)"
fi
pass "CurrAmp+EPU batch caput applied=${applied}/9"

# Echo-only IOCs — no slew; brief sleep for CA propagation.
sleep 0.3

# ─── Readback verification (CurrAmp + EPU) ───────────────────────────────
step "Readback verification (CurrAmp + EPU)"

# CurrAmp gain/decade are ENUMs. caproto reports the enum index for ENUM
# PVs over CA by default, so the value endpoint returns the integer index
# (e.g. "1" → index 0 in GAIN_VALS, "1 nA/V" → index 3 in GAIN_DECADES).
# Resolve the expected index from the IOC's enum table.
check_str_pv "$PV_SAMPLE_GAIN"   "0" "sample_sclr_gain (idx for '1')"
check_str_pv "$PV_SAMPLE_DECADE" "3" "sample_sclr_decade (idx for '1 nA/V')"
check_str_pv "$PV_AUMESH_GAIN"   "0" "aumesh_sclr_gain (idx for '1')"
check_str_pv "$PV_AUMESH_DECADE" "2" "aumesh_sclr_decade (idx for '100 pA/V')"
check_str_pv "$PV_PD_GAIN"       "0" "pd_sclr_gain (idx for '1')"
check_str_pv "$PV_PD_DECADE"     "3" "pd_sclr_decade (idx for '1 nA/V')"

# EPU PVs are plain numeric — check_pv with tight tolerance.
check_pv "$PV_EPU_TABLE"    "$NI_L_EPU_TABLE"    "epu1table"
check_pv "$PV_EPU_OFFSET"   "$NI_L_EPU_OFFSET"   "epu1offset"
check_pv "$PV_EPU_DEADBAND" "$NI_L_EPU_DEADBAND" "epu1.flt.output_deadband"

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

# ─── Phase 3: EPU FLT calc ───────────────────────────────────────────────
# Verify the FLT interpolator's calc dynamic: Out1-I = Inp1-SP + InpOff1-SP.
step "Phase 3: EPU FLT calc dynamic"

status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_EPU_FLT_INPUT}",  "value":${PHASE3_FLT_INPUT}},
  {"pv_name":"${PV_EPU_FLT_OFFSET}", "value":${PHASE3_FLT_OFFSET}}
]}
EOF
)")
expect_status 200 "$status" "POST FLT input + offset"
pass "FLT inputs caput (input=${PHASE3_FLT_INPUT}, offset=${PHASE3_FLT_OFFSET})"
sleep 0.2
check_pv "$PV_EPU_FLT_OUTPUT" "$PHASE3_FLT_OUTPUT" "epu1.flt.output (= input+offset)"

# ─── Phase 3: Vortex MCA spectrum + ROI integration ──────────────────────
step "Phase 3: Vortex MCA ROI integration"

status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_VORTEX_R2_LO}", "value":${PHASE3_R2_LO}},
  {"pv_name":"${PV_VORTEX_R2_HI}", "value":${PHASE3_R2_HI}},
  {"pv_name":"${PV_VORTEX_PRTM}",  "value":${PHASE3_PRTM}}
]}
EOF
)")
expect_status 200 "$status" "POST vortex R2 bounds + PRTM"
pass "vortex caput (R2LO=${PHASE3_R2_LO}, R2HI=${PHASE3_R2_HI}, PRTM=${PHASE3_PRTM})"

# Allow time for PRTM-write to regenerate the spectrum + refresh ROI sums
# (ROI putters fire create_task with a ~10ms delay, then write the sum).
sleep 0.3

# Read R2 sum — should be > 0 because the simulated peak at channel 300 is
# within the ROI bounds [250, 350]. Don't assert an exact value (Poisson
# variance), just verify the integration produced a non-trivial result.
# Numeric regex tolerates either int or float JSON output.
status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$PV_VORTEX_R2_SUM")/value")
expect_status 200 "$status" "GET vortex R2 sum"
got=$(jq -r '.value' < /tmp/exer_body)
if ! [[ "$got" =~ ^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$ ]]; then
    fail "vortex.mca.rois.roi2.count = '${got}' is not numeric"
fi
if awk -v g="$got" 'BEGIN{exit !(g >= 100)}'; then
    pass "vortex.mca.rois.roi2.count = ${got} (peak integrated, ≥ 100)"
else
    fail "vortex.mca.rois.roi2.count = ${got}, expected ≥ 100 (peak in [250,350] should integrate to thousands of counts)"
fi

# ─── Phase 3: Scaler preset-time count ───────────────────────────────────
step "Phase 3: Scaler preset-time count"

# Override TP to the test fixture value, then trigger CNT=1. The IOC
# counts for TP seconds, then auto-clears CNT.
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_SCLR_TP}",  "value":${PHASE3_TP}},
  {"pv_name":"${PV_SCLR_CNT}", "value":1}
]}
EOF
)")
expect_status 200 "$status" "POST scaler TP+CNT"
ok=$(jq -r '.ok' < /tmp/exer_body)
applied=$(jq -r '.applied' < /tmp/exer_body)
if [ "$ok" != "true" ] || [ "$applied" != "2" ]; then
    note "batch response: $(cat /tmp/exer_body)"
    fail "scaler TP+CNT batch ok=${ok} applied=${applied} (expected 2/2)"
fi
pass "scaler trigger sent (TP=${PHASE3_TP}s, will count then auto-clear)"

# Wait for the count to finish. Add 0.5s margin over TP.
sleep $(awk "BEGIN{print ${PHASE3_TP} + 0.5}")

# Verify CNT auto-cleared back to 0. CNT is integer-typed, exact compare.
status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$PV_SCLR_CNT")/value")
expect_status 200 "$status" "GET scaler CNT"
got=$(jq -r '.value' < /tmp/exer_body)
if [ "$got" != "0" ]; then
    fail "scaler CNT = ${got}, expected 0 after count completed"
fi
pass "scaler CNT auto-cleared to 0"

# Verify T elapsed approximately TP. T is float (precision=4), use awk.
check_pv "$PV_SCLR_T" "$PHASE3_TP" "scaler .T (elapsed)" 0.1

# Verify channel 1 (clock) accumulated counts. Rate is 1e7 cts/s × TP.
# S1 is now float (DOUBLE) to avoid int32 overflow at long TPs; accept
# floats via awk numeric compare. Expected ≈ 1e7 × 0.5 = 5e6.
status=$(req GET "${DIRECT_URL}/api/v1/pv/$(encode "$PV_SCLR_S1")/value")
expect_status 200 "$status" "GET scaler S1 (clock)"
got=$(jq -r '.value' < /tmp/exer_body)
expected_s1=$(awk "BEGIN{print 1.0e7 * ${PHASE3_TP}}")
if ! [[ "$got" =~ ^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$ ]]; then
    fail "scaler S1 = '${got}' is not numeric"
fi
# Loose tolerance — tick quantization at TICK_S=0.05 can drift by ±5%.
if awk -v g="$got" -v e="$expected_s1" \
   'BEGIN{r=g/e; exit !((r>=0.9)&&(r<=1.1))}'; then
    pass "scaler S1 (clock) = ${got} (≈ ${expected_s1}, ±10%)"
else
    fail "scaler S1 = ${got}, expected ≈ ${expected_s1} (±10%)"
fi

# ─── Phase 3: Feedback PID convergence ───────────────────────────────────
step "Phase 3: Feedback PID convergence"

# Caput SP (via top-level happi entry m1b1_setpoint) and enable the loop.
# The PID-SP and Sts:FB-Sel PVs are at literal prefix XF:23ID2-OP{FBck}
# regardless of M1bMirror's own prefix (FeedbackLoop uses add_prefix="").
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_FB_SP}",     "value":${PHASE3_PID_SP}},
  {"pv_name":"${PV_FB_ENABLE}", "value":"On"}
]}
EOF
)")
expect_status 200 "$status" "POST feedback SP + enable"
pass "feedback caput (SP=${PHASE3_PID_SP}, enable=On)"

# Loop runs at TICK_S=0.1s with P_GAIN=0.2 (16% error closed per tick).
# From CVAL=0 to SP=5.0: ~25 ticks to settle to within deadband (0.01).
# 3.5 seconds gives plenty of margin.
sleep 3.5

# CVAL should be ≈ SP (within deadband 0.01); use loose tol 0.1 to absorb
# any small drift.
check_pv "$PV_FB_CVAL" "$PHASE3_PID_SP" "m1b1.fbl.actual_value (PID converged)" 0.1

# Reset SP to 0 BEFORE disabling so a re-run against the same pod
# starts with CVAL≈SP=5 vs new-SP=5, error≠0, and the convergence test
# actually exercises the P-loop math. Without this reset the re-run
# would trivially pass even if P_GAIN regressed to 0.
status=$(req POST "${DIRECT_URL}/api/v1/pv/set/batch" "$(cat <<EOF
{"caputs":[
  {"pv_name":"${PV_FB_SP}",     "value":0.0},
  {"pv_name":"${PV_FB_ENABLE}", "value":"Off"}
]}
EOF
)")
expect_status 200 "$status" "POST feedback reset SP + disable"
pass "feedback reset (SP=0, disabled — next run sees error=5)"

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}configuration_service_ios: ALL CHECKS PASSED${RESET}\n"
