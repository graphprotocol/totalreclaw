#!/usr/bin/env bash
# subgraph/scripts/deploy-contracts.sh
# Deploy contracts to a Hardhat node or live network.
#
# Usage:
#   ./deploy-contracts.sh              # localhost (Hardhat node on 127.0.0.1:8545)
#
# LOCALHOST ONLY (#650 cleanup): live-network deploys go through the Foundry
# scripts (pinned CREATE2 + broadcast + hand-edited registry), not Hardhat.
set -euo pipefail

NETWORK="${1:-localhost}"
if [ "$NETWORK" != "localhost" ]; then
  echo "ERROR: '$NETWORK' is not supported — this script is the localhost dev-loop deployer."
  echo "       Live-network deploys: forge scripts in contracts/script/ (see the"
  echo "       Phase 3 contract deployment plan §2/§3)."
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUBGRAPH_DIR="$(dirname "$SCRIPT_DIR")"
CONTRACTS_DIR="$SUBGRAPH_DIR/../contracts"

echo "=== Deploying contracts to network: $NETWORK ==="
cd "$CONTRACTS_DIR"
npx hardhat run scripts/deploy.ts --network "$NETWORK"

ADDRESSES_FILE="$CONTRACTS_DIR/deployed-addresses.json"
if [ ! -f "$ADDRESSES_FILE" ]; then
  echo "ERROR: deployed-addresses.json not found"
  exit 1
fi

DATA_EDGE=$(python3 -c "import json; print(json.load(open('$ADDRESSES_FILE'))['local']['eventfulDataEdge'])")
START_BLOCK=$(python3 -c "import json; print(json.load(open('$ADDRESSES_FILE'))['local']['blockNumber'])")

echo "DataEdge: $DATA_EDGE (block $START_BLOCK)"

# Only update subgraph.yaml address/startBlock for localhost deployments.
# For live networks the address is stable and committed to source control.
if [ "$NETWORK" = "localhost" ]; then
  echo "=== Updating subgraph.yaml ==="
  cd "$SUBGRAPH_DIR"
  sed -i.bak "s|address: \"0x[0-9a-fA-F]*\"|address: \"$DATA_EDGE\"|" subgraph.yaml
  sed -i.bak "s|startBlock: [0-9]*|startBlock: $START_BLOCK|" subgraph.yaml
  rm -f subgraph.yaml.bak
else
  echo "=== Skipping subgraph.yaml update (non-localhost network) ==="
  echo "    Update address and startBlock manually if this is a fresh deployment:"
  echo "      address:    $DATA_EDGE"
  echo "      startBlock: $START_BLOCK"
fi

echo "=== Copying ABI ==="
mkdir -p "$SUBGRAPH_DIR/abis"
cp "$CONTRACTS_DIR/artifacts/contracts/EventfulDataEdge.sol/EventfulDataEdge.json" \
   "$SUBGRAPH_DIR/abis/EventfulDataEdge.json"

echo "=== Done ==="
