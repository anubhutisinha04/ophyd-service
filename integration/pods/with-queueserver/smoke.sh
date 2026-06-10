#!/usr/bin/env bash
# Side-B smoke for the with-queueserver pod: the three backends co-run AND the
# queueserver <-> configuration_service edge behaves (env-open sync, consume-mode
# device injection, websocket streaming). Assumes `docker compose up -d` is
# already running (or run `docker compose up --build -d` first). Run from this
# directory (the websocket probe uses `docker compose exec`). Exits non-zero on
# the first failed check.
set -euo pipefail

API_KEY="${QSERVER_API_KEY:-mad}"
QS=http://localhost:60610
CONFIG=http://localhost:8004
DC=http://localhost:8003
AUTH=(-H "Authorization: ApiKey ${API_KEY}")

pass() { printf '  ok  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; exit 1; }

echo "== health =="
curl -fsS "$CONFIG/health" >/dev/null || fail "config_service /health"
pass "config_service /health"
curl -fsS "$DC/health" >/dev/null || fail "direct_control /health"
pass "direct_control /health"

status=$(curl -fsS "${AUTH[@]}" "$QS/api/status") || fail "queueserver /api/status"
echo "$status" | jq -e '.manager_state == "idle"' >/dev/null || fail "manager not idle: $status"
pass "queueserver /api/status -> idle (unified mode)"

echo "== side-B: env-open syncs against configuration_service =="
curl -fsS -X POST "${AUTH[@]}" "$QS/api/environment/open" | jq -e '.success' >/dev/null \
    || fail "environment/open rejected"
for i in $(seq 1 30); do
    if curl -fsS "${AUTH[@]}" "$QS/api/status" | jq -e '.worker_environment_exists' >/dev/null; then
        break
    fi
    [ "$i" -eq 30 ] && fail "worker environment did not open within 60s"
    sleep 2
done
pass "worker environment open"

# Consume-mode proof: 'pinhole' exists ONLY in the happi-seeded config-service
# registry (not in the sim profile), so its presence in devices_allowed means
# the manager prefetched specs from config-service and injected them into the
# worker on env-open.
curl -fsS "${AUTH[@]}" "$QS/api/devices/allowed" \
    | jq -e '.devices_allowed | has("pinhole")' >/dev/null \
    || fail "registry device 'pinhole' not injected into worker (consume-mode broken)"
pass "config-service device 'pinhole' injected into worker namespace"

echo "== side-B: websockets stream (fed by the manager's 0MQ PUB channel) =="
docker compose exec -T queueserver python - <<'EOF' || fail "websocket probe"
import asyncio, websockets

HEADERS = {"Authorization": "ApiKey mad"}

async def expect_message(path):
    async with websockets.connect(
        f"ws://localhost:60610/api{path}", additional_headers=HEADERS, open_timeout=10
    ) as ws:
        await asyncio.wait_for(ws.recv(), timeout=20)
        print(f"  ok  {path} emitted")

async def expect_rejected(path):
    try:
        async with websockets.connect(f"ws://localhost:60610/api{path}", open_timeout=10) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
    except Exception:
        print(f"  ok  {path} rejected without credentials")
        return
    raise SystemExit(f"FAIL: {path} accepted an unauthenticated connection")

async def main():
    await asyncio.gather(
        expect_message("/status/ws"),
        expect_message("/info/ws"),
        expect_message("/console_output/ws"),
    )
    await expect_rejected("/status/ws")

asyncio.run(main())
EOF

echo
echo "All with-queueserver Side-B smoke checks passed."
