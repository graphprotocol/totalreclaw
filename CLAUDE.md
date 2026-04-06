# TotalReclaw - Project Guide

## Project Overview

**TotalReclaw** is an end-to-end encrypted memory vault for AI agents -- the "password manager for AI memory."

### Core Value Proposition
1. **Encrypted** -- End-to-end encrypted (AES-256-GCM). Server never sees plaintext.
2. **Portable** -- One-click plain-text export. No vendor lock-in.
3. **Universal** -- Works across OpenClaw, Claude Desktop, any MCP-compatible agent.

### Target Users
- Non-technical hosted OpenClaw users (memory locked to Railway/Vercel)
- Power users with multiple AI agents (fragmented memory across tools)

---

## Architecture (v0.3)

Two storage modes: **Managed Service** (default -- on-chain via The Graph, accessed through relay) and **Self-Hosted** (PostgreSQL backend you run yourself). The client-side E2EE pipeline is identical for both.

Managed Service uses a **dual-chain model**: Free tier stores on **Base Sepolia** testnet (chain 84532), Pro tier stores on **Gnosis mainnet** (chain 100). The relay routes bundler and subgraph requests to the correct chain based on user tier. Smart Account addresses are deterministic across both chains (CREATE2).

```
+-------------------------------------------------------------------------+
|                           CLIENT (OpenClaw Skill)                       |
+-------------------------------------------------------------------------+
|  +--------------+  +--------------+  +--------------+  +-------------+ |
|  | Fact Extract |->|   Encrypt    |->| Generate LSH |->| Blind Index | |
|  |    (LLM)     |  |  (AES-GCM)  |  |   Buckets    |  |  (SHA-256)  | |
|  +--------------+  +--------------+  +--------------+  +-------------+ |
|                              |                                          |
|                              v                                          |
|                    JSON over HTTP                                       |
+-------------------------------------------------------------------------+
                               |
              +----------------+----------------+
              v                                 v
+-------------------------------+  +-------------------------------+
|   MANAGED SERVICE (default)   |  |   SELF-HOSTED (alternative)   |
+-------------------------------+  +-------------------------------+
|  On-chain: DataEdge contract  |  |  PostgreSQL Tables:           |
|  Free: Base Sepolia (84532)   |  |  - raw_events (immutable log) |
|  Pro: Gnosis mainnet (100)    |  |  - facts (blind_indices,      |
|  Relay: Pimlico bundler       |  |          decay_score)         |
|  Index: The Graph subgraph    |  |                               |
|  Query: GraphQL via relay     |  |                               |
+-------------------------------+  +-------------------------------+
              |                                 |
              +----------------+----------------+
                               v
+-------------------------------------------------------------------------+
|                         CLIENT (Re-ranking)                             |
+-------------------------------------------------------------------------+
|  Decrypt candidates -> e5-small 384d embeds -> BM25+Cosine+RRF -> Top 8 |
+-------------------------------------------------------------------------+
```

---

## Repository Structure

```
/totalreclaw
├── CLAUDE.md              # This file
│
├── server/                # FastAPI + PostgreSQL backend (self-hosted only; billing/relay moved to private totalreclaw-relay repo)
├── client/                # TypeScript client library (E2EE, LSH, embeddings)
├── skill/                 # OpenClaw plugin (PoC v2: embedding, LSH, reranker)
├── skill-nanoclaw/        # NanoClaw skill package + MCP server
│   ├── src/               # Hooks, extraction logic
│   └── mcp/               # NanoClaw agent-runner (spawns @totalreclaw/mcp-server)
├── python/                # Python client library + Hermes Agent plugin
│   ├── src/totalreclaw/   # Client library (crypto, LSH, relay, reranker, embedding)
│   └── src/totalreclaw/hermes/  # Hermes Agent plugin (hooks, tools, state)
├── rust/                  # Rust crate for ZeroClaw memory backend
│   └── totalreclaw-memory/  # Native Rust implementation (crypto, relay, Memory trait)
├── mcp/                   # Generic MCP server (for Claude Desktop, etc.)
├── contracts/             # Solidity smart contracts (EventfulDataEdge, Paymaster)
├── subgraph/              # Graph Node indexer (AssemblyScript mappings)
├── database/              # Database schema (schema.sql)
├── tests/                 # Integration tests
│   ├── e2e-functional/    # E2E functional test suite
│   └── parity/            # Parity tests (plugin vs NanoClaw)
│
└── docs/                  # Specs and guides
    ├── specs/
    │   ├── totalreclaw/   # Core product specs (architecture, server, skills, MCP)
    │   ├── subgraph/      # Subgraph specs (seed-to-subgraph, billing)
    │   └── tee/           # TEE specs (architecture, TDX SaaS)
    ├── guides/            # User-facing guides (beta, import/migration, memory dedup)
    └── ROADMAP.md         # Phased roadmap
```

---

## Technical Specifications

Specs are organized by product area under `docs/specs/`:

### TotalReclaw (E2EE) -- `docs/specs/totalreclaw/`
| Spec | File | Status |
|------|------|--------|
| E2EE Architecture (LSH + Blind Buckets) | `architecture.md` | Implemented, validated |
| Server PoC v0.3.1b (Auth + Dedup) | `server.md` | Partially implemented |
| OpenClaw Skill | `skill-openclaw.md` | Implemented |
| NanoClaw Skill | `skill-nanoclaw.md` | Implemented |
| MCP Server | `mcp-server.md` | Implemented |
| Admin Dashboard | `admin-dashboard-design.md` (plans/) | Implemented (in private relay repo) |
| MCP Auto-Memory (Generic Hosts) | `mcp-auto-memory.md` | Spec complete |
| Benchmark Harness (OMBH) | `benchmark.md` | Implemented |
| LSH Tuning (Multi-Tenant SaaS) | `lsh-tuning.md` | Complete |
| Conflict Resolution v0.3.2 | `conflict-resolution.md` | Design complete, not implemented |
| Retrieval Improvements v3 | `retrieval-improvements-v3.md` | Implemented |

### Subgraph (Decentralized) -- `docs/specs/subgraph/`
| Spec | File | Status |
|------|------|--------|
| Seed-to-Subgraph v1.0 | `seed-to-subgraph.md` | Implemented, deployed to Base Sepolia + Gnosis mainnet |
| Billing & Onboarding | `billing-and-onboarding.md` | Implemented (Stripe) |
| Paymaster Comparison | `paymaster-comparison.md` | Complete (Pimlico chosen) |

### TEE (Trusted Execution) -- `docs/specs/tee/`
| Spec | File | Status |
|------|------|--------|
| TEE vs E2EE Comparison | `architecture.md` | Analysis complete |
| TDX SaaS v0.4 | `tdx-saas.md` | Spec complete, not started |

---

## Feature Compatibility Matrix

### Platform Support

Features across OpenClaw plugin (`skill/plugin/`), MCP server (`mcp/`), NanoClaw (`skill-nanoclaw/`), Hermes Agent (`python/`), IronClaw (via MCP server), and ZeroClaw (`rust/totalreclaw-memory/`).

| Feature | OpenClaw Plugin | MCP Server | NanoClaw | Hermes | IronClaw | ZeroClaw | Notes |
|---------|:-:|:-:|:-:|:-:|:-:|:-:|-------|
| **Core Tools** | | | | | | | |
| `totalreclaw_remember` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_recall` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_forget` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_export` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_status` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (billing cache) | |
| `totalreclaw_import_from` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Mem0, MCP Memory, ChatGPT, Claude adapters |
| `totalreclaw_import` | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | JSON/Markdown re-import (MCP only) |
| `totalreclaw_upgrade` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Stripe checkout URL |
| `totalreclaw_migrate` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Testnet-to-mainnet migration after Pro upgrade |
| `totalreclaw_consolidate` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Self-hosted only (no batch delete on managed service) |
| `totalreclaw_debrief` | Yes (auto) | Yes | Yes (auto) | Yes (auto) | Yes (via MCP) | Yes (debrief method) | Session debrief — broader context at conversation end |
| **Automatic Memory** | | | | | | | |
| Auto-search (before_agent_start) | Yes | -- | Yes (hook) | Yes (pre_llm_call) | -- | Yes (Memory trait) | ZeroClaw calls recall() at conversation start |
| Auto-extract (agent_end) | Yes | -- | Yes (hook) | Yes (post_llm_call) | -- | Yes (Memory trait) | ZeroClaw consolidation calls store() |
| Pre-compaction flush | Yes | -- | Yes (hook) | -- | -- | -- | Hermes has no compaction hook |
| Pre-reset flush | Yes | -- | -- | -- | -- | -- | OpenClaw only |
| Session-end flush | -- | -- | -- | Yes (on_session_end) | -- | -- | Hermes plugin flushes unprocessed messages |
| Session debrief | Yes (hook) | Yes (tool) | Yes (hook) | Yes (hook) | Yes (via MCP tool) | Yes (debrief method) | Captures broader context at session end; max 5 items |
| CLAUDE.md sync | -- | -- | Yes | -- | -- | -- | NanoClaw syncs high-importance facts |
| Routine-based extraction | -- | -- | -- | -- | Yes (cron) | -- | IronClaw routines engine; see ironclaw-setup.md |
| Decay handling | -- | -- | -- | -- | -- | Yes (via ZeroClaw) | ZeroClaw applies 7-day half-life at retrieval time |
| Conflict resolution | -- | -- | -- | -- | -- | Yes (via ZeroClaw) | ZeroClaw checks semantic similarity before storing Core |
| **Extraction** | | | | | | | |
| Expanded memory types (7 categories) | Yes | Yes (via prompt) | Yes | Yes (heuristic) | Yes (via prompt) | Yes (category mapping) | fact, preference, decision, episodic, goal, context, summary |
| Decision reasoning extraction | Yes | Yes (via prompt) | Yes | Yes (heuristic) | Yes (via prompt) | Yes (via ZeroClaw) | Extraction prompts require "chose X because Y" |
| **Dedup** | | | | | | |
| Content fingerprint (exact) | Yes | Yes | Yes | Yes | Yes (via MCP) | Yes | Server-side HMAC-SHA256 |
| Within-batch semantic dedup | Yes | -- | -- | -- | -- | -- | Cosine >= 0.9, during extraction |
| Store-time near-duplicate | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Yes (cosine >= 0.85) | `consolidation.ts` — both plugin and MCP; Rust checks 50 existing facts |
| LLM-guided dedup (ADD/UPDATE/DELETE) | Yes | -- | Yes | Yes | -- | -- | All tiers — uses user's own LLM API key, zero cost to us |
| Bulk consolidation tool | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Self-hosted only (no batch delete on managed service) |
| **Pro Tier Gating** | | | | | | |
| Feature gating via billing cache | Yes | -- | Yes | Yes | -- | Yes (2h TTL) | Server returns `features` dict, plugin/skill gates client-side |
| Server-side extraction config | Yes | -- | Yes | -- | -- | Yes | Relay returns `extraction_interval` + `max_facts_per_extraction` in billing status |
| Unified extraction interval (3 turns) | Yes | -- | Yes | Yes | -- | Yes | Server-tunable via relay config (no npm publish needed) |
| Max facts per extraction | Yes | -- | Yes | Yes | -- | Yes | Server-tunable via relay config (default 15) |
| Chain ID auto-detect (billing) | Yes | Yes | Yes | -- | -- | Yes | Defaults to 84532 (Base Sepolia/free); auto-detects Pro tier from billing, switches to 100 (Gnosis) |
| Dual-chain routing | -- | -- | -- | -- | -- | Relay-side: routes to Base Sepolia (free) or Gnosis mainnet (pro) |
| **Batching** | | | | | | |
| Client batching (multi-call UserOps) | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Hermes stores facts one-by-one (no ERC-4337 batch support in Python) |
| **Billing** | | | | | | |
| Quota warnings (>80%) | Yes | -- | Yes | Yes | -- | Yes | Injected via on_session_start hook; ZeroClaw via quota_warning() method |
| 403 handling + cache invalidation | Yes | Yes | Yes | -- | Yes (via MCP) | Yes | ZeroClaw invalidates billing cache on 403, returns QuotaExceeded error |
| **Search Optimizations** | | | | | | |
| Hot cache + two-tier search | Yes (managed) | -- | -- | -- | -- | Yes (30 entries, cosine >= 0.85) | Skips remote query if cached query similar |
| Dynamic candidate pool sizing | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Yes | Server-configurable via billing features; env overrides `CANDIDATE_POOL_MAX_FREE`/`CANDIDATE_POOL_MAX_PRO` |
| Server-side candidate pool | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Yes | Relay computes `max_candidate_pool` from vault size + tier; clients read from billing cache with local fallback |
| Broadened search fallback | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Fetches recent facts by owner when trapdoor search returns 0 (vague queries like "who am I?") |
| BM25 + Cosine + RRF reranking | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Intent-weighted |
| **Admin & Analytics** | | | | | | |
| X-TotalReclaw-Client header | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (rust-client:zeroclaw) | Sent on every relay request |
| Admin dashboard | -- | -- | -- | -- | -- | Admin-only (relay service), not a client feature |

### Storage Mode Support

Features across Self-Hosted (PostgreSQL) and Managed Service (default, dual-chain via The Graph).

Managed Service two-tier chain model: **Free** = Base Sepolia testnet (unlimited memories, test network — may be reset), **Pro** = Gnosis mainnet (pricing from Stripe, unlimited, permanent on-chain storage).

| Feature | Self-Hosted | Managed Service | Notes |
|---------|:-:|:-:|-------|
| Remember / Store | Yes | Yes | Managed service stores via Pimlico relay |
| Recall / Search | Yes | Yes | Managed service uses GraphQL queries |
| Forget / Delete | Yes (HTTP DELETE) | Yes (tombstone) | Managed service writes decayScore=0 on-chain |
| Export | Yes | Yes | Managed service queries by owner + isActive |
| Import (from Mem0/MCP) | Yes | Yes | |
| Consolidate tool | Yes | **No** | No batch delete on managed service |
| Store-time dedup (supersede) | Yes | Yes | Managed service: via on-chain tombstone |
| Billing / Status | Yes | Yes | Both query relay billing endpoint |
| Hot cache | -- | Yes | Self-hosted doesn't need it |
| Dual-chain routing | -- | Yes | Relay routes based on tier (free=Base Sepolia, pro=Gnosis) |
| Client batching | -- | Yes | Multi-call UserOps via batcher.ts (managed service only, uses ERC-4337 executeBatch) |
| Testnet-to-mainnet migration | -- | Yes | `totalreclaw_migrate` — copies facts from Base Sepolia to Gnosis after Pro upgrade. Idempotent, dry-run by default. |

### Known Gaps

| Gap | Severity | Description |
|-----|----------|-------------|
| Bulk consolidation not on managed service | LOW | Bulk consolidation tool requires batch-delete, which has no on-chain equivalent. Store-time dedup supersession now works via tombstones. |
| MCP no auto-memory | By design | MCP has no lifecycle hooks. Host agent (Claude, Cursor) must call tools explicitly. Documented in beta guide. |
| IronClaw no lifecycle hooks | By design | IronClaw uses routines engine (cron/event-driven) instead of lifecycle hooks. Auto-extraction requires routine setup. Documented in ironclaw-setup.md. |
| IronClaw CLI no MCP support | MEDIUM | `nearai mcp add` / `ironclaw mcp add` CLI does not exist. Users must manually configure MCP server in NEAR AI agent config. |
| Export/import not on managed service (MCP) | LOW | MCP server's export and import tools are self-hosted only. OpenClaw plugin handles both modes. |
| Crypto payments removed | LOW | Coinbase Commerce sunset March 31, 2026. Removed from relay, tools, and website. Stripe (fiat) is the sole payment method. |
| Hermes not on PyPI (hermes-agent) | MEDIUM | `hermes-agent` package is not published on PyPI. Users must install from git. The `totalreclaw[hermes]` extra depends on it, so Hermes plugin setup requires manual steps. |
| Hermes no import/migrate/consolidate | MEDIUM | Python client does not yet implement import adapters, migrate tool, or consolidation. Core remember/recall/forget/export/status work. |
| Hermes no client batching | LOW | Python client submits facts one-by-one (no ERC-4337 executeBatch). Acceptable for extraction volumes (max 15 facts). |
| Hermes no store-time dedup | LOW | Python client does not check for near-duplicates before storing. Server-side content fingerprint still prevents exact duplicates. |
| Hermes heuristic extraction only | MEDIUM | Hermes plugin uses regex-based extraction (no LLM call). Sufficient for preferences/facts/decisions but less comprehensive than LLM-guided extraction. |
| ZeroClaw no import/migrate | LOW | Rust crate implements core Memory trait + status + export + upgrade (Stripe checkout) but not import adapters or migrate tool. |
| ZeroClaw no client batching | RESOLVED | Rust crate supports executeBatch() multi-call UserOps (up to 15 facts per batch). |
| ZeroClaw UserOp submission | RESOLVED | Native Rust ERC-4337 v0.7 UserOp construction via alloy-primitives/alloy-sol-types. Hash + signing verified byte-for-byte against viem. |
| ZeroClaw client-consistency | RESOLVED | Rust crate now fully compliant: client ID header, billing cache (2h TTL), quota warnings, 403 handling, dynamic candidate pool, store-time cosine dedup (0.85), hot cache (30 entries), importance normalization, auto-recall top_k=8, broadened search fallback, chain ID auto-detect from billing. 24 spec compliance tests + 2 E2E tests against staging. |
| Hermes no chain ID auto-detect | LOW | Python client defaults to 84532 (Base Sepolia) but does not auto-detect Pro tier from billing to switch to chain 100 (Gnosis). Pro users must set `chain_id=100` manually. |
| Debrief bypasses store-time dedup | LOW | MCP, NanoClaw, Hermes call `client.remember()` directly for debrief items (no cosine dedup). Only OpenClaw routes through `storeExtractedFacts()`. LLM-level dedup via prompt + server-side content fingerprint mitigate. |
| Hermes debrief stores without embedding | LOW | `hooks.py` stores debrief items without embedding param — no LSH bucket hashes, search relies on word-level blind indices only. |
| NanoClaw debrief no 8-message guard | LOW | `pre-compact.ts` triggers debrief based on extraction results, not conversation length. LLM prompt handles it, but no code-level guard like other clients. |

---

## Implementation Rules

### New Feature Checklist

Every new feature implementation MUST include:

1. **Documentation** -- If user-facing: create or update a guide in `docs/guides/` explaining what it does and how users interact with it. If internal: add to the relevant spec in `docs/specs/`.

2. **Feature compatibility table** -- Update the "Feature Compatibility Matrix" section above:
   - Add the feature to the **Platform Support** table (OpenClaw / MCP / NanoClaw)
   - Add to the **Storage Mode Support** table if relevant (Self-Hosted / Managed Service)
   - If there are platform-specific caveats, add a note
   - If a platform lacks support, add to **Known Gaps**

3. **Cross-platform consideration** -- Before marking a feature as done, verify whether it should also work on other platforms. If a feature is added to the OpenClaw plugin but not the MCP server, that is a gap that must be documented (and ideally tracked for implementation).

4. **E2E testing (MANDATORY)** -- Every major feature, refactor, or code change MUST be validated with end-to-end integration tests BEFORE the feature can be considered complete. This is non-negotiable.
   - **What counts as major**: New on-chain submission patterns (batching), billing/routing changes, embedding model changes, protocol changes, env var renames, new extraction logic, anything touching the relay-bundler-subgraph pipeline.
   - **How to test**: Two levels of E2E testing:
     1. **Staging smoke tests** (`tests/e2e-batch/`): Direct API + on-chain tests against live staging. Fast (~2 min). Run for every feature.
     2. **Full agent lifecycle tests** (`totalreclaw-internal/e2e/`): OpenClaw Docker-based tests that mimic real user conversations. Tests auto-extraction, cross-session recall, explicit tools, forget/export. Run for major features and before releases.
   - **What to verify**: The full pipeline -- fact extraction → encryption → on-chain write → subgraph indexing → search → decryption → recall. Not just unit tests, not just type-checking.
   - **When E2E can't run immediately**: Explicitly plan when it will run and leave it as an open item. Do NOT mark the feature as complete.
   - **A feature is NOT done until E2E validates it.** Code that compiles and passes unit tests can still fail at the integration level (wrong chain behavior, subgraph not indexing, paymaster rejecting batched UserOps, etc.).

---

## Known Technical Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| LSH parameters | RESOLVED | 32-bit x 20 tables, 98.1% Recall@8 on real data |
| Authentication | RESOLVED | HKDF auth with SHA-256 key hashing |
| Embedding model | RESOLVED | Migrated to Xenova/multilingual-e5-small (384d, ~34MB, mean pooling). Harrier-OSS-v1-270M (640d) blocked by ONNX runtime incompatibility (GatherBlockQuantized op) -- revisit when @huggingface/transformers upgrades ONNX Runtime to 1.25+. |
| Client batching (A2) | RESOLVED | Implemented in client/src/userop/batcher.ts -- batch multiple facts per UserOp |
| Candidate pool sizing | RESOLVED | Server-configurable via relay billing endpoint (`max_candidate_pool` in FeatureFlags). Env overrides: `CANDIDATE_POOL_MAX_FREE`, `CANDIDATE_POOL_MAX_PRO`. |
| Load testing | RESOLVED | Managed service load test at `totalreclaw-internal/e2e/load-test-managed/`. Client-side <140ms p95 PASS up to 10K facts. |
| Stripe-driven tiers | PLANNED | Stripe as source of truth for pricing/limits. Plan at `totalreclaw-internal/plans/2026-03-26-stripe-driven-tiers.md` |
| LLM memory import (ChatGPT/Claude/Gemini) | PLANNED | Adapters for importing memory from major LLM providers |
| Conflict resolution (Layers 3-4) | MEDIUM | Spec'd in v0.3.2, not implemented |
| Migration tool (testnet to mainnet) | RESOLVED | Implemented as `totalreclaw_migrate` tool in MCP server + OpenClaw plugin. Dry-run by default, idempotent, batch submission. |
| Startup validation | MEDIUM | Validate Pimlico/Stripe/Subgraph reachability on relay boot |
| DB backup monitoring | LOW | Add alerting (Slack/email) if daily R2 backup fails |
| Graceful shutdown | LOW | Not yet configured in uvicorn |

---

## Key Constraints

- **Search latency**: <140ms p95 for 1M memories
- **Recall**: >=93% of true top-250
- **Storage overhead**: <=2.2x vs plaintext
- **Server-blind**: Server NEVER sees plaintext
- **Embedding model**: Xenova/multilingual-e5-small (384d, ~34MB, mean pooling)
- **Extraction cap**: Max 15 facts per extraction cycle, unified 3-turn interval
- **Memory types**: 7 categories -- fact, preference, decision (with reasoning), episodic, goal, context, summary

---

## Build and Test

```bash
# Server
pip install -r requirements.txt -r server/requirements.txt
cd server && python -m pytest tests/ -v

# Client library
cd client && npm install && npm test

# OpenClaw plugin
cd skill/plugin && npm install && npm test

# MCP server
cd mcp && npm install && npm run build

# NanoClaw skill
cd skill-nanoclaw && npm install

# Python client + Hermes plugin
cd python && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && python -m pytest tests/ -v

# Rust crate (ZeroClaw memory backend)
cd rust/totalreclaw-memory && cargo test

# Cross-language parity tests (Python ↔ TypeScript)
cd tests/parity && node --experimental-strip-types cross-impl-test.ts

# E2E Tests (internal repo -- requires staging access)
cd ../totalreclaw-internal/e2e
ZAI_API_KEY=xxx ANTHROPIC_API_KEY=xxx npm test           # All paths
npm run test:mcp                                          # MCP only (fastest)
ZAI_API_KEY=xxx npm run test:openclaw                    # OpenClaw only
ANTHROPIC_API_KEY=xxx npm run test:nanoclaw              # NanoClaw only

# Performance Benchmarks (internal repo -- requires Docker + Foundry/Anvil)
cd ../totalreclaw-internal/e2e/load-test-managed
npm install && bash setup-local.sh && npx tsx run-scaling-test.ts --queries-per-tier 100
```

Performance benchmarks for the managed service search pipeline live in `totalreclaw-internal/e2e/load-test-managed/`. These measure client-side search latency (decryption, cosine, BM25, reranking) at progressive vault sizes against the <140ms p95 target. Run after any change to the search pipeline, LSH parameters, embedding model, or candidate pool sizing. See the README in that directory for full documentation.

---

## CI/CD Pipeline

Full CI/CD pipeline documented in `totalreclaw-internal/docs/ci-cd-pipeline.md`. Key rules:

1. **Production NEVER auto-deploys from git push.** Staging auto-deploys; production requires manual promotion via `railway up -s totalreclaw-production -d` or `railway redeploy`.
2. **E2E tests gate production promotion.** At minimum, relay smoke tests must pass against staging before promoting.
3. **npm packages tested against staging before publish.** Run `tests/verify-publish.sh` after every publish.
4. **Subgraph deploys to staging first.** Verify indexing before deploying to production subgraph.

For deployment procedures, invoke the `deploy-totalreclaw` skill.

### Relay Environments

| Environment | Railway Service | URL | Deploys |
|-------------|----------------|-----|---------|
| **Staging** | `totalreclaw` | `https://api-staging.totalreclaw.xyz` | Auto on push to `main` |
| **Production** | `totalreclaw-production` | `https://api.totalreclaw.xyz` | Manual via `railway up -s totalreclaw-production -d` |

**IMPORTANT: All tests (E2E, integration, smoke, cross-client) MUST hit the staging relay (`api-staging.totalreclaw.xyz`), NEVER production.** Both environments use Base Sepolia testnet for free-tier users. Test registrations send `X-TotalReclaw-Test: true` header and show as `[TEST]` in Telegram notifications.

### Relay Quick Reference

```bash
# Staging deploys automatically on push to main
# Run smoke tests after staging deploys:
RELAY_URL=https://api-staging.totalreclaw.xyz npx tsx tests/e2e-relay/smoke-test.ts

# Promote to production (after smoke tests pass):
cd ../totalreclaw-relay && railway up -s totalreclaw-production -d

# Rollback production:
git checkout <known-good-commit>
railway up -s totalreclaw-production -d
git checkout main
```

### npm Publish Flow

```bash
# 1. Tests pass: cd client && npm test
# 2. E2E against staging: RELAY_URL=https://api-staging.totalreclaw.xyz npx tsx tests/e2e-relay/smoke-test.ts
# 3. Version bump + publish: cd client && npm version patch && npm run build && npm publish
# 4. Verify: ./tests/verify-publish.sh
```

---

## Current Status

- **Version**: v1.0-beta
- **Phase**: Private Beta
- **Default mode**: Managed Service with dual-chain (free=Base Sepolia testnet, pro=Gnosis mainnet)
- **Default chain ID**: 84532 (Base Sepolia) -- all clients default to free tier, auto-detect Pro (chain 100/Gnosis) from billing
- **Embedding model**: Xenova/multilingual-e5-small (384d, ~34MB, mean pooling). Harrier-OSS-v1-270M (640d) blocked by ONNX runtime incompatibility (GatherBlockQuantized op) -- revisit when @huggingface/transformers upgrades ONNX Runtime to 1.25+.
- **Crypto core**: `@totalreclaw/core` v1.0.0 (Rust WASM for npm, PyO3 for PyPI) -- 13 modules (crypto, reranker, wallet, userop, store, search, blind, lsh, fingerprint, hotcache, consolidation, debrief, stemmer). Single source of truth for all clients.
- **Packages**: `@totalreclaw/core@1.0.0`, `@totalreclaw/client@1.0.0`, `@totalreclaw/mcp-server@2.0.0`, `@totalreclaw/totalreclaw@3.4.0` (OpenClaw plugin, npm + ClawHub); `totalreclaw-core@1.0.0`, `totalreclaw@1.0.0` (PyPI)
- **OpenClaw integration**: Plugin installs without force flags (`openclaw plugins install`), hot-reload setup via `TOTALRECLAW_HOT_RELOAD=true`, auto-recall on `before_agent_start`, auto-extraction on `agent_end`, LLM config sourced from OpenClaw providers, plaintext fallback prevention
- **Relay**: Billing, Pimlico sponsorship, dual-chain routing, and query proxying extracted to private `totalreclaw-relay` TypeScript repo (p-diogo/totalreclaw-relay). Public server retains only self-hosted functionality (storage, search, auth).
- **Staging**: Base Sepolia (chain 84532) -- free testnet, no gas costs
- **Production**: Gnosis mainnet (chain 100) -- Pro tier only
- **All releases via CI**: GitHub Actions workflows for npm, PyPI, and ClawHub. Never publish manually.
