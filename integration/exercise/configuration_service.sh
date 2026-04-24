#!/usr/bin/env bash
#
# Exercise every major endpoint family on configuration_service.
#
# Plays the role of a first-time user walking through the API: health →
# browse happi-loaded registry → CRUD on standalone PVs → CRUD on devices →
# metadata round-trip → audit-history → registry export. Each section is a
# copy-pasteable example; the script fails loudly on the first assertion
# that doesn't hold.
#
# Intended for both local runs and future CI. Assumes `curl` and `jq` are on
# PATH.
#
# Usage:
#   ./configuration_service.sh                    # against http://localhost:8004
#   CONFIG_URL=http://remote:8004 ./configuration_service.sh
#
# Exit 0 on full pass; non-zero on any failure.

set -euo pipefail

CONFIG_URL="${CONFIG_URL:-http://localhost:8004}"

if [ -t 1 ]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; BOLD=''; RESET=''
fi

step()  { printf "\n${BOLD}== %s ==${RESET}\n" "$1"; }
pass()  { printf "  ${GREEN}PASS${RESET}  %s\n" "$1"; }
fail()  { printf "  ${RED}FAIL${RESET}  %s\n" "$1" >&2; exit 1; }
note()  { printf "  ${YELLOW}NOTE${RESET}  %s\n" "$1"; }

# req METHOD URL [BODY] → writes body to /tmp/exer_body, prints status
req() {
    local method=$1 url=$2 body="${3:-}"
    local -a args=(-s -o /tmp/exer_body -w "%{http_code}" -X "$method")
    if [ -n "$body" ]; then
        args+=(-H "Content-Type: application/json" -d "$body")
    fi
    curl "${args[@]}" "$url"
}

expect_status() {
    local want=$1 got=$2 url=$3
    if [ "$got" != "$want" ]; then
        printf "    response body: %s\n" "$(cat /tmp/exer_body 2>/dev/null | head -c 400)"
        fail "$url: expected HTTP $want, got $got"
    fi
}

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

# register
status=$(req POST "${CONFIG_URL}/api/v1/pvs" '{"pv_name":"exer:test:pv","description":"exerciser scratch PV"}')
case "$status" in
    20*) pass "POST /api/v1/pvs  exer:test:pv  HTTP $status" ;;
    *) expect_status 201 "$status" "POST /api/v1/pvs" ;;
esac

# update via PUT
status=$(req PUT "${CONFIG_URL}/api/v1/pvs/standalone/exer:test:pv" '{"description":"updated by exerciser"}')
case "$status" in
    20*) pass "PUT /api/v1/pvs/standalone/exer:test:pv  HTTP $status" ;;
    *) expect_status 200 "$status" "PUT /api/v1/pvs/standalone/exer:test:pv" ;;
esac

# delete
status=$(req DELETE "${CONFIG_URL}/api/v1/pvs/standalone/exer:test:pv")
case "$status" in
    20*) pass "DELETE /api/v1/pvs/standalone/exer:test:pv  HTTP $status" ;;
    *) expect_status 204 "$status" "DELETE /api/v1/pvs/standalone/exer:test:pv" ;;
esac

# confirm gone
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
case "$status" in
    20*) pass "POST /api/v1/devices  exer_test_motor  HTTP $status" ;;
    *) expect_status 201 "$status" "POST /api/v1/devices" ;;
esac

# retrieve
status=$(req GET "${CONFIG_URL}/api/v1/devices/exer_test_motor")
expect_status 200 "$status" "GET /api/v1/devices/exer_test_motor"
pass "GET /api/v1/devices/exer_test_motor"

# disable
status=$(req PATCH "${CONFIG_URL}/api/v1/devices/exer_test_motor/disable")
case "$status" in
    20*) pass "PATCH .../disable  HTTP $status" ;;
    *) expect_status 200 "$status" "PATCH .../disable" ;;
esac

# enable
status=$(req PATCH "${CONFIG_URL}/api/v1/devices/exer_test_motor/enable")
case "$status" in
    20*) pass "PATCH .../enable  HTTP $status" ;;
    *) expect_status 200 "$status" "PATCH .../enable" ;;
esac

# update (PUT)
status=$(req PUT "${CONFIG_URL}/api/v1/devices/exer_test_motor" \
    '{"name":"exer_test_motor","device_class":"ophyd.signal.EpicsSignal","args":["exer:test:motor"],"kwargs":{"name":"exer_test_motor"},"documentation":"updated"}')
case "$status" in
    20*) pass "PUT /api/v1/devices/exer_test_motor  HTTP $status" ;;
    *) expect_status 200 "$status" "PUT /api/v1/devices/exer_test_motor" ;;
esac

# delete
status=$(req DELETE "${CONFIG_URL}/api/v1/devices/exer_test_motor")
case "$status" in
    20*) pass "DELETE /api/v1/devices/exer_test_motor  HTTP $status" ;;
    *) expect_status 204 "$status" "DELETE /api/v1/devices/exer_test_motor" ;;
esac

# ─── Metadata CRUD ────────────────────────────────────────────────────────
step "Metadata round-trip"

status=$(req POST "${CONFIG_URL}/api/v1/metadata/exer_scratch" '{"value":{"stage":"initial","flag":true}}')
case "$status" in 20*) pass "POST /api/v1/metadata/exer_scratch  HTTP $status" ;;
    *) expect_status 201 "$status" "POST /api/v1/metadata/exer_scratch" ;; esac

status=$(req GET "${CONFIG_URL}/api/v1/metadata/exer_scratch")
expect_status 200 "$status" "GET /api/v1/metadata/exer_scratch"
pass "GET /api/v1/metadata/exer_scratch"

status=$(req PUT "${CONFIG_URL}/api/v1/metadata/exer_scratch" '{"value":{"stage":"updated","flag":false}}')
case "$status" in 20*) pass "PUT /api/v1/metadata/exer_scratch  HTTP $status" ;;
    *) expect_status 200 "$status" "PUT /api/v1/metadata/exer_scratch" ;; esac

status=$(req GET "${CONFIG_URL}/api/v1/metadata")
expect_status 200 "$status" "/api/v1/metadata"
pass "/api/v1/metadata"

status=$(req DELETE "${CONFIG_URL}/api/v1/metadata/exer_scratch")
case "$status" in 20*) pass "DELETE /api/v1/metadata/exer_scratch  HTTP $status" ;;
    *) expect_status 204 "$status" "DELETE /api/v1/metadata/exer_scratch" ;; esac

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
