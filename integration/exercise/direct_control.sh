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

# ─── Registry view (pass-through to configuration_service) ───────────────
step "Registry view"

status=$(req GET "${DIRECT_URL}/api/v1/devices")
expect_status 200 "$status" "/api/v1/devices"
pass "/api/v1/devices"

status=$(req GET "${DIRECT_URL}/api/v1/devices/beam_current")
expect_status 200 "$status" "/api/v1/devices/beam_current"
pass "/api/v1/devices/beam_current"

status=$(req GET "${DIRECT_URL}/api/v1/devices/beam_current/bundle")
expect_status 200 "$status" "/api/v1/devices/beam_current/bundle"
pass "/api/v1/devices/beam_current/bundle"

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

# Unimplemented device operations surface as 501 Not Implemented to prevent
# silent failures (e.g., /stop appearing to succeed when it doesn't actually do anything).
# This requires full ophyd-device integration via the Configuration Service.
step "Device-level operations"

body='{"device_name":"beam_current","method":"read","args":[],"kwargs":{}}'
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" "$body")
expect_status 501 "$status" "POST /api/v1/device/execute (requires ophyd integration)"
pass "POST /api/v1/device/execute"

status=$(req POST "${DIRECT_URL}/api/v1/device/beam_current/stop")
expect_status 501 "$status" "POST /api/v1/device/beam_current/stop (requires ophyd integration)"
pass "POST /api/v1/device/beam_current/stop"

status=$(req POST "${DIRECT_URL}/api/v1/device/beam_current.readback" '{"method":"read"}')
expect_status 501 "$status" "POST /api/v1/device/beam_current.readback (requires ophyd integration)"
pass "POST /api/v1/device/beam_current.readback (nested component)"

step "Device-path form"

status=$(req GET "${DIRECT_URL}/api/v1/device/spot.roi/value")
case "$status" in
    200) pass "/api/v1/device/spot.roi/value" ;;
    404) note "/api/v1/device/spot.roi → 404 (components not exposed via device-path yet; compound-leaf must be read as PV directly)" ;;
    *) note "/api/v1/device/spot.roi/value  HTTP $status" ;;
esac

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
