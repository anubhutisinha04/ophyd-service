#!/usr/bin/env bash
#
# reproduce.sh — stand up the REAL upstream bluesky-queueserver + bluesky-httpserver
# against an NSLS-II beamline profile collection, on any machine.
#
# It provisions the external dependencies the profile's startup code expects
# (discovered by opening the IOS profile under the queueserver), then launches
# the RE Manager and HTTP server from the profile's own pixi `qs` environment —
# exactly as the production ansible `bsqs` role does.
#
# Dependencies provisioned (Docker):
#   - redis (queue store)      : plain, port 60590   -> the RE Manager's queue/history
#   - redis (RE.md metadata)   : TLS,   port 6380    -> nslsii.configure_base() store
#   - mongodb                  : port 27017          -> databroker / tiled catalog
# Config files generated:
#   - tiled profile <endstation> -> the mongo catalog (databroker Broker.named)
#   - kafka.yml (broker-less, non-fatal)             -> nslsii Kafka publisher config
#   - ~/.pyOlog.conf                                 -> stops pyOlog prompting for a password
#   - redis TLS cert + password secret file
# Services launched (pixi, from the profile's qs env):
#   - start-re-manager --config ...                  -> 0MQ on IPC sockets
#   - uvicorn bluesky_httpserver.server:app          -> HTTP API on port 60610
#
# Kafka broker and an EPICS IOC are intentionally NOT started: the IOS profile
# opens without them (Kafka publishing is non-fatal; devices do not force PV
# connections at environment-open). Enable a catch-all IOC with WITH_IOC=1 if a
# profile you try does force connections.
#
# Usage:
#   ./reproduce.sh up          # provision + launch + open environment + verify
#   ./reproduce.sh verify      # re-run the plan/device count check
#   ./reproduce.sh status      # show container + service + environment status
#   ./reproduce.sh logs        # tail the RE Manager and HTTP server logs
#   ./reproduce.sh down        # stop services + remove containers (keeps the clone + env)
#   ./reproduce.sh nuke        # down + delete the whole work directory
#
# Configure via environment variables (defaults shown):
#   ENDSTATION=ios
#   PROFILE_REPO=https://github.com/NSLS2/ios-profile-collection.git
#   PROFILE_BRANCH=main
#   QS_REPRO_HOME=$HOME/qs-repro           # work dir (clone, configs, sockets, logs)
#   HTTP_PORT=60610                        # host port for the HTTP API
#   REDIS_QUEUE_PORT=60590                 # host port for the queue-store redis
#   REDIS_TLS_PORT=6380                    # host port for the RE.md TLS redis
#   MONGO_PORT=27017                       # host port for mongodb
#   REDIS_TLS_PASSWORD=<generated>         # RE.md redis password
#   HTTP_API_KEY=<generated>               # httpserver single-user API key (alphanumeric)
#   WITH_IOC=0                             # 1 = also run a caproto catch-all IOC
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENDSTATION="${ENDSTATION:-ios}"
PROFILE_REPO="${PROFILE_REPO:-https://github.com/NSLS2/${ENDSTATION}-profile-collection.git}"
PROFILE_BRANCH="${PROFILE_BRANCH:-main}"
QS_REPRO_HOME="${QS_REPRO_HOME:-$HOME/qs-repro}"

HTTP_PORT="${HTTP_PORT:-60610}"
REDIS_QUEUE_PORT="${REDIS_QUEUE_PORT:-60590}"
REDIS_TLS_PORT="${REDIS_TLS_PORT:-6380}"
MONGO_PORT="${MONGO_PORT:-27017}"
WITH_IOC="${WITH_IOC:-0}"

PROFILE_DIR="${PROFILE_DIR:-$QS_REPRO_HOME/profile_collection}"
CONFIG_DIR="$QS_REPRO_HOME/config"
RUN_DIR="$QS_REPRO_HOME/run"
CERT_DIR="$CONFIG_DIR/certs"

CTRL_SOCK="ipc://$RUN_DIR/qs-ctrl.sock"
INFO_SOCK="ipc://$RUN_DIR/qs-info.sock"

CT_PREFIX="qsrepro"
CT_REDIS_QUEUE="${CT_PREFIX}-redis-queue"
CT_REDIS_TLS="${CT_PREFIX}-redis-tls"
CT_MONGO="${CT_PREFIX}-mongo"
CT_IOC="${CT_PREFIX}-ioc"

# Persist generated secrets across invocations.
SECRETS_ENV="$CONFIG_DIR/secrets.env"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
    B=''; G=''; Y=''; R=''; N=''
fi
step() { printf "\n${B}== %s ==${N}\n" "$1"; }
ok()   { printf "  ${G}%s${N}\n" "$1"; }
note() { printf "  ${Y}%s${N}\n" "$1"; }
die()  { printf "${R}error: %s${N}\n" "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

ensure_pixi() {
    if command -v pixi >/dev/null 2>&1; then return; fi
    if [ -x "$HOME/.pixi/bin/pixi" ]; then export PATH="$HOME/.pixi/bin:$PATH"; return; fi
    note "pixi not found; installing to ~/.pixi ..."
    curl -fsSL https://pixi.sh/install.sh | bash >/dev/null
    export PATH="$HOME/.pixi/bin:$PATH"
    command -v pixi >/dev/null 2>&1 || die "pixi install failed"
}

docker_() {
    # Prefer docker; fall back to podman with a compatible CLI.
    if command -v docker >/dev/null 2>&1; then docker "$@";
    elif command -v podman >/dev/null 2>&1; then podman "$@";
    else die "neither docker nor podman found"; fi
}

preflight() {
    need_cmd git
    need_cmd curl
    need_cmd openssl
    command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1 \
        || die "docker (or podman) is required for redis/mongodb"
    ensure_pixi
}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
load_or_make_secrets() {
    mkdir -p "$CONFIG_DIR"
    if [ -f "$SECRETS_ENV" ]; then
        # shellcheck disable=SC1090
        . "$SECRETS_ENV"
    fi
    REDIS_TLS_PASSWORD="${REDIS_TLS_PASSWORD:-$(openssl rand -hex 16)}"
    # httpserver API key must be alphanumeric only.
    HTTP_API_KEY="${HTTP_API_KEY:-$(openssl rand -hex 16)}"
    cat > "$SECRETS_ENV" <<EOF
REDIS_TLS_PASSWORD=$REDIS_TLS_PASSWORD
HTTP_API_KEY=$HTTP_API_KEY
EOF
    chmod 600 "$SECRETS_ENV"
}

# ---------------------------------------------------------------------------
# Profile collection
# ---------------------------------------------------------------------------
clone_profile() {
    step "Profile collection ($ENDSTATION)"
    if [ -d "$PROFILE_DIR/.git" ]; then
        ok "already cloned at $PROFILE_DIR"
    elif [ -d "$PROFILE_DIR/startup" ]; then
        ok "using existing (non-git) profile at $PROFILE_DIR"
    else
        note "cloning $PROFILE_REPO ($PROFILE_BRANCH)"
        mkdir -p "$(dirname "$PROFILE_DIR")"
        git clone --branch "$PROFILE_BRANCH" "$PROFILE_REPO" "$PROFILE_DIR"
        ok "cloned to $PROFILE_DIR"
    fi
    [ -f "$PROFILE_DIR/pixi.toml" ] || die "no pixi.toml in $PROFILE_DIR"
    grep -q '^\[feature.qs' "$PROFILE_DIR/pixi.toml" 2>/dev/null \
        || note "profile pixi.toml has no [feature.qs...]; the 'qs' env may not solve"
}

install_env() {
    step "pixi qs environment"
    note "installing (first run downloads the full beamline stack; may take a while)"
    pixi install --manifest-path "$PROFILE_DIR/pixi.toml" -e qs
    ok "qs environment ready"
}

pixi_qs() { pixi run --manifest-path "$PROFILE_DIR/pixi.toml" -e qs "$@"; }

# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------
gen_configs() {
    step "Configuration files"
    mkdir -p "$CONFIG_DIR" "$RUN_DIR" "$CERT_DIR" \
             "$CONFIG_DIR/tiled/profiles"

    # --- redis TLS cert (local analog of the production ACME cert) ---
    if [ ! -f "$CERT_DIR/redis.crt" ]; then
        openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
            -keyout "$CERT_DIR/redis.key" -out "$CERT_DIR/redis.crt" \
            -subj "/CN=localhost" \
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
        chmod 644 "$CERT_DIR/redis.crt" "$CERT_DIR/redis.key"
    fi
    printf '%s' "$REDIS_TLS_PASSWORD" > "$CONFIG_DIR/redis.secret"
    chmod 600 "$CONFIG_DIR/redis.secret"
    ok "redis TLS cert + secret"

    # --- tiled profile: <endstation> -> the mongo catalog ---
    cat > "$CONFIG_DIR/tiled/profiles/${ENDSTATION}.yml" <<EOF
${ENDSTATION}:
  direct:
    authentication:
      allow_anonymous_access: true
    trees:
      - tree: databroker.mongo_normalized:Tree.from_uri
        path: /
        args:
          uri: mongodb://localhost:${MONGO_PORT}/metadatastore-local
          asset_registry_uri: mongodb://localhost:${MONGO_PORT}/asset-registry-local
EOF
    ok "tiled profile '${ENDSTATION}'"

    # --- kafka config (broker-less; abort disabled so runs never fail on kafka) ---
    cat > "$CONFIG_DIR/kafka.yml" <<EOF
---
  abort_run_on_kafka_exception: false
  bootstrap_servers:
    - 127.0.0.1:9092
  runengine_producer_config:
    security.protocol: PLAINTEXT
EOF
    ok "kafka.yml (broker-less)"

    # --- pyOlog client config (avoids the interactive password prompt) ---
    if [ -f "$HOME/.pyOlog.conf" ]; then
        note "$HOME/.pyOlog.conf exists; leaving it untouched"
    else
        cat > "$HOME/.pyOlog.conf" <<'EOF'
[DEFAULT]
url = http://localhost
logbooks = test
username = test
password = test
EOF
        ok "wrote ~/.pyOlog.conf"
    fi

    # --- RE Manager config YAML (mirrors the bsqs role's config) ---
    cat > "$CONFIG_DIR/queueserver-config.yml" <<EOF
network:
  zmq_control_addr: "$CTRL_SOCK"
  zmq_info_addr: "$INFO_SOCK"
  zmq_publish_console: true
  redis_addr: "localhost:${REDIS_QUEUE_PORT}"
startup:
  keep_re: true
  startup_dir: "$PROFILE_DIR/startup"
  existing_plans_and_devices_path: "$RUN_DIR/existing_plans_and_devices.yaml"
  user_group_permissions_path: "$CONFIG_DIR/user_group_permissions.yaml"
worker:
  use_ipython_kernel: false
operation:
  print_console_output: true
  console_logging_level: NORMAL
EOF

    # user_group_permissions: prefer the profile's own; else a permissive default.
    if [ -f "$PROFILE_DIR/startup/user_group_permissions.yaml" ]; then
        cp "$PROFILE_DIR/startup/user_group_permissions.yaml" "$CONFIG_DIR/user_group_permissions.yaml"
        ok "user_group_permissions.yaml (from profile)"
    else
        cat > "$CONFIG_DIR/user_group_permissions.yaml" <<'EOF'
user_groups:
  root:
    allowed_plans: [null]
    forbidden_plans: [":^_"]
    allowed_devices: [null]
    forbidden_devices: [":^_:?.*"]
    allowed_functions: [null]
    forbidden_functions: [":^_"]
  primary:
    allowed_plans: [":.*"]
    forbidden_plans: [null]
    allowed_devices: [":?.*:depth=5"]
    forbidden_devices: [null]
    allowed_functions: [null]
EOF
        ok "user_group_permissions.yaml (permissive default)"
    fi

    # --- HTTP server config YAML ---
    cat > "$CONFIG_DIR/httpserver-config.yml" <<EOF
qserver_zmq_configuration:
  control_address: $CTRL_SOCK
  info_address: $INFO_SOCK
authentication:
  allow_anonymous_access: True
api_access:
  policy: bluesky_httpserver.authorization:BasicAPIAccessControl
  args:
    roles:
      unauthenticated_public:
        scopes_add: read:console
      unauthenticated_single_user:
        scopes_remove:
          - write:scripts
          - user:apikeys
EOF
    ok "queueserver + httpserver config YAMLs"
}

# ---------------------------------------------------------------------------
# Infrastructure containers
# ---------------------------------------------------------------------------
ct_running() { docker_ ps --format '{{.Names}}' | grep -qx "$1"; }
ct_exists()  { docker_ ps -a --format '{{.Names}}' | grep -qx "$1"; }

start_container() {
    # $1 name; rest = docker run args (after --name)
    local name="$1"; shift
    if ct_running "$name"; then ok "$name already running"; return; fi
    if ct_exists "$name"; then docker_ rm -f "$name" >/dev/null; fi
    docker_ run -d --name "$name" "$@" >/dev/null
    ok "$name started"
}

start_infra() {
    step "Infrastructure (redis x2, mongodb)"

    # queue store: plain redis, no auth (matches the production `redis` role's
    # bluesky-queueserver-redis on port 60590).
    start_container "$CT_REDIS_QUEUE" \
        -p "${REDIS_QUEUE_PORT}:6379" \
        docker.io/redis:7 \
        redis-server --save '' --appendonly no

    # RE.md metadata store: TLS redis with password (matches the redis6 role).
    start_container "$CT_REDIS_TLS" \
        -p "${REDIS_TLS_PORT}:6380" \
        -v "$CERT_DIR/redis.crt:/certs/redis.crt:ro" \
        -v "$CERT_DIR/redis.key:/certs/redis.key:ro" \
        docker.io/redis:7 \
        redis-server --port 0 --tls-port 6380 \
            --tls-cert-file /certs/redis.crt \
            --tls-key-file /certs/redis.key \
            --tls-ca-cert-file /certs/redis.crt \
            --tls-auth-clients no \
            --tls-protocols "TLSv1.2 TLSv1.3" \
            --requirepass "$REDIS_TLS_PASSWORD" \
            --save '' --appendonly no

    # databroker / tiled catalog.
    start_container "$CT_MONGO" \
        -p "${MONGO_PORT}:27017" \
        docker.io/mongo:6

    if [ "$WITH_IOC" = "1" ]; then
        note "WITH_IOC=1: starting a caproto catch-all IOC on host network"
        start_container "$CT_IOC" --network host \
            docker.io/python:3.12-slim \
            bash -lc "pip install -q caproto && python -m caproto.ioc_examples.mini_beamline"
    fi

    wait_infra
}

wait_infra() {
    note "waiting for infrastructure to accept connections ..."
    for _ in $(seq 1 30); do
        if docker_ exec "$CT_REDIS_QUEUE" redis-cli ping 2>/dev/null | grep -q PONG \
           && docker_ exec "$CT_MONGO" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1 \
           && docker_ exec "$CT_REDIS_TLS" redis-cli --tls -p 6380 \
                --cert /certs/redis.crt --key /certs/redis.key --cacert /certs/redis.crt \
                -a "$REDIS_TLS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
            ok "redis (queue + TLS) and mongodb are up"
            return
        fi
        sleep 2
    done
    die "infrastructure did not become ready"
}

# ---------------------------------------------------------------------------
# Services (real bluesky-queueserver + bluesky-httpserver, via pixi qs env)
# ---------------------------------------------------------------------------
pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_services() {
    step "RE Manager + HTTP server (from the profile's qs env)"
    rm -f "$RUN_DIR"/qs-*.sock 2>/dev/null || true

    # Launch detached with `setsid env ... &`, redirecting all fds to the log.
    # Do NOT wrap in a `( ... )` subshell or a shell function: a lingering
    # wrapper process would keep this script's stdout pipe open and block
    # callers that read it (e.g. `reproduce.sh up | tee`).

    if pid_alive "$RUN_DIR/manager.pid"; then
        ok "RE Manager already running"
    else
        setsid env \
            MPLBACKEND=Agg \
            REDIS_HOST=localhost \
            REDIS_PORT="$REDIS_TLS_PORT" \
            REDIS_SECRET_FILE="$CONFIG_DIR/redis.secret" \
            SSL_CERT_FILE="$CERT_DIR/redis.crt" \
            REQUESTS_CA_BUNDLE="$CERT_DIR/redis.crt" \
            BLUESKY_KAFKA_CONFIG_PATH="$CONFIG_DIR/kafka.yml" \
            TILED_PROFILES="$CONFIG_DIR/tiled/profiles" \
            pixi run --manifest-path "$PROFILE_DIR/pixi.toml" -e qs \
            start-re-manager --config "$CONFIG_DIR/queueserver-config.yml" \
            > "$RUN_DIR/manager.log" 2>&1 < /dev/null &
        echo $! > "$RUN_DIR/manager.launcher.pid"
        sleep 8
        # record the actual manager pid (child of the launcher)
        pgrep -f "start-re-manager --config $CONFIG_DIR/queueserver-config.yml" | head -1 \
            > "$RUN_DIR/manager.pid" || true
        ok "RE Manager launched (log: $RUN_DIR/manager.log)"
    fi

    if pid_alive "$RUN_DIR/httpserver.pid"; then
        ok "HTTP server already running"
    else
        setsid env \
            QSERVER_HTTP_SERVER_CONFIG="$CONFIG_DIR/httpserver-config.yml" \
            QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY="$HTTP_API_KEY" \
            pixi run --manifest-path "$PROFILE_DIR/pixi.toml" -e qs \
            uvicorn --host 0.0.0.0 --port "$HTTP_PORT" bluesky_httpserver.server:app \
            > "$RUN_DIR/httpserver.log" 2>&1 < /dev/null &
        echo $! > "$RUN_DIR/httpserver.launcher.pid"
        sleep 6
        pgrep -f "uvicorn --host 0.0.0.0 --port $HTTP_PORT bluesky_httpserver" | head -1 \
            > "$RUN_DIR/httpserver.pid" || true
        ok "HTTP server launched (log: $RUN_DIR/httpserver.log)"
    fi
}

open_environment() {
    step "Opening the RE worker environment (loads the profile)"
    pixi_qs qserver environment open --zmq-control-addr "$CTRL_SOCK" >/dev/null 2>&1 || true
    local state
    for _ in $(seq 1 40); do
        state="$(pixi_qs qserver status --zmq-control-addr "$CTRL_SOCK" 2>/dev/null || true)"
        if echo "$state" | grep -q "'worker_environment_exists': True"; then
            ok "environment ready"
            return 0
        fi
        # surface a hard startup failure early
        if grep -q "Error while executing script" "$RUN_DIR/manager.log" 2>/dev/null; then
            note "startup error detected — see 'reproduce.sh logs'"
            return 1
        fi
        sleep 3
    done
    note "environment did not open within timeout — see 'reproduce.sh logs'"
    return 1
}

verify() {
    step "Verification"
    local plans devices
    plans="$(pixi_qs qserver allowed plans --zmq-control-addr "$CTRL_SOCK" 2>/dev/null \
             | grep -oE "'[A-Za-z0-9_]+': '\{\.\.\.\}'" | wc -l | tr -d ' ')"
    devices="$(pixi_qs qserver allowed devices --zmq-control-addr "$CTRL_SOCK" 2>/dev/null \
               | grep -oE "'[A-Za-z0-9_]+': '\{\.\.\.\}'" | wc -l | tr -d ' ')"
    ok "allowed plans:   $plans"
    ok "allowed devices: $devices"
    echo
    ok "HTTP API:  http://localhost:${HTTP_PORT}   (Swagger UI at /docs)"
    ok "API key:   $HTTP_API_KEY"
    note "example: curl -s http://localhost:${HTTP_PORT}/api/status -H \"Authorization: ApiKey $HTTP_API_KEY\""
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
stop_services() {
    for svc in httpserver manager; do
        if [ -f "$RUN_DIR/$svc.pid" ]; then
            pkill_tree "$(cat "$RUN_DIR/$svc.pid")" || true
        fi
        # also clean the launcher and any stragglers by config path
        [ -f "$RUN_DIR/$svc.launcher.pid" ] && kill "$(cat "$RUN_DIR/$svc.launcher.pid")" 2>/dev/null || true
        rm -f "$RUN_DIR/$svc.pid" "$RUN_DIR/$svc.launcher.pid"
    done
    pkill -f "start-re-manager --config $CONFIG_DIR/queueserver-config.yml" 2>/dev/null || true
    pkill -f "uvicorn --host 0.0.0.0 --port $HTTP_PORT bluesky_httpserver" 2>/dev/null || true
    rm -f "$RUN_DIR"/qs-*.sock 2>/dev/null || true
}

pkill_tree() { pkill -P "$1" 2>/dev/null || true; kill "$1" 2>/dev/null || true; }

stop_infra() {
    for c in "$CT_IOC" "$CT_MONGO" "$CT_REDIS_TLS" "$CT_REDIS_QUEUE"; do
        ct_exists "$c" && docker_ rm -f "$c" >/dev/null && ok "removed $c" || true
    done
}

cmd_up() {
    preflight
    mkdir -p "$QS_REPRO_HOME"
    load_or_make_secrets
    clone_profile
    install_env
    gen_configs
    start_infra
    start_services
    if open_environment; then
        verify
        step "Done"
        ok "the profile opened successfully under the real queueserver"
    else
        step "Startup did not complete"
        note "inspect: $RUN_DIR/manager.log"
        note "stop:    $0 down"
        exit 1
    fi
}

cmd_verify() { load_or_make_secrets; verify; }

cmd_status() {
    step "Containers"; docker_ ps --filter "name=${CT_PREFIX}-" \
        --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
    step "Services"
    pid_alive "$RUN_DIR/manager.pid"    && ok "RE Manager: running ($(cat "$RUN_DIR/manager.pid"))"    || note "RE Manager: not running"
    pid_alive "$RUN_DIR/httpserver.pid" && ok "HTTP server: running ($(cat "$RUN_DIR/httpserver.pid"))" || note "HTTP server: not running"
    step "Environment"
    ensure_pixi
    pixi_qs qserver status --zmq-control-addr "$CTRL_SOCK" 2>/dev/null \
        | grep -E "manager_state|worker_environment_exists|items_in" || note "manager not reachable"
}

cmd_logs() {
    step "RE Manager log (tail)"; tail -n 40 "$RUN_DIR/manager.log" 2>/dev/null || note "no manager log"
    step "HTTP server log (tail)"; tail -n 20 "$RUN_DIR/httpserver.log" 2>/dev/null || note "no httpserver log"
}

cmd_down() { step "Stopping"; stop_services; stop_infra; ok "stopped"; }

cmd_nuke() {
    cmd_down
    step "Removing work directory"
    rm -rf "$QS_REPRO_HOME"
    ok "removed $QS_REPRO_HOME"
}

usage() { sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-up}" in
    up)      cmd_up ;;
    verify)  cmd_verify ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    down)    cmd_down ;;
    nuke)    cmd_nuke ;;
    -h|--help|help) usage ;;
    *) die "unknown command '$1' (try: up | verify | status | logs | down | nuke)" ;;
esac
