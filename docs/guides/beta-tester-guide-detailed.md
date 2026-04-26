# TotalReclaw — Beta Tester Deep-Dive

**Version:** 3.3.1 (RC line; promotes to 3.3.1 stable)
**Date:** April 2026
**Audience:** Power users and beta testers who want to tune extraction, understand the encryption pipeline, run E2E validation, or self-host.
**Time to read:** ~15 minutes.

This guide is a companion to the 95-percent-of-users setup guides. **For installation, follow your runtime's setup guide first** — every flow is URL-driven and takes a single chat message:

- **OpenClaw** → [openclaw-setup.md](./openclaw-setup.md) (browser account-setup flow, auto-reload)
- **Hermes (Python)** → [hermes-setup.md](./hermes-setup.md) (browser account-setup flow, manual restart)
- **Claude Code / Claude Desktop / Cursor / Windsurf / IronClaw (MCP)** → [claude-code-setup.md](./claude-code-setup.md) (no account-setup flow — phrase from another client's credentials or offline)

Once setup is done and tools are bound, come back here for the knobs.

---

## Table of contents

1. [What's auto-running and on which client](#1-whats-auto-running-and-on-which-client)
2. [Architecture](#2-architecture)
3. [Memory types (v1 taxonomy)](#3-memory-types-v1-taxonomy)
4. [Tuning extraction and recall](#4-tuning-extraction-and-recall)
5. [Environment variables — full reference](#5-environment-variables--full-reference)
6. [Cross-client portability](#6-cross-client-portability)
7. [Self-hosted mode](#7-self-hosted-mode)
8. [Manual validation checklist](#8-manual-validation-checklist)
9. [E2E test suites](#9-e2e-test-suites)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What's auto-running and on which client

| Behavior | OpenClaw | Hermes | NanoClaw | MCP (Claude Code, etc.) |
|---|---|---|---|---|
| Auto-recall on every message | yes (`before_agent_start`) | yes | yes (`additionalContext`) | no — explicit `totalreclaw_recall` |
| Auto-extract every N turns | yes (`agent_end`) | yes | yes | no — explicit `totalreclaw_remember` |
| Pre-compaction flush | yes | yes | yes | no |
| Session debrief | yes | yes | yes | no (use `totalreclaw_debrief` tool) |
| First-run welcome | yes | yes | yes | no (MCP is stateless JSON-RPC) |
| Browser account-setup flow | yes | yes | reuse OpenClaw / Hermes phrase | no — phrase from another client |
| Auto-reload on plugin install | yes (`gateway.reload.mode = hybrid`) | no — manual restart | container respawn | host-restart only |

If you're on MCP, all the auto behavior is replaced by tool calls the host LLM makes from context. The same memories land in the same vault.

---

## 2. Architecture

### Identity and key derivation

Your 12-word recovery phrase is the single root of every key in the system.

1. **BIP-39 seed** — 12 words → 512-bit master seed.
2. **Private key** — BIP-32 path `m/44'/60'/0'/0/0` → 256-bit secp256k1 private key.
3. **EOA address** — standard Ethereum address derived from the private key.
4. **Smart Account** — deterministic ERC-4337 address via CREATE2 from the EOA (canonical SimpleAccountFactory v0.7).
5. **Encryption key** — HKDF-SHA256(seed, info=`totalreclaw-encryption-key-v1`) → 256-bit XChaCha20-Poly1305 key.
6. **Auth key** — HKDF-SHA256(seed, info=`totalreclaw-auth-key-v1`) → 256-bit auth secret. Its SHA-256 hash is your relay-side identity.

The same phrase always produces the same Smart Account, encryption key, and auth identity — that's how cross-device and cross-client portability works.

> **Core library.** All five derivation paths plus XChaCha20-Poly1305, blind-index hashing, content fingerprinting, and protobuf packing live in `@totalreclaw/core` (Rust core, compiled to native + WASM + PyO3 wheel). Every client links the same crate, so byte-for-byte parity is enforced by cross-runtime parity tests in CI.

### Storage pipeline (write path)

When a fact is stored:

1. Plain text + metadata are encrypted with XChaCha20-Poly1305 (24-byte nonce).
2. A SHA-256 content fingerprint over the canonical text dedupes duplicates client-side.
3. Blind search trapdoors are generated for keyword tokens (HMAC-SHA256 with a per-user index secret).
4. A 384-dim embedding (`Xenova/all-MiniLM-L6-v2`, INT8 quantized) is computed locally for semantic search.
5. The encrypted blob, blind indices, content fingerprint, and (for v1) the protobuf-packed taxonomy fields are wrapped in a v=4 outer envelope and submitted via ERC-4337 UserOp to the EventfulDataEdge contract.
6. The Pimlico paymaster sponsors gas. Free-tier writes hit Base Sepolia testnet; Pro-tier writes hit Gnosis mainnet. Chain auto-detection comes from your billing tier.
7. The Graph indexes the on-chain event so it's queryable seconds later.

### Retrieval pipeline (read path)

When you query:

1. Your query is embedded locally (same MiniLM model).
2. Blind trapdoors for query tokens are generated.
3. The relay returns up to several thousand encrypted candidates matching the trapdoors.
4. The plugin / MCP server / Hermes client decrypts every candidate locally.
5. Tier 1 source-weighted reranker scores: BM25 (lexical) + cosine (semantic) + RRF fusion + source weights (user > assistant) + scope bonus + pin override.
6. The top-N (default 8) are returned to the host LLM as context.

The relay never sees plaintext, never sees query terms, and never holds an encryption key.

### Privacy guarantees

- **Server-blind search.** The relay sees blind index tokens (HMAC outputs) and never the plaintext keywords. Even with a full database compromise, an attacker can't reverse a query without the per-user secret.
- **End-to-end encryption.** All fact bytes are XChaCha20-Poly1305 ciphertext when they leave your device.
- **No phrase ever leaves your device** as long as you follow the canonical setup. Browser setup flow encrypts the phrase locally with a fresh x25519/AES-GCM key derived against the gateway's ephemeral pubkey before posting; the relay only ever sees ciphertext.
- **Decay is local.** Importance decay and eviction run client-side from the decrypted set. The relay can't infer which memories matter to you.

---

## 3. Memory types (v1 taxonomy)

Plugin 3.0+ writes the v1 taxonomy. Six speech-act types map to Searle's classes:

| Type | What it covers | Example |
|---|---|---|
| **claim** | assertive — facts about the world, decisions made | "I live in Lisbon", "Chose PostgreSQL for the prod DB" |
| **preference** | expressive — likes / dislikes | "Likes dark mode", "Hates trailing commas" |
| **directive** | imperative — rules to follow | "Always check `d.get(errors)` before `d.errors`" |
| **commitment** | commissive — promises / goals | "Will ship v2 by Friday" |
| **episode** | narrative — events that happened | "Deployed v1.0 on March 15" |
| **summary** | derived — compressed synthesis from debrief | (auto-generated only) |

Three orthogonal axes attach to every memory:

- **source** — `user` (you said it), `assistant` (LLM inferred it), `extracted` (auto-extraction). Source-weighted reranking ranks user > extracted > assistant.
- **scope** — `work`, `personal`, `health`, `family`, `creative`, `finance`, `misc`. Scope bonus boosts in-scope hits.
- **volatility** — `stable`, `slow`, `fast`. Used by importance decay.

You can override the type / scope of any memory via natural language: *"that's actually a directive, not a preference"* → calls `totalreclaw_retype`. *"file that under work"* → calls `totalreclaw_set_scope`. See [memory-types-guide.md](./memory-types-guide.md).

---

## 4. Tuning extraction and recall

The defaults work for most users. These are the knobs that matter when something feels off.

### Extraction is too noisy (storing low-value facts)

Raise the importance floor:

```bash
TOTALRECLAW_MIN_IMPORTANCE=5     # default 3 (1-10 scale)
```

Or extract less often:

```bash
TOTALRECLAW_EXTRACT_EVERY_TURNS=5    # default 3
```

### Extraction is missing facts you wanted stored

Lower the importance floor (`TOTALRECLAW_MIN_IMPORTANCE=2`) or extract more often (`TOTALRECLAW_EXTRACT_EVERY_TURNS=2`). Or just say *"remember that…"* explicitly — that bypasses scoring.

### Recall is returning irrelevant memories

Raise the cosine threshold:

```bash
TOTALRECLAW_COSINE_THRESHOLD=0.25    # default 0.15 — strict
```

Or raise the auto-injection threshold (so fewer memories get pre-pended to context automatically):

```bash
TOTALRECLAW_RELEVANCE_THRESHOLD=0.4    # default 0.3
```

### Recall is missing relevant memories

Lower the same thresholds (`TOTALRECLAW_COSINE_THRESHOLD=0.10`, `TOTALRECLAW_RELEVANCE_THRESHOLD=0.2`).

### Auto-extraction silently produces zero facts

This usually means there's no LLM available. The plugin auto-detects your agent's provider key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ZAI_API_KEY`, etc.). If none is set or all are rate-limited, extraction silently no-ops. Plugin 3.3.1+ logs a single INFO at startup when no LLM resolves; Hermes 2.3.1+ raises on first extraction attempt. Configure at least one provider key.

### Stored memories aren't surviving across sessions

Two common causes:

1. **Wrong phrase.** Even one wrong word produces a different Smart Account and you'll be treated as a new user. BIP-39 is all-lowercase, single-spaced, exact order.
2. **Pre-compaction flush didn't fire.** OpenClaw flushes pending facts before context compaction; Hermes flushes on session end; MCP has no flush. If you're on MCP and the host compacted before any explicit `totalreclaw_remember` call, the unstored facts are lost. Workaround: call `totalreclaw_debrief` periodically, or migrate to OpenClaw / Hermes for that workload.

---

## 5. Environment variables — full reference

Canonical reference: [`docs/guides/env-vars-reference.md`](./env-vars-reference.md). Summary:

### Core (all clients)

| Variable | Description | Default |
|---|---|---|
| `TOTALRECLAW_RECOVERY_PHRASE` | 12-word BIP-39 phrase. Required for MCP / NanoClaw. OpenClaw + Hermes prefer the browser account-setup flow (writes `~/.totalreclaw/credentials.json`). | — |
| `TOTALRECLAW_SERVER_URL` | Relay URL. Only set for self-hosted. | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_SELF_HOSTED` | `true` to disable on-chain pipeline (PostgreSQL self-host). | `false` |
| `TOTALRECLAW_CREDENTIALS_PATH` | Override credentials file location. | `~/.totalreclaw/credentials.json` |
| `TOTALRECLAW_CACHE_PATH` | Override encrypted cache file location. | `~/.totalreclaw/cache.enc` |

### Extraction tuning (OpenClaw plugin + Hermes + NanoClaw — MCP has no extraction hooks)

| Variable | Description | Default |
|---|---|---|
| `TOTALRECLAW_EXTRACT_EVERY_TURNS` | Turns between auto-extractions. | `3` |
| `TOTALRECLAW_MIN_IMPORTANCE` | Floor for storing extracted facts (1–10). | `3` |
| `TOTALRECLAW_SEMANTIC_SKIP_THRESHOLD` | Cosine similarity above which a fact is treated as a near-duplicate. | `0.85` |

### Recall tuning

| Variable | Description | Default |
|---|---|---|
| `TOTALRECLAW_COSINE_THRESHOLD` | Minimum cosine of top result to return memories at all. | `0.15` |
| `TOTALRECLAW_RELEVANCE_THRESHOLD` | Minimum relevance for auto-injecting into context. | `0.3` |
| `TOTALRECLAW_CACHE_TTL_MS` | Hot-cache TTL for repeated queries. | `300000` (5 min) |

### LLM keys (auto-detected by OpenClaw + Hermes; not used by MCP)

OpenClaw + Hermes auto-detect whichever provider key is set in your environment and use a cheap model from that provider for fact extraction. Supported: `OPENAI_API_KEY` (gpt-4.1-mini), `ANTHROPIC_API_KEY` (claude-haiku-4-5), `ZAI_API_KEY` (glm-4.5-flash), `GEMINI_API_KEY` (gemini-2.0-flash), `MISTRAL_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY`, `TOGETHER_API_KEY`, `CEREBRAS_API_KEY`. MCP doesn't extract — the host LLM (Claude, etc.) is responsible.

### Removed in v1

These vars were removed in the v1 env cleanup and are silently ignored:

`TOTALRECLAW_CHAIN_ID`, `TOTALRECLAW_EMBEDDING_MODEL`, `TOTALRECLAW_STORE_DEDUP`, `TOTALRECLAW_LLM_MODEL`, `TOTALRECLAW_SESSION_ID`, `TOTALRECLAW_TAXONOMY_VERSION`, `TOTALRECLAW_CLAIM_FORMAT`, `TOTALRECLAW_DIGEST_MODE`.

Chain ID is auto-detected from billing tier. Embedding model is fixed (`Xenova/all-MiniLM-L6-v2`). Extraction model is auto-derived from the provider key. v1 taxonomy is always on.

---

## 6. Cross-client portability

Same recovery phrase = same Smart Account = same memories. Verified by Journey 3 in the integration test suite (a wrong mnemonic produces different keys; the relay rejects auth with a 401).

To migrate from one client to another:

```bash
# On the source machine, copy the credentials file:
cat ~/.totalreclaw/credentials.json
```

On the target machine, the agent's URL-driven setup will detect an existing credentials file and skip phrase entry. If you can't move files (e.g., switching to a phone), use the **Import existing** tab in OpenClaw / Hermes browser account-setup flow on the target — same phrase, same vault.

If you're moving from MCP (which doesn't have an account-setup flow) to OpenClaw / Hermes, just install the new client and reuse `~/.totalreclaw/credentials.json` — both runtimes read it on startup before falling back to the env-var path.

---

## 7. Self-hosted mode

For full control of your encrypted blobs (PostgreSQL instead of on-chain):

```bash
TOTALRECLAW_SELF_HOSTED=true
TOTALRECLAW_SERVER_URL=https://your-relay.example.com
```

The relay code is open-source (`totalreclaw-relay` package). It exposes the same auth + storage API but writes to a local PostgreSQL instead of submitting UserOps. Encryption guarantees are identical — the server never sees plaintext or query terms.

This trades:
- on-chain permanence (Gnosis mainnet) for whatever durability your DB has
- Pimlico-sponsored gas (free) for your own infra cost
- Pro tier billing (Stripe) for whatever auth you wire on your relay

Worthwhile if you have specific data-residency or compliance requirements.

---

## 8. Manual validation checklist

After setup, run this sequence to confirm everything is wired. Step numbers correspond to the auto-QA harness's manual scenario.

| # | Action | Expected |
|---|---|---|
| 1 | Open a fresh chat. Say *"I always use PostgreSQL and prefer TypeScript."* Continue chatting for 5+ turns on unrelated topics. | (silent) — auto-extract fires after N turns |
| 2 | Open a NEW chat. Ask *"What databases do I prefer?"* | Agent mentions PostgreSQL without you re-stating it |
| 3 | Say *"Remember that my favorite color is blue."* | Agent confirms `Memory stored (ID: …)` |
| 4 | Ask *"What's my favorite color?"* | Agent responds blue |
| 5 | Say *"Forget that my favorite color is blue."* | Agent confirms deletion |
| 6 | Ask *"What's my favorite color?"* again | Agent doesn't know |
| 7 | Say *"Export all my memories as markdown."* | Markdown list, no deleted color memory |
| 8 | Verify on-chain: `https://thegraph.com/explorer/subgraph/p-diogo/totalreclaw` (free tier on Base Sepolia → check the testnet subgraph) — query for your Smart Account address | Encrypted blobs visible with `version: 4` outer envelope and v1 taxonomy fields in the inner protobuf |

Any step failing → check [Troubleshooting](#10-troubleshooting) and `~/.openclaw/extensions/totalreclaw/` (or equivalent) logs.

---

## 9. E2E test suites

For technical testers running the validation harness:

### Functional suite (plugin-level, mock infrastructure)

```bash
git clone https://github.com/p-diogo/totalreclaw.git
cd totalreclaw/skill/plugin
npm install
npm test
```

Eight scenarios (A–H): preferences, technical facts, noise filtering, topic switching, long conversations, on-chain mock, pagination, freeform. All should PASS.

### Integration suite (real PostgreSQL, full API surface)

```bash
cd totalreclaw/relay
npm install
docker compose up -d    # PostgreSQL + mock services
npx tsx tests/run-integration-tests.ts
```

Eight journeys: core memory ops, dedup, wallet derivation, free-tier quota, Stripe upgrade, billing edge cases, security (auth + cross-user isolation + SQLi), full relay pipeline.

### Cross-client parity

The cross-runtime parity tests in `mcp/tests/reranker-cross-runtime-parity.test.ts`, `python/tests/test_reranker_cross_runtime_parity.py`, and `skill/plugin/reranker-cross-runtime-parity.test.ts` verify that the WASM, PyO3, and native paths in `@totalreclaw/core` produce byte-identical reranker output for the same input vector.

### Real-user QA harness

Internal: see `~/.claude/skills/qa-totalreclaw.md` (TotalReclaw team only). Runs natural-language conversations against the staging relay using published RC artifacts before any stable promote.

---

## 10. Troubleshooting

### `TOTALRECLAW_RECOVERY_PHRASE not set`

**Cause:** the env var is missing or empty in the runtime that needs it (typically MCP / NanoClaw).

**Fix:** OpenClaw + Hermes prefer credentials at `~/.totalreclaw/credentials.json` from the browser account-setup flow — re-run setup. MCP / NanoClaw need the env var in your host config.

### Memories aren't appearing in new conversations

1. Verify network reachability to `api.totalreclaw.xyz` (or your self-host).
2. Try an explicit recall: *"What do you remember about me?"* If that returns results, auto-search is working but your normal queries aren't hitting the threshold — lower `TOTALRECLAW_RELEVANCE_THRESHOLD`.
3. Check the runtime's logs (look for lines starting with `TotalReclaw:` in OpenClaw, `tr.` in Hermes, stderr in MCP).

### Free-tier quota exceeded (403)

Reads are never metered. The cap is monthly write volume on the free tier. Upgrade to Pro: *"Upgrade my TotalReclaw subscription."* Counter resets monthly.

### Slow retrieval

- Hot cache (5-min TTL) accelerates repeated queries.
- First query each session has to download + decrypt candidates client-side; with thousands of memories this is a few seconds.
- If consistently slow, check `TOTALRECLAW_TRAPDOOR_BATCH_SIZE` (default 5) and your network RTT to the relay.

### Wrong memories returned

Try adjusting `TOTALRECLAW_COSINE_THRESHOLD` (raise for stricter), `TOTALRECLAW_RELEVANCE_THRESHOLD`, or `TOTALRECLAW_MIN_IMPORTANCE` (raise to store fewer noisy facts in the first place).

### LLM call failed during extraction

Provider key invalid, expired, rate-limited, or the model isn't accessible. Test the key independently (`curl` the provider's API). Plugin retries on the next extraction cycle.

### Recovery on a new device doesn't surface memories

Same phrase MUST produce the same Smart Account. Check:
1. Every word matches what you wrote down, in order, lowercase, single-spaced.
2. No leading / trailing spaces.
3. Same managed-vs-self-hosted mode (chain auto-detection only works against the relay your subscription is on).

If still failing, check the integration test suite's Journey 3 — that's the canonical wallet-derivation reference.

### Permission denied on model download (Docker)

```bash
export HF_HOME=/tmp/hf-cache
```

Common when the npm cache directory isn't writable inside a container.

### Subscription not reflected after upgrade

Billing status is cached for up to 2 hours client-side. After Stripe webhook fires (~60s post-payment), restart the runtime to force a refresh.

---

## Further reading

- [Memory types guide](./memory-types-guide.md) — v1 taxonomy with override examples
- [Importing memories](./importing-memories.md) — Mem0, MCP Memory Server, ChatGPT, Claude, Gemini
- [Environment variables reference](./env-vars-reference.md) — full canonical list
- [Feature comparison](./feature-comparison.md) — what works on each client
- [v1 migration guide](./v1-migration.md) — upgrading from v0 taxonomy
- [Memory dedup](./memory-dedup.md) — how content fingerprinting + cosine dedup work

For implementation depth:

- `docs/specs/totalreclaw/architecture.md` — encryption, blind indices, server-blind search
- `docs/specs/totalreclaw/memory-taxonomy-v1.md` — the v1 taxonomy spec
- `docs/specs/subgraph/seed-to-subgraph.md` — BIP-39 → on-chain identity
- `docs/specs/subgraph/billing-and-onboarding.md` — tier structure and Gnosis Chain rationale

---

*This guide tracks plugin 3.3.1 / Hermes 2.3.1 / mcp-server 3.2.x / nanoclaw 3.1.x. Stable line: plugin 3.2.3, mcp-server 3.2.0, nanoclaw 3.0.0, Hermes 2.3.0.*
