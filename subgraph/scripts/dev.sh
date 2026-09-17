#!/usr/bin/env bash
# subgraph/scripts/dev.sh
# Local development: starts Hardhat node + Docker Graph Node stack
# Usage: ./scripts/dev.sh
#
# Prerequisites:
#   - Docker running
#   - npm install -g @graphprotocol/graph-cli
#   - Contracts compiled: cd ../contracts && npx hardhat compile
#
# This script:
#   1. Starts Hardhat node in background
#   2. Starts Docker stack (PostgreSQL + IPFS + Graph Node)
#   3. Deploys contracts to local Hardhat
#   4. Updates subgraph.yaml with deployed addresses
#   5. Builds and deploys subgraph to local Graph Node
#
# Endpoints:
#   - Hardhat RPC: http://127.0.0.1:8545
#   - GraphQL queries: http://localhost:8000/subgraphs/name/totalreclaw
#   - Graph Node admin: http://localhost:8020

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUBGRAPH_DIR="$(dirname "$SCRIPT_DIR")"
CONTRACTS_DIR="$SUBGRAPH_DIR/../contracts"

cleanup() {
  echo "Shutting down..."
  [ -n "${HARDHAT_PID:-}" ] && kill "$HARDHAT_PID" 2>/dev/null
  cd "$SUBGRAPH_DIR" && docker compose down 2>/dev/null
  exit 0
}
trap cleanup INT TERM

echo "=== Starting Hardhat node ==="
cd "$CONTRACTS_DIR"
npx hardhat node &
HARDHAT_PID=$!
sleep 3

echo "=== Starting Docker stack (PostgreSQL + IPFS + Graph Node) ==="
cd "$SUBGRAPH_DIR"
docker compose up -d
echo "Waiting for Graph Node to be ready..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8020/ > /dev/null 2>&1; then
    echo "Graph Node is ready."
    break
  fi
  sleep 2
done

echo "=== Deploying contracts ==="
cd "$CONTRACTS_DIR"
npx hardhat run scripts/deploy.ts --network localhost

echo "=== Updating subgraph config ==="
ADDRESSES_FILE="$CONTRACTS_DIR/deployed-addresses.json"
DATA_EDGE=$(python3 -c "import json; print(json.load(open('$ADDRESSES_FILE'))['local']['eventfulDataEdge'])")
START_BLOCK=$(python3 -c "import json; print(json.load(open('$ADDRESSES_FILE'))['local']['blockNumber'])")

cd "$SUBGRAPH_DIR"
# Use sed to update address and startBlock in subgraph.yaml
sed -i.bak "s|address: \"0x[0-9a-fA-F]*\"|address: \"$DATA_EDGE\"|" subgraph.yaml
sed -i.bak "s|startBlock: [0-9]*|startBlock: $START_BLOCK|" subgraph.yaml
rm -f subgraph.yaml.bak

echo "=== Copying ABI ==="
cp "$CONTRACTS_DIR/artifacts/contracts/EventfulDataEdge.sol/EventfulDataEdge.json" \
   "$SUBGRAPH_DIR/abis/EventfulDataEdge.json"

echo "=== Building subgraph ==="
npx graph codegen
npx graph build

echo "=== Deploying subgraph to local Graph Node ==="
npx graph create --node http://localhost:8020/ totalreclaw
npx graph deploy --node http://localhost:8020/ --ipfs http://localhost:15001 --version-label v0.0.1 totalreclaw

echo ""
echo "=== Dev environment ready ==="
echo "Query endpoint: http://localhost:8000/subgraphs/name/totalreclaw"
echo "Press Ctrl+C to stop."
wait "$HARDHAT_PID"
