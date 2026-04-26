<!--
Product: TotalReclaw
Version: v1.0 — 2026-04-26
Last updated: 2026-04-26
-->

# TotalReclaw Relay — Architecture (v1)

**Title:** TotalReclaw v1.0 — Relay (server-blind intermediary, AA bundler shim, pair-flow broker)
**Audience:** anyone reading, auditing, or operating the relay; integrators who need to understand what the relay can and cannot do for them.
**Scope:** the relay's architectural responsibilities, surface, and invariants. Concrete schemas (memory claim, reranker, retrieval) live in sibling specs. The relay codebase lives in a separate repo (`totalreclaw-relay`); details specific to deployment, ops runbooks, and CI live there.

This document is the **architectural** spec for the relay. Update it when the relay's role, surface boundaries, or invariants change. Do not duplicate claim-schema or reranker details here.

---

## 1. Role of the relay

The relay is **not** a memory backend in the traditional sense. It is a server-blind intermediary that makes day-to-day reads and writes fast — without ever decrypting anything, ever holding a key, or ever seeing a recovery phrase.

It has exactly four jobs:

1. **Store opaque ciphertext + trapdoors** under per-user `SHA256(authKey)` namespaces.
2. **Return candidates by trapdoor match** for blind retrieval (PostgreSQL GIN index over the blind-index array).
3. **Broker the pair-flow handshake** via ephemeral WebSocket sessions during onboarding.
4. **Shim ERC-4337 bundler calls** so clients can submit UserOps without running their own bundler.

Everything plaintext — extraction, embedding, dedup decisions, reranking, claim schema validation — happens on the client. The relay can be fully compromised without exposing user memories.

For the trust split that justifies this design, see [`architecture.md`](./architecture.md) §1.

---

## 2. Architectural diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    CLIENT (any of: OpenClaw, Hermes,             │
│                    MCP server, NanoClaw, browser pair tab)      │
├─────────────────────────────────────────────────────────────────┤
│  mnemonic                                                       │
│       │                                                         │
│       ├─► HKDF(seed, "totalreclaw-auth-v1")  ─► authKey         │
│       ├─► HKDF(seed, "totalreclaw-enc-v1")   ─► encryptionKey   │
│       ├─► HKDF(seed, "totalreclaw-dedup-v1") ─► dedupKey        │
│       ├─► HKDF(seed, "totalreclaw-lsh-v1")   ─► lshSeed         │
│       └─► BIP-44 m/44'/60'/0'/0/0  ─► secp256k1 → AA address    │
│                                                                 │
│  XChaCha20-Poly1305 encrypt {claim, embedding, metadata}        │
│  Compute trapdoors (token hashes + LSH bucket hashes)           │
│  Compute content_fp = HMAC-SHA256(dedupKey, normalize(text))    │
│  Sign UserOp (ERC-4337)                                         │
└─────────────────────────────────────────────────────────────────┘
          │                              │
          │ HTTPS / Protobuf v=4         │ WebSocket
          ▼                              ▼ (pair handshake only)
┌─────────────────────────────────────────────────────────────────┐
│                          RELAY                                   │
├─────────────────────────────────────────────────────────────────┤
│  Auth      Rate    Request Handlers                             │
│  ┌──────┐ ┌─────┐ ┌────────────────────────────────────────┐    │
│  │SHA256│►│limit│►│ /store /search /update /sync /export   │    │
│  │  fp  │ └─────┘ │ /pair-init /pair-claim (WS)            │    │
│  └──────┘         │ /aa-bundler-shim                        │    │
│                   └────────────────────────────────────────┘    │
│                                       │                          │
│                                       ▼                          │
│                              ┌──────────────────┐                │
│                              │   PostgreSQL     │                │
│                              │ • users           │                │
│                              │ • facts (GIN)    │                │
│                              │ • raw_events     │                │
│                              │ • tombstones     │                │
│                              └──────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
          │                              │
          ▼                              ▼
   Subgraph indexer                ERC-4337 bundler
   (The Graph)                     (sponsored by paymaster)
          │                              │
          ▼                              ▼
   Gnosis (Pro)                    Gnosis / Base Sepolia chain
   Base Sepolia (Free)             (EventfulDataEdge contract)
```

---

## 3. What the relay sees, and what it doesn't

The architectural invariant. Everything else in this doc serves it.

| The relay stores or sees | The relay never sees |
|---|---|
| `SHA256(authKey)` per user | the mnemonic |
| `encryptionKey`-encrypted ciphertext blobs | `encryptionKey` |
| `dedupKey`-derived `content_fp` | `dedupKey` |
| `lshSeed`-derived bucket trapdoors | `lshSeed` (and therefore the LSH hyperplanes) |
| Trapdoor sets at query time | the query text or query embedding |
| ERC-4337 UserOp bytes (signed) | the secp256k1 private key that signed them |
| Pair-flow ECDH pubkeys + ciphertext | the ECDH session key or paired-phrase plaintext |

If the relay's database leaks in full, an attacker recovers ciphertext + trapdoors + signed-UserOp history. They do not recover any plaintext, any embedding, any key, or any recovery phrase. This is by construction.

---

## 4. Wire format — protobuf v=4

The on-the-wire envelope between client and relay is protobuf, schema version 4 (locked rc.20+ as part of the v1 cutover). Architecturally, the envelope carries:

- **Encrypted blob.** `XChaCha20-Poly1305(encryptionKey, plaintext)` where the plaintext is a v1 Memory Claim (see [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md)) plus its embedding and metadata. The relay never parses inside the blob.
- **Blind indices.** SHA-256 of word tokens + SHA-256 of LSH bucket IDs.
- **Content fingerprint.** `HMAC-SHA256(dedupKey, normalize(text))`. Server uses this for exact-duplicate dedup.
- **Decay score, version, source channel, sequence id.** Server-readable scalars for indexing, ordering, and conflict checks. None of these reveal claim content.

**Schema version semantics:** the protobuf envelope version (`v=4`) gates the wire layout. The taxonomy `schema_version` (`"1.0"`, `"1.1"`, etc.) is *inside* the encrypted blob — invisible to the relay, evolved independently, additive on read.

The exhaustive `.proto` field list, RPC method signatures, and codegen targets live in `totalreclaw-relay/proto/` (canonical source). Don't duplicate them in this doc — they drift.

---

## 5. Surface — what endpoints exist and why

| Endpoint | Method | Purpose | Auth |
|---|---|---|---|
| `/health` | GET | Liveness probe | none |
| `/register` | POST | One-time user registration; stores `SHA256(authKey)` + salt | derives auth |
| `/store` | POST | Append encrypted facts; idempotent via `content_fp` | bearer `authKey` |
| `/search` | POST | Trapdoor-matched candidate retrieval | bearer `authKey` |
| `/update` | POST | Version-checked edits to existing facts | bearer `authKey` |
| `/facts/{id}` | DELETE | Soft-delete (tombstone) | bearer `authKey` |
| `/sync` | GET | Delta reconciliation since last `sequence_id` (multi-agent crash recovery) | bearer `authKey` |
| `/export` | GET | Full encrypted dump for offline backup / migration | bearer `authKey` |
| `/pair-init`, `/pair-claim` | WS | Pair-flow ECDH pubkey exchange + transient ciphertext relay | nonce-scoped |
| `/aa-bundler-shim` | POST | Forward signed UserOps to an ERC-4337 bundler; return tx hash | bearer `authKey` |

Health URLs:

- **Staging:** `https://api-staging.totalreclaw.xyz` — auto-QA always targets this
- **Production:** `https://api.totalreclaw.xyz`

---

## 6. Authentication — derived, not configured

Architecturally, there is **no** separate API key, no password reset, no session token. The mnemonic is the credential.

```
client:  authKey       = HKDF-SHA256(seed, "totalreclaw-auth-v1")
client:  Authorization = Bearer <authKey>
server:  if SHA256(authKey) == stored_auth_key_hash → ok
```

Cryptographic separation is structural:

```
mnemonic
   ├─► HKDF(..., "totalreclaw-auth-v1")  ─► authKey         (server sees SHA-256 only)
   ├─► HKDF(..., "totalreclaw-enc-v1")   ─► encryptionKey   (server NEVER derivable)
   └─► HKDF(..., "totalreclaw-dedup-v1") ─► dedupKey        (server NEVER derivable)
```

The relay can verify the user is authentic without learning anything that lets it decrypt their data. There is no "give me my data back" recovery path through the relay; the only recovery path is the mnemonic + the subgraph (see [`architecture.md`](./architecture.md) §2 + §5).

---

## 7. Dedup — content fingerprint at write, sync at reconnect

### Exact-content dedup (HMAC-SHA256)

```
content_fp = HMAC-SHA256(dedupKey, normalize(text))

normalize(text):
  Unicode NFC → lowercase → collapse whitespace → trim → UTF-8 encode
```

Server-side `/store` handler:

```
for each fact in request:
  if exists where (user_id = ?, content_fp = ?, is_active = true):
    skip → add existing id to response.duplicate_ids[]
  else:
    insert → add new id to response.ids[]
return success (partial = full)
```

**Why HMAC, not plain hash:** plain SHA-256 of plaintext would let the relay confirm or deny known content (build a rainbow table of common facts and probe). Keying with `dedupKey` (derived from the mnemonic) means the relay cannot probe — it can only match fingerprints from the same user.

**Why it's idempotent:** same fact pushed twice from a recovered agent or two parallel clients → second push is a no-op, returned via `duplicate_ids`. The store operation is safe to retry without coordination.

**Extraction determinism note:** for fingerprint dedup to be effective, the extraction LLM should run with `temperature=0` so the same input produces the same extracted text. In OpenClaw this is enforced via the `llm-task` plugin tool. NanoClaw inherits Agent SDK defaults until the SDK exposes temperature (open issue: anthropics/claude-agent-sdk-python#273); duplicates that slip through are a storage cost, not a correctness issue.

### Multi-agent sync (sequence_id watermark)

`/sync?since_sequence={seq}` returns facts with `sequence_id > seq`. Architecturally this lets multiple agents (e.g. OpenClaw on desktop + Hermes on a VPS, both paired with the same mnemonic) reconcile after one of them was offline:

```
1. Agent comes online.
2. GET /sync?since_sequence={last_known}.
3. Build server fingerprint set: { content_fp → fact_id }.
4. For each pending local fact:
     if content_fp in server set → skip.
     else → POST /store.
5. Update local last_known_sequence.
```

The store operation is also idempotent server-side (§7 above), so step 4's pre-filter is an optimization, not a correctness requirement.

### Beyond exact dedup

Semantic near-duplicates ("prefers Python" vs "likes Python over JS"), stale contradictions ("lives in Lisbon" vs "moved to Berlin"), and LLM-assisted merging are **client-side** concerns by design — the relay can't see plaintext, so it can't do semantic comparison. See `conflict-resolution.md` for the multi-layer client pipeline.

---

## 8. Database schema (architectural)

The relay uses PostgreSQL. Architecturally, there are four tables; their existence is part of the spec, their column-by-column shapes evolve in the codebase.

| Table | Purpose | Key invariants |
|---|---|---|
| `users` | One row per registered identity | Stores `SHA256(authKey)` + salt only. No phrase, no encryption material. Never deleted (would break recovery). |
| `facts` | Mutable view of active claims | GIN index on `blind_indices` powers retrieval. Unique `(user_id, content_fp) WHERE is_active` enforces exact dedup at the DB level. Monotonic `sequence_id` per user powers `/sync`. |
| `raw_events` | Immutable append-only log of `StoreRequest` payloads | Audit + debugging. Re-derivable view if `facts` corrupts. |
| `tombstones` | Soft-delete records | 30-day retention; lets sync resolve "this was deleted, don't re-add" across agents. |

The canonical DDL (column types, indices, migrations) lives in `totalreclaw-relay/db/`. This doc only fixes the architectural shape.

---

## 9. Search semantics

```sql
SELECT id, encrypted_blob, decay_score
FROM facts
WHERE user_id = ?
  AND is_active = true
  AND decay_score >= ?
  AND blind_indices && ARRAY[?]::text[]   -- GIN index
ORDER BY decay_score DESC
LIMIT ?;
```

The relay's job ends at "return matching ciphertext." Reranking — BM25, cosine, decay, importance, RRF, source-weighting (Tier 1) — is **client-side**, in `totalreclaw-core::reranker`. See [`retrieval-v2.md`](./retrieval-v2.md).

**Performance envelope:** GIN-indexed lookup over 100K facts target < 50 ms. End-to-end search latency target < 150 ms p95 (client + relay + reranking). LSH parameters and candidate-pool sizing live in [`lsh-tuning.md`](./lsh-tuning.md) — they're tunable knobs, not architectural commitments.

---

## 10. Pair-flow brokering

The relay is a transport, not a participant, in the onboarding handshake.

```
1. Browser tab opens pair-init WebSocket → relay assigns ephemeral nonce.
2. Client (OpenClaw / Hermes / MCP) opens pair-claim WebSocket with the same nonce
   and sends its ephemeral ECDH pubkey.
3. Relay forwards client's pubkey to browser tab.
4. Browser AES-256-GCM-encrypts the paired_phrase to client's pubkey,
   sends ciphertext through relay.
5. Relay forwards ciphertext to client.
6. Client decrypts with its ECDH session key, derives all keys from the mnemonic,
   never echoes the phrase back.
7. Pair session closes; relay drops all session state.
```

**Architectural invariants:**

- The relay holds no key. The ECDH session key exists only in the two endpoints.
- AES-GCM is the right primitive here (short-lived session, native WebCrypto, fresh key per session) — distinct from XChaCha20-Poly1305 used for long-term storage. See [`architecture.md`](./architecture.md) §3.
- The phrase never crosses an LLM context. The browser is the only place it lives in plaintext.

The pair handshake landed in relay rc.10+; AES-GCM payload encryption shipped client-side in rc.12+.

---

## 11. ERC-4337 bundler shim

The relay forwards signed UserOps to an ERC-4337 bundler so clients don't have to run one. Architecturally this is a transport convenience — the signature is generated client-side from the secp256k1 key derived from the mnemonic; the relay cannot forge it.

The paymaster sponsors gas (free for users on both Free and Pro tiers). The chain selection — Base Sepolia for Free, Gnosis mainnet for Pro — is part of the UserOp; the relay does not choose it.

For chain choice rationale, paymaster topology, and the contract surface (`EventfulDataEdge`), see [`architecture.md`](./architecture.md) §5 ("Storage tiers") and the on-chain repo.

---

## 12. Subgraph integration

The subgraph (The Graph, AssemblyScript indexer) sits **off** the relay's critical path. Writes go: client → relay → bundler → chain → subgraph indexer (5–30 s lag).

The relay does not query the subgraph. Clients do. As of `totalreclaw-core` rc.22+, a read-after-write primitive in `core` polls the subgraph until the just-written sequence is visible — this absorbs the indexer lag for callers without involving the relay.

Recovery from device loss does not touch the relay at all: mnemonic → subgraph → ciphertext → decrypt locally. This is the failure mode the architecture is designed for.

---

## 13. Operational expectations

The relay is single-tenant per deployment in PoC and small-scale, multi-tenant per `user_id` in staging/production. It is **stateless** in the sense that all durable state is in PostgreSQL — restarts are safe, horizontal scaling is bounded by DB writes.

| Concern | Approach |
|---|---|
| Rate limiting | Per-endpoint per-user (auth-keyed) sliding windows; protects against scraping but cannot prevent legitimate-looking traffic from a stolen `authKey` |
| Replay protection | Timestamp + sequence-id checks on signed payloads; bearer auth alone is not a replay defense |
| MITM | HTTPS everywhere; auth happens over TLS, never plain HTTP |
| DB leak | Recovers ciphertext + trapdoors + sequenced fingerprints; no plaintext, no keys |
| Relay compromise | Same blast radius as DB leak — the relay does not hold keys, so compromise yields ciphertext only |

Concrete thresholds, deploy topology, monitoring dashboards, and runbooks live in the relay repo. They evolve faster than this spec.

---

## 14. Out of scope for this doc

- Memory claim schema → [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md)
- Reranker behavior → [`retrieval-v2.md`](./retrieval-v2.md)
- Trust split, key model, install model, on-chain layer → [`architecture.md`](./architecture.md)
- Step-by-step user flows → [`flows/README.md`](./flows/README.md)
- Conflict resolution beyond `content_fp` dedup → `conflict-resolution.md`
- LSH parameter tuning → [`lsh-tuning.md`](./lsh-tuning.md)
- MCP-specific client surface → [`mcp-server.md`](./mcp-server.md)

If a change to the relay would alter what the relay can see, what it stores, or how it interacts with the chain or the pair flow — update this doc. If it just adjusts a constant, an index, or a column type — update the codebase or the tunable spec.

---

## See also

- [`architecture.md`](./architecture.md) — system architecture (sibling)
- [`memory-taxonomy-v1.md`](./memory-taxonomy-v1.md) — claim schema (canonical)
- [`retrieval-v2.md`](./retrieval-v2.md) — reranker (canonical)
- [`flows/README.md`](./flows/README.md) — flow walkthroughs
- [`conflict-resolution.md`](./conflict-resolution.md) — semantic conflict handling
- [`lsh-tuning.md`](./lsh-tuning.md) — LSH parameter tuning
- [`mcp-server.md`](./mcp-server.md) — MCP-specific surface
