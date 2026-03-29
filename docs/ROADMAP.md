# TotalReclaw Roadmap

## Overview

TotalReclaw is an end-to-end encrypted memory vault for AI agents. The project progresses through phases, each building on the last but with distinct goals:

1. **Phase 1 (PoC)** -- Validate the E2EE architecture end-to-end with local testing.
2. **Phase 2 (Free MVP)** -- Ship the managed service, publish on Claw Hub, and benchmark against Mem0.
3. **Phase 3 (Self-Hosted)** -- Self-hosted deployment option for users who want full control over their memory infrastructure.
4. **Phase 4 (TEE)** -- Hardware-enforced privacy via Intel TDX / AWS Nitro as a separate product stack.
5. **Phase 5 (Import)** -- Import & migrate memories from external systems (Mem0, Zep, LLM providers, etc.).
6. **Phase 6 (Platform)** -- Integrator support, knowledge graphs, and cross-agent intelligence.

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

## Phase 2: Free MVP (Managed Service)

**Goal:** Public launch via Claw Hub and other channels. Managed service online for real users.
**Specs:** `docs/specs/totalreclaw/server.md` (v0.3.1b) + production hardening

### 2.1 Managed Service Production Readiness

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
| Near-duplicate detection | Before storing, search for semantically similar facts; supersede/skip (cosine >= 0.85) | **DONE** — store-time dedup in auto-extraction + remember tool |
| On-demand consolidation | `totalreclaw_consolidate` tool scans all memories and merges duplicates | **DONE** — cluster by cosine (0.88), batch-delete, dry_run support |
| Extraction-time dedup | Integrate dedup check into `agent_end` extraction pipeline | **DONE** — wired into `storeExtractedFacts()` |
| Server batch-delete | `POST /v1/facts/batch-delete` for efficient consolidation | **DONE** |

**Constraint:** All comparison/merging happens client-side (server-blind). Server only has content fingerprints and blind indices.

**Plan:** `plans/2026-03-11-memory-consolidation-dedup.md` (internal repo)

### 2.5 Pro Tier Features (Post-Beta)

| Feature | Description | Status |
|---------|-------------|--------|
| LLM-guided dedup (Pro) | UPDATE/DELETE/NOOP classification gated behind Pro tier | **DONE** — client-side gating via billing cache |
| Unified extraction interval | 3 turns for all tiers (quota is per-transaction) | **DONE** — `getExtractInterval()` returns env override or default 3 |
| Memory retention / decay tuning (Pro) | Configurable importance thresholds and decay curves. Pro users keep memories longer or tune what gets retained. | NOT DONE |
| Namespace isolation (Pro) | Scope memories by namespace (e.g., "work" vs "personal"). Currently removed; will return as Pro-only. | NOT DONE -- MCP had it, stripped in prep for Pro gating |
| Supersession graph (Pro) | Track fact replacement chains for memory history. Foundation for knowledge graph (Phase 6). | NOT DONE -- see Phase 6.1 |

### 2.6 Memory Compression Stats

Surface token reduction metrics that users and integrators can see.

| Task | Description | Status |
|------|-------------|--------|
| Track context savings | Measure tokens injected vs full conversation replay | NOT DONE |
| Surface in `totalreclaw_status` | Show compression ratio alongside tier/usage | NOT DONE |

### 2.7 Competitor Import (MVP — Mem0 + MCP Memory)

Import tools to reduce switching costs. Prioritize the two most common sources for MVP.

| Task | Description | Status |
|------|-------------|--------|
| `totalreclaw_import_from` tool | Source-agnostic import with adapter pattern | **DONE** |
| Mem0 adapter | Import from mem0.ai API export (JSON) | **DONE** — unit tests + E2E validated (100% import, 100% recall) |
| MCP Memory adapter | Import from `@modelcontextprotocol/server-memory` JSONL | **DONE** (unit tests); untested with real JSONL |
| Progress tracking | Report progress for large imports | NOT DONE |

**Constraint:** All processing client-side. Content fingerprint dedup prevents double-import.

**Plan:** `plans/2026-03-11-competitor-import.md` (internal repo)

### 2.8 Multi-Agent Conflict Resolution (`docs/specs/totalreclaw/conflict-resolution.md` v0.3.2)

Applies to both managed service (PostgreSQL) and future self-hosted deployments. 4-layer protocol:

| Layer | What | Effort | Priority |
|-------|------|--------|----------|
| 1: Content fingerprint | HMAC-SHA256 exact dedup (server-side) | 2h | HIGH (in v0.3.1b) |
| 2: Sync watermark | Delta sync via sequence_id | 3h | HIGH (in v0.3.1b) |
| 3: Blind index overlap | Probabilistic near-dedup without plaintext | 3h | MEDIUM |
| 4: Client LLM merge | Decrypt, compare, LLM resolves contradictions | 4h | MEDIUM |

Layers 1-2 are part of PoC (v0.3.1b). Layers 3-4 are MVP enhancements (v0.3.2).

### 2.9 HTTP MCP & Hosted Agent Integration

Support hosted AI agents (IronClaw / NEAR AI Cloud, Windsurf, etc.) that cannot spawn local stdio processes.

**Hard Constraint:** The relay NEVER sees the recovery phrase (mnemonic). Encryption and signing always happen in the agent's runtime. The relay is a blind proxy.

**Architecture:** Hybrid model with two tiers:

| Tier | What | Mnemonic Needed? | Status |
|------|------|:----------------:|--------|
| **Thin HTTP MCP** | `/v1/mcp` on relay — status, upgrade, encrypted recall only | No | NOT DONE |
| **Client in TEE** | stdio MCP server inside IronClaw TEE — full E2EE | Yes (in TEE vault) | NOT DONE |

**Thin HTTP MCP (Tier 1):** 3 server-side-safe tools (`totalreclaw_status`, `totalreclaw_upgrade`, `totalreclaw_recall_encrypted`). Relay authenticates via API key mapped to wallet address. No mnemonic on relay. Any hosted agent can connect via HTTPS URL + Bearer token.

**Client in TEE (Tier 2):** Full 10-tool E2EE. Mnemonic stored in IronClaw's encrypted credential vault (AES-256-GCM, TEE-protected). `@totalreclaw/mcp-server` runs as stdio process inside the TEE, reads mnemonic from env var. Architecturally identical to local stdio model, but hardware-isolated.

| Task | Description | Effort | Status |
|------|-------------|--------|--------|
| Thin HTTP MCP endpoint | `/v1/mcp` on relay (3 server-safe tools) | 7-8 days | NOT DONE |
| Registration web UI | Browser-side key derivation, API key generation (mnemonic never leaves browser) | 1 day | NOT DONE |
| Factor MCP server handlers | Shared handler module for stdio + HTTP transports | 1 day | NOT DONE |
| stdio MCP inside IronClaw TEE | Validate local MCP spawning, document vault setup | 1-2 days | NOT DONE |
| IronClaw testing | Validate both tiers on NEAR AI Cloud | 2 days | NOT DONE |
| Documentation | HTTP MCP setup guide + IronClaw guide | 0.5 day | NOT DONE |

**Plans:** `plans/2026-03-28-http-mcp-endpoint.md` (v2), `plans/2026-03-28-ironclaw-native-extension.md` (internal repo)

---

## Phase 3: Self-Hosted

**Goal:** Self-hosted deployment option for users who want full control over their memory infrastructure.
**Specs:** `docs/specs/subgraph/seed-to-subgraph.md`, `docs/specs/subgraph/billing-and-onboarding.md`

Self-hosted mode uses the same E2EE architecture but stores encrypted facts on-chain (Gnosis Chain via ERC-4337 Smart Accounts) rather than the managed PostgreSQL service. User's BIP-39 seed derives both encryption key AND on-chain identity. Writes go through a paymaster-sponsored UserOperation, emitted as events by an EventfulDataEdge contract, indexed by The Graph Network.

### Go-Live Architecture (Decided 2026-03-03)

| Component | Decision | Rationale |
|-----------|----------|-----------|
| **Chain** | Gnosis Chain | $0.00076/fact, xDAI stablecoin gas, Graph indexing rewards, 640GB archive, permanent L1 storage |
| **Paymaster** | **Pimlico** | 60x cheaper than ZeroDev ($0.11/mo vs $69/mo at 100 ops/day), permissionless.js SDK, webhook policies |
| **Fiat payments** | Stripe Checkout | Agent-generated URL, card/Apple Pay/Google Pay. Sole payment method. |
| **Auth** | Wallet signature | No API keys — seed-derived key signs every request |
| **Free tier** | 500 memories/month | Users experience value before paying |
| **Subscription** | $5/month (unlimited) | Profitable on Gnosis at all scales (100-10K users) |
| **Indexing** | Subgraph on The Graph Network | Existing code, zero changes. GRT indexing rewards incentivize indexers |

### Economics (Power Users: 50 facts/day)

| Scale | Gas cost/mo | Query fees/mo | Revenue @ $5/mo | Net |
|-------|:---:|:---:|:---:|:---:|
| 100 users | $114 | $1 | $500 | +$385 |
| 1K users | $1,140 | $28 | $5,000 | +$3,832 |
| 10K users | $11,400 | $298 | $50,000 | +$38,302 |

### What's Built (Code Complete, Deployed to Base Sepolia + Gnosis Mainnet)

All scaffolding and smart contracts are built, tested, and deployed to Base Sepolia (free tier) and Gnosis mainnet (Pro tier). E2E validated.

| Component | Description | Tests | Status |
|-----------|-------------|-------|--------|
| EventfulDataEdge.sol | Minimal DA contract, fallback() emits Log(bytes), permissionless (no access control) | 14 | DONE |
| TotalReclawPaymaster.sol | ERC-4337 paymaster with per-sender sliding window rate limiting | 32 | DONE |
| Deploy/verify/fund scripts | Hardhat deploy, deployed to Base Sepolia + Gnosis mainnet via Pimlico CREATE2 | -- | DONE |
| Subgraph schema + mapping | 14-field FactEntity, AssemblyScript Protobuf decoder, GlobalState tracking | graph build OK | DONE |
| Client BIP-39 seed management | 12-word mnemonic, BIP-32/44 derivation, HKDF key compatibility with kdf.ts | 19 | DONE |
| Client UserOperation builder | Encode facts as calldata, sign with seed-derived key, submit to relay | 11 | DONE |
| Managed service relay proxy | Target/calldata validation, per-address rate limiting, Pimlico bundler submission, subgraph query proxy | 16 | DONE |
| Billing & onboarding | Stripe billing, subscription table, webhook handlers | -- | DONE |
| Admin dashboard | Two-factor auth (API key + OTP), 13 API endpoints, tier CRUD with Stripe, analytics, single-file HTML UI | 12 | DONE |
| Client type tracking | X-TotalReclaw-Client header on all relay requests, request_log table, analytics queries | -- | DONE |

### Relay Extraction (Complete)

The managed service relay has been extracted to a private `totalreclaw-relay` TypeScript repo (`p-diogo/totalreclaw-relay`). The relay handles billing, Pimlico bundler submission, dual-chain routing, and subgraph query proxying. Deployed independently on Railway.

### Current Deployment

| Component | Status |
|-----------|--------|
| Relay extraction to `totalreclaw-relay` | DONE |
| Base Sepolia deployment (free tier) | DONE |
| Gnosis mainnet deployment (Pro tier) | DONE |
| Dual-chain routing | DONE |
| Load testing (managed service) | DONE -- <140ms p95 up to 10K facts |

---

## Phase 4: TEE (TDX SaaS)

**Goal:** Hardware-enforced privacy with server-side intelligence on encrypted data. Separate stack from E2EE.
**Specs:** `docs/specs/tee/architecture.md`, `docs/specs/tee/tdx-saas.md`

This is a fundamentally different architecture. Since TEE (Intel TDX / AWS Nitro) guarantees hardware isolation, the server CAN work with unencrypted data inside the enclave. This enables:

- Server-side embeddings (no more LSH blind indices needed)
- Semantic search with real vector similarity
- LLM-powered enrichment and summarization on the server
- Higher recall with simpler architecture
- **Cross-agent conflict detection** -- blind index overlap patterns can be resolved with full plaintext access inside the enclave (see Phase 6)

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

## Phase 6: Platform (Future)

**Goal:** Integrator ecosystem, knowledge graphs, and cross-agent intelligence.

### 6.1 Supersession Graph (Pro)

Track fact replacement chains for memory history. When a fact is updated or superseded, maintain a linked chain of previous versions. Enables "what did I used to believe about X?" queries and audit trails.

- Foundation for the knowledge graph (6.2)
- Pro tier feature -- free users see only current facts
- Requires encrypted graph edges stored alongside facts

### 6.2 Knowledge Graph

Build on the supersession graph foundation to create a full knowledge graph of entity relationships. Extract and maintain relationships between entities (people, tools, projects) from stored facts.

- Depends on supersession graph (6.1)
- Client-side extraction during fact storage
- Encrypted relationship edges (E2EE preserved)

### 6.3 Integrator Support

Partner onboarding, referral tracking, and revenue share dashboard for third-party integrators who build on TotalReclaw.

| Component | Description | Status |
|-----------|-------------|--------|
| Partner onboarding flow | API key provisioning, sandbox environment | NOT DONE |
| Referral tracking | Track signups and conversions per integrator | NOT DONE |
| Revenue share dashboard | Real-time earnings, payout history, usage metrics | NOT DONE |

### 6.4 Cross-Agent Conflict Detection

Detect when multiple agents write conflicting facts about the same topic using blind index overlap patterns. In E2EE mode, detection is probabilistic (blind indices only). Full resolution requires TEE (Phase 4) for plaintext access inside the enclave.

- E2EE mode: detect overlap via blind index intersection, surface conflicts to user
- TEE mode: full semantic conflict resolution server-side
- Builds on conflict resolution layers 3-4 (Phase 2.8)

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
| Seed-to-Subgraph v1.0 | `docs/specs/subgraph/seed-to-subgraph.md` | Phase 3 (Self-Hosted) | Deployed to Base Sepolia + Gnosis mainnet |
| TEE vs E2EE | `docs/specs/tee/architecture.md` | Phase 4 (TDX SaaS) | Analysis complete |
| TDX SaaS v0.4 | `docs/specs/tee/tdx-saas.md` | Phase 4 (TDX SaaS) | Spec complete |
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
| v1.0 (Seed-to-Subgraph) | Self-hosted architecture spec (on-chain storage via Gnosis) |
| v0.4 (TDX SaaS) | TEE architecture spec (server-side intelligence on encrypted data) |

The rollout phases (PoC, MVP, Self-Hosted, TEE, Import, Platform) are the canonical way to think about what ships when.
