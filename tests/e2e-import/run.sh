#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== Mem0 Import E2E Test ==="
echo ""

# Check .env exists
if [ ! -f .env ]; then
  echo "Error: .env not found. Copy .env.example and fill in API keys:"
  echo "  cp .env.example .env"
  exit 1
fi

# Load env
set -a
source .env
set +a

# Start containers
echo "Starting Docker containers..."
docker compose up -d --build
echo "Waiting 60s for containers to start..."
sleep 60

# Verify health
echo "Checking container health..."
curl -sf http://127.0.0.1:8082/ > /dev/null && echo "  Mem0 instance: healthy" || { echo "  Mem0 instance: UNHEALTHY"; exit 1; }
curl -sf http://127.0.0.1:18789/ > /dev/null && echo "  TotalReclaw instance: healthy" || { echo "  TotalReclaw instance: UNHEALTHY"; exit 1; }

# Install deps and run
npm install
npx tsx e2e-mem0-import.ts "$@"

# Cleanup hint
echo ""
echo "To tear down: docker compose down -v"
