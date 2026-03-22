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
|  Decrypt candidates -> Qwen3 1024d embeds -> BM25+Cosine+RRF -> Top 8 |
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
| Billing & Onboarding | `billing-and-onboarding.md` | Implemented (Stripe + Coinbase) |
| Paymaster Comparison | `paymaster-comparison.md` | Complete (Pimlico chosen) |

### TEE (Trusted Execution) -- `docs/specs/tee/`
| Spec | File | Status |
|------|------|--------|
| TEE vs E2EE Comparison | `architecture.md` | Analysis complete |
| TDX SaaS v0.4 | `tdx-saas.md` | Spec complete, not started |

---

## Feature Compatibility Matrix

### Platform Support

Features across OpenClaw plugin (`skill/plugin/`), MCP server (`mcp/`), and NanoClaw (`skill-nanoclaw/`).

| Feature | OpenClaw Plugin | MCP Server | NanoClaw | Notes |
|---------|:-:|:-:|:-:|-------|
| **Core Tools** | | | | |
| `totalreclaw_remember` | Yes | Yes | Yes (via MCP) | |
| `totalreclaw_recall` | Yes | Yes | Yes (via MCP) | |
| `totalreclaw_forget` | Yes | Yes | Yes (via MCP) | |
| `totalreclaw_export` | Yes | Yes | Yes (via MCP) | |
| `totalreclaw_status` | Yes | Yes | Yes (via MCP) | |
| `totalreclaw_import_from` | Yes | Yes | Yes (via MCP) | Mem0 + MCP Memory adapters |
| `totalreclaw_import` | -- | Yes | Yes (via MCP) | JSON/Markdown re-import (MCP only) |
| `totalreclaw_upgrade` | -- | Yes | Yes (via MCP) | Stripe/Coinbase checkout URL |
| `totalreclaw_consolidate` | Yes | Yes | Yes (via MCP) | Self-hosted only (no batch delete on managed service) |
| **Automatic Memory** | | | | |
| Auto-search (before_agent_start) | Yes | -- | Yes (hook) | MCP has no lifecycle hooks |
| Auto-extract (agent_end) | Yes | -- | Yes (hook) | MCP relies on host agent |
| Pre-compaction flush | Yes | -- | Yes (hook) | |
| Pre-reset flush | Yes | -- | -- | OpenClaw only |
| CLAUDE.md sync | -- | -- | Yes | NanoClaw syncs high-importance facts |
| **Dedup** | | | | |
| Content fingerprint (exact) | Yes | Yes | Yes | Server-side HMAC-SHA256 |
| Within-batch semantic dedup | Yes | -- | -- | Cosine >= 0.9, during extraction |
| Store-time near-duplicate | Yes | Yes | Yes (via MCP) | `consolidation.ts` — both plugin and MCP |
| LLM-guided dedup (ADD/UPDATE/DELETE) | Yes (Pro) | -- | Yes | OpenClaw + NanoClaw extraction prompts |
| Bulk consolidation tool | Yes | Yes | Yes (via MCP) | Self-hosted only (no batch delete on managed service) |
| **Pro Tier Gating** | | | | |
| Feature gating via billing cache | Yes | -- | -- | Server returns `features` dict, plugin gates client-side |
| Unified extraction interval (3 turns) | Yes | -- | Yes (env) | Same for all tiers (quota is per-tx). Configurable via env var |
| 15-fact extraction cap | Yes | -- | Yes | Max 15 facts per extraction cycle |
| Dual-chain routing | -- | -- | -- | Relay-side: routes to Base Sepolia (free) or Gnosis mainnet (pro) |
| **Billing** | | | | |
| Quota warnings (>80%) | Yes | -- | -- | Injected via before_agent_start |
| 403 handling + cache invalidation | Yes | Yes | Yes | |
| **Search Optimizations** | | | | |
| Hot cache + two-tier search | Yes (managed) | -- | -- | Skips remote query if cached query similar |
| Dynamic candidate pool sizing | Yes | Yes | Yes (via MCP) | 400-5000 based on vault size |
| BM25 + Cosine + RRF reranking | Yes | Yes | Yes (via MCP) | Intent-weighted |
| **Admin & Analytics** | | | | |
| X-TotalReclaw-Client header | Yes | Yes | Yes (via MCP) | Sent on every relay request |
| Admin dashboard | -- | -- | -- | Admin-only (relay service), not a client feature |

### Storage Mode Support

Features across Self-Hosted (PostgreSQL) and Managed Service (default, dual-chain via The Graph).

Managed Service two-tier chain model: **Free** = Base Sepolia testnet (500 memories/month), **Pro** = Gnosis mainnet ($5/month, unlimited, permanent storage).

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

### Known Gaps

| Gap | Severity | Description |
|-----|----------|-------------|
| Bulk consolidation not on managed service | LOW | Bulk consolidation tool requires batch-delete, which has no on-chain equivalent. Store-time dedup supersession now works via tombstones. |
| MCP no auto-memory | By design | MCP has no lifecycle hooks. Host agent (Claude, Cursor) must call tools explicitly. Documented in beta guide. |
| Export/import not on managed service (MCP) | LOW | MCP server's export and import tools are self-hosted only. OpenClaw plugin handles both modes. |
| Crypto payments blocked | MEDIUM | Coinbase Commerce sunset March 31, 2026. Coinbase Business US/Singapore only. Business entity in Portugal. Stripe (fiat) works. |

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

---

## Known Technical Gaps

| Gap | Severity | Status |
|-----|----------|--------|
| LSH parameters | RESOLVED | 32-bit x 20 tables, 98.1% Recall@8 on real data |
| Authentication | RESOLVED | HKDF auth with SHA-256 key hashing |
| Embedding model | RESOLVED | Migrated to Qwen3-Embedding-0.6B (1024d, multilingual) |
| Conflict resolution (Layers 3-4) | MEDIUM | Spec'd in v0.3.2, not implemented |
| Client batching (A2) | MEDIUM | Designed, not implemented -- batch multiple facts per UserOp |
| Migration tool (testnet to mainnet) | MEDIUM | Designed, not implemented -- re-encrypt + re-store on upgrade |
| Load testing | MEDIUM | Not done -- need to validate <140ms p95 |
| Graceful shutdown | LOW | Not yet configured in uvicorn |

---

## Key Constraints

- **Search latency**: <140ms p95 for 1M memories
- **Recall**: >=93% of true top-250
- **Storage overhead**: <=2.2x vs plaintext
- **Server-blind**: Server NEVER sees plaintext
- **Embedding model**: Qwen3-Embedding-0.6B (1024d, 100+ languages, last-token pooling)
- **Extraction cap**: Max 15 facts per extraction cycle, unified 3-turn interval

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
```

---

## Current Status

- **Version**: v1.0-beta
- **Phase**: Private Beta
- **Default mode**: Managed Service with dual-chain (free=Base Sepolia testnet, pro=Gnosis mainnet)
- **Embedding model**: Qwen3-Embedding-0.6B (1024d, multilingual, last-token pooling)
- **Relay**: Billing, Pimlico sponsorship, dual-chain routing, and query proxying extracted to private `totalreclaw-relay` TypeScript repo (p-diogo/totalreclaw-relay). Public server retains only self-hosted functionality (storage, search, auth).
- **Staging**: Base Sepolia (chain 84532) -- free testnet, no gas costs
- **Production**: Gnosis mainnet (chain 100) -- Pro tier only
