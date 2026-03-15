# TotalReclaw Roadmap

## Overview

TotalReclaw is a zero-knowledge encrypted memory vault for AI agents. The project progresses through four phases, each building on the last but with distinct goals:

1. **Phase 1 (PoC)** -- Validate the E2EE architecture end-to-end with local testing.
2. **Phase 2 (Free MVP)** -- Ship a production server, publish on Claw Hub, and benchmark against Mem0.
3. **Phase 3 (Subgraph)** -- Decentralize storage onto Base L2 for censorship-resistant, self-sovereign memory.
4. **Phase 4 (TEE)** -- Hardware-enforced privacy via Intel TDX / AWS Nitro as a separate product stack.
5. **Phase 5 (Import)** -- Import & migrate memories from external systems (Mem0, Zep, LLM providers, etc.).

---

## Phase 1: Proof of Concept (PoC) — COMPLETE

**Goal:** Validate the E2EE architecture end-to-end. Test locally with friends.
**Specs:** `docs/specs/totalreclaw/architecture.md` (v0.3), `docs/specs/totalreclaw/server.md` (v0.3.1b)
**Status:** All code complete. 836+ tests passing. Ready for local testing.

### What's Built

| Component | Description | Tests |
|-----------|-------------|-------|
| Server | FastAPI + PostgreSQL, HKDF auth, blind index search, content dedup, /sync endpoint | 142 |
| TypeScript client library | Argon2id KDF, AES-256-GCM, LSH buckets, BM25+cosine+RRF reranking, fingerprint dedup, sync client | 180 |
| OpenClaw skill | Lifecycle hooks, fact extraction, export, host LLM injection | 309 |
| NanoClaw skill + generic MCP server | 5 MCP tools, 3 hooks | 59 |
| Credential management | OS keychain integration, session manager | 118 |
| Benchmark harness (OMBH) | 3 backends, real WhatsApp+Slack data, HTML dashboard | -- |
| Content fingerprint dedup (v0.3.1b) | HMAC-SHA256 fingerprints, server-side dedup check | 28 |
| Sync endpoint (v0.3.1b) | Delta sync via sequence_id, client reconnection protocol | 23 |
| Protobuf schema (v0.3.1b) | content_fp, agent_id, sequence_id, SyncRequest/SyncResponse | 10 |

### Remaining for PoC (non-code)

| Task | IDs | Description |
|------|-----|-------------|
| Repo split | T073-T077 | Split monorepo into totalreclaw-poc and totalreclaw-specs (IN PROGRESS) |

---

## Phase 2: Free MVP

**Goal:** Public launch via Claw Hub and other channels. Production server online for real users.
**Specs:** `docs/specs/totalreclaw/server.md` (v0.3.1b) + production hardening

### 2.1 Server Production Readiness

#### Critical (must-fix before launch)

| Gap | Description | Current State |
|-----|-------------|---------------|
| Rate limiting | Per-user rate limiting keyed on auth_hash (1000/hr store, 1000/hr search) | **DONE** — custom middleware, Prometheus counter, logging |
| Request size limits | 50MB body limit in Caddy + Pydantic validation (2MB blob, 1000 indices, 500 facts) | **DONE** |
| Secrets management | docker-compose uses env_file, .env.example with placeholder passwords | **DONE** |
| SQL injection in GIN query | Trapdoor validation (64-char hex regex) in database.py | **DONE** |
| Audit logging | JSON audit records with correlation IDs, sensitive data filter | **DONE** |
| Database backups | pg_dump scripts + restore procedure + cron example | **DONE** (IN PROGRESS) |
| CORS | Configurable origins via Settings, localhost default | **DONE** — needs production origin configured |
| Account deletion endpoint | DELETE /account with soft-delete + 30-day purge (GDPR day-one) | **DONE** |
| Connection pool sizing | Pool=20, max_overflow=30, pool_recycle=3600, pool_pre_ping=True | **DONE** |
| Structured logging | JSON logging via python-json-logger with correlation IDs | **DONE** |

#### Infrastructure (reverse proxy + protection)

| Component | Recommendation | Purpose |
|-----------|---------------|---------|
| Reverse proxy | Caddy or NGINX | TLS termination, HTTP/2, automatic HTTPS via Let's Encrypt |
| CDN / WAF | Cloudflare (free tier) | DDoS protection, bot mitigation, rate limiting at edge, IP reputation |
| TLS certificates | Caddy auto-HTTPS or Cloudflare Origin CA | End-to-end encryption in transit |
| DNS | Cloudflare DNS | Fast resolution + proxy mode for hiding origin IP |

The FastAPI server does NOT handle TLS. A reverse proxy (Caddy recommended for simplicity, NGINX for more control) sits in front and handles TLS termination with auto-renewed certificates, request buffering and size limits, basic rate limiting (defense in depth), and proxy headers (X-Forwarded-For, X-Real-IP).

Cloudflare sits in front of the reverse proxy and provides DDoS mitigation, bot detection, WAF rules, edge rate limiting, and IP reputation filtering.

#### High Priority (before SLA commitment)

| Gap | Description | Current State |
|-----|-------------|---------------|
| Prometheus metrics | /metrics endpoint with request counters, latency histograms, error rates, pool gauges, rate limit hits | **DONE** |
| Alerting | No thresholds defined. Need alerts for: error rate >5%, p95 >500ms, pool exhaustion | NOT DONE — define after baseline established |
| Database migrations (Alembic) | alembic.ini + migrations/env.py + 001_initial_schema baseline | **DONE** — needs v0.3.1b migration for upgrades |
| API versioning | /v1/ prefix on all API routes (breaking change, done before external users exist) | **DONE** (IN PROGRESS) |
| Pagination on /export | Cursor-based pagination with limit + has_more | **DONE** (IN PROGRESS) |
| Load testing | Have not validated <140ms p95 latency under concurrent load | NOT DONE |
| Environment-specific config | dev/staging/prod via Settings.environment, CORS/debug/pool toggles | **DONE** |
| Health checks | /health (liveness) + /ready (readiness) endpoints | **DONE** |
| Error correlation IDs | X-Correlation-ID in all responses, JSON structured logs | **DONE** |
| Graceful shutdown timeout | Not yet configured in uvicorn | NOT DONE (low effort) |

#### Medium Priority (within 3 months post-launch)

| Gap | Description | Current State |
|-----|-------------|---------------|
| Temporal awareness | `totalreclaw_timeline` tool for "what changed this month?" queries. Already store timestamps but don't use them for temporal queries. | NOT DONE |
| Client-side relationship extraction | Extract entity relationships during fact extraction. Store as encrypted graph edges. Enables "what tools does Acme use?" without server-side graphs. | NOT DONE |
| Row-level security (RLS) | Multi-tenant without Postgres RLS. Defense-in-depth for data isolation | NOT DONE |
| Distributed tracing (OpenTelemetry) | Cross-request tracing for debugging | NOT DONE |
| Log aggregation | Central logging system (ELK, Datadog, etc.) | NOT DONE — JSON logs ready for ingestion |
| CI/CD pipeline | Automated testing on push/PR | **DONE** — GitHub Actions: 4 parallel jobs (server pytest, client Jest, plugin build, MCP build) |
| Tombstone cleanup job | Tombstones and raw_events accumulate indefinitely | NOT DONE |
| API documentation | Static openapi.json exported; Swagger/ReDoc in dev mode only | **PARTIAL** — needs hosting for production |

### 2.2 Mem0 Competitive Benchmark (Pre-MVP)

Before launching the MVP publicly, complete a fair competitive benchmark against Mem0. The current benchmark results are NOT representative.

**Current results (unusable):**

| System | Recall@8 | Latency | Privacy |
|--------|----------|---------|---------|
| TotalReclaw | 67.5% | 57.5ms | 100% |
| Mem0 | 0.5% (only 29/500 conversations indexed) | 469ms | 0% |

**Why current results are bad:**

1. Mem0 free tier async processing only indexed 29 out of 500 conversations within the polling window.
2. Mem0 returned 502 Bad Gateway errors during retrieval.
3. TotalReclaw used a weak free LLM (13B active params) for extraction -- production will use the host agent's LLM.
4. The comparison does not reflect either system's true capability.

**Lessons learned for the next benchmark attempt:**

1. Slow down Mem0 ingestion: 1 conversation at a time with 10-30s waits between.
2. Reduce scale to 50-100 conversations (within Mem0 free tier: 10K memories, 1K searches/month).
3. Poll much longer for Mem0 async processing (10+ minutes, not 5).
4. Use a production-quality LLM for TotalReclaw extraction (or the host agent's LLM).
5. Monitor Mem0 memory count via API to confirm all conversations were processed before running retrieval.
6. Consider running on a weekday/off-peak to avoid Mem0 platform load issues.
7. Consider self-hosting Mem0 via Docker for a controlled comparison (tests different product but eliminates platform flakiness).
8. Use OpenClaw's LLM task feature for TotalReclaw's fact extraction (leveraging the host agent's LLM, e.g., Claude or GPT-4). This matches production behavior and provides a fair comparison against Mem0's platform, which also uses a powerful LLM (gpt-4.1-nano) internally for extraction.

**What we DO know from retrieval-only benchmark:**

- TotalReclaw: 98.1% Recall@8 on 8,727 pre-indexed memories (with full E2EE privacy).
- This validates the core search architecture works well.
- The E2E extraction quality depends on the LLM used, not TotalReclaw's architecture.

### 2.3 Claw Hub Publishing

| Task | Status | Blocker |
|------|--------|---------|
| SKILL.md with Claw Hub frontmatter | Done | -- |
| README.md for public audience | Done | -- |
| skill.json with Claw Hub schema | Done | -- |
| CLAWHUB.md publishing checklist | Done | -- |
| Screenshots (3-5, 1920x1080) | Pending | Manual |
| Demo video (30-90s) | Pending | Manual |
| Make totalreclaw-poc repo public | Pending | User action |
| Submit to Claw Hub review | Blocked | Repo must be public |
| Test skill uses host agent's LLM | Pending (T088) | -- |

### 2.4 Memory Consolidation & Dedup (Pre-MVP)

Reduce fact pile-up by detecting near-duplicates and merging them client-side.

| Task | Description | Status |
|------|-------------|--------|
| Near-duplicate detection | Before storing, search for semantically similar facts; merge/replace instead of creating new | NOT DONE |
| On-demand consolidation | `totalreclaw_consolidate` tool scans all memories and merges duplicates | NOT DONE |
| Extraction-time dedup | Integrate dedup check into `agent_end` extraction pipeline | NOT DONE |

**Constraint:** All comparison/merging happens client-side (zero-knowledge). Server only has content fingerprints and blind indices.

**Plan:** `plans/2026-03-11-memory-consolidation-dedup.md` (internal repo)

### 2.5 Memory Compression Stats

Surface token reduction metrics that users and integrators can see.

| Task | Description | Status |
|------|-------------|--------|
| Track context savings | Measure tokens injected vs full conversation replay | NOT DONE |
| Surface in `totalreclaw_status` | Show compression ratio alongside tier/usage | NOT DONE |

### 2.6 Competitor Import (MVP — Mem0 + MCP Memory)

Import tools to reduce switching costs. Prioritize the two most common sources for MVP.

| Task | Description | Status |
|------|-------------|--------|
| `totalreclaw_import_from` tool | Source-agnostic import with adapter pattern | **DONE** |
| Mem0 adapter | Import from mem0.ai API export (JSON) | **DONE** — unit tests + E2E validated (100% import, 100% recall) |
| MCP Memory adapter | Import from `@modelcontextprotocol/server-memory` JSONL | **DONE** (unit tests); untested with real JSONL |
| Progress tracking | Report progress for large imports | NOT DONE |

**Constraint:** All processing client-side. Content fingerprint dedup prevents double-import.

**Plan:** `plans/2026-03-11-competitor-import.md` (internal repo)

### 2.7 Multi-Agent Conflict Resolution (`docs/specs/totalreclaw/conflict-resolution.md` v0.3.2)

Applies to both MVP (PostgreSQL) and future Subgraph path. 4-layer protocol:

| Layer | What | Effort | Priority |
|-------|------|--------|----------|
| 1: Content fingerprint | HMAC-SHA256 exact dedup (server-side) | 2h | HIGH (in v0.3.1b) |
| 2: Sync watermark | Delta sync via sequence_id | 3h | HIGH (in v0.3.1b) |
| 3: Blind index overlap | Probabilistic near-dedup without plaintext | 3h | MEDIUM |
| 4: Client LLM merge | Decrypt, compare, LLM resolves contradictions | 4h | MEDIUM |

Layers 1-2 are part of PoC (v0.3.1b). Layers 3-4 are MVP enhancements (v0.3.2).

---

## Phase 3: Subgraph (Decentralized)

**Goal:** Censorship-resistant, self-sovereign memory. User's 12-word seed is the only secret.
**Specs:** `docs/specs/subgraph/seed-to-subgraph.md`, `docs/specs/subgraph/billing-and-onboarding.md`

Architecture: User's BIP-39 seed derives both encryption key AND on-chain identity (ERC-4337 Smart Account). Writes go through a paymaster-sponsored UserOperation on **Gnosis Chain**, emitted as events by an EventfulDataEdge contract, indexed by The Graph Network.

### Go-Live Architecture (Decided 2026-03-03)

| Component | Decision | Rationale |
|-----------|----------|-----------|
| **Chain** | Gnosis Chain | $0.00076/fact, xDAI stablecoin gas, Graph indexing rewards, 640GB archive, permanent L1 storage |
| **Paymaster** | **Pimlico** | 60x cheaper than ZeroDev ($0.11/mo vs $69/mo at 100 ops/day), permissionless.js SDK, webhook policies |
| **Fiat payments** | Stripe Checkout | Agent-generated URL, card/Apple Pay/Google Pay |
| **Crypto payments** | Coinbase Commerce | USDC/USDT on Solana, Base, Ethereum, Polygon, Arbitrum (no bridging) |
| **Auth** | Wallet signature | No API keys — seed-derived key signs every request |
| **Free tier** | Yes (threshold TBD) | Users experience value before paying |
| **Subscription** | $2-5/month (TBD) | Profitable on Gnosis at all scales (100-10K users) |
| **Indexing** | Subgraph on The Graph Network | Existing code, zero changes. GRT indexing rewards incentivize indexers |

### Economics (Power Users: 50 facts/day)

| Scale | Gas cost/mo | Query fees/mo | Revenue @ $2/mo | Net |
|-------|:---:|:---:|:---:|:---:|
| 100 users | $114 | $1 | $200 | +$85 |
| 1K users | $1,140 | $28 | $2,000 | +$832 |
| 10K users | $11,400 | $298 | $20,000 | +$8,302 |

### What's Built (Code Complete, Not Deployed)

All scaffolding and smart contracts are built and locally tested. Need to redeploy to Gnosis (was originally targeting Base).

| Component | Description | Tests | Status |
|-----------|-------------|-------|--------|
| EventfulDataEdge.sol | Minimal DA contract, fallback() emits Log(bytes), EntryPoint access control | 14 | DONE |
| TotalReclawPaymaster.sol | ERC-4337 paymaster with per-sender sliding window rate limiting | 32 | DONE |
| Deploy/verify/fund scripts | Hardhat deploy (need to retarget Gnosis) | — | NEEDS UPDATE |
| Subgraph schema + mapping | 14-field FactEntity, AssemblyScript Protobuf decoder, GlobalState tracking | graph build OK | DONE |
| Client BIP-39 seed management | 12-word mnemonic, BIP-32/44 derivation, HKDF key compatibility with kdf.ts | 19 | DONE |
| Client UserOperation builder | Encode facts as calldata, sign with seed-derived key, submit to relay | 11 | DONE |
| Server /relay endpoint | Target/calldata validation, per-address rate limiting, Pimlico bundler submission | 16 | DONE |
| Billing & onboarding spec | Full go-live architecture: payments, auth, tiers, onboarding flows | — | DONE (v1.0) |

### Next Steps

| Step | Effort | Description |
|------|--------|-------------|
| Retarget deploy scripts to Gnosis | 1 day | Update Hardhat config, subgraph.yaml network field |
| Fix subgraph recall gap | 1-2 days | Raise `GRAPH_GRAPHQL_MAX_FIRST` to 5,000, add pagination |
| Pimlico/ZeroDev evaluation | 1 day | Test webhook policy support on Gnosis |
| Stripe + Coinbase Commerce integration | 1 week | Checkout flow, webhooks, subscription table |
| Deploy to Gnosis testnet (Chiado) | 1 day | E2E validation on real network |

**Deployment deferred until MVP validates demand.**

---

## Phase 4: TEE (Trusted Execution Environment)

**Goal:** Hardware-enforced privacy with server-side intelligence. Separate stack from E2EE.
**Specs:** `docs/specs/tee/architecture.md`, `docs/specs/tee/tdx-saas.md`

This is a fundamentally different architecture. Since TEE (Intel TDX / AWS Nitro) guarantees hardware isolation, the server CAN work with unencrypted data inside the enclave. This enables:

- Server-side embeddings (no more LSH blind indices needed)
- Semantic search with real vector similarity
- LLM-powered enrichment and summarization on the server
- Higher recall with simpler architecture

This is NOT an upgrade to the E2EE path -- it is a separate product/stack. The E2EE path remains for users who want mathematical guarantees (do not trust hardware). The TEE path is for users who want better search quality with hardware-based privacy.

**Not started. Separate initiative.**

---

## Phase 5: Import & Migration (Future)

**Goal:** Let users consolidate fragmented AI memory from other systems into TotalReclaw. One-time or recurring import, re-encrypted into the user's vault.

**Not started. Post-beta.**

### 5.1 Import from External Memory Systems

Adapters to ingest memories from competing or complementary AI memory products. Each adapter reads from the source system's API or export format, extracts facts, encrypts them client-side, and stores them in TotalReclaw.

**MVP (Phase 2.6):** Mem0 + MCP Memory adapters ship with the free MVP launch.

| Source | Type | Priority | Notes |
|--------|------|----------|-------|
| **Mem0** (mem0.ai) | Hosted AI memory | **MVP** | API export of structured memories (JSON, 7-day link) |
| **MCP Memory Server** | Local JSONL | **MVP** | `@modelcontextprotocol/server-memory` — entities, relations, observations |
| **MemoClaw** (memoclaw.com) | Hosted API | Post-MVP | SDK is MIT; read via their API, re-encrypt into TotalReclaw |
| **Zep** (getzep.com) | Hosted AI memory | Post-MVP | Session-based memory with facts and summaries |
| **LanceDB** | Vector store | Post-MVP | Local or cloud; export embeddings + metadata |
| **QMD** (OpenClaw native) | Platform memory | Post-MVP | OpenClaw's built-in memory system |
| **Generic JSON/CSV** | File import | Post-MVP | Catch-all for other tools |
| **Other vector stores** | Generic adapter | Future | Chroma, Pinecone, Weaviate, Milvus, etc. |

### 5.2 Import from Major LLM Providers

Import conversation history and/or memory features from the major LLM providers. These require data export (GDPR/CCPA download or API) followed by LLM-based fact extraction and re-encryption.

| Provider | Data Source | Notes |
|----------|------------|-------|
| **Claude** (Anthropic) | Conversation history, memory | Data export or API access |
| **ChatGPT** (OpenAI) | Conversation history, memory | Data export (Settings → Export) or API |
| **Gemini** (Google) | Conversation history, memory | Google Takeout or API |

### Design Considerations

- All imports run **client-side** -- source data is decrypted/parsed locally, re-encrypted with the user's key, then stored. Server never sees plaintext.
- Fact extraction uses the same LLM pipeline as normal memory capture (extract → deduplicate → encrypt → store).
- Deduplication via content fingerprints prevents double-ingestion if an import is run multiple times.
- Bulk import should support progress tracking and resumption for large histories.

---

## Spec Inventory

Maps each spec to its rollout phase:

| Spec | Path | Phase | Status |
|------|------|-------|--------|
| E2EE with LSH + Blind Buckets | `docs/specs/totalreclaw/architecture.md` | Phase 1 (PoC) | Implemented, validated |
| TotalReclaw Skill for OpenClaw | `docs/specs/totalreclaw/skill-openclaw.md` | Phase 1 (PoC) | Implemented |
| Benchmark Harness (OMBH) | `docs/specs/totalreclaw/benchmark.md` | Phase 1 (PoC) | Implemented |
| Server PoC (no auth, superseded) | `docs/specs/archive/server-no-auth-superseded.md` | Phase 1 (PoC) | Superseded by v0.3.1 |
| Server PoC v0.3.1b (Auth + Dedup) | `docs/specs/totalreclaw/server.md` | Phase 1-2 (PoC to MVP) | Partially implemented |
| Multi-Agent Conflict Resolution v0.3.2 | `docs/specs/totalreclaw/conflict-resolution.md` | Phase 2 (MVP) | Draft spec |
| TotalReclaw MCP Server | `docs/specs/totalreclaw/mcp-server.md` | Phase 1 (PoC) | Implemented |
| TotalReclaw Skill for NanoClaw | `docs/specs/totalreclaw/skill-nanoclaw.md` | Phase 1 (PoC) | Implemented |
| Seed-to-Subgraph v1.0 | `docs/specs/subgraph/seed-to-subgraph.md` | Phase 3 (Subgraph) | Spec complete |
| TEE vs E2EE | `docs/specs/tee/architecture.md` | Phase 4 (TEE) | Analysis complete |
| TDX SaaS v0.4 | `docs/specs/tee/tdx-saas.md` | Phase 4 (TEE) | Spec complete |
| Import: External Memory Systems | — | Phase 2.6 / Phase 5 (Import) | Mem0 DONE (E2E validated), MCP Memory DONE (unit tests), others not started |
| Import: LLM Provider Histories | — | Phase 5 (Import) | Not started |

---

## Version Number Clarification

The codebase had old prototype directories (now in archive/prototypes/v02/ through v06/) that are NOT rollout versions. They are archived experiments. The actual versioning is:

| Version | What it means |
|---------|--------------|
| v0.3 | Current spec family (E2EE + LSH architecture) |
| v0.3.1 / v0.3.1b | Server PoC with auth + content fingerprint dedup |
| v0.3.2 | Multi-agent conflict resolution protocol |
| v1.0 (Seed-to-Subgraph) | Decentralized architecture spec |
| v0.4 (TDX SaaS) | TEE architecture spec |

The rollout phases (PoC, MVP, Subgraph, TEE) are the canonical way to think about what ships when.
