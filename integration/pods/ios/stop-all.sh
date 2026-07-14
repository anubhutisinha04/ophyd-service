#!/bin/bash
# Stop all IOS backend services
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose-backend.yaml"
QS_REPRO_DIR="$SCRIPT_DIR/../../queueserver-repro"

echo "==> Stopping queueserver..."
cd "$QS_REPRO_DIR"
./reproduce.sh down

echo ""
echo "==> Stopping Docker backend services..."
cd "$SCRIPT_DIR"
podman-compose -f "$COMPOSE_FILE" down

echo ""
echo "All services stopped."
