# Architecture

A technical deep dive into how TotalReclaw provides semantic search over fully encrypted memories — without ever seeing the data.

> This document covers the full system design. For a high-level overview, see the [README](../README.md).

<p align="center">
  <img src="assets/architecture.svg" alt="TotalReclaw Architecture" />
</p>

---

## System Overview

TotalReclaw splits trust across three layers. Encryption and search intelligence run entirely on the client. The relay is a blind intermediary that stores and retrieves opaque blobs. The open network provides permanent, verifiable storage and serverless recovery.

```
  Client (device)               Relay Service                 Open Network
 ┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
 │ Key derivation    │        │ Stores encrypted  │        │ Gnosis Chain     │
 │ AES-256-GCM       │──────▸ │ blobs + blind     │──────▸ │ anchors data     │
 │ Embeddings + LSH  │ cipher │ indices           │ anchor │ permanently      │
 │ Re-ranking        │ text   │ GIN index search  │        │                  │
 │ Fact extraction   │◂────── │ Gas sponsorship   │◂────── │ The Graph        │
 └──────────────────┘ results └──────────────────┘ index  │ indexes data     │
                                                          └──────────────────┘
```

Every operation that touches plaintext — encryption, embedding generation, search re-ranking — happens on the device. The relay only sees SHA-256 hashed tokens and AES-256-GCM ciphertext. Even if the relay is fully compromised, memories remain unreadable.

---

## End-to-End Encryption

### Key Derivation

A 12-word BIP-39 recovery phrase is the single root of trust. From it, three independent keys are derived — none of which ever leave the device.

**Step 1 — Seed Generation**

The 12 or 24 words pass through PBKDF2 (2048 rounds) to produce a 512-bit seed. For arbitrary passwords, Argon2id (t=3, m=64 MB, p=4) is used instead — memory-hard to resist GPU brute-force.

**Step 2 — Key Expansion**

The seed is expanded into three 256-bit keys using HKDF-SHA256 with distinct info strings, ensuring cryptographic independence:

```
HKDF(seed, "totalreclaw-auth-key-v1")       → authKey
HKDF(seed, "totalreclaw-encryption-key-v1")  → encryptionKey
HKDF(seed, "openmemory-dedup-v1")            → dedupKey
```

**Step 3 — Key Usage**

| Key | Purpose | What the server sees |
| --- | --- | --- |
| `authKey` | Authenticates with the relay | SHA-256 hash only |
| `encryptionKey` | Encrypts all memory content via AES-256-GCM | Nothing — never transmitted |
| `dedupKey` | Generates HMAC-SHA256 content fingerprints for dedup | Hash only |

### AES-256-GCM Wire Format

Every memory is encrypted as a single blob:

```
[IV: 12 bytes] [Auth Tag: 16 bytes] [Ciphertext: variable]
```

The 128-bit authentication tag ensures both confidentiality and integrity — any tampering is detected on decryption.

### What the relay never sees

- Recovery phrase
- Encryption key or dedup key
- Plaintext memories
- Raw embeddings
- Search query content

---

## Fact Extraction

Memories aren't stored as raw conversations. The agent's LLM extracts atomic facts — concise, self-contained statements scored by importance and categorized by type.

```json
// Example: "I've been vegan for 3 years and I work at Anthropic."
[
  { "text": "User is vegan", "importance": 8, "type": "preference" },
  { "text": "User works at Anthropic", "importance": 7, "type": "fact" }
]
```

Each fact is tagged with an importance score (1–10) that feeds into decay scoring, and a type (`fact`, `preference`, `decision`, `goal`, `episodic`) that aids retrieval context. The extraction prompt filters out credentials, ephemeral context, and third-party data.

Before storage, the client computes a content fingerprint using `HMAC-SHA256(dedupKey, normalized_text)`. The relay checks this fingerprint against existing records — duplicates are rejected without ever comparing plaintext.

---

## Deduplication

Agents tend to store the same information repeatedly. Over weeks, a vault accumulates multiple copies of facts like "User prefers dark mode" — diluting search results and creating contradictions. TotalReclaw uses a 5-layer dedup pipeline that catches duplicates at every stage while preserving the zero-knowledge guarantee.

| Layer | Method | Threshold | Where | Description |
| --- | --- | --- | --- | --- |
| 1 | HMAC-SHA256 fingerprint | Exact match | Server | Catches verbatim duplicates via hash comparison. Cheapest gate — no decryption needed. |
| 2 | Within-batch cosine | >= 0.9 | Client | Removes paraphrases from the same LLM extraction call. Strict threshold avoids false positives. |
| 3 | Store-time cosine | >= 0.85 | Client | Queries 200 nearest candidates via blind indices, decrypts client-side, compares. Higher importance supersedes; lower is skipped. |
| 4 | Bulk consolidation | >= 0.88 | Client | On-demand greedy clustering across the entire vault. Best representative per cluster survives; rest are tombstoned. |
| 5 | LLM judge | Semantic | Client | Evaluates new facts against existing memories. Returns ADD, UPDATE, DELETE, or NOOP. Catches contradictions cosine alone misses. |

**Design decisions:**
- **Fail-open:** if any dedup layer fails, the memory is stored anyway. A duplicate is recoverable; lost data is not.
- **Multi-threshold:** stricter within a single extraction batch (0.9), balanced across sessions (0.85), with slight tolerance for bulk cleanup (0.88).
- **All client-side:** every dedup decision involving plaintext, embeddings, or similarity scores happens on the client.

---

## Semantic Search over Encrypted Data

The core challenge: how do you search data the server can't read?

TotalReclaw splits search into a server-side candidate retrieval step (using blind indices) and a client-side re-ranking step (using decrypted plaintext).

### Step 1 — Embedding and LSH (client-side)

Each fact is embedded using Qwen3-Embedding-0.6B — a multilingual model supporting 100+ languages with 1024-dimensional output. The model runs locally via ONNX Runtime (int8 quantized).

For search queries, a task instruction is prepended to improve retrieval accuracy:

```
Instruct: Given a user query about personal preferences, facts, or history,
retrieve the most relevant stored memories
Query: <user's search query>
```

Facts are embedded without instructions. This asymmetry follows the model's training design and yields a 1–5% accuracy improvement.

The embedding is then projected through 20 independent LSH hash tables, each with 32-bit signatures. Similar embeddings land in the same LSH buckets with high probability. The bucket IDs are SHA-256 hashed before being sent to the relay as blind indices.

| Parameter | Value |
| --- | --- |
| Embedding model | Qwen3-Embedding-0.6B |
| Dimensions | 1024 |
| Languages | 100+ |
| Hash tables | 20 |
| Bits per table | 32 |
| Validated recall | 98.1% Recall@8 on real data |
| Query latency | <10ms (trapdoor generation + GIN lookup) |

### Step 2 — Candidate retrieval (server-side)

The relay's PostgreSQL database uses a GIN (Generalized Inverted Index) over the blind indices array column. The search query becomes an array overlap:

```sql
SELECT * FROM memories
WHERE blind_indices && $trapdoors
  AND decay_score > $threshold
ORDER BY decay_score DESC
LIMIT $candidate_pool;
```

This returns 400–3,000 encrypted candidates in under 15ms. The relay has no idea what concepts it just matched — it only compared opaque SHA-256 hashes.

### Step 3 — Decrypt and re-rank (client-side)

Candidates are decrypted with AES-256-GCM, then scored across four signals:

| Signal | What it measures | Method |
| --- | --- | --- |
| BM25 | Text keyword relevance | Okapi BM25 (k1=1.2, b=0.75), Porter stemming |
| Cosine | Semantic similarity | Dot product of L2-normalized 1024-dim embeddings |
| Decay | Recency | Exponential decay with 7-day half-life: importance x e^(-lambda*t) |
| Importance | User relevance | LLM-assigned score (1–10) from extraction |

Scores are fused via Reciprocal Rank Fusion (RRF) with k=60:

```
RRF_score = sum of 1/(k + rank_i) across all signals
```

RRF is chosen over learned-weight fusion because it's robust without training data and stable across different corpus sizes. The top 8 results are returned to the agent. Optional MMR (Maximal Marginal Relevance) diversification ensures results don't cluster around a single topic.

**End-to-end search latency: <150ms.**

---

## Locality-Sensitive Hashing — How It Works

LSH is the bridge that makes blind search possible. Rather than storing raw vectors on the server (which would leak semantic information), the client projects each embedding into hashed buckets.

Each of the 20 hash tables uses random hyperplanes generated deterministically from the user's HKDF seed. For a given embedding vector, each hyperplane produces a single bit: which side of the plane does the vector fall on? 32 hyperplanes produce a 32-bit signature — a bucket ID. Two similar embeddings will share bucket IDs across multiple tables with high probability.

The bucket IDs are then SHA-256 hashed. The server stores these hashed IDs and can match them (via GIN array overlap) without knowing what semantic concept they represent.

**Why not just download everything?** At 50,000 memories, downloading and decrypting the entire vault for every search is impractical. LSH lets the server do the cheap, fast filtering (narrowing to a few hundred candidates) while the client does the expensive, accurate re-ranking — and neither side has the complete picture.

---

## Open Network Layer

For users who want persistence beyond any single server, TotalReclaw anchors encrypted memories to a decentralized network.

### Gnosis Chain

A minimal smart contract (`EventfulDataEdge`) with a single `fallback()` function that emits `Log(msg.data)`. No storage, no access control, no state — just a permissionless event emitter. Cost: approximately 5,300 gas per 256-byte fact.

### ERC-4337 Smart Accounts

Each user gets a counterfactual smart account derived from their BIP-39 mnemonic (`m/44'/60'/0'/0/0`). Writes are sponsored by a Paymaster (gasless for users). The same mnemonic recovers the same account and decryption keys on any device.

### The Graph Subgraph

An AssemblyScript subgraph indexes all `Log` events by smart account address. Recovery is straightforward: query the subgraph for the account address, download all events, decrypt with the mnemonic-derived key. No server required.

### Why this matters

The relay handles fast day-to-day reads and writes. The network layer is what turns portability and data ownership from promises into structural guarantees. If TotalReclaw shuts down, if the relay goes offline, if the company gets acquired — the data still exists on a global, open network that no single entity controls. A recovery phrase and the subgraph are all that's needed.

---

## Key Parameters Reference

| Component | Detail |
| --- | --- |
| Encryption | AES-256-GCM — 256-bit key, 96-bit IV, 128-bit auth tag |
| KDF (password) | Argon2id — t=3, m=64 MB, p=4 |
| KDF (mnemonic) | BIP-39 PBKDF2 — 2048 rounds → HKDF-SHA256 expansion |
| Embeddings | Qwen3-Embedding-0.6B — 1024 dimensions, ONNX int8 quantized, 100+ languages |
| LSH | 20 hash tables, 32 bits per table |
| Blind indices | SHA-256 of word tokens + LSH bucket IDs |
| Content dedup | HMAC-SHA256 fingerprint |
| Search latency | <150ms end-to-end |
| BM25 | k1=1.2, b=0.75, Porter stemming, English stop words |
| RRF constant | k=60 |
| Decay half-life | 7 days (configurable) |
| On-chain contract | EventfulDataEdge on Gnosis Chain |
| Account abstraction | ERC-4337 with Paymaster gas sponsorship |
| Subgraph | The Graph (AssemblyScript indexer) |
