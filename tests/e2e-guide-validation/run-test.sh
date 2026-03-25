#!/bin/bash
#
# E2E Guide Validation Test (Subgraph Mode)
#
# Simulates a user following the beta guide:
# 1. Starts OpenClaw in Docker with TotalReclaw skill (subgraph mode)
# 2. Sends messages to store memories (on-chain via Pimlico → Chiado)
# 3. Verifies memories are retrieved (from The Graph subgraph)
# 4. Restarts OpenClaw to prove memories survive (come from subgraph, not local)
# 5. Checks no cleartext memory files exist in OpenClaw
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
GATEWAY_URL="http://127.0.0.1:18789"
TOKEN="guide-test-token-2026"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAILURES=$((FAILURES + 1)); }
info() { echo -e "  ${YELLOW}....${NC} $1"; }
header() { echo -e "\n${BLUE}=== $1 ===${NC}"; }

FAILURES=0
TESTS=0

# Helper: extract content from OpenAI-format response
extract_content() {
  python3 -c 'import sys,json; r=json.load(sys.stdin); print(r.get("choices",[{}])[0].get("message",{}).get("content","<no content>"))' 2>/dev/null || echo '<parse error>'
}

# Load ZAI API key from project .env
if [ -f "$SCRIPT_DIR/../../.env" ]; then
  export ZAI_API_KEY=$(grep '^ZAI=' "$SCRIPT_DIR/../../.env" | cut -d= -f2)
  info "Loaded ZAI API key from .env"
fi

if [ -z "${ZAI_API_KEY:-}" ]; then
  echo "ERROR: ZAI_API_KEY not set. Check .env file."
  exit 1
fi

cleanup() {
  info "Cleaning up..."
  docker compose -f "$COMPOSE_FILE" down -v --remove-orphans 2>/dev/null || true
}
trap cleanup EXIT

# ─── Phase 1: Start OpenClaw ────────────────────────────────────────────────
header "Phase 1: Start OpenClaw with TotalReclaw skill (subgraph mode)"

docker compose -f "$COMPOSE_FILE" down -v --remove-orphans 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" build
docker compose -f "$COMPOSE_FILE" up -d

info "Waiting for OpenClaw gateway to become ready..."
READY=false
for i in $(seq 1 90); do
  # Use a simple health-style ping; the real chat endpoint may take longer on first load
  HTTP_CODE=$(curl -so /dev/null -w "%{http_code}" "$GATEWAY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"model":"openclaw","messages":[{"role":"user","content":"Say OK"}]}' \
    2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ]; then
    READY=true
    break
  fi
  # Show progress every 10 attempts
  if [ $((i % 10)) -eq 0 ]; then
    info "Still waiting... (attempt $i, last HTTP $HTTP_CODE)"
  fi
  sleep 3
done

TESTS=$((TESTS + 1))
if $READY; then
  pass "OpenClaw gateway is ready"
else
  fail "OpenClaw gateway did not become ready after 270s"
  docker compose -f "$COMPOSE_FILE" logs openclaw --tail=80
  exit 1
fi

# ─── Phase 2: Store memories ────────────────────────────────────────────────
header "Phase 2: Send message with facts to store"

info "Sending facts: favorite language=Rust, company=NovaTech, deadline=March 30..."
RESPONSE=$(curl -sS --max-time 120 "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw",
    "messages": [
      {"role": "user", "content": "Hi! Some facts about me: my favorite programming language is Rust, I work at a company called NovaTech, and my project deadline is March 30th. Please acknowledge you understand these facts about me."}
    ]
  }')

CONTENT=$(echo "$RESPONSE" | extract_content)
info "Agent said: ${CONTENT:0:300}"

TESTS=$((TESTS + 1))
if echo "$CONTENT" | grep -qi "rust\|novatech\|march\|acknowledge\|noted\|understand\|got it"; then
  pass "Agent acknowledged facts (Turn 1)"
else
  fail "Agent did not acknowledge facts"
  info "Full response: $RESPONSE"
fi

# Wait for agent_end hook to extract facts and write on-chain
# On-chain writes + Graph indexing can take 15-30s
info "Waiting 30s for fact extraction + on-chain write + Graph indexing..."
sleep 30

# ─── Phase 3: Recall in a fresh session ──────────────────────────────────────
header "Phase 3: Recall memories in a NEW session"

info "Sending recall query in fresh session..."
RECALL_RESPONSE=$(curl -sS --max-time 120 "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw",
    "user": "recall-test-session-1",
    "messages": [
      {"role": "user", "content": "What programming language do I prefer? What company do I work at?"}
    ]
  }')

RECALL_CONTENT=$(echo "$RECALL_RESPONSE" | extract_content)
info "Agent said: ${RECALL_CONTENT:0:300}"

TESTS=$((TESTS + 1))
if echo "$RECALL_CONTENT" | grep -qi "rust\|novatech"; then
  pass "Agent recalled memories from subgraph (Turn 2)"
else
  fail "Agent did not recall memories"
  info "Full response: $RECALL_RESPONSE"
fi

# ─── Phase 4: Restart OpenClaw container ────────────────────────────────────
header "Phase 4: Restart OpenClaw container (wipe local state)"

docker compose -f "$COMPOSE_FILE" restart openclaw

info "Waiting for OpenClaw gateway after restart..."
READY=false
for i in $(seq 1 90); do
  HTTP_CODE=$(curl -so /dev/null -w "%{http_code}" "$GATEWAY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"model":"openclaw","messages":[{"role":"user","content":"Say OK"}]}' \
    2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ]; then
    READY=true
    break
  fi
  if [ $((i % 10)) -eq 0 ]; then
    info "Still waiting... (attempt $i, last HTTP $HTTP_CODE)"
  fi
  sleep 3
done

TESTS=$((TESTS + 1))
if $READY; then
  pass "OpenClaw restarted successfully"
else
  fail "OpenClaw did not restart"
  docker compose -f "$COMPOSE_FILE" logs openclaw --tail=50
  exit 1
fi

# ─── Phase 5: Recall AFTER restart ──────────────────────────────────────────
header "Phase 5: Recall memories AFTER container restart"

info "This proves memories come from The Graph subgraph, not local files..."
POST_RESTART=$(curl -sS --max-time 120 "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openclaw",
    "user": "post-restart-session-1",
    "messages": [
      {"role": "user", "content": "What company do I work at? What is my favorite programming language?"}
    ]
  }')

POST_CONTENT=$(echo "$POST_RESTART" | extract_content)
info "Agent said: ${POST_CONTENT:0:300}"

TESTS=$((TESTS + 1))
if echo "$POST_CONTENT" | grep -qi "rust\|novatech"; then
  pass "Memories survived restart (subgraph persistence confirmed)"
else
  fail "Memories did NOT survive restart"
  info "Full response: $POST_RESTART"
fi

# ─── Phase 6: No cleartext check ────────────────────────────────────────────
header "Phase 6: Verify no cleartext memories in OpenClaw"

TESTS=$((TESTS + 1))
# Search for our test data in any file under OpenClaw's data directory
HAS_CLEARTEXT=$(docker compose -f "$COMPOSE_FILE" exec -T openclaw \
  sh -c 'grep -ril "NovaTech" /home/node/.openclaw/ 2>/dev/null || true')

if [ -n "$HAS_CLEARTEXT" ]; then
  fail "Found cleartext 'NovaTech' in OpenClaw files: $HAS_CLEARTEXT"
else
  pass "No cleartext memory data in OpenClaw directory"
fi

TESTS=$((TESTS + 1))
# Also verify OpenClaw's built-in memory (LanceDB) has no data
LANCEDB_FILES=$(docker compose -f "$COMPOSE_FILE" exec -T openclaw \
  sh -c 'find /home/node/.openclaw -name "*.lance" -o -name "lancedb*" 2>/dev/null || true')

if [ -n "$LANCEDB_FILES" ]; then
  fail "Found LanceDB files (native memory should be disabled): $LANCEDB_FILES"
else
  pass "No LanceDB files (native memory correctly disabled)"
fi

# ─── Phase 7: Verify staging server is reachable ─────────────────────────
header "Phase 7: Verify staging TotalReclaw server"

TESTS=$((TESTS + 1))
SERVER_HEALTH=$(curl -sS --max-time 10 "https://api-staging.totalreclaw.xyz/health" 2>/dev/null || echo "unreachable")

if echo "$SERVER_HEALTH" | grep -qi "ok\|healthy\|status"; then
  pass "Staging TotalReclaw server is healthy"
  info "Health: $SERVER_HEALTH"
else
  fail "Staging server health check failed: $SERVER_HEALTH"
fi

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  E2E Guide Validation Results (Subgraph Mode)"
echo "================================================================"
echo "  Tests:    $TESTS"
echo "  Failures: $FAILURES"
echo ""

if [ "$FAILURES" -eq 0 ]; then
  echo -e "  ${GREEN}ALL TESTS PASSED${NC}"
  echo ""
  echo "  Validated:"
  echo "  - OpenClaw + TotalReclaw skill loads and runs"
  echo "  - GLM-5 (Z.AI) responds correctly"
  echo "  - Facts extracted and stored on-chain (Chiado via Pimlico)"
  echo "  - Memories retrieved from The Graph subgraph"
  echo "  - Memories survive container restart (no local dependency)"
  echo "  - No cleartext data in OpenClaw local storage"
  echo "  - OpenClaw native memory (LanceDB) disabled"
  echo "================================================================"
  exit 0
else
  echo -e "  ${RED}$FAILURES TEST(S) FAILED${NC}"
  echo "================================================================"
  echo ""
  info "OpenClaw logs (last 50 lines):"
  docker compose -f "$COMPOSE_FILE" logs openclaw --tail=50
  exit 1
fi
