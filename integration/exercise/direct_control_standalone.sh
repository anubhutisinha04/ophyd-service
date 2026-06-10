#!/usr/bin/env bash
#
# Exercise direct_control_service in STANDALONE (file-registry) mode.
# Target: integration/pods/standalone — no configuration_service exists,
# so this walk proves the file registry drives the registry gate, PV
# control, and device-level control end to end.
#
# Usage:
#   ./direct_control_standalone.sh
#   DIRECT_URL=http://remote:8003 ./direct_control_standalone.sh

set -euo pipefail

. "$(dirname "$0")/_exerciser_lib.sh"

DIRECT_URL="${DIRECT_URL:-http://localhost:8003}"

printf "${BOLD}direct_control standalone exerciser${RESET}  target=${DIRECT_URL}\n"

# ─── Mode visibility ─────────────────────────────────────────────────────
step "Health & standalone mode"

status=$(req GET "${DIRECT_URL}/health")
expect_status 200 "$status" "/health"
backend=$(jq -r '.registry_backend' < /tmp/exer_body)
[ "$backend" = "file" ] || fail "/health registry_backend=${backend}, expected file"
pass "/health healthy with registry_backend=file (no config-service)"

status=$(req GET "${DIRECT_URL}/api/v1/stats")
expect_status 200 "$status" "/api/v1/stats"
coord=$(jq -r '.coordination_enabled' < /tmp/exer_body)
[ "$coord" = "false" ] || fail "coordination_enabled=${coord}, expected false in standalone mode"
pass "/api/v1/stats coordination_enabled=false, read_only=$(jq -r '.read_only' < /tmp/exer_body)"

# ─── PV-level operations through the file registry gate ──────────────────
step "PV operations (file-registry gate)"

status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:current/value")
expect_status 200 "$status" "pv/mini:current (device-owned PV)"
pass "pv/mini:current  value=$(jq -r '.value' < /tmp/exer_body)"

status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:exp/value")
expect_status 200 "$status" "pv/mini:dot:exp (standalone_pvs entry)"
pass "pv/mini:dot:exp  value=$(jq -r '.value' < /tmp/exer_body)"

status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:ph:mtr/value")
expect_status 404 "$status" "pv/mini:ph:mtr (exists on IOC but NOT in the file registry)"
pass "unregistered PV rejected by the file-registry gate → 404"

target=3.5
status=$(req POST "${DIRECT_URL}/api/v1/pv/set" "{\"pv_name\":\"mini:dot:mtrx\",\"value\":${target},\"wait\":true}")
expect_success "$status" "POST /api/v1/pv/set mini:dot:mtrx=${target}" 200
sleep 0.5
status=$(req GET "${DIRECT_URL}/api/v1/pv/mini:dot:mtrx/value")
expect_status 200 "$status" "pv/mini:dot:mtrx readback"
observed=$(jq -r '.value' < /tmp/exer_body)
if awk -v o="$observed" -v t="$target" 'BEGIN{exit !(o-t <= 0.01 && t-o <= 0.01)}'; then
    pass "PV write round-trip observed=${observed}"
else
    fail "PV readback observed=${observed} != target=${target}"
fi

# ─── Device-level control from file-registry instantiation specs ─────────
step "Device-level control (classes from the registry file)"

status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"beam_current","method":"read"}')
expect_status 200 "$status" "device/execute beam_current read()"
pass "device/execute beam_current read()  value=$(jq -r '.result.beam_current.value' < /tmp/exer_body)"

dev_target=1.75
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" "{\"device_name\":\"motor_spotx\",\"method\":\"set\",\"args\":[${dev_target}]}")
expect_status 200 "$status" "device/execute motor_spotx set(${dev_target})"
sleep 0.5
status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"motor_spotx","method":"get"}')
expect_status 200 "$status" "device/execute motor_spotx get()"
dev_observed=$(jq -r '.result' < /tmp/exer_body)
if awk -v o="$dev_observed" -v t="$dev_target" 'BEGIN{exit !(o-t <= 0.01 && t-o <= 0.01)}'; then
    pass "device-level set→get round-trip observed=${dev_observed}"
else
    fail "device-level readback observed=${dev_observed} != target=${dev_target}"
fi

status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"pinhole","method":"read"}')
expect_status 200 "$status" "device/execute pinhole read() (compound localdevs.Det)"
pass "device/execute pinhole read()  keys=$(jq -r '.result | keys | join(",")' < /tmp/exer_body)"

status=$(req POST "${DIRECT_URL}/api/v1/device/pinhole.exp" '{"method":"get"}')
expect_status 200 "$status" "device/pinhole.exp get (nested component)"
pass "device/pinhole.exp get  value=$(jq -r '.result' < /tmp/exer_body)"

# ─── Typed refusals ──────────────────────────────────────────────────────
step "Refusals (typed, never silent)"

status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"slit_det","method":"read"}')
expect_status 422 "$status" "device/execute slit_det (no device_class in the file)"
pass "device without class info → 422 (PV-level operations still work)"

status=$(req POST "${DIRECT_URL}/api/v1/device/execute" '{"device_name":"beam_current","method":"destroy"}')
expect_status 400 "$status" "device/execute method=destroy (outside allowlist)"
pass "non-allowlisted method → 400"

status=$(req POST "${DIRECT_URL}/api/v1/device/pinhole.no_such" '{"method":"read"}')
expect_status 404 "$status" "device/pinhole.no_such (unknown component)"
pass "unknown nested component → 404"

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}direct_control standalone: ALL CHECKS PASSED${RESET}\n"
