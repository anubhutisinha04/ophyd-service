#!/bin/bash
# Start all IOS backend services: docker-compose backends + queueserver on host

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose-backend.yaml"
QS_REPRO_DIR="$SCRIPT_DIR/../../queueserver-repro"

echo "==> Starting Docker backend services (configuration, direct_control, presets, IOCs)..."
echo "(Starting containers... you may Ctrl+C after seeing container IDs)"

# Use --no-deps to avoid waiting for health checks
podman-compose -f "$COMPOSE_FILE" up -d --no-deps || true

echo ""
echo "==> Starting queueserver on host (via reproduce.sh)..."
cd "$QS_REPRO_DIR"
./reproduce.sh up

echo ""
echo "=========================================="
echo "All services running:"
echo "=========================================="
echo "Docker backends:"
echo "  - Configuration service: http://localhost:8004"
echo "  - Direct control service: http://localhost:8003"
echo "  - Presets service: http://localhost:8005"
echo "  - IOCs: ios_pgm, ios_curramp, ios_epu, ios_vortex, ios_scaler, ios_feedback"
echo ""
echo "Queueserver (host):"
echo "  - HTTP API: http://localhost:60610"
echo "  - Swagger UI: http://localhost:60610/docs"

if [ -f "$HOME/qs-repro/config/secrets.env" ]; then
    API_KEY=$(grep -oP 'HTTP_API_KEY=\K.*' "$HOME/qs-repro/config/secrets.env" 2>/dev/null || echo "<not found>")
    echo "  - API key: $API_KEY"
else
    echo "  - API key: (check ~/qs-repro/config/secrets.env)"
fi

echo ""
echo "=========================================="
echo "Frontend setup (.env):"
echo "=========================================="
echo "VITE_CONFIG_SERVICE_URL=http://localhost:8004"
echo "VITE_DIRECT_CONTROL_SERVICE_URL=http://localhost:8003"
echo "VITE_PRESETS_SERVICE_URL=http://localhost:8005"
echo "VITE_QSERVER_URL=http://localhost:60610"
echo "VITE_QSERVER_API_KEY=<paste API key from above>"
echo ""
echo "=========================================="
echo "To stop all services:"
echo "=========================================="
echo "  podman-compose -f $COMPOSE_FILE down"
echo "  cd $QS_REPRO_DIR && ./reproduce.sh down"
echo "  OR: run ./stop-all.sh"
echo ""
