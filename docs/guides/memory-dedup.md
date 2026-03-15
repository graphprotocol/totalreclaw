# Memory Deduplication & Consolidation

Over time, AI agents extract overlapping facts from conversations. "User prefers dark mode" might be stored five times across five sessions. Without dedup, your vault fills with redundant entries, wasting storage, slowing search, and diluting recall quality.

TotalReclaw prevents this at multiple layers -- all operating client-side, preserving the zero-knowledge guarantee.

---

## How It Works

Deduplication happens at three levels, each catching duplicates the previous layer missed:

```
  Conversation
       |
       v
+------------------+
| Fact Extraction  |  LLM extracts atomic facts from conversation
+------------------+
       |
       v
+--------------------------+
| Layer 1: Exact           |  HMAC-SHA256 fingerprint match
| Fingerprint (server)     |  Catches verbatim duplicates
+--------------------------+
       |  (new facts only)
       v
+--------------------------+
| Layer 2: Within-Batch    |  Cosine similarity >= 0.9
| Dedup (client)           |  Catches paraphrases in same extraction
+--------------------------+
       |  (unique facts only)
       v
+--------------------------+
| Layer 3: Store-Time      |  Cosine similarity >= 0.85
| Dedup (client)           |  Catches cross-session duplicates
+--------------------------+
       |  (non-duplicate facts only)
       v
  Encrypted & Stored

                    +---------------------------+
  On demand ------> | Layer 4: Bulk             |
  ("consolidate")   | Consolidation (client)    |
                    | Cosine similarity >= 0.88 |
                    +---------------------------+
```

All semantic comparison (cosine similarity) happens on your device after decryption. The server only ever sees encrypted blobs and blind indices.

---

## Three Layers of Dedup

| Layer | Where | Trigger | Method | Threshold | Scope |
|-------|-------|---------|--------|-----------|-------|
| **Exact Fingerprint** | Server | Every store | HMAC-SHA256 content hash | Exact match | All stored facts |
| **Within-Batch** | Client | Each extraction | Cosine similarity on embeddings | 0.9 | Facts in current batch |
| **Store-Time** | Client | Each store | Cosine similarity on encrypted embeddings | 0.85 | 200 nearest candidates in vault |
| **Bulk Consolidation** | Client | On-demand tool | Cosine similarity clustering | 0.88 | Entire vault |

---

## Store-Time Dedup

This is the primary cross-session dedup layer. It runs automatically every time a memory is about to be stored -- whether from auto-extraction (the `agent_end` hook) or an explicit "remember X" command.

### Flow

```
New fact: "User prefers dark mode for all editors"
       |
       v
+-------------------------------+
| 1. Generate embedding         |  (client-side, from plaintext)
+-------------------------------+
       |
       v
+-------------------------------+
| 2. Generate blind indices     |  (LSH buckets + word trapdoors)
+-------------------------------+
       |
       v
+-------------------------------+
| 3. Search vault via blind     |  Server returns up to 200
|    indices                    |  encrypted candidates
+-------------------------------+
       |
       v
+-------------------------------+
| 4. Decrypt candidates         |  (client-side, AES-256-GCM)
|    Extract their embeddings   |
+-------------------------------+
       |
       v
+-------------------------------+
| 5. Cosine similarity vs       |
|    each candidate             |
+-------------------------------+
       |
       +-- No match >= 0.85 --> STORE new fact normally
       |
       +-- Match found (>= 0.85):
               |
               +-- New importance >= existing --> SUPERSEDE
               |     Soft-delete old fact, store new
               |     (inherits the higher importance score)
               |
               +-- New importance < existing  --> SKIP
                     Don't store the new fact
```

### Examples

| Existing Fact | New Fact | Similarity | Outcome |
|---------------|----------|------------|---------|
| "User prefers dark mode" | "User likes dark themes in editors" | 0.91 | **Supersede** (newer, same importance) |
| "User works at Acme Corp as CTO" | "User works at Acme" | 0.87 | **Skip** (existing has more detail, higher importance) |
| "User prefers PostgreSQL" | "User switched to MySQL" | 0.72 | **Store** (below threshold -- semantically different) |
| "User lives in Berlin" | "User lives in Berlin" | 1.00 | **Blocked earlier** (exact fingerprint match at Layer 1) |

### Design Decisions

- **200 candidate limit** -- Searching the entire vault would be expensive (decrypt + embed + compare). Blind index search narrows to the most relevant 200, which is sufficient to catch near-duplicates since they share LSH buckets.
- **Fail-open** -- If embedding generation fails (model unavailable, context too short), the fact is stored normally. Better to have a duplicate than to lose a memory.
- **Importance-aware** -- A more detailed or more important version of a fact supersedes a less important one. A vague restatement of a high-importance fact is skipped.

---

## Bulk Consolidation

For vaults that accumulated duplicates before store-time dedup was enabled, or after large imports, the `totalreclaw_consolidate` tool performs a one-time cleanup across the entire vault.

### When to Use

- After importing memories from another tool (Mem0, MCP Memory Server)
- If you suspect your vault has grown bloated with redundant facts
- Periodically, as a hygiene measure

### Usage

Ask your agent:

> "Clean up my memories"

Or more specifically:

> "Consolidate my memories with a dry run first"

The agent calls the `totalreclaw_consolidate` tool:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `dry_run` | Preview clusters without deleting | `false` |

### How It Works

1. **Export** -- All memories are fetched from the vault (encrypted).
2. **Decrypt** -- All memories are decrypted client-side.
3. **Cluster** -- Facts are grouped by cosine similarity at threshold 0.88.
4. **Select representative** -- For each cluster, the best fact is kept based on:
   - Highest importance score (first priority)
   - Most recent timestamp (second priority)
   - Longest text (third priority -- more detail is better)
5. **Soft-delete** -- All non-representative facts in each cluster are soft-deleted.

### Dry Run Output

With `dry_run: true`, the tool returns the clusters it would merge without deleting anything:

```
Found 7 clusters with duplicates (23 facts → 7 after consolidation):

Cluster 1 (4 facts → 1):
  KEEP: "User prefers dark mode in all editors and terminals" (importance: 0.8)
  DELETE: "User likes dark mode" (importance: 0.5)
  DELETE: "User prefers dark themes" (importance: 0.5)
  DELETE: "Dark mode is preferred by user" (importance: 0.4)

Cluster 2 (3 facts → 1):
  KEEP: "User works at Acme Corp as CTO since 2024" (importance: 0.9)
  ...
```

### Limitations

- **Centralized mode only** -- Bulk consolidation currently works with the HTTP storage backend. Subgraph support (on-chain soft-delete) is planned.
- **Full vault download** -- The tool downloads and decrypts all memories to perform clustering. For large vaults (10,000+ facts), this may take a minute.

---

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `TOTALRECLAW_STORE_DEDUP` | `true` | Enable/disable store-time dedup. Set to `false` to skip cross-session duplicate detection. |

No configuration is needed for exact fingerprint dedup (always on, server-side) or within-batch dedup (always on during extraction).

Bulk consolidation is an on-demand tool with no persistent configuration -- just call `totalreclaw_consolidate` when needed.

---

## Zero-Knowledge Constraint

All semantic comparison happens on your device:

```
+---------------------+                    +---------------------+
|   YOUR DEVICE       |                    |   SERVER            |
|                     |                    |                     |
| Plaintext facts     |  -- encrypted -->  | Encrypted blobs     |
| Embeddings          |                    | Blind indices       |
| Cosine similarity   |  <-- encrypted --  | Candidate lookup    |
| Dedup decisions     |                    |                     |
+---------------------+                    +---------------------+

The server NEVER sees:
  - Plaintext fact text
  - Plaintext embeddings
  - Similarity scores
  - Which facts are duplicates of which
```

The server participates only in two ways:

1. **Exact fingerprint dedup** -- The server checks HMAC-SHA256 content fingerprints. These are cryptographic hashes derived from the plaintext, but they reveal nothing about the content (the server cannot reverse HMAC-SHA256). This catches exact duplicates without any decryption.

2. **Blind index search** -- When store-time dedup needs candidates to compare against, it queries the server using blind trapdoors (derived from the new fact's LSH buckets). The server returns matching encrypted blobs. It knows which blind indices matched, but not what they represent.

All semantic analysis -- embedding comparison, cosine similarity calculation, importance comparison, supersede/skip decisions -- happens client-side after decryption.

---

## FAQ / Troubleshooting

| Question | Answer |
|----------|--------|
| **How do I know dedup is working?** | Store-time dedup is invisible by design. If you store similar facts across sessions, you won't see duplicates in your export. Run `totalreclaw_export` to inspect your vault. |
| **I see duplicates in my export** | They may be below the 0.85 similarity threshold (semantically distinct enough to keep). Or they were stored before store-time dedup was enabled. Run `totalreclaw_consolidate` to clean them up. |
| **Can dedup accidentally delete important memories?** | Store-time dedup never deletes -- it either supersedes (replaces with a better version) or skips (keeps the existing better version). Bulk consolidation soft-deletes, which is reversible. |
| **Does disabling store-time dedup affect other layers?** | No. `TOTALRECLAW_STORE_DEDUP=false` only disables Layer 3. Exact fingerprint (Layer 1) and within-batch dedup (Layer 2) remain active. |
| **How does dedup work with subgraph mode?** | Store-time dedup works identically in both HTTP and subgraph modes -- candidates are fetched and compared client-side either way. Bulk consolidation is HTTP-only for now. |
| **What if embedding generation fails?** | The system fails open. The fact is stored normally without dedup. This is intentional -- losing a memory is worse than having a duplicate. |
| **Does importing trigger dedup?** | Yes. Imported memories go through exact fingerprint dedup (prevents identical re-imports) and store-time dedup (catches semantic overlaps with existing vault contents). |
| **Can I adjust the similarity thresholds?** | Not currently. The thresholds (0.85 store-time, 0.88 consolidation, 0.9 within-batch) were tuned on real-world memory data to balance recall and precision. Exposing them is on the roadmap. |

---

## Availability

| Layer | OpenClaw Plugin | MCP Server | NanoClaw |
|-------|:---:|:---:|:---:|
| **Exact fingerprint** (Layer 1) | Yes | Yes | Yes |
| **Within-batch dedup** (Layer 2) | Yes | -- | -- |
| **Store-time dedup** (Layer 3) | Yes | Yes | Yes |
| **Bulk consolidation** (Layer 4) | Yes | Yes | Yes (via MCP server) |
| **LLM-guided classification** (Layer 5) | Yes | -- | Yes |

**Notes:**

- **Within-batch dedup** is specific to the OpenClaw plugin's extraction pipeline, which produces multiple facts in a single batch from one conversation turn. MCP and NanoClaw store facts individually via tool calls, so there is no "batch" to deduplicate within.
- **Bulk consolidation** (`totalreclaw_consolidate`) works in **server (HTTP) mode only**. In subgraph mode, the tool is unavailable because there is no batch-delete on-chain equivalent. Store-time dedup supersession (tombstone old + store new) works in both modes.
- **LLM-guided classification** requires lifecycle hooks (extraction context). MCP has no hooks -- it relies on cosine-based dedup only.

### Layer 5: LLM-Guided Classification (OpenClaw + NanoClaw)

The extraction prompt includes existing memories as context. The LLM classifies each extracted fact:

| Action | Meaning | What Happens |
|--------|---------|-------------|
| **ADD** | New fact, no conflict | Stored normally (cosine dedup still applies as safety net) |
| **UPDATE** | Refines or changes an existing fact | Old fact tombstoned, new version stored |
| **DELETE** | Contradicts an existing fact | Old fact tombstoned, nothing new stored |
| **NOOP** | Already captured or not worth storing | Skipped entirely |

This catches what cosine similarity cannot: contradictions ("prefers dark mode" → "switched to light mode") and semantic updates where the meaning changes but embeddings are dissimilar.

**Platform support:**

| Platform | LLM-Guided Dedup | When |
|----------|:---:|--------|
| OpenClaw | Yes | agent_end, before_compaction, before_reset hooks |
| MCP (Claude Desktop, Cursor) | No | No lifecycle hooks -- MCP relies on cosine dedup only |
| NanoClaw | Yes | agent_end (ADD only), pre_compaction (full CRUD) |

### Two Complementary Dedup Approaches

TotalReclaw uses two complementary strategies that operate at different layers:

1. **Cosine-based dedup (storage layer)** -- A mathematical guard that runs on all platforms. Before any fact is stored, its embedding is compared against existing vault contents via cosine similarity. This catches near-duplicates (paraphrases, restatements) regardless of how the fact was produced.

2. **LLM-guided dedup (extraction layer)** -- OpenClaw and NanoClaw. During extraction hooks, the LLM sees existing memories and classifies each new fact as ADD/UPDATE/DELETE/NOOP. This catches contradictions and semantic updates that cosine similarity misses (e.g., "prefers dark mode" → "switched to light mode" have low cosine similarity but are a clear UPDATE).

**In short: cosine catches paraphrases, LLM catches contradictions.** Neither alone is sufficient — together they cover the full spectrum of semantic overlap.

OpenClaw and NanoClaw benefit from both layers; MCP relies on cosine-based dedup alone (which is sufficient for most workloads — contradictions require lifecycle hooks to detect).

---

*TotalReclaw beta v1.0-beta -- [totalreclaw.xyz](https://totalreclaw.xyz)*
