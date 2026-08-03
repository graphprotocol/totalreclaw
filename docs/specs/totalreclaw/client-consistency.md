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

After the v1 env cleanup the surface is minimal. See
[`docs/guides/env-vars-reference.md`](../../guides/env-vars-reference.md)
for the canonical list.

| Env Var | Purpose | Used By |
|---------|---------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | BIP-39 mnemonic | All clients |
| `TOTALRECLAW_SERVER_URL` | Relay URL (default: `https://api.totalreclaw.xyz`) | All clients |
| `TOTALRECLAW_SELF_HOSTED` | Set "true" for self-hosted mode | All clients |
| `TOTALRECLAW_CREDENTIALS_PATH` | Override credentials file location | All clients |
| `TOTALRECLAW_CACHE_PATH` | Override encrypted cache file location | All clients |
| `TOTALRECLAW_TEST` | Set "true" to mark as test client | Test suites only |
| `TOTALRECLAW_CREDENTIALS_PROVIDER` | `file` (default) or `external` — selects the credential source | All clients |

Tuning knobs (`TOTALRECLAW_EXTRACT_INTERVAL`, `TOTALRECLAW_MIN_IMPORTANCE`,
`TOTALRECLAW_COSINE_THRESHOLD`, etc.) are still read by clients but only as
env-var fallbacks for self-hosted deployments. On managed service, the
relay billing response carries these values — see the tables below.

### Credential Material

A client is configured from **either** a BIP-39 recovery phrase (the root) **or** a
`derived-bundle-v1` credential bundle. Both must produce byte-identical cryptographic
behaviour on the same vault. A client that supports only one of the two is not
spec-compliant.

**Bundle schema** (`credentials.json` `version: 2`, or the external-provider payload):

```jsonc
{
  "version": 2,
  "schema": "derived-bundle-v1",
  "vault": {
    "encryption_key": "<64 hex>",   // 32B — HKDF info "totalreclaw-encryption-key-v1"
    "dedup_key":      "<64 hex>",   // 32B — HKDF info "openmemory-dedup-v1"
    "auth_key":       "<64 hex>",   // 32B — HKDF info "totalreclaw-auth-key-v1"
    "lsh_seed":       "<64 hex>"    // 32B — HKDF info "openmemory-lsh-seed-v1"
  },
  "signing": {
    "kind": "owner-eoa",            // closed enum: "owner-eoa" | "session-key"
    "private_key": "<64 hex>",      // 32B secp256k1
    "address": "0x<40 hex>"         // EIP-55 checksummed address of private_key
    // "grant": {...}               // required iff kind == "session-key"
  },
  "account": {
    "smart_account": "0x<40 hex>",  // CREATE2 Smart Account address — facts are indexed by this
    "chain_id": 100
  },
  "provisioned_at": "<RFC 3339 UTC>",
  "provisioned_by": "spa" | "local-migration" | "external"
}
```

All key material is lowercase hex, no `0x` prefix, exactly 64 characters. Hex casing
and prefix are load-bearing — a client that emits `0x`-prefixed or uppercase values
produces a bundle other clients will reject.

**Derivation (canonical).** The four `vault` values are the outputs clients already
compute from a phrase today. Given `seed = PBKDF2-HMAC-SHA512(mnemonic, "mnemonic",
2048)` and `salt = seed[0..32]`, each key is
`HKDF-SHA256(ikm = seed, salt = salt, info = <the info string above>, length = 32)`.
This is a repackaging of the existing per-key derivation — no new key derivation is
introduced.

The salt is an *intermediate* of that derivation and is **not** carried in the bundle:
once the four outputs exist there is nothing left to derive from it, and a
bundle-configured client never needs it.

**The bundle never contains the mnemonic or the 64-byte seed.** Including the seed
would reproduce every derived key *and* full wallet control, defeating the purpose of
the bundle. Implementations MUST NOT add a seed field, and MUST assert its absence in
tests.

**Derivation and validation live in `@totalreclaw/core` / `totalreclaw-core`**
(`deriveBundleFromMnemonic` / `derive_bundle_from_mnemonic`, `parseBundleV1` /
`parse_bundle_v1`). Clients MUST NOT implement HKDF, PBKDF2 or bundle parsing
themselves.

**`signing.kind`:**

| Value | `private_key` is | Scoped? | Revocable? |
|---|---|---|---|
| `owner-eoa` | the BIP-44 `m/44'/60'/0'/0/0` key — the Smart Account **owner** | No | No |
| `session-key` | 32 CSPRNG bytes, authorised by a `SessionKeyPermissionGrant` | Yes (DataEdge target, `execute`/`executeBatch`, `valueMax = 0`) | Yes (on-chain) |

`owner-eoa` carries full account authority and MUST be handled with the same care as
the recovery phrase. Unknown `kind` values MUST be rejected loudly — never treated as
`owner-eoa`.

**Validation (all mandatory, all loud failures — never a silent downgrade):**

- `version == 2`, `schema == "derived-bundle-v1"`.
- All four `vault.*` present, each exactly 64 lowercase hex chars.
- `signing.kind` in the closed enum; `signing.private_key` 64 hex chars;
  `address(signing.private_key) == signing.address`.
- `kind == "session-key"` ⇒ `grant` present and `grant.account == account.smart_account`,
  `grant.signer == signing.address`, `grant.domain.chainId == account.chain_id`.
- `kind == "owner-eoa"` ⇒ `grant` absent.

**Behavioural requirements for a bundle-configured client:**

1. No code path may require, request, or attempt to reconstruct a mnemonic.
2. `wallet_address` resolves from `account.smart_account` directly — it MUST NOT be
   re-derived, because the owner EOA may be absent under `session-key`.
3. `POST /v1/register` MUST NOT be called. The vault is registered by the provisioning
   origin, which holds the root and is the only party able to register correctly.
   Provisioning a bundle for a never-registered vault is a provisioning-origin bug.
4. Relay authentication uses `SHA-256(vault.auth_key)` exactly as in the phrase path.
5. No value under `vault` or `signing.private_key` may be logged, printed, or included
   in an exception message.

**Storage.** Keychain-capable hosts store the `{vault, signing}` subtree as one
keychain item under service `totalreclaw`, account `__keychain__:v2:<smart_account>`,
leaving only non-secret discovery metadata (`version`, `schema`, `keychain_wrapped`,
`account`, `signing.kind`, `signing.address`, `provisioned_*`) in `credentials.json`.
This namespace is distinct from the v1 marker `__keychain__:v1:<eoa>`, which keys on
the EOA and wraps a mnemonic; the two coexist during migration. Headless hosts hold
the full object at mode `0600`, or receive it through the external credential provider
(`TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH` / `_JSON`).

**Credential states a client MUST read.** All four, in this precedence order:

1. `TOTALRECLAW_RECOVERY_PHRASE` env var (phrase).
2. External credential provider payload — phrase or bundle.
3. `credentials.json` `version: 2` — bundle (keychain-unwrapped if marked).
4. `credentials.json` legacy — `{"mnemonic": "…"}` plaintext, or the
   `__keychain__:v1:<eoa>` marker.

**Parity fixture.** `tests/parity/fixtures/derived-bundle-v1.json`, generated from the
canonical `abandon … about` test mnemonic. Every implementation MUST assert its
`derive_bundle_from_mnemonic` output is byte-equal to the fixture before shipping, in
addition to the existing crypto-parity checks below.

**Shipped clients (as of this writing):** Hermes (Python) — the only implementation.
See the public CLAUDE.md Feature Compatibility Matrix for the current per-client
status; MCP and the parked clients inherit this frozen contract when they implement it.

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

### Session Debrief

| Parameter | Value | Notes |
|-----------|-------|-------|
| Debrief prompt | See `mcp/src/tools/debrief.ts:DEBRIEF_SYSTEM_PROMPT` | Canonical source of truth — identical across all clients |
| Max items | 5 | Per debrief |
| Types | summary, context | Only these two types |
| Importance | 7-8 typical (filter < 6) | High-value by definition |
| Minimum conversation | 4 turns (8 messages) | Skip trivial sessions |
| Source tag | `{client}_debrief` | e.g. `mcp_debrief`, `openclaw_debrief`, `hermes_debrief`, `nanoclaw_debrief`, `zeroclaw_debrief` |
| LLM required | Yes — no heuristic fallback | Debrief is inherently an LLM task |
| Dedup context | Already-stored fact texts passed to prompt | Prevents duplicate extraction |

**Trigger per platform:**

| Agent | Trigger |
|-------|---------|
| MCP / Claude Desktop / IronClaw | Host agent calls `totalreclaw_debrief` tool (prompt-guided) |
| OpenClaw | Automatic in `before_compaction` and `before_reset` hooks |
| NanoClaw | Automatic in `pre_compact` hook |
| Hermes | Automatic in `on_session_end` hook |
| ZeroClaw | Framework calls `debrief()` method on `TotalReclawMemory` |

**Known debrief gaps (low severity):**

| Gap | Clients Affected | Severity | Notes |
|-----|-----------------|----------|-------|
| Debrief items bypass store-time near-duplicate detection | MCP, NanoClaw, Hermes | LOW | Only OpenClaw routes debrief through `storeExtractedFacts()` with batch+cosine dedup. Others call `client.remember()` directly. LLM-level dedup via `{already_stored_facts}` prompt + server-side content fingerprint mitigate. |
| Hermes debrief stores without embedding | Hermes | LOW | `hooks.py` calls `client.remember()` without `embedding=` param, so debrief items lack LSH bucket hashes. Search relies on word-level blind indices only. |
| NanoClaw debrief has no code-level 8-message guard | NanoClaw | LOW | Triggers based on `validation.facts.length > 0`, not conversation length. LLM prompt returns `[]` for trivial conversations, but no code guard like other clients. |

### On-Chain Write Pipeline

Every client must implement this pipeline identically:

1. **Encrypt** plaintext with XChaCha20-Poly1305 → base64 → hex for protobuf
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

**Bundle interop (required once a client supports `derived-bundle-v1`):** repeat the
7-step sequence with one side configured from a bundle and the other from the phrase,
in both directions. This proves the bundle path and the phrase path address the same
vault and produce mutually decryptable ciphertext.

**All E2E tests MUST hit the staging relay (`api-staging.totalreclaw.xyz`), never production.**

### Crypto Parity Validation

Before cross-client E2E, validate crypto parity offline:

1. Generate test vectors from TypeScript: `node --experimental-strip-types tests/parity/generate-fixtures.ts > fixtures.json`
2. New client derives keys from the test mnemonic → must match fixture values byte-for-byte
3. New client decrypts TypeScript-encrypted ciphertext → must produce original plaintext
4. New client generates blind indices → must match fixture values exactly
5. New client generates content fingerprint → must match fixture value
6. New client generates LSH bucket hashes → must match all 20 fixture values
7. New client derives a `derived-bundle-v1` from the test mnemonic → must match
   `tests/parity/fixtures/derived-bundle-v1.json` byte-for-byte, and configuring the
   client from that bundle must reproduce steps 2-6 identically to configuring it from
   the mnemonic.

See `python/tests/fixtures/crypto_vectors.json` for the fixture format and `tests/parity/cross-impl-test.ts` for the validation script.

## Reference Implementations

| Language | Location | Tests |
|----------|----------|-------|
| TypeScript (MCP) | `mcp/src/subgraph/` | `mcp/tests/` |
| TypeScript (OpenClaw) | `skill/plugin/` | `skill/plugin/tests/` |
| Python | `python/src/totalreclaw/` | `python/tests/` |
| Rust | `rust/` | `rust/tests/` |

The **TypeScript MCP server** (`mcp/src/subgraph/crypto.ts`) is the canonical crypto reference. All other implementations must produce identical output for identical inputs.
