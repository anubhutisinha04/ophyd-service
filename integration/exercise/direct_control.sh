#!/usr/bin/env bash
#
# Exercise the HTTP endpoints on direct_control_service.
# WebSocket endpoints live in the companion direct_control_ws.py script.
#
# Usage:
#   ./direct_control.sh
#   DIRECT_URL=http://remote:8003 ./direct_control.sh

set -euo pipefail

. "$(dirname "$0")/_exerciser_lib.sh"

DIRECT_URL="${DIRECT_URL:-http://localhost:8003}"

printf "${BOLD}direct_control exerciser${RESET}  target=${DIRECT_URL}\n"

# ─── Health & stats ──────────────────────────────────────────────────────
step "Health & service state"

status=$(req GET "${DIRECT_URL}/health")
expect_status 200 "$status" "/health"
pass "/health"

status=$(req GET "${DIRECT_URL}/api/v1/stats")
expect_status 200 "$status" "/api/v1/stats"
pass "/api/v1/stats"

status=$(req GET "${DIRECT_URL}/api/v1/pvs/connected")
expect_status 200 "$status" "/api/v1/pvs/connected"
pass "/api/v1/pvs/connected"

# ─── PV reads (HTTP) ─────────────────────────────────────────────────────
step "PV reads"

# scalar
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:current/value")
expect_status 200 "$status" "pv/mini:current"
scalar_value=$(jq -r '.value' < /tmp/exer_body)
pass "pv/mini:current  value=${scalar_value}"

# motor position
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:mtrx/value")
expect_status 200 "$status" "pv/mini:dot:mtrx"
pass "pv/mini:dot:mtrx  value=$(jq -r '.value' < /tmp/exer_body)"

# array PV (waveform)
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:det/value")
expect_status 200 "$status" "pv/mini:dot:det (array)"
shape=$(jq -r '.shape // "?"' < /tmp/exer_body)
pass "pv/mini:dot:det  shape=${shape}"

# compound-device leaf — exercises the happi option-(a) pattern
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:img_sum/value")
expect_status 200 "$status" "pv/mini:dot:img_sum (compound leaf)"
pass "pv/mini:dot:img_sum  value=$(jq -r '.value' < /tmp/exer_body)"

# alternative /pvs/ pluralized path (mapped to the same handler)
status=$(req GET "${DIRECT_URL}/api/v1/pvs/mini:current/value")
expect_status 200 "$status" "/api/v1/pvs/mini:current/value"
pass "/api/v1/pvs/mini:current/value"

# ─── PV write + round-trip ───────────────────────────────────────────────
step "PV write → readback round-trip"

# Capture the current value so we can restore it at the end.
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:mtrx/value")
expect_status 200 "$status" "pv/mini:dot:mtrx (pre-write)"
original=$(jq -r '.value' < /tmp/exer_body)

target=2.5
status=$(req POST "${DIRECT_URL}/api/v1/pv/set" "{\"pv_name\":\"mini:dot:mtrx\",\"value\":${target}}")
expect_success "$status" "POST /api/v1/pv/set  mini:dot:mtrx=${target}" 200

# Caproto applies puts asynchronously; give the IOC a tick before reading back.
sleep 0.5

status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:mtrx/value")
expect_status 200 "$status" "pv/mini:dot:mtrx (readback)"
observed=$(jq -r '.value' < /tmp/exer_body)

if awk -v o="$observed" -v t="$target" 'BEGIN{exit !(o-t <= 0.01 && t-o <= 0.01)}'; then
    pass "readback observed=${observed} within tolerance of ${target}"
else
    fail "readback observed=${observed} != target=${target}"
fi

status=$(req POST "${DIRECT_URL}/api/v1/pv/set" "{\"pv_name\":\"mini:dot:mtrx\",\"value\":${original}}")
expect_success "$status" "restored mini:dot:mtrx to ${original}" 200

# Device-level operations are real: direct_control fetches the device's
# instantiation spec from configuration_service, instantiates the class
# (classic ophyd or ophyd-async, dispatched per device), connects it, and
# runs the verb with Status completion awaited.
step "Device-level operations (live ophyd devices)"

# read on a signal-backed device (ophyd.signal.EpicsSignal)
body='{"device_name":"beam_current","method":"read","args":[],"kwargs":{}}'
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" "$body")
expect_status 200 "$status" "POST /api/v1/device/execute beam_current read()"
val=$(jq -r '.result.beam_current.value' < /tmp/exer_body)
pass "device/execute beam_current read()  value=${val}"

# device-level set → get round-trip (set waits for the ophyd Status)
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"motor_spotx","method":"get"}')
expect_status 200 "$status" "device/execute motor_spotx get() (pre-write)"
dev_original=$(jq -r '.result' < /tmp/exer_body)

dev_target=1.25
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" "{\"device_name\":\"motor_spotx\",\"method\":\"set\",\"args\":[${dev_target}]}")
expect_status 200 "$status" "device/execute motor_spotx set(${dev_target})"

sleep 0.5
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"motor_spotx","method":"get"}')
expect_status 200 "$status" "device/execute motor_spotx get() (readback)"
dev_observed=$(jq -r '.result' < /tmp/exer_body)

if awk -v o="$dev_observed" -v t="$dev_target" 'BEGIN{exit !(o-t <= 0.01 && t-o <= 0.01)}'; then
    pass "device-level readback observed=${dev_observed} within tolerance of ${dev_target}"
else
    fail "device-level readback observed=${dev_observed} != target=${dev_target}"
fi

status=$(req POST "${DIRECT_URL}/api/v1/device/execute" "{\"device_name\":\"motor_spotx\",\"method\":\"set\",\"args\":[${dev_original}]}")
expect_status 200 "$status" "restored motor_spotx to ${dev_original}"

# compound device (localdevs.Det): device-level read + nested component access.
# Requires the localdevs mount on direct_control_service (see the pod compose).
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"pinhole","method":"read"}')
expect_status 200 "$status" "device/execute pinhole read() (compound device)"
pass "device/execute pinhole read()  keys=$(jq -r '.result | keys | join(",")' < /tmp/exer_body)"

status=$(req POST "${DIRECT_URL}/api/v1/device/pinhole.exp" '{"method":"get"}')
expect_status 200 "$status" "POST /api/v1/device/pinhole.exp get (nested component)"
pass "device/pinhole.exp get  value=$(jq -r '.result' < /tmp/exer_body)"

status=$(req GET "${DIRECT_URL}/api/v1/device/pinhole.det/value")
expect_status 200 "$status" "GET /api/v1/device/pinhole.det/value"
pass "/api/v1/device/pinhole.det/value"

step "Device-level refusals (typed, never silent)"

# stop on a bare signal: 'stop' is an allowlisted verb but EpicsSignal
# doesn't implement it → 400, not a fake success
status=$(req POST "${DIRECT_URL}/api/v1/device/beam_current/stop")
expect_status 400 "$status" "POST /api/v1/device/beam_current/stop (Signal has no stop)"
pass "stop on a signal device → 400 (does not support)"

# arbitrary method names are rejected by the allowlist
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"beam_current","method":"destroy"}')
expect_status 400 "$status" "device/execute method=destroy (outside allowlist)"
pass "non-allowlisted method → 400"

# unknown nested component on a live device
status=$(req POST "${DIRECT_URL}/api/v1/device/pinhole.no_such_signal" '{"method":"read"}')
expect_status 404 "$status" "device/pinhole.no_such_signal (unknown component)"
pass "unknown nested component → 404"

# ─── Error cases ─────────────────────────────────────────────────────────
step "Error handling"

status=$(req GET "${DIRECT_URL}/api/v1/pv/does:not:exist/value")
if [ "$status" = "404" ]; then
    pass "unknown PV → HTTP 404"
else
    fail "unknown PV returned HTTP $status (expected 404)"
fi

status=$(req POST "${DIRECT_URL}/api/v1/pv/set" '{"pv_name":"does:not:exist","value":1.0}')
case "$status" in
    404) pass "set on unknown PV → HTTP 404" ;;
    400|422) pass "set on unknown PV → HTTP $status (validation rejection)" ;;
    *) fail "set on unknown PV returned HTTP $status (expected 404/400/422)" ;;
esac

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}direct_control: ALL CHECKS PASSED${RESET}\n"
