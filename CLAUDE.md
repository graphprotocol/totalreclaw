# TotalReclaw - Project Guide

## Project Overview

**TotalReclaw** is an end-to-end encrypted memory vault for AI agents -- the "password manager for AI memory."

### Core Value Proposition
1. **Encrypted** -- End-to-end encrypted (XChaCha20-Poly1305). Server never sees plaintext.
2. **Portable** -- One-click plain-text export. No vendor lock-in.
3. **Universal** -- Works across OpenClaw, Claude Desktop, any MCP-compatible agent.

### Target Users
- Non-technical hosted OpenClaw users (memory locked to Railway/Vercel)
- Power users with multiple AI agents (fragmented memory across tools)

---

## Architecture (v0.3)

Two storage modes: **Managed Service** (default -- on-chain via The Graph, accessed through relay) and **Self-Hosted** (PostgreSQL backend you run yourself). The client-side E2EE pipeline is identical for both.

Managed Service chain routing (**single-chain Gnosis — ops-1 shipped 2026-06-05, `totalreclaw-internal#283` closed**):

- **Single-chain Gnosis is LIVE.** Both tiers run on **Gnosis mainnet (chain 100)**. Env: `PIMLICO_CHAIN_ID=100` (free) and `PRO_PIMLICO_CHAIN_ID=100` (pro) on both `totalreclaw` (staging) and `totalreclaw-production`. The legacy Free → Base Sepolia (84532) routing was retired in ops-1.
- **Why single-chain:** the real cost unit is **Pimlico UserOps (chain-independent) — we don't pay gas directly** — so there's no economic reason to keep free on a testnet; and, decisively, a free→pro upgrade must not strand data. With both tiers on the same chain + DataEdge + subgraph, an upgrade is a pure `tier='pro'` flag flip with zero data migration. Verified live: a free registration's `GET /v1/billing/status` returns `chain_id: 100`.
- **Clients are chain-aware via billing.** The relay returns authoritative `chain_id` + `data_edge_address` in `/v1/billing/status` (sourced from the per-tier chain-router); chain-aware clients consume them verbatim, so a future chain change needs zero client release.

Smart Account addresses are deterministic (CREATE2) and byte-equal across chains. **Production** DataEdge = `0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca` (both tiers), indexed by the `total-reclaw-gnosis` subgraph. **Staging is on-chain isolated** (shipped): its own DataEdge `0xE7a4D2677B686e13775Ba9092631089e35F0BB91` + dedicated `total-reclaw-gnosis-staging` subgraph, so staging writes never touch production data. See `totalreclaw-internal/docs/specs/ops/staging-chain-isolation.md`.

```
+-------------------------------------------------------------------------+
|                           CLIENT (OpenClaw Skill)                       |
+-------------------------------------------------------------------------+
|  +--------------+  +--------------+  +--------------+  +-------------+ |
|  | Fact Extract |->|   Encrypt    |->| Generate LSH |->| Blind Index | |
|  |    (LLM)     |  | (XChaCha20) |  |   Buckets    |  |  (SHA-256)  | |
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
|  All tiers: Gnosis (100)      |  |  - raw_events (immutable log) |
|  Relay: Pimlico bundler       |  |  - facts (blind_indices,      |
|  Index: The Graph subgraph    |  |          decay_score)         |
|  Query: GraphQL via relay     |  |                               |
|                               |  |                               |
+-------------------------------+  +-------------------------------+
              |                                 |
              +----------------+----------------+
                               v
+-------------------------------------------------------------------------+
|                         CLIENT (Re-ranking)                             |
+-------------------------------------------------------------------------+
|  Decrypt candidates -> Harrier 640d embeds -> BM25+Cosine+RRF -> Top 8 |
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
├── skill/                 # OpenClaw plugin (@totalreclaw/totalreclaw at skill/plugin/: automatic memory via lifecycle hooks)
├── skill-nanoclaw/        # NanoClaw skill package + MCP server
│   ├── src/               # Hooks, extraction logic
│   └── mcp/               # NanoClaw agent-runner (spawns @totalreclaw/mcp-server)
├── python/                # Python client library + Hermes Agent plugin
│   ├── src/totalreclaw/   # Client library (crypto, LSH, relay, reranker, embedding)
│   └── src/totalreclaw/hermes/  # Hermes Agent plugin (hooks, tools, state)
├── rust/                  # Rust crate for ZeroClaw memory backend
│   └── totalreclaw-memory/  # Native Rust implementation (crypto, relay, Memory trait)
├── mcp/                   # Generic MCP server (for Claude Desktop, etc.)
├── app/                   # Vault SPA (Vite + React + TS, managed-mode reads via relay subgraph proxy)
├── tools/                 # QA + ops helpers
│   └── qa-vault.mjs       # Playwright vault driver (keychain or QA_RECOVERY_PHRASE env)
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
    └── guides/            # User-facing guides (beta, import/migration, memory dedup)
```

---

## Technical Specifications

Specs are organized by product area under `docs/specs/`:

### TotalReclaw (E2EE) -- `docs/specs/totalreclaw/`
| Spec | File | Status |
|------|------|--------|
| E2EE Architecture (LSH + Blind Buckets) | `architecture.md` | Implemented, validated |
| **Memory Taxonomy v1** | `memory-taxonomy-v1.md` | **Shipped 2026-04-18 across 5 clients + core 2.0.0** |
| **Retrieval v2 (Tier 1 source-weighted)** | `retrieval-v2.md` | **Tier 1 shipped in core 2.0.0; Tier 2-4 designed** |
| **Tiered Retrieval (impl deep dive)** | `tiered-retrieval.md` | Shipped |
| Server PoC v0.3.1b (Auth + Dedup) | `server.md` | Partially implemented |
| Client Consistency | `client-consistency.md` | Canonical reference for all 5 clients |
| OpenClaw Skill | `skill-openclaw.md` | Implemented |
| NanoClaw Skill | `skill-nanoclaw.md` | Implemented |
| MCP Server | `mcp-server.md` | Implemented |
| Admin Dashboard | `admin-dashboard-design.md` (plans/) | Implemented (in private relay repo) |
| MCP Auto-Memory (Generic Hosts) | `mcp-auto-memory.md` | Spec complete |
| Benchmark Harness (OMBH) | `benchmark.md` | Implemented |
| LSH Tuning (Multi-Tenant SaaS) | `lsh-tuning.md` | Complete |
| Conflict Resolution v0.3.2 | `conflict-resolution.md` | Design complete, not implemented |
| Retrieval Improvements v3 | `retrieval-improvements-v3.md` | Implemented (superseded by retrieval-v2 in v1) |

### Subgraph (Decentralized) -- `docs/specs/subgraph/`
| Spec | File | Status |
|------|------|--------|
| Seed-to-Subgraph v1.0 | `seed-to-subgraph.md` | Implemented, deployed to Gnosis mainnet |
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

Features across OpenClaw plugin (`skill/plugin/`), MCP server (`mcp/`), NanoClaw (`skill-nanoclaw/`), Hermes Agent (`python/`), IronClaw (via MCP server — **WIP, paused as of 2026-04-18**), and ZeroClaw (`rust/totalreclaw-memory/`).

**IronClaw status (2026-04-18)**: paused. Functional via MCP server but lacks first-class CLI integration (`nearai mcp add` / `ironclaw mcp add` does not exist) + no lifecycle hooks (uses routine engine). Existing column kept in the matrix for historical reference; no new IronClaw work tracked until further notice.

| Feature | OpenClaw Plugin | MCP Server | NanoClaw | Hermes | IronClaw | ZeroClaw | Notes |
|---------|:-:|:-:|:-:|:-:|:-:|:-:|-------|
| **Core Tools** | | | | | | | |
| `totalreclaw_remember` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_recall` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_forget` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_export` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (via Memory trait) | |
| `totalreclaw_status` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (billing cache) | |
| `totalreclaw_import_from` | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- | Mem0, MCP Memory, ChatGPT, Claude, Gemini adapters. Gemini parsing (MyActivity.json + HTML + Saved-info paste) is hoisted to `@totalreclaw/core@2.5.0` `parseGemini` — one universal, locale-robust, lossless parser shared by TS (WASM) + Python (PyO3). |
| `totalreclaw_import` | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | JSON/Markdown re-import (MCP only) |
| Session segmentation (import Crystal grouping) | Pending (parked client) | -- | Pending (parked client) | Yes (core + local fallback) | -- | -- | Centroid-walk semantic session segmentation for imports — **core-hoisted** (`totalreclaw_core.segment_sessions` / WASM `segmentSessions`, #368). Pure math only; embedding stays client-side. Hermes' `import_engine` prefers the core fn and falls back to the local Python impl when the installed core wheel predates the hoist. TS/WASM binding shipped but consumer wiring is pending client unpark. |
| Flat per-turn import view (`parse_gemini.turns`) | Pending (parked client) | -- | Pending (parked client) | Yes (core + chunk fallback) | -- | -- | #368 Part 2. Core `parse_gemini` (`totalreclaw_core.parse_gemini` / WASM `parseGemini`) now emits a flat `turns` list — one entry per prompt→reply exchange with its OWN `ts_iso`/`ts_unix` + embed `text` + authoritative `chunk_index`. Lets `_get_session_assignments` segment over REAL per-turn timestamps instead of re-deriving turns from chunks and sharing one chunk timestamp. Hermes uses `parsed.turns` when present, falls back to the pre-Part-2 chunk re-derivation when absent (older core wheel, or non-Gemini source). TS/WASM binding ships the field automatically; consumer wiring pending client unpark. |
| Per-turn straddle-splitting (import fact attribution) | Pending (parked client) | -- | Pending (parked client) | Yes (core + fallback) | -- | -- | #368 Part 2 (fact-attribution fidelity). Each `ParsedTurn` now also carries its message-index range within its chunk (`chunk_msg_start`/`chunk_msg_end`). When a chunk's turns straddle a session boundary (mid-chunk topic/time shift), `import_engine` splits it into per-session sub-chunks for extraction so each fact lands in the session its turn belongs to — closing the last chunk-level approximation ("a straddling chunk assigned wholesale to its first turn's session"). Non-straddling chunks extract wholesale (behaviour-preserving); the `_turns_from_chunks` fallback derives the same ranges so it works without the core range fields too. Crystal id resolved by session index so a shared straddling chunk doesn't collapse two sessions' Crystals. Embed cost unchanged (one embed per turn). TS plugin/MCP import rewire remains PARKED. |
| `totalreclaw_upgrade` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Stripe checkout URL |
| `totalreclaw_consolidate` | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Self-hosted only (no batch delete on managed service) |
| `totalreclaw_debrief` | Yes (auto) | Yes | Yes (auto) | Yes (auto) | Yes (via MCP) | Yes (debrief method) | Session debrief — broader context at conversation end |
| **Automatic Memory** | | | | | | | |
| Auto-search (before_agent_start) | Yes | -- | Yes (hook) | Yes (pre_llm_call) | -- | Yes (Memory trait) | ZeroClaw calls recall() at conversation start |
| Auto-extract (agent_end) | Yes | -- | Yes (hook) | Yes (post_llm_call) | -- | Yes (Memory trait) | ZeroClaw consolidation calls store() |
| Pre-compaction flush | Yes | -- | Yes (hook) | -- | -- | -- | Hermes has no compaction hook |
| Pre-reset flush | Yes | -- | -- | -- | -- | -- | OpenClaw only |
| Session-end flush | -- | -- | -- | Yes (on_session_end) | -- | -- | Hermes plugin flushes unprocessed messages |
| Session debrief | Yes (hook) | Yes (tool) | Yes (hook) | Yes (hook) | Yes (via MCP tool) | Yes (debrief method) | Captures broader context at session end; max 5 items |
| Content-aware Crystal gate | -- | -- | -- | Yes (auto) | -- | -- | #434, shipped 2026-07-05. Auto session-debrief Crystal length gate is content-aware: hard floor `< 4` messages never crystallizes; `4-7` messages (2-3 turns) crystallize only with substance (`>= 2` stored facts); `>= 8` messages always qualify. Enforced in `lifecycle.session_debrief`, defended in depth in `generate_crystal`/`generate_debrief`. The explicit `totalreclaw_debrief` tool keeps the simple 4-turn (`< 8`) floor — no stored-fact context to judge substance. Hermes-only — MCP/NanoClaw/ZeroClaw have no auto-extraction pipeline to wire it into. |
| Per-conversation session isolation + idle Crystals | -- | -- | -- | Yes (auto) | -- | -- | #441 (2026-07-05). Parallel conversations / Telegram topics each get their OWN slot (buffer + turn counter + session id) keyed by the per-conversation `session_id` Hermes passes to `MemoryProvider.sync_turn` / per-turn hooks → each mints a separate Crystal instead of one mixed blob. Plus a per-conversation **idle Crystal sweep** (`AgentState.find_idle_slots`/`sweep_idle_slots`, wired in `ingest_turn`): a topic idle past `TOTALRECLAW_SESSION_IDLE_MINUTES` (default 60) crystallizes on another topic's turn — so Crystals mint even when Hermes `session_reset: none` disarms the host's finalize watcher. Backward-compatible (empty slot store ⇒ legacy single-session). Hermes-only. |
| CLAUDE.md sync | -- | -- | Yes | -- | -- | -- | NanoClaw syncs high-importance facts |
| Routine-based extraction | -- | -- | -- | -- | Yes (cron) | -- | IronClaw routines engine; see ironclaw-setup.md |
| Decay handling | -- | -- | -- | -- | -- | Yes (via ZeroClaw) | ZeroClaw applies 7-day half-life at retrieval time |
| Conflict resolution | -- | -- | -- | -- | -- | Yes (via ZeroClaw) | ZeroClaw checks semantic similarity before storing Core |
| **Extraction** | | | | | | | |
| Expanded memory types (8 categories, v0 legacy) | Read-only | Read-only | Read-only | Read-only | Read-only | Read-only | fact, preference, decision, episodic, goal, context, summary, rule — reads only, coerced to v1 on recall |
| **Memory Taxonomy v1 (6 types)** | Yes (write default) | Yes (write default) | Yes (write default, via MCP) | Yes (write default) | Yes (via MCP) | Yes (write via `store_v1`) | `claim / preference / directive / commitment / episode / summary`. v1 is the only write path. |
| `source` field (provenance) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | user / user-inferred / assistant / external / derived |
| `scope` field (life domain) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | work/personal/health/family/creative/finance/misc/unspecified |
| `volatility` field | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | stable / updatable / ephemeral |
| `reasoning` field (decision-style claims) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Separate field; `claim` type with `reasoning` replaces v0 `decision` |
| `agent_name` field (agent-instance provenance, #317) | -- | -- | -- | Yes (write) | -- | -- | User-given instance name persisted in the encrypted blob so provenance renders "John (Hermes)". Env `TOTALRECLAW_AGENT_NAME` or explicit `remember(agent_name=…)`. Additive metadata, different axis from `source`; rides the encrypted inner blob (no on-chain/subgraph schema change). Other clients get it for free once they set the name (metadata-map, no core change). **SPA now READS + renders it** — `app/` decrypts the top-level blob key and shows a "via John (Hermes)" line on each memory (ClaimCard) plus a per-session agent breakdown (SidePanel); `composeProvenanceLabel` in `app/src/lib/provenance.ts` mirrors the Python helper. Non-Hermes client capture still pending. |
| Decision reasoning extraction | Yes | Yes (via prompt) | Yes | Yes (LLM or heuristic) | Yes (via prompt) | Yes (via ZeroClaw) | Extraction prompts require "chose X because Y" |
| Protobuf v4 outer wrapper | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | `version = 4` signals v1 inner JSON blob; subgraph schema unchanged |
| G-pipeline extraction | Yes | -- | Yes (hook) | Yes | -- | Yes (store pipeline) | Merged-topic prompt + provenance filter lax + comparative rescore + volatility heuristic |
| **Knowledge Graph (Core Hoist Tier 1)** | | | | | | | All via `@totalreclaw/core` 1.5.0 WASM/PyO3 with local fallbacks |
| Store-time dedup (best-match) | Yes (core) | Yes (core) | Yes (via MCP) | Yes (core) | Yes (via MCP) | Yes (core) | `find_best_near_duplicate` — returns highest-similarity match, not first |
| Bulk clustering | Yes (core) | Yes (core) | Yes (via MCP) | -- | Yes (via MCP) | -- | `cluster_facts` — greedy single-pass for consolidation tool |
| Pin status semantics | Yes (core) | -- | -- | -- | -- | Yes (core) | `is_pinned_claim`, `respect_pin_in_resolution` |
| Contradiction detection orchestration | Yes (core) | -- | -- | Yes (core) | -- | Yes (core) | `resolve_with_candidates` — full pipeline: detect → pin check → resolve → tie-zone |
| Decision log types | Yes (core) | -- | -- | -- | -- | Yes (core) | `DecisionLogEntry`, `find_loser_claim_in_decision_log` — enables pin-on-tombstone recovery |
| Shadow mode filtering | Yes (core) | -- | -- | -- | -- | Yes (core) | `filter_shadow_mode` — observer-only validation mode |
| Importance rubric (1-10 anchored) | Yes | Yes (via prompt) | Yes | Yes | Yes (via prompt) | Yes | Phase 2.2.6 — explicit band definitions in extraction prompt |
| Lexical importance bump | Yes | -- | -- | Yes | -- | -- | Phase 2.2.6 — +1/+2 post-processing for intent/emphasis/repetition signals |
| Bump cap (≥8 → max +1) | Yes | -- | -- | Yes | -- | -- | Phase 2.2.7 — prevents over-scoring already-high facts |
| Type in recall results | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- | `[rule]` `[fact]` `[decision]` prefix tags in recall output |
| Remember type+importance params | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | -- | Phase 2.2.6 — `type` and `importance` in totalreclaw_remember tool |
| **Retrieval v2 — Tier 1 source-weighted rerank** | Yes (core) | Yes (core) | Yes (via MCP) | Yes (core) | Yes (via MCP) | Yes (core) | All via `@totalreclaw/core@2.0.0` `rerankWithConfig` — user=1.0, user-inferred=0.9, derived/external=0.7, assistant=0.55, legacy=0.85 |
| `totalreclaw_pin` tool | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | New in v1 — locks memory against auto-supersession |
| `totalreclaw_unpin` tool | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | New in v1 |
| `totalreclaw_retype` tool | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | New in v1 — change memory type (e.g. preference → directive) |
| `totalreclaw_set_scope` tool | -- | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | New in v1 — assign memory to a scope |
| **Dedup** | | | | | | |
| Content fingerprint (exact) | Yes | Yes | Yes | Yes | Yes (via MCP) | Yes | Server-side HMAC-SHA256 |
| Within-batch semantic dedup | Yes | -- | -- | -- | -- | -- | Cosine >= 0.9, during extraction |
| Store-time near-duplicate | Yes (core) | Yes (core) | Yes (via MCP) | Yes (core) | Yes (via MCP) | Yes (cosine >= 0.85) | Via `@totalreclaw/core` `findBestNearDuplicate` with local fallback |
| LLM-guided dedup (ADD/UPDATE/DELETE) | Yes | -- | Yes | Yes | -- | -- | All tiers — uses user's own LLM API key, zero cost to us |
| Bulk consolidation tool | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | -- | Self-hosted only (no batch delete on managed service) |
| **Pro Tier Gating** | | | | | | |
| Feature gating via billing cache | Yes | -- | Yes | Yes | -- | Yes (2h TTL) | Server returns `features` dict, plugin/skill gates client-side |
| Server-side extraction config | Yes | -- | Yes | -- | -- | Yes | Relay returns `extraction_interval` + `max_facts_per_extraction` in billing status |
| Unified extraction interval (3 turns) | Yes | -- | Yes | Yes | -- | Yes | Server-tunable via relay config (no npm publish needed) |
| Max facts per extraction | Yes | -- | Yes | Yes | -- | Yes | Server-tunable via relay config (default 15) |
| Chain auto-detect from billing | Yes | Yes | Yes | -- | -- | Yes | Client reads its chain from the relay billing response. Relay routes both tiers to Gnosis (100) — single-chain shipped (ops-1, #283 closed 2026-06-05). |
| **Batching** | | | | | | |
| Client batching (multi-call UserOps) | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Hermes batches via `client.remember_batch()` (PyPI 2.0.0+). With both tiers on Gnosis (chain 100) post-ops-1, free batches via the same `executeBatch` path as pro — the old Base Sepolia gas-estimation blocker no longer applies. **Verified 2026-07-04 on staging**: fresh free-tier account, chain_id=100, 5 facts written via one `executeBatch` UserOp (tx `0xca441bc4...`), all 5 indexed + individually recallable in ~21s, then tombstoned. |
| **Billing** | | | | | | |
| Quota warnings (>80%) | Yes | -- | Yes | Yes | -- | Yes | Injected via on_session_start hook; ZeroClaw via quota_warning() method |
| 403 handling + cache invalidation | Yes | Yes | Yes | -- | Yes (via MCP) | Yes | ZeroClaw invalidates billing cache on 403, returns QuotaExceeded error |
| Update-available notice | -- | -- | -- | Yes | -- | -- | Hermes-only. Relay serves `features.latest_stable_python` (set via `LATEST_STABLE_PYTHON` env — dark until configured); client compares vs installed `__version__` (rc<final aware), nudges once/24h via quota-warning channel. Kill-switch `TOTALRECLAW_DISABLE_UPDATE_NOTICE=1`. Fires fleet-wide on the one env flip at each stable promote. |
| **Search Optimizations** | | | | | | |
| Hot cache + two-tier search | Yes (managed) | -- | -- | -- | -- | Yes (30 entries, cosine >= 0.85) | Skips remote query if cached query similar |
| Dynamic candidate pool sizing | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Yes | Server-configurable via billing features; env overrides `CANDIDATE_POOL_MAX_FREE`/`CANDIDATE_POOL_MAX_PRO` |
| Server-side candidate pool | Yes | Yes | Yes (via MCP) | -- | Yes (via MCP) | Yes | Relay computes `max_candidate_pool` from vault size + tier; clients read from billing cache with local fallback |
| Broadened search fallback | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Fetches recent facts by owner when trapdoor search returns 0 (vague queries like "who am I?") |
| BM25 + Cosine + RRF reranking | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes | Intent-weighted |
| Entity-trapdoor recall (read-side) | -- | -- | -- | Yes | -- | -- | #370, shipped 2026-07-03 (#377). Query entities (heuristic) → unkeyed `sha256("entity:"+name)` trapdoors appended to search. +2.4pp Hit@16 at ~2700-fact scale (production-config A/B; Sonnet-gated). Write-side (`entities[]` → blind_indices) shipped across plugin/MCP/Hermes since `ac1b872`; read-side is Hermes-only until other clients un-park. |
| Session-id in blob metadata (write) | -- | -- | -- | Yes | -- | -- | Hermes stamps the active conversation's `session_id` into `metadata.session_id` on **both** atomic facts and the session-end Crystal (memq-2/-3 plumbing existed but was never wired at write time). Encrypted-blob-only. Enables the SPA to group memories by real conversation; degrades to time-gap grouping where absent. |
| Vault conversation grouping (SPA read) | n/a | n/a | n/a | n/a | n/a | n/a | Vault SPA (`app/`): the **Conversations** view partitions decrypted memories by `metadata.session_id` (surfaced as `VaultItem.sessionId`), time-gap sub-split within each session + a pure time-gap fallback where absent. Not a per-client column — it's the web vault reading whatever the write-side stamped. |
| **Admin & Analytics** | | | | | | |
| X-TotalReclaw-Client header | Yes | Yes | Yes (via MCP) | Yes | Yes (via MCP) | Yes (rust-client:zeroclaw) | Sent on every relay request. Python client appends its version (`python-client/<version>`) for observability; the relay buckets analytics on the part before the first `/`, so per-release version suffixes don't fragment client-type aggregation. |
| Admin dashboard | -- | -- | -- | -- | -- | Admin-only (relay service), not a client feature |

### Storage Mode Support

Features across Self-Hosted (PostgreSQL) and Managed Service (default, Gnosis mainnet via The Graph).

Managed Service tier model (single-chain Gnosis — ops-1 shipped, #283 closed 2026-06-05): **Free** = 250 memories/month on **Gnosis mainnet** (chain 100) — permanent, E2E encrypted, no credit card required. **Pro** = 1,500 memories/month on **Gnosis mainnet** (chain 100) — permanent, LLM-guided dedup, custom extraction interval, **plus import (ChatGPT/Gemini/Claude) which is a Pro-only feature**; pricing via Stripe — see `totalreclaw_status`. Both tiers run on Gnosis; the only difference is quota + Pro features.

**Batching (UserOp `executeBatch`) is a universal mechanism, not a tier feature.** Cost = Pimlico UserOps (chain-independent), so batching 15 facts → 1 UserOp cuts cost 15× for everyone. Hermes auto-extraction (`lifecycle.py`) already batches unconditionally for both tiers; the import engine chunk-batches. The Gnosis-only chain gate (`batch-gate.ts`/`batch_gate.py`, imp-16) was only ever a guard against a **Base Sepolia gas-estimation bug** (the old free chain) — a technical blocker, NOT a pricing decision. Now that ops-1 (#283, closed 2026-06-05) has put free on Gnosis, that gate is a no-op and batching is cleanly universal. Distinction to keep straight: **batching = universal; the import feature = Pro-only** (the batching inside import is incidental).

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
| Single-chain Gnosis routing | -- | Yes | **Single-chain shipped** (ops-1, `totalreclaw-internal#283` closed 2026-06-05): both Free and Pro route to Gnosis (chain 100). The legacy Free → Base Sepolia (84532) routing is retired. |
| Client batching | -- | Yes | Multi-call UserOps via batcher.ts (managed service only, uses ERC-4337 executeBatch). Universal mechanism; with both tiers on Gnosis post-ops-1 the old Gnosis-only gate is a no-op. |

### Known Gaps

| Gap | Severity | Description |
|-----|----------|-------------|
| Bulk consolidation not on managed service | LOW | Bulk consolidation tool requires batch-delete, which has no on-chain equivalent. Store-time dedup supersession now works via tombstones. |
| MCP no auto-memory | By design | MCP has no lifecycle hooks. Host agent (Claude, Cursor) must call tools explicitly. Documented in beta guide. |
| IronClaw integration paused (WIP) | LOW | All IronClaw work paused 2026-04-18. MCP server route still functional. CLI integration + lifecycle hooks deferred indefinitely. |
| Export/import not on managed service (MCP) | LOW | MCP server's export and import tools are self-hosted only. OpenClaw plugin handles both modes. |
| Crypto payments removed | LOW | Coinbase Commerce sunset March 31, 2026. Removed from relay, tools, and website. Stripe (fiat) is the sole payment method. |
| Hermes not on PyPI (hermes-agent) | RESOLVED | `totalreclaw` (now 2.4.6 on PyPI) includes the generic agent layer (no hermes-agent dependency). OPENAI_BASE_URL fix + model detection included. |
| Hermes no migrate/consolidate | MEDIUM | Imports SHIPPED via `python/src/totalreclaw/imports/` (Gemini/ChatGPT/Claude adapters; Hermes is imports-first among clients). What remains: no **migrate** tool (retired across all clients) and no **consolidate** on Hermes. The TypeScript (plugin/MCP) import wiring is parked. Core remember/recall/forget/export/status/import all work in Python. |
| Entity-trapdoor read-side Hermes-only | LOW | Query-entity trapdoors (#370/#377) retrieve only in the Python client. MCP/plugin/ZeroClaw store write-side entity trapdoors but don't query them — no accuracy regression vs before, just no +2.4pp lift. Port read-side (+ hoist `compute_entity_trapdoor` to core, Task 5 deferred) when clients un-park. |
| session_id blob-stamping Hermes-only | LOW | Only the Hermes write-side stamps `metadata.session_id` onto atomic facts + Crystals, so only Hermes-written vaults group by true conversation in the SPA; plugin/MCP/NanoClaw/ZeroClaw writes carry no session_id and the SPA falls back to time-gap grouping for them. No regression (all clients were time-gap-only before). Hoisting the stamp helper to core + wiring the other clients is the port when they un-park. Only takes effect on data written after the PyPI release that ships it; pre-existing vaults stay time-gap. |
| Hermes cannot use a self-hosted server | MEDIUM | The Python client's `RelayClient` speaks the managed-relay protocol only (`/v1/subgraph`, `/v1/bundler`, `/v1/billing/*`) — it has no HTTP storage path (`/v1/store`, `/v1/search`). Self-hosted mode requires the MCP server (or TS client / NanoClaw). Same limitation in the ZeroClaw Rust crate. Tracked in #364; see `docs/guides/self-hosted-deployment.md`. |
| Re-crystallize / re-key backfill (collapsed sessions) | IMPLEMENTED, NOT RUN | Client-side backfill that re-segments a vault whose on-chain `session_id`s collapsed (pre-#441/#443 write-side bug) into coherent sessions + fresh Crystals, tombstoning the old. Spec: `docs/specs/totalreclaw/recrystallize-backfill.md`; tool: `python/src/totalreclaw/recrystallize.py`. Dry-run planner + quota estimator (`2·F + S_multi + C_old`) + the guarded on-chain write/tombstone loop + checkpoint-resume are all **IMPLEMENTED + staging-E2E-validated** (#438 → #451) — dry-run is the default; execute requires `--write-side-fix-confirmed` + interactive confirm. **NOT yet run against any real user vault** (operator action, gated on Pedro; dry-run first). Must run AFTER the write-side fix is live on the target client. **Fidelity limit (by design):** it can only re-group **semantically** (Harrier embedding + `created_at` time-gap over *extracted facts*) — the original conversation turns + Hermes per-topic `session_id`s are NOT on-chain (only extracted facts are, tagged with the *collapsed* id), so it produces coherent-but-approximate groups, NOT a byte-identical replay of the live per-topic write-side grouping (§3 caveat + §10.1). |
| Hermes no client batching | RESOLVED | Python batch path shipped in PyPI 2.0.0+ (`client.remember_batch()`). With both tiers on Gnosis (chain 100) post-ops-1 (#283, closed 2026-06-05), free and pro both batch via `executeBatch` (ceiling raised 15 -> 30 in core 2.5.5, #392 Part 2) — the old Base Sepolia gas-estimation blocker no longer applies. **Independently E2E-verified 2026-07-04 on staging**: a fresh free-tier account (tier=free, chain_id=100) submitted 5 facts in one `executeBatch` UserOp (tx `0xca441bc437273f5067ca4d0abf6338e4aaa1079b1e924f2aa8f51017ea7d9b85`), all 5 indexed by the staging subgraph and individually recallable within ~21s, then cleanly tombstoned via `forget()`. |
| Free-tier batch UserOps on Gnosis | RESOLVED | ops-1 (#283, closed 2026-06-05) moved free to Gnosis (chain 100), so free batches via the same `executeBatch` path as pro; the old Base Sepolia gas-estimation blocker is gone. **Independently E2E-verified 2026-07-04 on staging** (see "Hermes no client batching" row above for the full evidence trail: tier=free, chain_id=100, 5-fact batch, 1 UserOp, tx `0xca441bc4...`, ~21s indexing latency). |
| Hermes no store-time dedup | RESOLVED | Generic agent layer now performs cosine-based near-duplicate detection (>= 0.85) before storing. |
| Hermes heuristic extraction only | LOW | Generic agent layer tries LLM extraction first, falls back to heuristic regex. LLM path requires compatible provider (OpenAI-compatible endpoint). |
| ZeroClaw no import/migrate | LOW | Rust crate implements core Memory trait + status + export + upgrade (Stripe checkout) but not import adapters or migrate tool. |
| ZeroClaw no client batching | RESOLVED | Rust crate supports executeBatch() multi-call UserOps (up to 15 facts per batch). |
| ZeroClaw UserOp submission | RESOLVED | Native Rust ERC-4337 v0.7 UserOp construction via alloy-primitives/alloy-sol-types. Hash + signing verified byte-for-byte against viem. |
| ZeroClaw client-consistency | RESOLVED | Rust crate now fully compliant: client ID header, billing cache (2h TTL), quota warnings, 403 handling, dynamic candidate pool, store-time cosine dedup (0.85), hot cache (30 entries), importance normalization, auto-recall top_k=8, broadened search fallback, chain ID auto-detect from billing. 24 spec compliance tests + 2 E2E tests against staging. |
| Hermes chain ID default | RESOLVED | Hermes auto-detects chain from the relay billing response (no hardcoded chain). Post-ops-1 (#283, closed 2026-06-05) the relay routes both tiers to Gnosis (chain 100) — single-chain. |
| OpenClaw plugin onboarding CLI dead rc.12→rc.20 | HIGH | `openclaw totalreclaw <sub>` throws `ReferenceError: tr is not defined` (3.3.13 import/upgrade `registerCli` wiring references an undeclared `tr`) — every published RC since the native rewrite affected; combined with the deliberate retirement of auto-pair-on-load, fresh users have NO onboarding path. Found by rc.20 auto-QA (internal#402/#434). **Fix in flight for 3.3.12-rc.21** (`fix/cli-tr-referenceerror` + openclaw-setup guide correction). |
| MCP server free-tier chain mapping | RESOLVED | #439 fixed. The MCP now consumes billing `chain_id` + `data_edge_address` verbatim (`src/subgraph/chain-config.ts` `resolveChainConfig`) and threads both into all 5 managed-write `getSubgraphConfig` sites via `stateWriteOverrides` (`buildSubgraphOverrides`). Pre-fix, EVERY managed write (free AND pro) fell to the `getSubgraphConfig` default 84532 + PROD DataEdge `0xC445…` — the pro `chainId=100` flip was a dead write, never threaded. Store default flipped 84532→100. Env `TOTALRECLAW_DATA_EDGE_ADDRESS` still wins over billing. Sibling of plugin #402 (chain_id) + #460 (data_edge_address). **Staging write→read E2E PASSED 2026-07-09** (fresh SA `0x02727cea…`: 3 facts in 1 UserOp `0xf51b429d…`, zero AA10/AA25; on-chain receipt shows the write hit the STAGING DataEdge `0xE7a4D2…` NOT prod `0xC445`; staging subgraph indexed all 3 — verdict on PR #487). MCP server clear to republish. |
| OpenClaw 2026.6.11 exec moved to paired node hosts | MEDIUM | Agent-driven `openclaw plugins install` via chat requires an exec-capable surface; on 2026.6.11+ the agent has no host exec by default (works on ≤2026.6.8 gateway-exec setups). Affects install-via-chat guidance; noted in openclaw-setup guide (rc.21). |
| Plugin reinstall over stale config lands disabled (memory slot) | LOW (mitigated via docs) | Since rc.20 (#402) the plugin no longer writes `plugins.slots.memory` — OpenClaw's native install slot-selection owns it. On a **reinstall over leftover config** (stale `plugins.allow`/`bundledDiscovery`/`entries` from a prior install), that native slot-selection can silently skip, leaving the plugin installed but `Status: disabled` with the slot stuck at `memory-core` → `register()` never runs → memory tools + pair HTTP routes never register → pairing 502. Also `openclaw plugins uninstall` leaves the package dir in `~/.openclaw/npm/projects/<hash>/`, blocking a fresh install (`already exists`); `--force` is the WRONG fix (writes a broken `version=None` record). **Mitigation (docs):** the install flow now always runs `openclaw plugins enable totalreclaw` (idempotent slot-bind), and the reinstall recovery is uninstall + `rm -rf ~/.openclaw/npm/projects/*totalreclaw-totalreclaw*` + reinstall + enable (never `--force`). Documented in `SKILL.md` + `docs/guides/openclaw-setup.md` + `-quickstart.md`. A genuinely-fresh install (no leftover config) is unaffected — native slot-selection binds on its own. |
| Hermes large-vault export writes a plaintext file | By design (0600) | `totalreclaw_export` on vaults whose serialized facts exceed 20KB writes the full decrypted dump to `<state_dir>/exports/totalreclaw-export-<UTC>.json` (dir 0700, file 0600 — sensitive-file convention) and returns the path + a data-derived summary instead of inlining, fixing the internal#468 truncation/provenance-confabulation. The file is intentionally plaintext (same portability contract as SPA/MCP export); users should store or delete it deliberately. |
| Debrief bypasses store-time dedup | LOW | MCP, NanoClaw, Hermes call `client.remember()` directly for debrief items (no cosine dedup). Only OpenClaw routes through `storeExtractedFacts()`. LLM-level dedup via prompt + server-side content fingerprint mitigate. |
| Hermes debrief stores without embedding | LOW | `hooks.py` stores debrief items without embedding param — no LSH bucket hashes, search relies on word-level blind indices only. |
| NanoClaw debrief no 8-message guard | LOW | `pre-compact.ts` triggers debrief based on extraction results, not conversation length. LLM prompt handles it, but no code-level guard like other clients. |
| KG features MCP/NanoClaw-only dedup | LOW | MCP and NanoClaw have core-backed store-time dedup but not contradiction detection or pin semantics (no auto-extraction pipeline to wire them into). OpenClaw, Hermes, and ZeroClaw have full KG wiring. |
| Lexical importance bump not in MCP/NanoClaw | LOW | `computeLexicalImportanceBump` only runs in OpenClaw plugin and Python Hermes. MCP and NanoClaw don't have auto-extraction, so this is by-design for now. |
| Per-chunk import "0 facts" diagnostics (Hermes-only) | LOW | Python `import_engine` now classifies why each chunk produced 0 facts (`extractor_empty` / `filtered_importance` / `filtered_text` / `filtered`), surfaces a per-chunk `chunk_diagnostics` list on `BatchImportResult`, and summarises reason counts in the `errors` line (issue #389 follow-up, shipped via the `asdict(result)` tool output). TS plugin (`handleBatchImport`) and MCP import paths still emit the generic message — port if those surfaces need per-chunk diagnostics. |
| Agent-instance provenance (`agent_name`, #317) capture Hermes-only | LOW | **Write + SPA display shipped.** Data layer: Python (`TOTALRECLAW_AGENT_NAME` → encrypted blob → recall/export surface + "John (Hermes)" label). Field is a plain metadata key on the inner blob (Python re-attach after core validation, mirroring `volatility`; no core/protobuf/subgraph change). **SPA display shipped** — `app/` decrypts the top-level `agent_name` key and renders "via John (Hermes)" on each memory (ClaimCard) + a per-session agent breakdown (SidePanel); `app/src/lib/provenance.ts` `composeProvenanceLabel` mirrors the Python helper. Remaining gap is **capture**: OpenClaw/MCP/ZeroClaw would decode+surface it for free (they read the same blob) but don't yet write a name (parked per reorg roadmap); and Hermes capture is env-only — the Hermes `ctx` gives the plugin no agent-instance name, so host-readable capture is deliberately deferred (product call). |
| ZeroClaw no type in recall | RESOLVED | ZeroClaw now parses category from decrypted envelope and surfaces it in recall results. |
| skill/plugin index.ts session-state coupling | LOW | index.ts (carved 5,507 → ~4,250 lines; import subsystem, runtime types, format helpers, config schema extracted to domain modules) still holds ~2k lines of pipeline + the plugin object, both coupled to ~28 module-level mutable singletons (authKeyHex, encryptionKey, dedupKey, userId, subgraphOwner, apiClient, lshHasher, pluginHotCache, …; ~355 reference sites). Further carving requires a shared session-state module via AST rename (ts-morph — regex is unsafe: property-key collisions like `authKeyHex: authKeyHex!`), gated on integration-level verification since many state paths are not unit-covered. |
| v1 taxonomy adoption | RESOLVED (2026-04-18) | All 5 clients ship v1 (core 2.0.0, plugin 3.0.0, mcp-server 3.0.0, nanoclaw 3.0.0, python 2.0.0, totalreclaw-memory 2.0.0). No env-var gating. Legacy v0 writes are no longer possible. |
| v1 VPS QA | PENDING | End-to-end validation on VPS per `totalreclaw-internal/docs/plans/2026-04-18-v1-vps-qa-plan.md`. 8 scenarios + cross-client interop + Bangkok recall test. Gates production promotion. |
| Cross-client parity tests | PENDING | `tests/parity/` cross-language v1 round-trip tests pass in isolation; `totalreclaw-internal/e2e/cross-client/` multi-client parity bed still queued. |

---

## Implementation Rules

### Architectural Principle: shared core first, client-native only when justified

**Default: any KG / memory feature that's pure computation belongs in `rust/totalreclaw-core/`** and is exposed to clients via WASM bindings (TS clients) and PyO3 bindings (Python clients). Client packages (skill/plugin, mcp, python/totalreclaw, rust/totalreclaw-memory) should be **thin adapters** that wire the core logic into their respective framework's lifecycle hooks, tool schemas, and storage.

**The test for "shared core vs client-native":** *Would this same logic, given the same inputs, produce the same outputs regardless of which client is calling it?* Yes → core. No (depends on client framework state, hook shape, or schema format) → adapter.

**What belongs in the shared Rust core:**
- Canonical Claim construction (`buildCanonicalClaim`, type → category mapping, `VALID_MEMORY_TYPES` enum)
- Phase 2 contradiction detection orchestration (`detectAndResolveContradictions`)
- Store-time dedup logic (`findNearDuplicate`, `shouldSupersede`, threshold helpers)
- Digest compilation pipeline
- Pin status semantics (`isPinnedClaim`, supersede guards)
- `EXTRACTION_SYSTEM_PROMPT` and any prompt templates
- Importance scoring helpers (rubric thresholds, lexical bumps, comparative re-scoring formula)
- Weight tuning loop math
- Decision log / feedback log row schemas

**What legitimately stays client-native (adapter layer):**
- Lifecycle hook wiring (each framework has its own hook shape — `agent_end` in OpenClaw, `post_llm_call` in Hermes, etc.)
- Tool schema definitions (MCP JSON Schema vs OpenAI function-calling vs OpenClaw's own format)
- File-system layout for per-client state (`~/.openclaw/` vs `~/.hermes/`)
- Logger interfaces (each framework has its own logger contract)
- Any code that reads/writes live conversation state via client APIs

**Why this matters:** the alternative (duplicating computation logic across TS plugin / TS MCP / Python / Rust) creates silent feature gaps. As of Phase 2.2.5, Phase 2 contradiction detection lives in OpenClaw plugin TypeScript only — MCP, Hermes, and ZeroClaw write facts to the same vault but skip the entire contradiction-detection pipeline. A user pinning a fact from MCP cannot trust that the OpenClaw plugin auto-extraction won't supersede it (because plugin runs Phase 2 + sees the pin) AND cannot trust that an MCP write won't trample a plugin-pinned fact (because MCP doesn't run Phase 2 at all). This kind of cross-client inconsistency is invisible to unit tests and only surfaces as user-visible weirdness in production.

**The "move to core" backlog** lives at `totalreclaw-internal/docs/plans/core-hoist-backlog.md` and tracks every piece of computation that should be in core but isn't yet. Every new feature should default to shipping in core; any deviation must be justified in writing against the test above.

**For new features**: start the implementation in `rust/totalreclaw-core/`, then write the WASM binding, then the PyO3 binding, then the thin client adapters last. Resist the temptation to "prototype in TS first and hoist later" — every "later" hoist costs ~3-5 days vs ~1 day if you start in core.

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
| Per-fact timestamps | RESOLVED | Subgraph decodes protobuf field 2 (client-generated ISO 8601) into `createdAt: BigInt!` (Unix seconds). Batched facts now retain individual timestamps. `timestamp` (block time) kept for on-chain confirmation time. |
| LSH parameters | RESOLVED | 32-bit x 20 tables, 98.1% Recall@8 on real data |
| Authentication | RESOLVED | HKDF auth with SHA-256 key hashing |
| Embedding model | RESOLVED | Locked to onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4, pre-pooled). `TOTALRECLAW_EMBEDDING_MODEL` env var was removed in the v1 env cleanup — switching models at runtime breaks search across existing vaults. |
| Client batching (A2) | RESOLVED | Implemented in client/src/userop/batcher.ts -- batch multiple facts per UserOp |
| Candidate pool sizing | RESOLVED | Server-configurable via relay billing endpoint (`max_candidate_pool` in FeatureFlags). Env overrides: `CANDIDATE_POOL_MAX_FREE`, `CANDIDATE_POOL_MAX_PRO`. |
| Load testing | RESOLVED | Managed service load test at `totalreclaw-internal/e2e/load-test-managed/`. Client-side <140ms p95 PASS up to 10K facts. |
| Stripe-driven tiers | PLANNED | Stripe as source of truth for pricing/limits. Plan at `totalreclaw-internal/plans/2026-03-26-stripe-driven-tiers.md` |
| LLM memory import (ChatGPT/Claude/Gemini) | PLANNED | Adapters for importing memory from major LLM providers |
| Conflict resolution (Layers 3-4) | MEDIUM | Spec'd in v0.3.2, not implemented |
| Single-chain (Gnosis-only) policy | RESOLVED | **Shipped — ops-1 (`totalreclaw-internal#283`, CLOSED 2026-06-05).** Both tiers route to Gnosis mainnet (chain 100); the legacy Free → Base Sepolia (84532) routing is retired. This closes the data-loss-on-upgrade risk: a Stripe free→pro upgrade is now a pure `tier='pro'` flag flip with zero data migration, since the free vault already lives on Gnosis. |
| Staging on-chain isolation | RESOLVED | **Shipped.** Staging runs an isolated DataEdge (`0xE7a4D2677B686e13775Ba9092631089e35F0BB91`) + dedicated `total-reclaw-gnosis-staging` subgraph, so staging writes never touch production data. See `totalreclaw-internal/docs/specs/ops/staging-chain-isolation.md`. |
| `/v1/tiers` advertise/route mismatch | RESOLVED | Post-ops-1, free routes to Gnosis (chain 100), matching the `features.chain="gnosis"` the tiers endpoint advertises — advertise and route now agree. |
| Startup validation | MEDIUM | Validate Pimlico/Stripe/Subgraph reachability on relay boot |
| DB backup monitoring | LOW | Add alerting (Slack/email) if daily R2 backup fails |
| Graceful shutdown | LOW | Not yet configured in uvicorn |

---

## Key Constraints

- **Search latency**: <140ms p95 for 1M memories
- **Recall**: >=93% of true top-250
- **Storage overhead**: <=2.2x vs plaintext
- **Server-blind**: Server NEVER sees plaintext
- **Embedding model**: onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4, pre-pooled)
- **Extraction cap**: Max 15 facts per extraction cycle, unified 3-turn interval
- **Memory types**: 6 v1 types -- claim, preference, directive, commitment, episode, summary (closed enum, per `docs/specs/totalreclaw/memory-taxonomy-v1.md`). v0 tokens (fact, context, decision, episodic, goal, rule) are read-only for pre-v1 vault entries and normalized to v1 on recall.

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
5. **After every subgraph deploy, immediately update the relay's `SUBGRAPH_ENDPOINT` env var on Railway.** The relay queries the subgraph via this URL — a stale version means the relay reads from an outdated schema. Update both services:
   - **Staging**: `railway variables set "SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis/<new-version>" -s totalreclaw`
   - **Production** (production runs TWO endpoint vars — free `SUBGRAPH_ENDPOINT` + `PRO_SUBGRAPH_ENDPOINT` — update BOTH): `railway variables --set "SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis/<new-version>" --set "PRO_SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis/<new-version>" -s totalreclaw-production`
   Setting env vars triggers an automatic Railway redeploy. Verify the relay restarts cleanly by checking `/health`. NOTE: `railway variables --set` may time out on the response yet still apply — re-read with `railway variables -s <svc> --json` to confirm before retrying. Also: deploy the prod subgraph from `subgraph/subgraph-gnosis-mainnet.yaml` (the default `subgraph.yaml` is a stale base-sepolia manifest — a bare `graph deploy` targets the wrong chain).

6. **After every release event, update the release pipeline tracker (MANDATORY).** The single source of truth for "what's live / next RC / what's been QA'd" is **`totalreclaw-internal/docs/release-pipeline.md`**. The session that runs the event updates it **the same session** — never defer to a follow-up:
   - **RC publish** (workflow success) → update Latest RC + Status + history.
   - **Stable promote** (you dispatched `release-type=stable`) → swap the new version into Production, set Status `promoted`, append history.
   - **Prod infra change** (subgraph redeploy, relay endpoint repoint, chain flip) → note it under the affected integration.
   - **QA verdict** (GO / NO-GO) → update Status + blocker.

   This rule is canonical in `totalreclaw-internal/CLAUDE.md` (§"Release pipeline tracker") but is repeated HERE because release events are dispatched from THIS (public) repo — an agent operating only in the public repo otherwise never loads the rule and the tracker silently rots. (Added 2026-06-06 after a 2.4.4 stable promote shipped without a tracker update; the tracker had gone a month stale.)

For deployment procedures, follow the canonical runbook **[`docs/guides/deployment.md`](docs/guides/deployment.md)** (version-controlled source of truth — verified service↔env↔domain map, TS-relay `/health` contract, deploy-SHA sentinel, SHA hard-gate, subgraph + rollback). The machine-local `deploy-totalreclaw` skill mirrors it; if they disagree, the doc wins.

### Relay Environments

| Environment | Railway Service | URL | Deploys |
|-------------|----------------|-----|---------|
| **Staging** | `totalreclaw` | `https://api-staging.totalreclaw.xyz` | Auto on push to `main` |
| **Production** | `totalreclaw-production` | `https://api.totalreclaw.xyz` | Manual via `railway up -s totalreclaw-production -d` |

**IMPORTANT: All tests (E2E, integration, smoke, cross-client) MUST hit the staging relay (`api-staging.totalreclaw.xyz`), NEVER production.** Staging runs single-chain Gnosis (chain 100) for both tiers — same routing as production post-ops-1 (#283, closed 2026-06-05). Staging is on-chain **isolated**: it writes to its own DataEdge (`0xE7a4D2677B686e13775Ba9092631089e35F0BB91`) + dedicated `total-reclaw-gnosis-staging` subgraph, so staging E2E txs never touch production data. Test registrations send `X-TotalReclaw-Test: true` header and show as `[TEST]` in Telegram notifications.

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

### Vault SPA (`app/`)

End-user web UI for the managed-mode vault. Recovery-phrase → derive EOA + Smart Account address → read encrypted facts from the subgraph through the relay → decrypt client-side. Phase 1 (reads) shipped 2026-05-17 in [#236](https://github.com/p-diogo/totalreclaw/pull/236); writes (delete/pin/retype via ERC-4337 UserOps) are stubbed pending Phase 2.

- **Stack**: Vite + React 18 + TS strict, `@tanstack/react-query`, `@noble/curves`/`@noble/hashes`/`@scure/bip32`/`@scure/bip39` for crypto (no viem in bundle).
- **Relay endpoints used**: `GET /v1/smart-account?eoa=&chain=` (deterministic Smart Account derivation via `SimpleAccountFactory.getAddress`), `POST /v1/register` (idempotent), `GET /v1/billing/status?wallet_address=`, `POST /v1/subgraph` (GraphQL — `facts(where: { owner, isActive: true })` paginated).
- **Hosting**: Cloudflare Pages project `totalreclaw-app`. `main` → prod alias (`app.totalreclaw.xyz` once attached); branches → `<branch>.totalreclaw-app.pages.dev`. CI in `.github/workflows/deploy-app.yml` (test-gated + path-scoped). Build env: `VITE_SERVER_URL=https://api-staging.totalreclaw.xyz` for previews, `https://api.totalreclaw.xyz` for prod.
- **Relay CORS**: `CORS_ORIGINS` env var supports leftmost-subdomain wildcards. Current set covers `https://totalreclaw.xyz`, `https://app.totalreclaw.xyz`, `https://totalreclaw-app.pages.dev`, `https://*.totalreclaw-app.pages.dev`; staging also allows `http://localhost:5173` and `http://127.0.0.1:5173` for dev.

### QA Autopilot (vault SPA)

Autonomous regression harness for the vault SPA. Driver lives in `tools/qa-vault.mjs` (Playwright, reads phrase from macOS keychain locally or `QA_RECOVERY_PHRASE` env in CI, redacts any phrase fragment from console/network output, exits non-zero on regression).

- **Trigger 1 — preview deploy**: public-repo `deploy-app.yml` fires `gh workflow run qa-autopilot.yml` against `p-diogo/totalreclaw-internal` after every Pages preview, passing the preview URL + PR number. Gated by the `INTERNAL_DISPATCH_PAT` secret.
- **Trigger 2 — daily cron**: 07:30 UTC against production (`https://app.totalreclaw.xyz`).
- **Trigger 3 — manual**: `gh workflow run qa-autopilot.yml -R p-diogo/totalreclaw-internal -f target_url=<url>`.
- **On regression**: internal workflow opens an issue on the public repo tagged `qa-autopilot` with the run URL, redacted summary, and a 14-day artifact retaining the full JSON report + screenshot. Phrase + screenshots stay on the private side; only sanitized summary leaks across the wall.
- **Required secrets**: `QA_RECOVERY_PHRASE` + `PUBLIC_ISSUE_PAT` on `totalreclaw-internal`; `INTERNAL_DISPATCH_PAT` on `totalreclaw`. All three are fine-grained PATs scoped to a single repo with minimum permission (`issues:write` and `actions:write` respectively).
- **Docs**: see `docs/guides/qa-autopilot.md`.

---

## Current Status

- **Version**: [v1.0.0](https://github.com/p-diogo/totalreclaw/releases/tag/release-v1.0.0) (tagged 2026-04-18) is the last GitHub release-tag milestone (Memory Taxonomy v1 + Retrieval v2 Tier 1). The `v1.0.0` tag is a milestone, not a per-package version — each package's current version advances per-release via CI (see "Packages published" below).
- **Phase**: Private Beta; v1 is the default extraction + write path across every client with zero env-var toggles
- **Packages published (current stables)**: `@totalreclaw/totalreclaw` (OpenClaw plugin) **3.3.13** (npm + ClawHub), `@totalreclaw/mcp-server` **3.4.0** (npm), `@totalreclaw/core` **2.5.6** (npm) / `totalreclaw-core` **2.5.5** (PyPI + crates.io), `totalreclaw` (Python) **2.4.6** (PyPI), `@totalreclaw/skill-nanoclaw` **3.0.0** (npm), `@totalreclaw/client` **1.3.0** (npm), `totalreclaw-memory` **2.0.1** (crates.io).
- **Default mode**: Managed Service. **Chain routing is single-chain Gnosis (ops-1 shipped, `totalreclaw-internal#283` closed 2026-06-05):** both Free and Pro route to Gnosis mainnet (chain 100). The legacy Free → Base Sepolia (84532) routing is retired.
- **Chain ID**: client `TOTALRECLAW_CHAIN_ID` env var removed in v1 — the client auto-detects its chain from the relay billing response (the relay decides per tier). So the client is chain-agnostic; the relay's `PIMLICO_CHAIN_ID` / `PRO_PIMLICO_CHAIN_ID` env vars are the source of truth.
- **Embedding model**: onnx-community/harrier-oss-v1-270m-ONNX (640d, ~344MB, q4, pre-pooled). Only supported model in v1; `TOTALRECLAW_EMBEDDING_MODEL` env var removed.
- **Memory taxonomy**: v1 (6 types: claim / preference / directive / commitment / episode / summary + 3 axes: source / scope / volatility). See `docs/specs/totalreclaw/memory-taxonomy-v1.md`.
- **Outer protobuf**: v4 (inner blob now v1 JSON; subgraph schema unchanged). See `totalreclaw-internal/docs/plans/2026-04-18-protobuf-v4-design.md`.
- **Crypto core**: `@totalreclaw/core@2.0.0` (Rust WASM for npm, PyO3 for PyPI) — adds `MemoryClaimV1` types, `validateMemoryClaimV1`, `rerankWithConfig` (Tier 1 source-weighted reranker), `parseMemoryTypeV1` / `parseMemorySource`. 455 native + 498 WASM + 508 PyO3 tests pass. Legacy v0 `rerank()` preserved for back-compat.
- **OpenClaw integration**: Plugin installs without force flags (`openclaw plugins install`), hot-reload setup via `TOTALRECLAW_HOT_RELOAD=true`, auto-recall on `before_agent_start`, auto-extraction on `agent_end`, LLM config sourced from OpenClaw providers, plaintext fallback prevention
- **Relay**: Billing, Pimlico sponsorship, Gnosis-mainnet routing, and query proxying extracted to private `totalreclaw-relay` TypeScript repo (p-diogo/totalreclaw-relay). Public server retains only self-hosted functionality (storage, search, auth).
- **Server-side tuning**: Relay billing response carries `extraction_interval`, `max_facts_per_extraction`, `max_candidate_pool`, and (planned) `ephemeral_ttl_days`. Clients read these from the billing cache — no npm/PyPI publish required to retune.
- **Staging**: Gnosis mainnet (chain 100) — staging relay endpoint, Pimlico-sponsored gas
- **Production**: Gnosis mainnet (chain 100) — production relay endpoint, Pimlico-sponsored gas
- **All releases via CI**: GitHub Actions workflows for npm, PyPI, and ClawHub. Never publish manually.
