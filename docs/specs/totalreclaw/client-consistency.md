# Client Implementation Consistency Spec

> **For agents implementing new TotalReclaw clients:** This document defines the canonical parameter values and behaviors that ALL client implementations must follow. Deviating from these values creates inconsistent user experience across platforms.

## Canonical Parameters

### Extraction

| Parameter | Value | Source | Notes |
|-----------|-------|--------|-------|
| Extraction interval | 3 turns (default) | Server-driven via `billing.features.extraction_interval` | Env override: `TOTALRECLAW_EXTRACT_INTERVAL` |
| Max facts per extraction | 15 (default) | Server-driven via `billing.features.max_facts_per_extraction` | |
| Importance threshold | >= 6 (1-10 scale) | Prompt-level + parser-level | DELETE actions bypass threshold |
| Importance storage | `importance / 10` (0.0-1.0) | All clients normalize | Stored as `decayScore` on-chain |
| Message window (turn mode) | All unprocessed since last extraction | Bounded by extraction interval | Do NOT hardcode a number of messages |
| Message window (full mode) | All messages, truncated to ~12,000 chars | ~3,000 tokens budget | |
| Extraction prompt | See `skill/plugin/extractor.ts:40-74` | Canonical source of truth | Must be identical across all clients |
| Memory types | fact, preference, decision, episodic, goal, context, summary | 7 types | |
| Dedup actions | ADD, UPDATE, DELETE, NOOP | LLM-guided | Always enabled (all tiers) |

### Auto-Recall

| Parameter | Value | Notes |
|-----------|-------|-------|
| Trigger | First turn of each session | Hook: `before_agent_start` / `pre_llm_call` |
| top_k | 8 | After reranking |
| Query | Raw user message | No preprocessing |

### Billing

| Parameter | Value | Notes |
|-----------|-------|-------|
| Cache TTL | 7200 seconds (2 hours) | |
| Quota warning threshold | 80% | Inject into agent context, not just log |
| Feature flags source | `GET /v1/billing/status?wallet_address=<addr>` | `features` dict in response |

### Dedup

| Parameter | Value | Notes |
|-----------|-------|-------|
| Content fingerprint | HMAC-SHA256(dedupKey, normalizeText(plaintext)) | Server-side exact dedup |
| Store-time near-duplicate | Cosine similarity >= 0.85 | Client-side, before storing |
| LLM-guided dedup | Always enabled (all tiers) | Uses user's own LLM API key |
| Existing memories for dedup context | Fetch up to 50 via recall | Pass to extraction prompt |

### Env Vars

| Env Var | Purpose | Used By |
|---------|---------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | BIP-39 mnemonic | All clients |
| `TOTALRECLAW_SERVER_URL` | Relay URL (default: `https://api.totalreclaw.xyz`) | All clients |
| `TOTALRECLAW_EXTRACT_INTERVAL` | Override extraction interval | All clients |
| `TOTALRECLAW_MIN_IMPORTANCE` | Override minimum importance | All clients |
| `TOTALRECLAW_LLM_MODEL` | Override extraction model | Clients with LLM extraction |
| `TOTALRECLAW_SELF_HOSTED` | Set "true" for self-hosted mode | All clients |
| `TOTALRECLAW_TEST` | Set "true" to mark as test client | Test suites only |

### Client Identification

| Header | Format | Example |
|--------|--------|---------|
| `X-TotalReclaw-Client` | `{client-type}:{host-agent}` | `python-client:hermes-agent`, `mcp-server:claude-desktop`, `rust-client:zeroclaw` |
| `X-TotalReclaw-Test` | `true` or absent | Only in test suites |

### Relay Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `POST /v1/register` | POST | None | Register user |
| `POST /v1/bundler` | POST | Bearer + X-Wallet-Address | Submit UserOp (JSON-RPC proxy to Pimlico) |
| `POST /v1/subgraph` | POST | Bearer | GraphQL query (proxy to The Graph) |
| `GET /v1/billing/status?wallet_address=<addr>` | GET | Bearer | Billing status + feature flags |
| `POST /v1/billing/checkout` | POST | Bearer | Create Stripe checkout |

### On-Chain Write Pipeline

Every client must implement this pipeline identically:

1. **Encrypt** plaintext with AES-256-GCM → base64 → hex for protobuf
2. **Generate blind indices** — SHA-256 of lowercase tokens + Porter stems
3. **Generate LSH bucket hashes** — if embedding available (32-bit × 20 tables)
4. **Generate content fingerprint** — HMAC-SHA256(dedupKey, normalizeText(text))
5. **Encode protobuf** — 13-field wire format matching `server/proto/totalreclaw.proto`
6. **Build UserOp** — `SmartAccount.execute(dataEdgeAddress, 0, protobufPayload)`
7. **Get gas estimates** — `pimlico_getUserOperationGasPrice` via relay bundler
8. **Get paymaster sponsorship** — `pm_sponsorUserOperation` via relay bundler
9. **Sign UserOp** — ERC-4337 v0.7 hash with ETH signed message prefix, EOA private key
10. **Submit** — `eth_sendUserOperation` via relay bundler

### Cross-Client E2E Validation

**Every new client implementation MUST run cross-client E2E tests before release.** The test proves interoperability:

1. Generate a fresh BIP-39 mnemonic (clean slate)
2. New client stores a fact via relay
3. Wait ~35s for subgraph indexing
4. TypeScript MCP server recalls and decrypts the fact — must match
5. TypeScript MCP server stores a different fact
6. Wait ~35s for subgraph indexing
7. New client recalls and decrypts the TypeScript-stored fact — must match

See `python/tests/cross_client_e2e.py` for the reference implementation.

**All E2E tests MUST hit the staging relay (`api-staging.totalreclaw.xyz`), never production.**

### Crypto Parity Validation

Before cross-client E2E, validate crypto parity offline:

1. Generate test vectors from TypeScript: `node --experimental-strip-types tests/parity/generate-fixtures.ts > fixtures.json`
2. New client derives keys from the test mnemonic → must match fixture values byte-for-byte
3. New client decrypts TypeScript-encrypted ciphertext → must produce original plaintext
4. New client generates blind indices → must match fixture values exactly
5. New client generates content fingerprint → must match fixture value
6. New client generates LSH bucket hashes → must match all 20 fixture values

See `python/tests/fixtures/crypto_vectors.json` for the fixture format and `tests/parity/cross-impl-test.ts` for the validation script.

## Reference Implementations

| Language | Location | Tests |
|----------|----------|-------|
| TypeScript (MCP) | `mcp/src/subgraph/` | `mcp/tests/` |
| TypeScript (OpenClaw) | `skill/plugin/` | `skill/plugin/tests/` |
| Python | `python/src/totalreclaw/` | `python/tests/` |
| Rust | `rust/` | `rust/tests/` |

The **TypeScript MCP server** (`mcp/src/subgraph/crypto.ts`) is the canonical crypto reference. All other implementations must produce identical output for identical inputs.
