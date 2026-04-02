# Greenfield Architecture Assessment

> **Purpose:** If we were rebuilding TotalReclaw from scratch today, what would we keep, what would we change, and why? This document captures the architectural trade-offs of the current system and proposes a simplified alternative that preserves all value propositions.

## What We'd Keep Exactly As-Is

| Component | Why it works |
|-----------|-------------|
| **BIP-39 mnemonic → HKDF key derivation** | Elegant, proven. One phrase = all keys. The "password manager" mental model is the right UX. |
| **AES-256-GCM client-side encryption** | Non-negotiable for the value prop. Industry standard, well-audited. |
| **Blind index search (SHA-256 token hashes)** | Simple, fast, server-blind. Zero-knowledge search that actually works. |
| **Client-side reranking (BM25 + cosine + RRF)** | Keeping intelligence on the client is correct for the trust model. |
| **Content fingerprint dedup (HMAC-SHA256)** | Cheap, effective server-side exact dedup without seeing plaintext. |
| **Relay-as-blind-proxy model** | Sound trust architecture. Relay never sees plaintext, can't tamper with user intent. |

## What We'd Change

### 1. Storage: Skip the Blockchain (Default Path)

**Current:** ERC-4337 Smart Accounts → DataEdge contract → Pimlico bundler → Base Sepolia/Gnosis → The Graph subgraph → GraphQL queries.

**Problem:** This adds enormous complexity for a property (decentralization/permanence) that most users don't need yet:
- 35-second write latency (block confirmation + subgraph indexing)
- Pimlico gas cost management, sponsorship policies, webhook handlers
- Protobuf wire format encoding/decoding
- Smart Account address derivation (CREATE2, ERC-4337 v0.7)
- UserOp construction + EOA signing per client language
- Subgraph deployment, indexing lag, `_meta.block.number` races
- `0x` hex prefix bugs between subgraph and client decryption

**Greenfield alternative:** Encrypted blobs in object storage (Cloudflare R2 / S3) with PostgreSQL metadata index.

```
Client encrypts → HTTPS PUT /v1/facts → Relay stores encrypted blob in R2
                                       → Relay stores blind indices in PostgreSQL
Client searches → HTTPS POST /v1/search → Relay queries PostgreSQL blind indices
                                         → Returns encrypted blobs
                                         → Client decrypts + reranks locally
```

**Same guarantees preserved:**
- E2EE: server never sees plaintext (identical)
- Blind index search: SHA-256 token hashes in PostgreSQL (identical)
- Client-side reranking: BM25 + cosine + RRF (identical)
- Content fingerprint dedup: HMAC-SHA256 (identical)
- Export: same API, plain-text export works (identical)
- Portability: one mnemonic, every agent (identical)

**What changes:**
- Writes are instant (no 35s indexing wait)
- No gas costs, no Pimlico, no sponsorship policies
- No protobuf encoding — just JSON over HTTPS
- Simpler client libraries (HTTP PUT/GET vs. UserOp construction + signing)
- Cheaper to operate (R2 is ~$0.015/GB/month vs. gas costs)

**The "if we disappear" story still works:** Open-source + export + R2 replication. Users can self-host the relay + PostgreSQL. The on-chain permanence guarantee is powerful but can be offered as a **Pro add-on** rather than the default path.

**Keep on-chain as an option:**
- Pro users who want verifiable permanence get on-chain storage
- Free users get fast, cheap, reliable R2 storage
- Migration path from R2 → on-chain when upgrading to Pro

### 2. Embedding: Server-Side with TEE

**Current:** 600MB ONNX model download on client. Every client language needs its own embedding runtime. LSH bucket hashing (20 tables × 32-bit) adds complexity to every implementation.

**Problem:**
- First-use experience is poor (600MB download)
- Embedding quality varies across client ONNX runtimes
- LSH implementation must be identical across 4+ languages
- Some clients skip embedding entirely (Hermes debrief stores without embedding)

**Greenfield alternative:** Run embeddings in a TEE (Intel TDX / AWS Nitro Enclaves) on the server.

```
Client sends encrypted text → TEE decrypts in hardware-isolated memory
  → Computes embedding → Encrypts embedding → Returns to relay
  → Relay stores encrypted embedding (never sees plaintext or raw embedding)
```

**Benefits:**
- No client-side model download
- Consistent embedding quality across all clients
- LSH becomes optional (TEE can do exact cosine on encrypted embeddings)
- Simpler client libraries — just encrypt/decrypt + HTTP
- The trust surface is the TEE hardware, not the server process

**Trade-off:** Trusting the TEE. Intel TDX and AWS Nitro have had some side-channel concerns, but the trust surface is orders of magnitude smaller than trusting the whole server. For users who want maximum privacy, client-side embedding remains an option.

### 3. Client Libraries: One SDK, Not Five

**Current:** 4 implementations of the same crypto pipeline (Rust, TS MCP, TS Plugin, Python), plus the TS client library. 5 places for bugs. Every feature (debrief, import, consolidation) implemented 5 times with parity tests.

**Greenfield alternative:** One canonical Rust implementation, compiled to:
- **WASM** for TypeScript/Node.js (MCP server, OpenClaw, NanoClaw)
- **Python bindings** via PyO3 (Hermes)
- **Native** for ZeroClaw (already Rust)

One implementation, one test suite, guaranteed parity. The FFI boundary is clean:
```
encrypt(text, key) → blob
decrypt(blob, key) → text
derive_keys(mnemonic) → {auth_key, encryption_key, dedup_key}
generate_blind_indices(text) → [hash1, hash2, ...]
lsh_hash(embedding) → [bucket1, bucket2, ...]
```

**Note:** This is currently being implemented as `totalreclaw-core` (Phase 2 of the active plan). The Rust crate is extracted; WASM and PyO3 bindings are next.

### 4. Search: Simpler Retrieval Pipeline

**Current pipeline:** Blind indices → subgraph GraphQL → decrypt all candidates → compute embeddings client-side → BM25 + cosine + RRF reranking → top-k.

**Problem:** Every client must implement the full reranker pipeline. The subgraph query returns encrypted blobs that must be decrypted and re-embedded client-side.

**Greenfield with TEE embeddings:** Client sends encrypted query → TEE decrypts, embeds, runs cosine against stored embeddings → returns top-k encrypted results → client decrypts. No client-side reranking needed.

**Without TEE (R2 + PostgreSQL path):** Same blind index search as today, but:
- PostgreSQL full-text search on blind indices (faster than GraphQL)
- Optional: store encrypted embeddings in PostgreSQL, cosine similarity via `pgvector`
- Client-side reranking only for the final top-k refinement

### 5. Plugin Model: MCP-First

**Current:** Bespoke plugin code for each framework (OpenClaw hooks, NanoClaw hooks, Hermes hooks). Each has different lifecycle events, APIs, hook names. Every new feature needs 5 implementations.

**Greenfield alternative:** Ship only the MCP server. Every framework that supports MCP gets TotalReclaw for free. The MCP protocol is becoming the standard for tool integration.

**Trade-off:** You lose auto-extraction (no `agent_end` hook equivalent in pure MCP). Compensate with:
- More aggressive debrief at conversation boundaries
- Prompt-guided periodic extraction ("every few turns, call totalreclaw_remember with key facts")
- Host agents increasingly support MCP lifecycle events

For frameworks with rich hook support (OpenClaw, ZeroClaw), keep thin adapter layers that wire hooks to MCP tool calls — but the actual logic lives in the MCP server.

---

## Greenfield Stack Summary

```
┌─────────────────────────────────────────┐
│            Client (any language)         │
│  ┌─────────────────────────────────┐    │
│  │  totalreclaw-core (Rust → WASM) │    │
│  │  encrypt / decrypt / key-derive │    │
│  │  blind indices / LSH / protobuf │    │
│  └─────────────────────────────────┘    │
│  HTTP client → relay API                 │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│              Relay Server                │
│  Express/Fastify                        │
│  Auth, quota, rate limiting             │
│  ┌─────────────────────────────┐        │
│  │  TEE Enclave (optional)      │       │
│  │  Decrypt → Embed → Encrypt   │       │
│  └─────────────────────────────┘        │
│  R2/S3 → encrypted blob storage         │
│  PostgreSQL → metadata, blind indices   │
└─────────────────────────────────────────┘
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
┌──────────────┐   ┌──────────────────┐
│  R2 / S3     │   │  On-chain (Pro)   │
│  Default     │   │  ERC-4337 + Graph │
│  Fast, cheap │   │  Verifiable       │
└──────────────┘   └──────────────────┘
```

## What This Preserves

| Value Proposition | Preserved? | How |
|-------------------|:----------:|-----|
| E2E encrypted | Yes | Same AES-256-GCM, same key derivation |
| Portable | Yes | Same BIP-39 mnemonic, same cross-client support |
| Server-blind | Yes | Relay never sees plaintext |
| Export anytime | Yes | Same export API |
| No vendor lock-in | Yes | Open source + export + self-hostable |
| Verifiable permanence | Yes (Pro) | On-chain option preserved for Pro tier |

## What This Eliminates

| Complexity | Current Cost | Eliminated? |
|-----------|-------------|:-----------:|
| 35s write latency | UX friction | Yes (default path) |
| Pimlico gas management | Operational cost + billing risk | Yes (default path) |
| 5× crypto implementations | Bug surface + maintenance | Yes (unified core) |
| 600MB model download | First-use friction | Yes (with TEE) |
| Protobuf encoding | Per-language implementation | Yes (JSON over HTTPS) |
| Subgraph deployment + indexing | Operational complexity | Yes (default path) |
| Smart Account derivation | Per-language ERC-4337 code | Yes (default path) |

## Honest Assessment

The current architecture is **over-engineered for the current stage**. The on-chain storage is a bet on a future where verifiable permanence matters to a critical mass of users. The five-implementation model is a bet on framework diversity.

If the on-chain bet pays off, the current architecture is ahead of its time. If it doesn't, a simpler stack would have reached product-market fit faster.

The greenfield path doesn't abandon on-chain — it repositions it as a premium feature rather than the default, reducing operational complexity and cost while preserving every user-facing value proposition.

## Incremental Migration Path

This doesn't require a rewrite. The current system can evolve toward the greenfield architecture incrementally:

1. **Unified core** (in progress) — `totalreclaw-core` Rust crate with WASM + PyO3
2. **R2 storage option** — Add alongside on-chain, default for free tier
3. **TEE embedding** — Add as a relay service, client-side remains as fallback
4. **MCP-first plugins** — Thin adapters that delegate to MCP tools
5. **On-chain as Pro add-on** — Keep the pipeline but make it optional
