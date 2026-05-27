#!/usr/bin/env bash
#
# IOS demo one-command launcher.
#
# Default: build images (if needed) → bring the pod up → wait for all six
# IOCs + both backends healthy → run the exerciser → leave the pod
# running so you can poke at PVs by hand.
#
# Usage:
#   ./integration/pods/ios/run_demo.sh                # build+up+exerciser (default)
#   ./integration/pods/ios/run_demo.sh --rebuild      # force --build even on warm cache
#   ./integration/pods/ios/run_demo.sh --skip-exerciser  # just up the pod
#   ./integration/pods/ios/run_demo.sh --tear-down    # exerciser then full down
#   ./integration/pods/ios/run_demo.sh --logs         # tail direct-control logs after
#   ./integration/pods/ios/run_demo.sh --help

set -euo pipefail

# Locate the compose file + exerciser regardless of where this script is
# invoked from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE="${SCRIPT_DIR}/docker-compose.yaml"
EXERCISER="${SCRIPT_DIR}/../../exercise/configuration_service_ios.sh"

# Color helpers (no-op on non-tty stdout).
if [ -t 1 ]; then
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
    GREEN=''; YELLOW=''; BOLD=''; RESET=''
fi
step() { printf "\n${BOLD}== %s ==${RESET}\n" "$1"; }
note() { printf "  ${YELLOW}%s${RESET}\n" "$1"; }
ok()   { printf "  ${GREEN}%s${RESET}\n" "$1"; }

# ─── Args ───────────────────────────────────────────────────────────────
REBUILD=0
RUN_EXERCISER=1
TEAR_DOWN=0
TAIL_LOGS=0

for arg in "$@"; do
    case "$arg" in
        --rebuild)        REBUILD=1 ;;
        --skip-exerciser) RUN_EXERCISER=0 ;;
        --tear-down)      TEAR_DOWN=1 ;;
        --logs)           TAIL_LOGS=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "unknown arg: $arg (try --help)" >&2
            exit 2 ;;
    esac
done

# ─── Sanity ─────────────────────────────────────────────────────────────
[ -f "$COMPOSE" ] || { echo "compose file not found: $COMPOSE" >&2; exit 1; }
[ -x "$EXERCISER" ] || { echo "exerciser not found or not executable: $EXERCISER" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker not on PATH" >&2; exit 1; }

# ─── Build + up ─────────────────────────────────────────────────────────
step "Build + up (8 services: 6 IOCs + config-service + direct-control)"
build_flag=""
[ "$REBUILD" = "1" ] && build_flag="--build"

# `up -d` waits for `depends_on: condition: service_healthy` to satisfy
# before considering each service started. With healthchecks at 5s
# interval × 10 retries = up to 50s per IOC, the full bring-up window is
# bounded. Most of the time it's under 30s.
docker compose -f "$COMPOSE" up -d $build_flag
ok "pod up"

# Poll the backend health endpoints until they respond. compose up -d
# returns when containers are STARTED, not necessarily READY for HTTP —
# direct-control's own healthcheck has a 15 s start-period so the
# exerciser's first health probe can race the listener bind.
step "Waiting for backends ready"
deadline=$(( $(date +%s) + 60 ))
for url in http://localhost:8004/health http://localhost:8003/health; do
    while ! curl -sfo /dev/null "$url"; do
        if [ "$(date +%s)" -gt "$deadline" ]; then
            note "timeout waiting for $url (60 s)"
            note "container state: docker compose -f $COMPOSE ps"
            exit 1
        fi
        sleep 0.5
    done
    ok "ready: $url"
done

# ─── Run exerciser ──────────────────────────────────────────────────────
if [ "$RUN_EXERCISER" = "1" ]; then
    step "Running exerciser (38 checks across all 6 IOCs)"
    if "$EXERCISER"; then
        ok "exerciser: ALL CHECKS PASSED"
    else
        rc=$?
        note "exerciser exited rc=$rc — pod left running for triage"
        note "tail logs with: docker compose -f $COMPOSE logs <service>"
        exit $rc
    fi
else
    step "Skipping exerciser (--skip-exerciser)"
    note "Pod is up. Poke at it with curl examples in README.md."
fi

# ─── Optional teardown ──────────────────────────────────────────────────
if [ "$TEAR_DOWN" = "1" ]; then
    step "Tearing down (--tear-down)"
    docker compose -f "$COMPOSE" down
    ok "pod removed"
elif [ "$TAIL_LOGS" = "1" ]; then
    step "Tailing direct-control logs (Ctrl-C to stop; pod stays up)"
    docker compose -f "$COMPOSE" logs -f direct_control_service
else
    printf "\nPod left running at:\n"
    printf "  config-service : http://localhost:8004\n"
    printf "  direct-control : http://localhost:8003\n"
    printf "Tear down with: docker compose -f %s down\n" "$COMPOSE"
fi
