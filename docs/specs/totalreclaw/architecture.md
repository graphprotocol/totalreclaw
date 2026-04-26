<!--
Product: TotalReclaw
Version: v1.0 — 2026-04-26
Last updated: 2026-04-26
-->

# TotalReclaw — System Architecture (v1)

**Title:** TotalReclaw v1.0 — End-to-end-encrypted memory across AI clients, with permanent on-chain anchoring
**Audience:** anyone integrating, auditing, or extending TotalReclaw (engineers, security reviewers, downstream client authors)
**Scope:** the architectural picture — identity, crypto, storage, retrieval, multi-client model, on-chain layer, install model. Implementation-level schemas live in their own specs (linked).

This document is intentionally architectural. Concrete schemas, retrieval pipeline weights, and individual flow diagrams live in:

- [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md) — canonical claim schema (6 types + source / scope / volatility axes)
- [`retrieval-v2.md`](./retrieval-v2.md) — Tier 1 source-weighted reranker + tiers
- [`server.md`](./server.md) — relay surface (sibling spec)
- [`flows/`](./flows/README.md) — write/read/recovery flow walkthroughs

Update this guide when **architectural** invariants change (trust split, key model, on-chain primitives, install model). Schema-level evolution belongs in the linked specs, not here.

---

## 1. Trust split — what runs where, and what each layer can see

TotalReclaw is split across three trust layers. This split is the single load-bearing claim of the architecture; everything else is in service of preserving it.

```
   Client (device)               Relay Service                     Open Network
 ┌──────────────────┐         ┌──────────────────┐             ┌──────────────────┐
 │ Mnemonic-rooted   │         │ Encrypted blobs   │             │ Gnosis (Pro)     │
 │ key derivation    │ cipher  │ + LSH trapdoors   │   anchor    │ Base Sepolia     │
 │ Embedding model   │ + LSH   │ GIN-indexed       │  (sponsored │ (Free testnet)   │
 │ Extraction LLM    │ ─────▸  │ candidate retrieval│ ERC-4337) ▸ │ EventfulDataEdge │
 │ Reranker (core)   │ ◂─────  │ AA bundler shim   │  ◂────────  │ Subgraph indexer │
 │ Smart Account sig │ results │ Pair-flow relay   │   indexed   └──────────────────┘
 └──────────────────┘         └──────────────────┘    events
```

| Layer | Sees plaintext? | Sees embeddings? | Holds keys? | Notes |
|---|---|---|---|---|
| Client | yes | yes (locally) | yes (mnemonic + derived) | All extraction, embedding, encryption, reranking |
| Relay | **no** | **no** | **no** (only `SHA256(authKey)`) | Stores ciphertext, returns candidates by trapdoor match, brokers ephemeral pair-flow handshakes, shims AA bundler calls |
| Open network | **no** | **no** | **no** | Anchors ciphertext as event logs; permanent, permissionless |

**Critical invariant:** the embedding model stays client-side. Putting it on the relay would break the trust split — the server would learn semantic content from queries, even if storage stayed encrypted. This is why the lazy-CDN embedder (§7) downloads the model to the device, not to the relay.

---

## 2. Identity — one mnemonic, deterministic everything

A 12-word BIP-39 mnemonic is the **only** secret the user holds. Every other key, address, and stable identifier is derived from it.

```
BIP-39 mnemonic
   │
   ├─► PBKDF2 (BIP-39 standard, 2048 rounds) → 512-bit seed
   │
   ├─► HKDF-SHA256(seed, "totalreclaw-auth-v1")        → authKey         (relay auth)
   ├─► HKDF-SHA256(seed, "totalreclaw-enc-v1")         → encryptionKey   (XChaCha20)
   ├─► HKDF-SHA256(seed, "totalreclaw-dedup-v1")       → dedupKey        (HMAC fingerprints)
   ├─► HKDF-SHA256(seed, "totalreclaw-lsh-v1")         → lshSeed         (deterministic hyperplanes)
   │
   └─► BIP-44 derivation m/44'/60'/0'/0/0
         → secp256k1 keypair → ERC-4337 Smart Account address (counterfactual, deterministic)
```

**Properties this gives us:**

- **Cross-client portability.** Same mnemonic on OpenClaw, Hermes, NanoClaw, MCP server → identical Smart Account address, same encryption key, same LSH hyperplanes → all clients see the same memories. No server-side coordination required.
- **Server blindness.** The relay only ever stores `SHA256(authKey)`. It cannot derive `encryptionKey`, `dedupKey`, or `lshSeed` from this.
- **Recovery.** Lose the device, keep the mnemonic → re-derive everything, query the subgraph for the smart-account's event history, decrypt locally. Relay outage / acquisition / shutdown does not lose data.

### Phrase-safety rule

The recovery phrase MUST NEVER cross an LLM context. Concretely:

- Agents do **not** display, store, log, or transmit the phrase.
- Setup happens via a browser-side QR-pair flow (§6) — the phrase never leaves the user's browser tab.
- No CLI tool exposed to an agent emits or accepts the phrase as text.

This rule is structural, not advisory. All client implementations are audited against it.

---

## 3. Crypto choices — which primitive, where, why

Two separate authenticated symmetric ciphers are used, deliberately, because the threat model differs.

| Use site | Primitive | Rationale |
|---|---|---|
| **Long-term storage** of memory ciphertext (relay + on-chain) | **XChaCha20-Poly1305** | 192-bit nonce → safe to pick at random without a counter; constant-time ChaCha is faster than AES-GCM on TotalReclaw's targets (browsers, Node, PyO3, embedded Rust); long-term storage means many writes, large nonce space matters |
| **Pair-flow ECDH session** (browser ↔ client during onboarding, rc.12+) | **AES-256-GCM** | Short-lived session payload (the paired phrase, encrypted to the client's ephemeral pubkey); native WebCrypto support across browsers; nonce reuse risk is trivial because each pair session uses a fresh ECDH session key |
| Authentication | HKDF-SHA256-derived `authKey`, sent as bearer token; relay stores only `SHA256(authKey)` | Stateless; no separate password; same mnemonic re-derives auth on any device |
| Dedup | `HMAC-SHA256(dedupKey, normalize(plaintext))` → content fingerprint | Server can de-duplicate without learning plaintext; one bit of leakage per pair (same/not-same) |
| Blind search | `SHA-256` of word tokens + LSH bucket IDs (trapdoors) | Server matches trapdoors via GIN index; cannot invert |

**One-line summary:** XChaCha20-Poly1305 for things that live a long time, AES-GCM for the brief ECDH onboarding payload, SHA-256/HMAC for everything the server needs to *match without decrypting*.

---

## 4. Memory model — the v1 taxonomy

The plaintext inside each ciphertext is a **v1 Memory Claim** — a structured object, not a free-text blob. Six closed types, three orthogonal axes, provenance as a first-class field.

The architectural points:

- **Six types, closed enum:** `claim`, `preference`, `directive`, `commitment`, `episode`, `summary`. Cross-client agreement requires a small, fixed vocabulary.
- **Three orthogonal axes:** `source` (who authored — user / user-inferred / derived / external / agent), `scope` (life domain), `volatility` (how stable over time).
- **Provenance is structural, not metadata.** The reranker reads `source` and weights it (Tier 1, §5). This is what defends against the Mem0 97.8%-junk failure mode: assistant-tagged content cannot score equal to user-authored claims.
- **Importance is advisory.** Receivers may recompute it.
- **Pin status is additive (v1.1).** Pinned claims are immune to auto-supersede; the field is optional and ignorable by older readers.

Full schema, axis enums, validation rules, version-history details — see **[`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md)**. Do not duplicate them here; this section is the architectural framing only.

---

## 5. Storage and retrieval

### Write path (architectural)

```
plaintext claim (v1 taxonomy)
   │
   ├─► extract → embed → compute LSH buckets → hash buckets to trapdoors
   ├─► compute content fingerprint = HMAC-SHA256(dedupKey, normalize(text))
   ├─► XChaCha20-Poly1305 encrypt {claim, embedding, metadata}
   │
   ▼
protobuf v=4 envelope { ciphertext, blind_indices[], content_fp, ... }
   │
   ├─► POST to relay (fast path) — stored under user's authKeyHash
   └─► ERC-4337 UserOp via relay's bundler shim → on-chain log → indexed by subgraph
```

### Read path (architectural)

```
query text
   │
   ├─► embed query (client-side; instruction-prefixed)
   ├─► generate trapdoors (token hashes + LSH bucket hashes)
   │
   ▼
relay GIN index returns N encrypted candidates (auto-tuned by corpus size)
   │
   ▼
client decrypts, runs core::reranker:
   ├─► BM25 + cosine + decay + importance
   ├─► RRF fusion
   ├─► Tier 1 source-weighted multiplier (v1)
   │
   ▼
top K (default 8)
```

### Source-weighted reranker (Tier 1)

Lives in `rust/totalreclaw-core/src/reranker.rs` and is consumed by every client (WASM for JS/TS, PyO3 for Python). After RRF fusion, the final score is multiplied by a `source` weight (user > user-inferred > derived ≈ external > agent). This is the minimum retrieval change that lets v1 taxonomy actually do work — without it, provenance tagging is observability, not behavior.

Full reranker spec including tiers 2–4 and validation results: **[`retrieval-v2.md`](./retrieval-v2.md)**.

### Read-after-write consistency

On-chain writes are visible only after the subgraph indexes them. Empirically: **~5–30s lag** in steady state. As of `totalreclaw-core` rc.22+, reads opt into a primitive that polls the subgraph until the just-written sequence is visible, so callers don't see "I just stored that — why isn't it there?" races. The primitive is in `core`, so all clients inherit it.

### Storage tiers

| Tier | Anchor | Cost | Persistence |
|---|---|---|---|
| **Free** | Base Sepolia testnet | $0 | May reset (testnet) — fine for evaluation, not for irreplaceable data |
| **Pro** | Gnosis mainnet | $3.99 / mo | Permanent, permissionless |

Both tiers use the same `EventfulDataEdge` contract pattern (single `fallback()` emitting `Log(msg.data)`), the same ERC-4337 Smart Account derivation, and the same paymaster-sponsored UserOps. Tier choice is at write time and recorded per claim.

---

## 6. Onboarding — the QR-pair flow

The mnemonic must reach the client without ever crossing an LLM context. The flow:

```
┌─────────────────┐                                 ┌─────────────────┐
│ Browser tab     │   1. Show pair QR + nonce       │ Client (e.g.    │
│ (totalreclaw.xyz)│ ◀──────────────────────────────│  OpenClaw,      │
│                 │                                 │  Hermes, MCP)   │
│ User enters or  │   2. Scan QR → ECDH pubkey      │                 │
│ generates       │      exchange via relay         │ Generates       │
│ phrase locally  │      (ephemeral WebSocket)      │ ephemeral       │
│                 │                                 │ ECDH keypair    │
│ AES-GCM encrypt │   3. Encrypted paired_phrase    │                 │
│ phrase to       │ ──────────────────────────────▶ │ Decrypts with   │
│ client's pubkey │      via relay (transient)      │ session key,    │
│                 │                                 │ derives all keys│
└─────────────────┘                                 └─────────────────┘
```

Properties:

- **Phrase never enters an agent context.** The browser holds it; the client receives only the AES-GCM ciphertext.
- **Relay is a dumb transport.** It brokers the WebSocket pubkey exchange and forwards the encrypted blob. It cannot decrypt — it doesn't have the ECDH session key.
- **Same mnemonic, any client.** Pair OpenClaw today, pair Hermes tomorrow with the same phrase → same Smart Account, same memories.

The pair handshake mechanism shipped in relay rc.10+; AES-GCM payload encryption shipped client-side in rc.12+.

---

## 7. Install model — URL-driven setup, lazy-CDN embedder

### URL-driven install (rc.20+)

The user does not install via copy-pasted shell commands. The flow:

1. User pastes a setup URL (e.g. `https://totalreclaw.xyz/setup/openclaw`) into their agent.
2. Agent fetches the markdown at that URL.
3. The markdown contains the install commands; the agent runs them.

This decouples install instructions from agent prompt-engineering and gives the project a single place to update setup steps without re-publishing every client.

### Lazy-CDN embedder (rc.22+)

The plugin tarball is small (~5–10 MB) — small enough to install fast even on slow connections. The embedding model (~325 MB) is **not** in the tarball. On the first auto-extraction turn, the client downloads the embedder from GitHub Releases, caches it locally, and uses it from then on.

**E2EE invariant preserved:** the embedder runs on the device. The relay never sees it, never proxies it, and could not host it without breaking the trust split.

---

## 8. Clients — one core, many surfaces

Every client speaks to the relay and the chain through the same Rust core (`totalreclaw-core`), exposed via WASM (browsers / Node / Deno) or PyO3 (Python). This means crypto, retrieval, dedup, and protobuf framing have **one** implementation — clients are thin agent-integration shells around it.

| Client | Package | Role |
|---|---|---|
| OpenClaw plugin | `@totalreclaw/totalreclaw` | Plugin for the OpenClaw agent runtime |
| Hermes | `totalreclaw` (PyPI) | Python client for Claude Agent SDK / direct API users |
| NanoClaw skill | bundled | Lightweight overlay skill |
| MCP server | `@totalreclaw/mcp-server` | Model Context Protocol server — any MCP host (Claude Desktop, Cursor, etc.) |

Stable versions are tracked in [`docs/release-pipeline.md`](../../release-pipeline.md) (in the internal repo) and in the public-facing release pages. Don't pin them in this doc — they drift.

**Architectural consequence:** to add a new client (e.g. a new IDE), you bind to `totalreclaw-core` and implement the agent-glue. You don't reimplement crypto, retrieval, or dedup. This is what keeps cross-client behavior consistent as the surface area grows.

---

## 9. Relay — a blind intermediary

The relay (separate `totalreclaw-relay` repo) is **not** a memory backend in the traditional sense. It does four things:

1. **Stores opaque ciphertext + trapdoors** under `SHA256(authKey)` namespaces.
2. **Returns candidates by trapdoor match** (PostgreSQL GIN index over the blind-index array).
3. **Brokers pair-flow handshakes** via ephemeral WebSocket sessions.
4. **Shims ERC-4337 bundler calls** so clients can send UserOps without running their own bundler.

Health endpoints:

- Staging: `https://api-staging.totalreclaw.xyz`
- Production: `https://api.totalreclaw.xyz`

Full surface (protobuf wire format, endpoints, dedup behavior, sync semantics, schema, deployment) lives in the sibling spec: **[`server.md`](./server.md)**.

---

## 10. Performance envelope (architectural targets)

The following are the architectural targets the system is designed around. Implementation-specific measured numbers live next to each implementation in code or in the relevant spec.

| Concern | Target |
|---|---|
| End-to-end search latency (client + relay roundtrip) | < 150 ms p95 |
| Trapdoor generation + GIN lookup | < 15 ms |
| Recall@8 on real-user corpora | ≥ 95% (validated 98.1% on benchmark) |
| Plugin tarball size | ~5–10 MB |
| Embedder download (first-turn lazy fetch) | ~325 MB |
| On-chain write visibility (subgraph lag) | 5–30 s typical |
| Smart-account derivation determinism | 100% — same mnemonic → same address, every client |

When these targets slip, the architecture is wrong, not the implementation. (LSH parameters, reranker tiers, embedder choice — all live in linked specs and can be tuned without touching this doc.)

---

## 11. What this doc does NOT cover

By design. Each topic below has a canonical home:

- **Claim schema, axis enums, version history** → [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md)
- **Reranker weights, tier definitions, validation** → [`retrieval-v2.md`](./retrieval-v2.md)
- **Relay protobuf wire format, endpoints, DB schema, dedup behavior** → [`server.md`](./server.md)
- **Step-by-step user flows** (identity setup, write, read, recovery, storage modes) → [`flows/README.md`](./flows/README.md)
- **Conflict resolution beyond exact-fingerprint dedup** → conflict-resolution spec (see `conflict-resolution.md`)
- **LSH parameter tuning + scaling formula** → [`lsh-tuning.md`](./lsh-tuning.md)
- **MCP server specifics** → [`mcp-server.md`](./mcp-server.md)
- **Skills (OpenClaw / NanoClaw)** → [`skill-openclaw.md`](./skill-openclaw.md), [`skill-nanoclaw.md`](./skill-nanoclaw.md)

If you find yourself wanting to extend this doc with a schema field, a tuning constant, or a sequence diagram for a specific flow, you're probably in the wrong file.

---

## See also

- [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md) — claim schema (canonical)
- [`retrieval-v2.md`](./retrieval-v2.md) — reranker (canonical)
- [`server.md`](./server.md) — relay surface (sibling)
- [`flows/README.md`](./flows/README.md) — flow walkthroughs
- [`mcp-server.md`](./mcp-server.md) — MCP client specifics
- [`tiered-retrieval.md`](./tiered-retrieval.md) — tier rationale + validation
- [`benchmark.md`](./benchmark.md) — retrieval benchmark methodology
