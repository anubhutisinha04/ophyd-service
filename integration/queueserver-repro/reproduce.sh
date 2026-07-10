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
#   - kafka (KRaft broker)     : port 9092           -> RunEngine document publishing
#                                                       (needed to RUN plans; WITH_KAFKA=1)
# Config files generated:
#   - tiled profile <endstation> -> the mongo catalog (databroker Broker.named)
#   - kafka.yml                                      -> nslsii Kafka publisher config
#   - ~/.pyOlog.conf                                 -> points the Olog callback at the mock Olog
#   - redis TLS cert + password secret file
# Extra host services launched:
#   - mock Olog server (:8181): the IOS profile's Olog logbook callback POSTs a
#     logbook entry on every run start; production runs a real Olog, so plans
#     need a responsive one here (WITH_OLOG=1).
# Services launched (pixi, from the profile's qs env):
#   - start-re-manager --config ...                  -> 0MQ on IPC sockets
#   - uvicorn bluesky_httpserver.server:app          -> HTTP API on port 60610
#   - six simulated IOS IOCs (caproto), one CA port each, plus a catch-all
#     "blackhole" IOC, so plans can actually read/move the profile's devices
#     (WITH_IOS_IOCS=1, on by default)
#
# The simulated IOCs are the purpose-built IOS caproto servers from the
# demo/ios-nsls2 work (pgm, curramp, epu, vortex, scaler, feedback). Each runs
# as its own Channel Access server on 127.0.0.1:<port>. Because the IOS profile
# force-connects ~100 devices at startup, a catch-all blackhole IOC answers
# every OTHER PV so the whole profile opens; it is told not to answer the six
# realistic prefixes, so those IOCs serve them without a duplicate-PV race. The
# RE worker reaches them all via an explicit EPICS_CA_ADDR_LIST with
# EPICS_CA_AUTO_ADDR_LIST=NO. In production these run on separate IOC hosts,
# reached over the queueserver VM's dedicated (broadcast-disabled) EPICS NIC —
# hence explicit addresses, no auto-discovery.
#
# Running a plan additionally requires a Kafka broker (document publishing) and
# a responsive Olog server (the profile's logbook callback) — both provided
# here. WITH_IOC=1 additionally runs a caproto catch-all IOC for profiles that
# need PVs the six IOS IOCs don't serve.
#
# Usage:
#   ./reproduce.sh up          # provision + launch IOCs + services + open + verify
#   ./reproduce.sh verify      # re-run the plan/device count check
#   ./reproduce.sh status      # show container + IOC + service + environment status
#   ./reproduce.sh logs        # tail the RE Manager and HTTP server logs
#   ./reproduce.sh down        # stop services + IOCs + remove containers
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
#   WITH_IOS_IOCS=1                        # 0 = don't start the simulated IOS IOCs
#   WITH_REALISTIC_IOCS=1                  # 0 = blackhole catch-all only (no realistic values)
#   WITH_BLACKHOLE=1                       # 0 = no catch-all (profile likely won't fully open)
#   IOC_BASE_PORT=5064                     # first IOC's CA port (others step by 2)
#   WITH_KAFKA=1                           # 0 = no kafka broker (plans that publish may block)
#   KAFKA_PORT=9092                        # host port for the kafka broker
#   WITH_OLOG=1                            # 0 = no mock Olog (plans may fail on the logbook callback)
#   OLOG_PORT=8181                         # host port for the mock Olog server
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

# Kafka broker. Opening the profile does not need one, but RUNNING a plan does:
# the RunEngine publishes documents to Kafka, and nslsii's publisher blocks if
# the broker is unreachable. A single-node KRaft broker on :9092 (as the
# NSLS-II profile-testing CI uses) lets plans complete. On by default here
# because this branch's purpose is running plans (e.g. E_ramp).
WITH_KAFKA="${WITH_KAFKA:-1}"
KAFKA_PORT="${KAFKA_PORT:-9092}"

# Mock Olog server. The IOS profile subscribes an Olog logbook callback to the
# RunEngine (nslsii.configure_olog) that HTTP-POSTs a logbook entry on every run
# start. Production runs a real Olog (ansible-epics-tools); without a responsive
# server the callback errors and the plan cannot complete. This minimal mock
# implements just enough of the pyOlog REST API to accept those entries.
WITH_OLOG="${WITH_OLOG:-1}"
OLOG_PORT="${OLOG_PORT:-8181}"

# Simulated IOS IOCs (caproto). This branch adds the six purpose-built IOS
# simulation IOCs from the demo/ios-nsls2 work, so plans can actually read and
# move the profile's devices. Each IOC is a separate caproto Channel Access
# server on its own CA port (production runs them on separate IOC hosts reached
# over the queueserver VM's dedicated EPICS NIC; here they are localhost:PORT).
WITH_IOS_IOCS="${WITH_IOS_IOCS:-1}"
IOC_BASE_PORT="${IOC_BASE_PORT:-5064}"
IOS_IOCS="ioc_ios_pgm ioc_ios_curramp ioc_ios_epu ioc_ios_vortex ioc_ios_scaler ioc_ios_feedback"

# The six realistic IOCs each cover one device family. Toggle them off to run
# the blackhole alone (still opens the whole profile, just no realistic values).
WITH_REALISTIC_IOCS="${WITH_REALISTIC_IOCS:-1}"

# The IOS profile force-connects ~100 devices at startup. A catch-all
# "blackhole" IOC answers every PV that the realistic IOCs do NOT serve, so the
# whole profile opens. The realistic IOCs are started with --list-pvs; their
# exact PV names are harvested and handed to the blackhole so it defers to them
# (no Channel Access duplicate-PV race), while still answering the sub-PVs of
# those same devices that the realistic IOCs happen not to serve.
WITH_BLACKHOLE="${WITH_BLACKHOLE:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOC_DIR="${IOC_DIR:-$SCRIPT_DIR/iocs}"

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
CT_KAFKA="${CT_PREFIX}-kafka"
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

    # --- kafka config: point at the local broker (WITH_KAFKA) or nowhere ---
    # abort_run_on_kafka_exception stays false so a flaky broker never fails a
    # run; the broker itself is what lets document publishing (and thus the
    # plan) complete without blocking.
    cat > "$CONFIG_DIR/kafka.yml" <<EOF
---
  abort_run_on_kafka_exception: false
  bootstrap_servers:
    - 127.0.0.1:${KAFKA_PORT}
  runengine_producer_config:
    security.protocol: PLAINTEXT
EOF
    if [ "$WITH_KAFKA" = "1" ]; then
        ok "kafka.yml (broker on 127.0.0.1:${KAFKA_PORT})"
    else
        note "kafka.yml written but WITH_KAFKA=0 — plans that publish may block"
    fi

    # --- pyOlog client config: point at the mock Olog (or leave a placeholder) ---
    # Written every run so the url matches the mock Olog port. Any pre-existing
    # user file is backed up once.
    local olog_url="http://localhost:${OLOG_PORT}"
    [ "$WITH_OLOG" = "1" ] || olog_url="http://localhost"
    if [ -f "$HOME/.pyOlog.conf" ] && [ ! -f "$HOME/.pyOlog.conf.qsrepro-bak" ]; then
        cp "$HOME/.pyOlog.conf" "$HOME/.pyOlog.conf.qsrepro-bak"
        note "backed up existing ~/.pyOlog.conf to ~/.pyOlog.conf.qsrepro-bak"
    fi
    cat > "$HOME/.pyOlog.conf" <<EOF
[DEFAULT]
url = ${olog_url}
logbooks = Data Acquisition
username = test
password = test
EOF
    ok "wrote ~/.pyOlog.conf (url=${olog_url})"

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
    if [ "$WITH_KAFKA" = "1" ]; then
        step "Infrastructure (redis x2, mongodb, kafka)"
    else
        step "Infrastructure (redis x2, mongodb)"
    fi

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

    # Kafka broker so the RunEngine can publish documents during a plan.
    # Single-node KRaft, as the NSLS-II profile-testing CI uses.
    if [ "$WITH_KAFKA" = "1" ]; then
        start_container "$CT_KAFKA" \
            -p "${KAFKA_PORT}:9092" -p 9093:9093 \
            -e KAFKA_NODE_ID=1 \
            -e KAFKA_PROCESS_ROLES=broker,controller \
            -e KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
            -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 \
            -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
            -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
            -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
            -e KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT \
            -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
            -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
            -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
            docker.io/apache/kafka:3.9.0
    fi

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
    local core=0
    for _ in $(seq 1 30); do
        if docker_ exec "$CT_REDIS_QUEUE" redis-cli ping 2>/dev/null | grep -q PONG \
           && docker_ exec "$CT_MONGO" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1 \
           && docker_ exec "$CT_REDIS_TLS" redis-cli --tls -p 6380 \
                --cert /certs/redis.crt --key /certs/redis.key --cacert /certs/redis.crt \
                -a "$REDIS_TLS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
            core=1; break
        fi
        sleep 2
    done
    [ "$core" = "1" ] || die "infrastructure did not become ready"
    ok "redis (queue + TLS) and mongodb are up"
    if [ "$WITH_KAFKA" = "1" ]; then
        note "waiting for the kafka broker ..."
        local ready=0
        for _ in $(seq 1 60); do
            if docker_ exec "$CT_KAFKA" \
                /opt/kafka/bin/kafka-broker-api-versions.sh \
                --bootstrap-server localhost:9092 >/dev/null 2>&1; then
                ready=1; break
            fi
            sleep 2
        done
        [ "$ready" = "1" ] && ok "kafka broker is up" \
            || note "kafka broker not confirmed ready — plans that publish may block"
    fi
}

# ---------------------------------------------------------------------------
# Mock Olog server (stdlib only; runs on system python3)
# ---------------------------------------------------------------------------
start_olog() {
    [ "$WITH_OLOG" = "1" ] || { note "WITH_OLOG=0: not starting the mock Olog"; return; }
    step "Mock Olog server"
    mkdir -p "$RUN_DIR"
    [ -f "$IOC_DIR/mock_olog.py" ] || die "mock_olog.py not found: $IOC_DIR/mock_olog.py"
    if pid_alive "$RUN_DIR/olog.pid"; then
        ok "mock Olog already running (port $OLOG_PORT)"
        return
    fi
    setsid python3 "$IOC_DIR/mock_olog.py" --host 127.0.0.1 --port "$OLOG_PORT" \
        > "$RUN_DIR/olog.log" 2>&1 < /dev/null &
    echo $! > "$RUN_DIR/olog.pid"
    sleep 1
    if grep -q "listening" "$RUN_DIR/olog.log" 2>/dev/null; then
        ok "mock Olog  ->  http://localhost:$OLOG_PORT"
    else
        note "mock Olog may not have started — see $RUN_DIR/olog.log"
    fi
}

stop_olog() {
    [ -f "$RUN_DIR/olog.pid" ] && pkill_tree "$(cat "$RUN_DIR/olog.pid")" || true
    pkill -f "mock_olog.py --host 127.0.0.1 --port $OLOG_PORT" 2>/dev/null || true
    rm -f "$RUN_DIR/olog.pid"
}

# ---------------------------------------------------------------------------
# Simulated IOS IOCs (caproto Channel Access servers)
# ---------------------------------------------------------------------------
pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

# CA port for the Nth IOC in $IOS_IOCS (step 2 so no port lands on the EPICS
# repeater port, base+1).
ioc_port() { echo $(( IOC_BASE_PORT + 2 * $1 )); }

# The blackhole catch-all gets the port just past the realistic IOCs.
blackhole_port() { echo $(( IOC_BASE_PORT + 2 * $(echo "$IOS_IOCS" | wc -w) )); }

# EPICS_CA_ADDR_LIST the RE worker uses to find every simulated IOC. Explicit
# addresses only (paired with EPICS_CA_AUTO_ADDR_LIST=NO) — the analog of the
# queueserver VM's locked-down EPICS NIC where broadcast discovery is off.
epics_addr_list() {
    local list="" i=0 name
    if [ "$WITH_REALISTIC_IOCS" = "1" ]; then
        for name in $IOS_IOCS; do
            list="$list 127.0.0.1:$(ioc_port $i)"
            i=$((i + 1))
        done
    fi
    [ "$WITH_BLACKHOLE" = "1" ] && list="$list 127.0.0.1:$(blackhole_port)"
    echo "${list# }"
}

start_iocs() {
    if [ "$WITH_IOS_IOCS" != "1" ]; then
        note "WITH_IOS_IOCS=0: not starting simulated IOCs"
        return
    fi
    step "Simulated IOS IOCs (caproto, one CA port each)"
    mkdir -p "$RUN_DIR/iocs"
    local exclude_file="$RUN_DIR/iocs/exclude_pvs.txt"
    : > "$exclude_file"

    # 1) Realistic per-device IOCs, each with --list-pvs so we can harvest the
    #    exact PVs it owns.
    local i=0 name port
    if [ "$WITH_REALISTIC_IOCS" = "1" ]; then
        for name in $IOS_IOCS; do
            port="$(ioc_port $i)"
            i=$((i + 1))
            [ -f "$IOC_DIR/$name.py" ] || die "IOC script not found: $IOC_DIR/$name.py"
            if pid_alive "$RUN_DIR/iocs/$name.pid"; then
                ok "$name already running (CA port $port)"
                continue
            fi
            setsid env EPICS_CA_SERVER_PORT="$port" \
                pixi run --manifest-path "$PROFILE_DIR/pixi.toml" -e qs \
                python "$IOC_DIR/$name.py" --list-pvs --interfaces 127.0.0.1 \
                > "$RUN_DIR/iocs/$name.log" 2>&1 < /dev/null &
            echo $! > "$RUN_DIR/iocs/$name.launcher.pid"
        done
        sleep 6
        i=0
        for name in $IOS_IOCS; do
            port="$(ioc_port $i)"
            i=$((i + 1))
            pgrep -f "python $IOC_DIR/$name.py" | head -1 > "$RUN_DIR/iocs/$name.pid" || true
            if grep -q "Listening on" "$RUN_DIR/iocs/$name.log" 2>/dev/null; then
                ok "$name  ->  CA port $port"
            else
                note "$name may not have started — see $RUN_DIR/iocs/$name.log"
            fi
            # Harvest the exact PV names this IOC serves (from its --list-pvs dump).
            grep -oE 'XF:[^ ]+' "$RUN_DIR/iocs/$name.log" 2>/dev/null >> "$exclude_file" || true
        done
        sort -u -o "$exclude_file" "$exclude_file"
        ok "harvested $(wc -l < "$exclude_file" | tr -d ' ') realistic PVs (blackhole will defer)"
    else
        note "WITH_REALISTIC_IOCS=0: running the blackhole catch-all only"
    fi

    # 2) Catch-all so every other device PV resolves and the profile opens.
    if [ "$WITH_BLACKHOLE" = "1" ]; then
        local bhport; bhport="$(blackhole_port)"
        [ -f "$IOC_DIR/blackhole_ioc.py" ] || die "blackhole IOC not found: $IOC_DIR/blackhole_ioc.py"
        if pid_alive "$RUN_DIR/iocs/blackhole.pid"; then
            ok "blackhole already running (CA port $bhport)"
        else
            setsid env EPICS_CA_SERVER_PORT="$bhport" \
                BLACKHOLE_EXCLUDE_PVS_FILE="$exclude_file" \
                pixi run --manifest-path "$PROFILE_DIR/pixi.toml" -e qs \
                python "$IOC_DIR/blackhole_ioc.py" --interfaces 127.0.0.1 \
                > "$RUN_DIR/iocs/blackhole.log" 2>&1 < /dev/null &
            echo $! > "$RUN_DIR/iocs/blackhole.launcher.pid"
            sleep 4
            pgrep -f "python $IOC_DIR/blackhole_ioc.py" | head -1 > "$RUN_DIR/iocs/blackhole.pid" || true
            if grep -q "Listening on" "$RUN_DIR/iocs/blackhole.log" 2>/dev/null; then
                ok "blackhole (catch-all)  ->  CA port $bhport"
            else
                note "blackhole may not have started — see $RUN_DIR/iocs/blackhole.log"
            fi
        fi
    fi
}

# ---------------------------------------------------------------------------
# Services (real bluesky-queueserver + bluesky-httpserver, via pixi qs env)
# ---------------------------------------------------------------------------

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
        # Point the RE worker at the simulated IOCs (explicit addresses, no
        # broadcast) when IOCs are enabled; otherwise leave EPICS discovery
        # at its normal defaults.
        local epics_auto="YES" epics_list=""
        if [ "$WITH_IOS_IOCS" = "1" ]; then
            epics_auto="NO"
            epics_list="$(epics_addr_list)"
        fi
        setsid env \
            MPLBACKEND=Agg \
            EPICS_CA_AUTO_ADDR_LIST="$epics_auto" \
            EPICS_CA_ADDR_LIST="$epics_list" \
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
    stop_iocs
    stop_olog
}

stop_iocs() {
    local name
    for name in $IOS_IOCS blackhole; do
        [ -f "$RUN_DIR/iocs/$name.pid" ] && pkill_tree "$(cat "$RUN_DIR/iocs/$name.pid")" || true
        [ -f "$RUN_DIR/iocs/$name.launcher.pid" ] && kill "$(cat "$RUN_DIR/iocs/$name.launcher.pid")" 2>/dev/null || true
        rm -f "$RUN_DIR/iocs/$name.pid" "$RUN_DIR/iocs/$name.launcher.pid"
    done
    for name in $IOS_IOCS; do pkill -f "python $IOC_DIR/$name.py" 2>/dev/null || true; done
    pkill -f "python $IOC_DIR/blackhole_ioc.py" 2>/dev/null || true
}

pkill_tree() { pkill -P "$1" 2>/dev/null || true; kill "$1" 2>/dev/null || true; }

stop_infra() {
    for c in "$CT_IOC" "$CT_KAFKA" "$CT_MONGO" "$CT_REDIS_TLS" "$CT_REDIS_QUEUE"; do
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
    start_olog
    start_iocs
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
    if [ "$WITH_IOS_IOCS" = "1" ]; then
        step "Simulated IOCs"
        local i=0 name
        if [ "$WITH_REALISTIC_IOCS" = "1" ]; then
            for name in $IOS_IOCS; do
                pid_alive "$RUN_DIR/iocs/$name.pid" \
                    && ok "$name: running (CA port $(ioc_port $i))" \
                    || note "$name: not running"
                i=$((i + 1))
            done
        fi
        if [ "$WITH_BLACKHOLE" = "1" ]; then
            pid_alive "$RUN_DIR/iocs/blackhole.pid" \
                && ok "blackhole: running (CA port $(blackhole_port))" \
                || note "blackhole: not running"
        fi
    fi
    if [ "$WITH_OLOG" = "1" ]; then
        step "Mock Olog"
        pid_alive "$RUN_DIR/olog.pid" \
            && ok "mock Olog: running (http://localhost:$OLOG_PORT)" \
            || note "mock Olog: not running"
    fi
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

usage() { sed -n '2,77p' "$0" | sed 's/^# \{0,1\}//'; }

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
