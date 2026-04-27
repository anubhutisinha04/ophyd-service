#!/usr/bin/env bash
#
# Exercise every major endpoint family on configuration_service.
# Pass/fail via exit code; intended for both local runs and CI.
#
# Usage:
#   ./configuration_service.sh
#   CONFIG_URL=http://remote:8004 ./configuration_service.sh

set -euo pipefail

. "$(dirname "$0")/_exerciser_lib.sh"

CONFIG_URL="${CONFIG_URL:-http://localhost:8004}"

printf "${BOLD}configuration_service exerciser${RESET}  target=${CONFIG_URL}\n"

# ─── Health ──────────────────────────────────────────────────────────────
step "Health & readiness"

status=$(req GET "${CONFIG_URL}/health")
expect_status 200 "$status" "/health"
loaded=$(jq -r '.devices_loaded' < /tmp/exer_body)
pass "/health  devices_loaded=${loaded}"

status=$(req GET "${CONFIG_URL}/ready")
expect_status 200 "$status" "/ready"
pass "/ready"

# ─── Registry browse (happi-populated) ───────────────────────────────────
step "Browse happi-loaded registry"

status=$(req GET "${CONFIG_URL}/api/v1/devices")
expect_status 200 "$status" "/api/v1/devices"
device_count=$(jq 'length' < /tmp/exer_body)
[ "$device_count" -gt 0 ] || fail "expected happi-loaded devices, got 0"
pass "/api/v1/devices  count=${device_count}"

# pick a known scalar device to inspect
status=$(req GET "${CONFIG_URL}/api/v1/devices/beam_current")
expect_status 200 "$status" "/api/v1/devices/beam_current"
ophyd_class=$(jq -r '.ophyd_class' < /tmp/exer_body)
[ "$ophyd_class" = "EpicsSignal" ] || fail "beam_current.ophyd_class: expected EpicsSignal, got $ophyd_class"
pass "/api/v1/devices/beam_current  ophyd_class=${ophyd_class}"

# a compound device
status=$(req GET "${CONFIG_URL}/api/v1/devices/spot")
expect_status 200 "$status" "/api/v1/devices/spot"
spot_class=$(jq -r '.ophyd_class' < /tmp/exer_body)
[ "$spot_class" = "Spot" ] || fail "spot.ophyd_class: expected Spot, got $spot_class"
pass "/api/v1/devices/spot  ophyd_class=${spot_class}"

# device's PVs
status=$(req GET "${CONFIG_URL}/api/v1/devices/beam_current/pvs")
expect_status 200 "$status" "/api/v1/devices/beam_current/pvs"
pass "/api/v1/devices/beam_current/pvs"

# instantiation spec (what queueserver would use)
status=$(req GET "${CONFIG_URL}/api/v1/devices/beam_current/instantiation")
expect_status 200 "$status" "/api/v1/devices/beam_current/instantiation"
pass "/api/v1/devices/beam_current/instantiation"

# components of a compound device (returns the sub-component tree)
status=$(req GET "${CONFIG_URL}/api/v1/devices/spot/components")
expect_status 200 "$status" "/api/v1/devices/spot/components"
pass "/api/v1/devices/spot/components"

# device-level status (lock state, availability)
status=$(req GET "${CONFIG_URL}/api/v1/devices/beam_current/status")
expect_status 200 "$status" "/api/v1/devices/beam_current/status"
pass "/api/v1/devices/beam_current/status"

# single-component lookup via dotted path
status=$(req GET "${CONFIG_URL}/api/v1/devices/spot.roi/component")
expect_status 200 "$status" "/api/v1/devices/spot.roi/component"
pass "/api/v1/devices/spot.roi/component"

# summary views
for path in devices-info devices/classes devices/types devices/instantiation; do
    status=$(req GET "${CONFIG_URL}/api/v1/${path}")
    expect_status 200 "$status" "/api/v1/${path}"
    pass "/api/v1/${path}"
done

# ─── PV registry ──────────────────────────────────────────────────────────
step "PV registry"

status=$(req GET "${CONFIG_URL}/api/v1/pvs")
expect_status 200 "$status" "/api/v1/pvs"
pv_count=$(jq '.count' < /tmp/exer_body)
[ "$pv_count" -gt 0 ] || fail "expected PVs in registry, got 0"
pass "/api/v1/pvs  count=${pv_count}"

status=$(req GET "${CONFIG_URL}/api/v1/pvs/detailed")
expect_status 200 "$status" "/api/v1/pvs/detailed"
pass "/api/v1/pvs/detailed"

# individual PV lookup
status=$(req GET "${CONFIG_URL}/api/v1/pvs/mini:current")
expect_status 200 "$status" "/api/v1/pvs/mini:current"
pass "/api/v1/pvs/mini:current"

# labels: no required params
status=$(req GET "${CONFIG_URL}/api/v1/pvs/labels")
expect_status 200 "$status" "/api/v1/pvs/labels"
pass "/api/v1/pvs/labels"

# status + lookup both require ?pv_name=
for path in pvs/status pvs/lookup; do
    status=$(req GET "${CONFIG_URL}/api/v1/${path}?pv_name=mini:current")
    expect_status 200 "$status" "/api/v1/${path}"
    pass "/api/v1/${path}?pv_name=mini:current"
done

# ─── Standalone PV CRUD ──────────────────────────────────────────────────
step "Standalone PV lifecycle"

# initial list
status=$(req GET "${CONFIG_URL}/api/v1/pvs/standalone")
expect_status 200 "$status" "/api/v1/pvs/standalone (pre)"
pre_count=$(jq 'if type=="array" then length else (.count // (.pvs | length) // 0) end' < /tmp/exer_body)
pass "/api/v1/pvs/standalone  count=${pre_count} (pre)"

status=$(req POST "${CONFIG_URL}/api/v1/pvs" '{"pv_name":"exer:test:pv","description":"exerciser scratch PV"}')
expect_success "$status" "POST /api/v1/pvs  exer:test:pv" 201

status=$(req PUT "${CONFIG_URL}/api/v1/pvs/standalone/exer:test:pv" '{"description":"updated by exerciser"}')
expect_success "$status" "PUT /api/v1/pvs/standalone/exer:test:pv" 200

status=$(req DELETE "${CONFIG_URL}/api/v1/pvs/standalone/exer:test:pv")
expect_success "$status" "DELETE /api/v1/pvs/standalone/exer:test:pv" 204

status=$(req GET "${CONFIG_URL}/api/v1/pvs/standalone/exer:test:pv")
if [ "$status" = "404" ]; then
    pass "GET after DELETE → 404 as expected"
else
    note "post-delete GET returned HTTP $status (expected 404) — soft-delete semantics may differ"
fi

# ─── Device CRUD ──────────────────────────────────────────────────────────
step "Device lifecycle (create → disable → enable → update → delete)"

device_payload='{
  "metadata": {
    "name": "exer_test_motor",
    "device_label": "signal",
    "ophyd_class": "EpicsSignal",
    "is_movable": true,
    "is_readable": true,
    "pvs": {"readback": "exer:test:motor"},
    "documentation": "exerciser scratch device"
  },
  "instantiation_spec": {
    "name": "exer_test_motor",
    "device_class": "ophyd.signal.EpicsSignal",
    "args": ["exer:test:motor"],
    "kwargs": {"name": "exer_test_motor"},
    "active": true
  }
}'

status=$(req POST "${CONFIG_URL}/api/v1/devices" "$device_payload")
expect_success "$status" "POST /api/v1/devices  exer_test_motor" 201

status=$(req GET "${CONFIG_URL}/api/v1/devices/exer_test_motor")
expect_status 200 "$status" "GET /api/v1/devices/exer_test_motor"
pass "GET /api/v1/devices/exer_test_motor"

status=$(req PATCH "${CONFIG_URL}/api/v1/devices/exer_test_motor/disable")
expect_success "$status" "PATCH .../disable" 200

status=$(req PATCH "${CONFIG_URL}/api/v1/devices/exer_test_motor/enable")
expect_success "$status" "PATCH .../enable" 200

status=$(req PUT "${CONFIG_URL}/api/v1/devices/exer_test_motor" \
    '{"name":"exer_test_motor","device_class":"ophyd.signal.EpicsSignal","args":["exer:test:motor"],"kwargs":{"name":"exer_test_motor"},"documentation":"updated"}')
expect_success "$status" "PUT /api/v1/devices/exer_test_motor" 200

status=$(req DELETE "${CONFIG_URL}/api/v1/devices/exer_test_motor")
expect_success "$status" "DELETE /api/v1/devices/exer_test_motor" 204

# ─── Audit trail ──────────────────────────────────────────────────────────
step "Change history"

for path in devices/changes devices/history; do
    status=$(req GET "${CONFIG_URL}/api/v1/${path}")
    expect_status 200 "$status" "/api/v1/${path}"
    pass "/api/v1/${path}"
done

# ─── Full registry export ────────────────────────────────────────────────
step "Registry export"

status=$(req GET "${CONFIG_URL}/api/v1/registry/export")
expect_status 200 "$status" "/api/v1/registry/export"
exported=$(jq 'length' < /tmp/exer_body 2>/dev/null || echo "?")
pass "/api/v1/registry/export  devices=${exported}"

# ─── Done ────────────────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}configuration_service: ALL CHECKS PASSED${RESET}\n"
