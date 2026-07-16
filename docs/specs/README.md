# Specs — status index

Status of every spec under `docs/specs/`, so visitors can tell what is current
versus historical. Statuses are taken from each file's own header/banner.

**Legend:** **Shipped** = live in a released client · **Current** = canonical
living reference · **Design only** = spec'd, not implemented · **Analysis** =
comparison / exploration that informed a decision · **Historical** = predates
the 2026-06 single-chain migration (carries a ⚠️ banner) — read for context
only, see [`../architecture.md`](../architecture.md) for the live picture.

## totalreclaw/

| File | Description | Status |
|------|-------------|--------|
| [architecture.md](totalreclaw/architecture.md) | System architecture v1.0 — identity, crypto, storage, retrieval, multi-client, on-chain layer | Current |
| [memory-taxonomy-v1.md](totalreclaw/memory-taxonomy-v1.md) | v1 taxonomy: 6 speech-act types + source / scope / volatility axes | Shipped (2026-04-18) |
| [retrieval-v2.md](totalreclaw/retrieval-v2.md) | Source / scope / volatility-aware ranking | Shipped — Tier 1 (Tiers 2–4 designed) |
| [tiered-retrieval.md](totalreclaw/tiered-retrieval.md) | Tiered, source-aware retrieval | Shipped — Tier 1 (Tiers 2–4 designed) |
| [client-consistency.md](totalreclaw/client-consistency.md) | Canonical parameters all 5 clients must follow | Current — reference |
| [crypto-modules.md](totalreclaw/crypto-modules.md) | Why crypto primitives are duplicated across 3 packages + parity checks | Current — reference |
| [benchmark.md](totalreclaw/benchmark.md) | Benchmark Harness (OMBH) v1.1 — retrieval-quality harness | Shipped |
| [benchmark-v2-improvements.md](totalreclaw/benchmark-v2-improvements.md) | v2 benchmark learnings / improvement plan | Draft — not implemented |
| [conflict-resolution.md](totalreclaw/conflict-resolution.md) | Multi-agent conflict resolution v0.3.2 | Design only, not implemented |
| [mcp-auto-memory.md](totalreclaw/mcp-auto-memory.md) | Auto-memory for generic MCP hosts | Design complete, not implemented |
| [mcp-onboarding.md](totalreclaw/mcp-onboarding.md) | NanoClaw onboarding & payment UX | Design complete |
| [mcp-server.md](totalreclaw/mcp-server.md) | MCP server spec v1.0 | Shipped |
| [skill-openclaw.md](totalreclaw/skill-openclaw.md) | OpenClaw skill spec | Shipped (spec predates implementation) |
| [skill-nanoclaw.md](totalreclaw/skill-nanoclaw.md) | NanoClaw skill spec | Shipped (spec predates implementation) |
| [recrystallize-backfill.md](totalreclaw/recrystallize-backfill.md) | Re-crystallize / re-key backfill for collapsed sessions | Implemented — not yet run on a user vault |
| [lsh-tuning.md](totalreclaw/lsh-tuning.md) | LSH parameter tuning for multi-tenant SaaS | Analysis (validated; informs shipped config) |
| [retrieval-improvements-v3.md](totalreclaw/retrieval-improvements-v3.md) | Retrieval improvements v3 — comprehensive plan | Historical — superseded by retrieval-v2.md |
| [greenfield-architecture.md](totalreclaw/greenfield-architecture.md) | "If we rebuilt today" assessment | ⚠️ Historical |
| [server.md](totalreclaw/server.md) | Relay architecture v1.0 | ⚠️ Historical (relay now in a separate repo) |

Sequence-diagram flows (identity setup, write/read paths, cross-agent hooks,
knowledge graph, wiki bridge, storage modes) live in
[totalreclaw/flows/](totalreclaw/flows/).

## subgraph/

| File | Description | Status |
|------|-------------|--------|
| [seed-to-subgraph.md](subgraph/seed-to-subgraph.md) | Decentralized E2EE + account abstraction + gasless on-chain memory | Shipped (Gnosis mainnet) |
| [billing-and-onboarding.md](subgraph/billing-and-onboarding.md) | Billing & onboarding — Stripe, chain selection, paymaster auth | Shipped (Stripe) |
| [paymaster-comparison.md](subgraph/paymaster-comparison.md) | Pimlico vs ZeroDev paymaster comparison | Analysis — Pimlico chosen |

## tee/

| File | Description | Status |
|------|-------------|--------|
| [architecture.md](tee/architecture.md) | TEE vs E2EE comparison | Analysis |
| [grok-tee-notes.md](tee/grok-tee-notes.md) | Exploratory TEE edition notes | Analysis |
| [tdx-saas.md](tee/tdx-saas.md) | TDX SaaS MVP spec v0.4 | Design only, not started |
