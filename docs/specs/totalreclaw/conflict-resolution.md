<!--
Product: TotalReclaw
Formerly: tech specs/v0.3 (grok)/TS v0.3.2: Multi-Agent Conflict Resolution.md
Version: 0.3.2
Last updated: 2026-02-24
-->

# Technical Specification v0.3.2: Multi-Agent Conflict Resolution

**Addendum to:** TS v0.3.1 (Server-side PoC with Auth), TS v0.3 (Subgraphs and Account Abstraction)
**Version:** 0.3.2
**Date:** February 24, 2026
**Author:** Claude (with Pedro Diogo)
**Status:** Draft — Pending Review
**Applies to:** MVP (PostgreSQL PoC) and Production (Subgraph/EventfulDataEdge)

---

## 0. Document Structure

This spec is organized in two parts:

1. **Rationale (Sections 1-4):** The problem analysis, constraints, rejected alternatives, and reasoning that led to the proposed solution. Included so that future agents and contributors understand *why* these choices were made, not just *what* they are.

2. **Specification (Sections 5-10):** The formal protocol, data model changes, server behavior, client behavior, and subgraph mapping changes.

---

# PART I: RATIONALE

## 1. Problem Statement

TotalReclaw supports multiple AI agents operating on behalf of a single user. Each agent independently extracts facts from conversations and stores them in the shared memory vault. The current spec (v0.3.1 §8) handles one narrow conflict case — two agents updating the *same fact ID* simultaneously — via optimistic locking. It does not address the broader and more common scenario:

**An agent goes offline, accumulates local memories, and then reconnects to push them — after another agent has already stored overlapping, contradictory, or semantically equivalent facts during the offline period.**

### 1.1 Concrete Scenario

```
Day 1, 09:00  Agent A and Agent B both active, synced to server seq=100
Day 1, 09:30  Agent B loses connectivity (laptop closes, Docker stops, etc.)
Day 1, 10:00  Agent A extracts: "User prefers Python over JavaScript"    → stored, seq=101
Day 1, 11:00  Agent A extracts: "User moved to Berlin last week"         → stored, seq=102
Day 1, 14:00  Agent B comes online with local queue:
               - "User prefers Python" (extracted at 09:15 from same conversation)
               - "User lives in Lisbon" (extracted at 09:20 from older conversation)
               - "User's dog is named Max" (genuinely new, extracted at 09:25)
```

Agent B's queue contains:
1. An **exact duplicate** ("prefers Python" — same information, possibly different wording)
2. A **stale contradiction** ("lives in Lisbon" vs the newer "moved to Berlin")
3. A **genuinely new fact** ("dog named Max")

The system must handle all three cases without the server ever seeing plaintext.

### 1.2 Failure Modes if Unaddressed

| Failure | Impact | Frequency |
|---------|--------|-----------|
| Duplicate facts accumulate | Bloated storage, confused retrieval ranking, wasted blind index space | HIGH — most common case |
| Stale facts overwrite current ones | Agent acts on outdated information, user loses trust | MEDIUM — happens with location, preferences, status changes |
| Contradictory facts coexist | Agent retrieves both "lives in Lisbon" and "moved to Berlin", gives confused answers | MEDIUM |
| Silent data loss | Newer fact silently replaces richer older fact | LOW but catastrophic for trust |

---

## 2. Constraints

### 2.1 E2EE Invariant

The server (PostgreSQL in MVP, subgraph in production) MUST NOT see plaintext content. This is TotalReclaw's core value proposition and is non-negotiable.

**What the server CAN see** (already exposed in v0.3.1):
- `fact_id` (UUIDv7, time-sortable)
- `user_id` / `owner` (Smart Account address)
- `encrypted_blob` (opaque ciphertext)
- `blind_indices[]` (LSH bucket hashes — already designed to be server-visible)
- `version` (integer counter)
- `timestamp` (ISO 8601, when fact was created)
- `decay_score` (float)
- `is_active` (boolean)
- `source` (conversation | pre_compaction | explicit)

**What the server MUST NOT see:**
- Plaintext fact text
- Plaintext entities or relations
- Embedding vectors
- Any information that reveals semantic content beyond what blind indices already leak

### 2.2 Append-Only Constraint (Subgraph)

In the production subgraph architecture, writes go on-chain via `EventfulDataEdge.fallback()`. The smart contract emits events — it does not validate, reject, or deduplicate. Every write succeeds. Conflict resolution cannot happen at write time.

### 2.3 Client Heterogeneity

Agents may be different software (OpenClaw, Claude Desktop, custom MCP clients). The protocol must work with any client that implements the TotalReclaw MCP/skill interface. We cannot assume agents coordinate with each other directly — they only share the server as a common state.

### 2.4 Latency Budget

Reconnection sync should not block the agent from operating. The user should not notice a multi-second pause when an agent comes online. Target: <500ms for the sync protocol itself (excluding LLM-assisted merge, which can happen asynchronously).

---

## 3. Rejected Alternatives

### 3.1 Vector Clocks

**Considered:** Assign each agent a slot in a vector clock. Facts carry the full vector on every write. Conflicts detected when vectors are concurrent (neither dominates).

**Rejected because:**
- Requires every agent to know about every other agent upfront (or dynamically grow the vector)
- Vector clock metadata grows with agent count, inflating every fact
- The server would need to compare vectors, which means understanding the sync protocol deeply — over-engineering for a PoC
- Most conflicts in our domain are *semantic* (same meaning, different words), not *causal* (concurrent writes to the same register). Vector clocks solve causal ordering, but that's not the hard part here.

### 3.2 CRDTs (Conflict-Free Replicated Data Types)

**Considered:** Model facts as a G-Set (grow-only set) with tombstones for deletion. Merges are set union — inherently conflict-free.

**Rejected because:**
- CRDTs work when the "merge" operation is well-defined. For text facts, merge = set union just means "keep both," which is exactly the duplicate problem we're trying to solve
- Semantic deduplication ("prefers Python" vs "likes Python more than JS") is not a lattice operation — it requires understanding content
- CRDTs add implementation complexity without solving the core problem (semantic duplicates under E2EE)

### 3.3 Server-Side Embedding Comparison

**Considered:** Send embeddings to the server for duplicate detection via cosine similarity.

**Rejected because:** This directly violates the server-blind guarantee. Embeddings reveal semantic content — a server with embeddings can cluster, infer topics, and partially reconstruct meaning. This is explicitly forbidden by the architecture.

### 3.4 Full Client-Side Reconciliation Only (No Server Help)

**Considered:** The server is completely dumb. On reconnect, the client pulls ALL facts, decrypts everything, deduplicates locally, pushes corrections.

**Rejected as the SOLE approach because:**
- Pulling and decrypting all facts is O(n) in memory count. At 100K+ facts, this is slow and wasteful
- Every reconnection triggers a full scan — terrible for latency
- However, this IS the final layer (Layer 4) in our solution — it's necessary but not sufficient alone

### 3.5 Deterministic Fact IDs (Content-Addressed Storage)

**Considered:** Instead of UUIDv7, derive fact IDs from content: `fact_id = SHA256(plaintext)`. Same content = same ID = automatic dedup.

**Rejected because:**
- Reveals that two facts have identical content (the server sees the same ID appear twice). This leaks information under E2EE: the server learns "these two writes from different sessions had the same content" without knowing what that content is. For a privacy-focused system, this is an unacceptable metadata leak at the ID level.
- Doesn't handle semantic duplicates (different wording, same meaning = different IDs)
- We achieve the same exact-dedup benefit via `content_fingerprint` (Section 5.2) which is a separate, explicitly-purposed field — making the privacy tradeoff visible and auditable rather than hidden in the ID scheme.

---

## 4. Design Reasoning: How We Arrived at the Layered Solution

### 4.1 Starting Observation

Conflict resolution requires understanding content. The server-blind guarantee forbids the server from understanding content. Therefore, **the final arbiter of any conflict must be the client.** This is inescapable.

But making the client do ALL the work (rejected alternative 3.4) is inefficient. The question becomes: **what metadata can we expose to the server to reduce the client's workload without breaking the E2EE guarantee?**

### 4.2 Classifying Conflicts by Detectability

We categorized the types of conflicts that can occur and asked: which can be detected without seeing plaintext?

| Conflict Type | Example | Detectable Server-Side? | How? |
|---------------|---------|------------------------|------|
| Exact duplicate | Same text, maybe different whitespace | YES | Content fingerprint (HMAC) |
| Same-source duplicate | Two agents processed same conversation | YES | Source event ID matching |
| Semantic duplicate | "prefers Python" vs "likes Python over JS" | PARTIALLY | Blind index overlap (LSH) |
| Stale contradiction | "lives in Lisbon" vs "moved to Berlin" | NO | Requires decryption + understanding |
| Complementary facts | "has a dog" + "dog named Max" | NO | Requires decryption + understanding |

This classification directly produced the layered architecture:

- **Layer 1** catches exact duplicates server-side (cheapest, most common)
- **Layer 2** provides efficient delta sync (reduces what the client needs to process)
- **Layer 3** flags semantic near-duplicates server-side (reduces client scan scope)
- **Layer 4** handles everything else client-side (the unavoidable final layer)

### 4.3 The Content Fingerprint Insight

The key insight for Layer 1: we can compute a deterministic hash of the plaintext *using the user's encryption key* as the HMAC key:

```
content_fingerprint = HMAC-SHA256(encryption_key, normalize(plaintext))
```

This gives us:
- **Determinism:** Same content from any agent produces the same fingerprint
- **E2EE safety:** Without the encryption key, the fingerprint is indistinguishable from random. The server learns "these two writes have the same fingerprint" but cannot reverse it to content. This is the same class of metadata leak as blind indices (which we already accept).
- **Exact dedup:** The server can reject exact duplicates without decrypting

The privacy tradeoff is minimal and already accepted by the architecture: blind indices ALREADY tell the server which facts are "about similar topics." A content fingerprint reveals slightly less (only exact matches, not semantic similarity). If the blind index leak is acceptable, the fingerprint leak certainly is.

### 4.4 Why Blind Index Overlap Works for Layer 3

Blind indices are LSH bucket hashes. Two facts about "User prefers Python" will hash into many of the same LSH buckets even if the exact wording differs. The overlap ratio of blind indices between two facts correlates with semantic similarity.

We already have a GIN index on `blind_indices[]` for search. Checking overlap is a natural extension:

```sql
blind_indices && $candidate_indices  -- already supported by GIN
```

The server doesn't learn *what* the facts say — only that they share LSH buckets (which it already knows, since it stores them for search). No new information is leaked.

### 4.5 Adapting for the Subgraph

The append-only constraint of the subgraph means Layers 1 and 3 cannot *reject* writes — they can only *classify* them after the fact. This shifts the architecture:

- **MVP (PostgreSQL):** Layers 1 and 3 are *preventive* (reject at write time)
- **Subgraph:** Layers 1 and 3 are *corrective* (classify after indexing, mark duplicates)

Both converge to the same end state. The subgraph just takes a slightly longer path to get there, with a small cost in extra on-chain events.

We also introduce a new event type, `SUPERSEDE`, to handle the case where a client resolves a conflict and needs to tell the subgraph "fact X replaces facts Y and Z." This is the subgraph-native equivalent of the MVP's UPDATE-with-version-check.

---

# PART II: SPECIFICATION

## 5. Data Model Changes

### 5.1 New Fields on `facts` Table (MVP)

```sql
ALTER TABLE facts ADD COLUMN sequence_id    BIGSERIAL;   -- monotonic, gap-free per user
ALTER TABLE facts ADD COLUMN content_fp     TEXT;         -- HMAC-SHA256 fingerprint
ALTER TABLE facts ADD COLUMN agent_id       TEXT;         -- which agent created this fact
ALTER TABLE facts ADD COLUMN source_event_id TEXT;        -- ID of the conversation/event that triggered extraction
ALTER TABLE facts ADD COLUMN created_at     TIMESTAMPTZ DEFAULT NOW();  -- when fact was learned (not synced)
ALTER TABLE facts ADD COLUMN synced_at      TIMESTAMPTZ DEFAULT NOW();  -- when fact reached server

CREATE UNIQUE INDEX idx_facts_user_fp ON facts(user_id, content_fp) WHERE is_active = true;
CREATE INDEX idx_facts_user_seq ON facts(user_id, sequence_id);
CREATE INDEX idx_facts_user_source ON facts(user_id, source_event_id);
```

### 5.2 Content Fingerprint Computation (Client)

```
content_fingerprint = HMAC-SHA256(
    key  = encryption_key,          -- derived from master password/seed
    data = normalize(plaintext)
)

normalize(text):
    1. Unicode NFC normalization
    2. Lowercase
    3. Collapse whitespace (multiple spaces/newlines → single space)
    4. Trim leading/trailing whitespace
    5. UTF-8 encode
```

The fingerprint is computed client-side and sent alongside the encrypted blob. The server stores it but cannot reverse it.

### 5.3 New Protobuf Fields

```proto
message StoreRequest {
  string user_id = 1;
  string fact_id = 2;               // UUIDv7
  bytes  encrypted_blob = 3;
  repeated string blind_indices = 4;
  float  decay_score = 5;
  string source = 6;
  int32  version = 7;
  // --- NEW in v0.3.2 ---
  string content_fp = 8;            // HMAC-SHA256 fingerprint
  string agent_id = 9;              // identifier of the storing agent
  string source_event_id = 10;      // conversation/event that triggered this fact
  string created_at = 11;           // ISO 8601 — when fact was learned
}

message StoreResponse {
  bool   success = 1;
  string fact_id = 2;
  int64  sequence_id = 3;           // NEW: server-assigned sequence
  ErrorCode error = 4;
  // --- NEW in v0.3.2 ---
  DuplicateInfo duplicate_info = 5; // populated if DUPLICATE or POTENTIAL_DUPLICATE
}

message DuplicateInfo {
  DuplicateType type = 1;
  repeated string candidate_fact_ids = 2;   // IDs of existing similar facts
  repeated bytes  candidate_blobs = 3;      // encrypted blobs for client inspection
  float overlap_ratio = 4;                  // blind index overlap (0.0-1.0)
}

enum DuplicateType {
  NONE = 0;
  EXACT = 1;                // content_fp match — server auto-rejected
  SAME_SOURCE = 2;          // source_event_id match — likely duplicate
  POTENTIAL_SEMANTIC = 3;   // blind index overlap > threshold
}

enum ErrorCode {
  OK = 0;
  INVALID_REQUEST = 1;
  UNAUTHORIZED = 2;
  RATE_LIMITED = 3;
  NOT_FOUND = 4;
  INTERNAL_ERROR = 5;
  VERSION_CONFLICT = 6;
  DUPLICATE_CONTENT = 7;    // NEW: exact fingerprint match
  POTENTIAL_DUPLICATE = 8;  // NEW: high blind index overlap
}
```

### 5.4 New Sync Endpoint

```proto
message SyncRequest {
  string user_id = 1;
  int64  since_sequence = 2;        // agent's last known sequence_id
  int32  limit = 3;                 // max facts to return (default 1000)
}

message SyncResponse {
  repeated SyncedFact facts = 1;
  int64  latest_sequence = 2;       // current highest sequence_id
  bool   has_more = 3;              // true if more facts available (paginate)
}

message SyncedFact {
  string fact_id = 1;
  int64  sequence_id = 2;
  bytes  encrypted_blob = 3;
  repeated string blind_indices = 4;
  float  decay_score = 5;
  string content_fp = 6;
  string agent_id = 7;
  bool   is_active = 8;
  int32  version = 9;
  string created_at = 10;
  string synced_at = 11;
}
```

### 5.5 Subgraph Event Types

```proto
// Wraps all events sent to EventfulDataEdge
message TotalReclawEvent {
  EventType type = 1;
  string    fact_id = 2;
  bytes     encrypted_blob = 3;
  repeated string blind_indices = 4;
  float     decay_score = 5;
  string    content_fp = 6;
  string    agent_id = 7;
  string    created_at = 8;
  // For SUPERSEDE events:
  repeated string superseded_fact_ids = 9;  // facts being replaced
  string          replacement_fact_id = 10; // the merged/replacement fact
}

enum EventType {
  STORE = 0;
  UPDATE = 1;
  DELETE = 2;
  SUPERSEDE = 3;   // NEW: "fact X replaces facts Y and Z"
}
```

---

## 6. Protocol: MVP (PostgreSQL)

### 6.1 Store with Dedup (Server)

```
POST /store

Server receives StoreRequest:

1. AUTH CHECK (existing v0.3.1 §5)

2. LAYER 1 — Exact Fingerprint Check:
   SELECT id FROM facts
   WHERE user_id = $uid AND content_fp = $fp AND is_active = true;

   IF found → return ErrorCode.DUPLICATE_CONTENT
              (fact already exists, no action needed)

3. LAYER 1b — Same Source Check:
   IF source_event_id IS NOT NULL:
     SELECT id FROM facts
     WHERE user_id = $uid AND source_event_id = $source_event_id AND is_active = true;

     IF found → return DuplicateInfo { type: SAME_SOURCE, candidate_fact_ids: [...] }
                (same conversation already processed — client decides)

4. LAYER 3 — Blind Index Overlap Check:
   SELECT id, encrypted_blob, blind_indices,
          array_length(
            (SELECT array_agg(x) FROM unnest(blind_indices) x
             WHERE x = ANY($new_indices)),
            1
          )::float / GREATEST(array_length(blind_indices, 1), 1) AS overlap
   FROM facts
   WHERE user_id = $uid
     AND is_active = true
     AND blind_indices && $new_indices     -- GIN index hit
   ORDER BY overlap DESC
   LIMIT 5;

   IF any result has overlap > 0.6:
     return ErrorCode.POTENTIAL_DUPLICATE with DuplicateInfo {
       type: POTENTIAL_SEMANTIC,
       candidate_fact_ids, candidate_blobs, overlap_ratio
     }

5. NO CONFLICT → Insert fact, assign sequence_id, return success.
```

### 6.2 Sync on Reconnect (Client)

```
When agent comes online after being offline:

1. PULL DELTA:
   GET /sync?since={last_known_sequence}&limit=1000
   Repeat with pagination until has_more = false.

2. BUILD SERVER STATE MAP:
   server_fps = { content_fp → fact_id } for all synced facts
   server_sources = { source_event_id → fact_id } for all synced facts

3. FOR EACH local pending fact:

   a. EXACT DEDUP (Layer 1, client-side pre-check):
      IF fact.content_fp IN server_fps → SKIP (already on server)

   b. SAME SOURCE DEDUP:
      IF fact.source_event_id IN server_sources → SKIP (same conversation already processed)

   c. SEMANTIC DEDUP (Layer 3 + 4):
      POST /store with fact
      IF response = POTENTIAL_DUPLICATE:
        Decrypt candidate_blobs
        Compute embedding similarity between local fact and each candidate
        IF similarity > 0.85:
          Compare timestamps:
            IF server fact is newer → SKIP local (server wins)
            IF local fact is newer → UPDATE server fact with local version
            IF timestamps equal → LLM-assisted merge, push merged version
        ELSE:
          Genuinely different fact → retry POST /store with force=true flag

   d. NO CONFLICT → POST /store succeeds, record new sequence_id

4. UPDATE WATERMARK to latest_sequence from SyncResponse.
```

### 6.3 Conflict Merge Rules

When the client must merge two conflicting facts (Step 3c above):

| Signal | Rule |
|--------|------|
| `created_at` timestamp | More recent wins (information is newer) |
| `importance` score | Higher importance wins (user explicitly marked it) |
| Entities/relations | Union (keep all entities from both facts) |
| Contradictory text | LLM-assisted merge with prompt: "Fact A says X (learned at T1). Fact B says Y (learned at T2). Which is current? Produce a single merged fact." |

**Critical rule:** `created_at` (when the information was learned) takes precedence over `synced_at` (when it reached the server). An agent that learned something at 3pm but synced at 5pm still has "newer" information than something synced at 4pm but learned at 2pm.

---

## 7. Protocol: Subgraph (Append-Only)

### 7.1 Write Path (No Rejection)

All events go on-chain. The smart contract does not validate or deduplicate:

```solidity
// EventfulDataEdge.sol — unchanged from v0.3
fallback() external payable {
    emit Log(msg.sender, msg.data);
}
```

Every agent's write succeeds. Duplicates land on-chain. This is by design — the cost is negligible (~$0.0002 per event) and the subgraph handles classification.

### 7.2 Subgraph Mapping (Classification)

```typescript
// mapping.ts (AssemblyScript)

export function handleLog(event: Log): void {
  let e = decodeProtobuf<TotalReclawEvent>(event.params.data);
  let owner = event.transaction.from.toHex();  // Smart Account address

  if (e.type == EventType.STORE) {
    handleStore(e, owner, event);
  } else if (e.type == EventType.UPDATE) {
    handleUpdate(e, owner);
  } else if (e.type == EventType.DELETE) {
    handleDelete(e, owner);
  } else if (e.type == EventType.SUPERSEDE) {
    handleSupersede(e, owner);
  }
}

function handleStore(e: TotalReclawEvent, owner: string, event: Log): void {
  // Layer 1: Check content fingerprint
  let fpKey = owner + ":" + e.content_fp;
  let existing = FingerprintEntity.load(fpKey);

  if (existing != null) {
    // Exact duplicate — record it but mark as duplicate
    let fact = new FactEntity(e.fact_id);
    fact.owner = owner;
    fact.encryptedBlob = e.encrypted_blob;
    fact.isActive = false;                    // inactive — duplicate
    fact.isDuplicate = true;
    fact.duplicateOf = existing.factId;
    fact.blockNumber = event.block.number;
    fact.save();

    // Log the duplicate event for audit
    let dup = new DuplicateLogEntity(event.transaction.hash.toHex());
    dup.originalFactId = existing.factId;
    dup.duplicateFactId = e.fact_id;
    dup.detectionMethod = "content_fingerprint";
    dup.save();

    return;
  }

  // No exact duplicate — create active fact
  let fact = new FactEntity(e.fact_id);
  fact.owner = owner;
  fact.encryptedBlob = e.encrypted_blob;
  fact.blindIndices = e.blind_indices;
  fact.decayScore = e.decay_score;
  fact.contentFp = e.content_fp;
  fact.agentId = e.agent_id;
  fact.isActive = true;
  fact.isDuplicate = false;
  fact.blockNumber = event.block.number;
  fact.createdAt = e.created_at;
  fact.save();

  // Index fingerprint for future dedup
  let fp = new FingerprintEntity(fpKey);
  fp.factId = e.fact_id;
  fp.save();
}

function handleSupersede(e: TotalReclawEvent, owner: string): void {
  // Client resolved a conflict — mark old facts as superseded
  for (let i = 0; i < e.superseded_fact_ids.length; i++) {
    let old = FactEntity.load(e.superseded_fact_ids[i]);
    if (old != null && old.owner == owner) {
      old.isActive = false;
      old.supersededBy = e.replacement_fact_id;
      old.save();
    }
  }

  // The replacement fact was already stored via a separate STORE event
  // (or is included in this event's encrypted_blob)
}
```

### 7.3 Client Reconciliation (Subgraph)

```
When agent comes online after being offline:

1. QUERY SUBGRAPH:
   query {
     facts(where: { owner: $address, isActive: true }, orderBy: blockNumber) {
       id, encryptedBlob, blindIndices, contentFp, agentId, createdAt
     }
   }

2. DECRYPT all active facts locally.

3. FOR EACH local pending fact:
   a. Check content_fp against subgraph facts → match? Don't emit STORE event.
   b. Compute embedding similarity → near-duplicate? Resolve locally.
   c. Genuinely new → emit STORE event to chain.

4. FOR ANY resolved conflicts:
   a. Compute merged fact.
   b. Emit STORE event for merged fact.
   c. Emit SUPERSEDE event: { superseded_fact_ids: [old1, old2], replacement_fact_id: merged }

5. Subgraph processes events → deactivates old facts, activates merged fact.
```

---

## 8. Agent Identity and Session Tracking

### 8.1 Agent ID

Each agent generates a persistent identifier on first launch:

```
agent_id = "agent-" + SHA256(installation_path + machine_id + seed)[:12]
```

This ID is:
- Deterministic per installation (same agent on same machine = same ID)
- Different across machines (same seed on different machines = different IDs)
- Not secret (included in plaintext metadata alongside encrypted blob)
- Useful for: distinguishing which agent wrote what, debugging, audit

### 8.2 Local Pending Queue

Each agent maintains a local queue of facts not yet synced:

```
~/.totalreclaw/pending/
  {fact_id}.json  — encrypted blob + metadata, ready to push
```

On successful sync, the file is deleted. On conflict, the file is updated with resolution metadata and retried.

### 8.3 Watermark Persistence

```
~/.totalreclaw/sync_state.json
{
  "user_id": "...",
  "agent_id": "agent-a1b2c3d4e5f6",
  "last_sequence": 4721,           // MVP: PostgreSQL sequence_id
  "last_block": 18234567,          // Subgraph: Ethereum block number
  "last_sync_at": "2026-02-24T14:30:00Z",
  "pending_count": 3
}
```

---

## 9. Privacy Analysis

### 9.1 New Information Exposed to Server

| Field | What server learns | Acceptable? | Justification |
|-------|-------------------|-------------|---------------|
| `content_fp` | "These two facts have identical content" | YES | Same class as blind indices. Server already knows facts are "similar" via LSH overlap. Fingerprint only reveals *exact* matches — strictly less information than blind indices reveal about *approximate* matches. |
| `agent_id` | "This fact came from agent X" | YES | No content information. Useful for debugging. User can opt out by sending a constant agent_id. |
| `source_event_id` | "These facts came from the same conversation" | YES | No content information. Reveals conversation boundaries, which the server could already infer from temporal clustering of stores. |
| `sequence_id` | "This is the Nth fact stored" | YES | Ordering metadata. Server already knows insertion order from database sequence. |

### 9.2 What the Server Still Cannot Do

- Determine what any fact says
- Cluster facts by topic (beyond what blind indices already allow)
- Determine if two facts contradict each other
- Read, summarize, or analyze the user's memories
- Determine the content of any conversation

### 9.3 Differential Privacy Consideration

The `content_fp` field leaks one bit of information per pair of facts: "same content or not." For a user with N facts, this is N*(N-1)/2 bits in the worst case. However:
- The server already stores blind indices, which leak approximate similarity for ALL pairs
- Content fingerprint is strictly less revealing (exact match only, no similarity gradient)
- An attacker with access to the server cannot exploit fingerprints without the encryption key

The privacy cost is negligible relative to what blind indices already expose.

---

## 10. Implementation Priority

### 10.1 MVP (PostgreSQL) — Implement in Order

| Step | Layer | Effort | Impact |
|------|-------|--------|--------|
| 1. Add `content_fp`, `sequence_id`, `agent_id` columns | 1, 2 | 1h | Schema foundation |
| 2. Fingerprint check in `/store` handler | 1 | 2h | Catches ~70% of duplicates |
| 3. `GET /sync` endpoint | 2 | 3h | Enables reconnection protocol |
| 4. Blind index overlap check in `/store` | 3 | 3h | Catches semantic duplicates |
| 5. Client-side sync protocol | 4 | 6h | Full reconciliation |
| 6. LLM-assisted merge logic | 4 | 4h | Handles contradictions |

**Total: ~19 hours. Layers 1-2 alone (6 hours) solve the most common cases.**

### 10.2 Subgraph — Implement After MVP Validation

| Step | Layer | Effort | Impact |
|------|-------|--------|--------|
| 1. Add `content_fp`, `agent_id` to Protobuf | 1 | 1h | Schema foundation |
| 2. `FingerprintEntity` + dedup in mapping | 1 | 3h | Subgraph-level exact dedup |
| 3. `SUPERSEDE` event type + mapping | — | 4h | Enables conflict resolution |
| 4. Client reconciliation against subgraph | 4 | 6h | Full reconciliation |

**Total: ~14 hours (reuses client logic from MVP).**

---

## 11. Open Questions

| Question | Impact | Recommendation |
|----------|--------|----------------|
| Should `POTENTIAL_DUPLICATE` be a hard rejection or a soft warning? | UX: hard rejection forces client to handle; soft warning allows "push anyway" | **Soft warning** for MVP. Return candidates, let client decide. The `force=true` flag overrides. |
| Should the blind index overlap threshold (0.6) be configurable per user? | Power users may want stricter/looser dedup | **No** for MVP. Hardcode at 0.6, tune based on benchmark data. |
| Should the content fingerprint use the encryption key or a separate dedup key? | If encryption key rotates, fingerprints break | **Separate HKDF-derived key**: `dedup_key = HKDF(master, salt, "dedup")`. Survives encryption key rotation. |
| How long should duplicate/superseded facts be retained before hard deletion? | Storage cost vs audit trail | **90 days** (matches existing tombstone TTL in v0.3.1 §7). |
| Should agents be notified of duplicates from other agents? | Helps agents learn what's already stored | **Yes** — the `/sync` response includes all facts, so agents naturally learn what exists. No extra notification needed. |

---

## Appendix A: Sequence Diagram — Reconnection Flow (MVP)

```
Agent B                          Server                         Agent A
   |                               |                               |
   |  [OFFLINE since seq=100]      |                               |
   |                               |  ← store(fact, seq=101)       |
   |                               |  ← store(fact, seq=102)       |
   |                               |                               |
   |  [COMES ONLINE]               |                               |
   |                               |                               |
   |  GET /sync?since=100 ──────→  |                               |
   |  ←── facts 101,102 ─────────  |                               |
   |                               |                               |
   |  [Decrypt 101, 102]           |                               |
   |  [Compare with local queue]   |                               |
   |                               |                               |
   |  [Local fact X: fp matches    |                               |
   |   server fact 101 → SKIP]     |                               |
   |                               |                               |
   |  [Local fact Y: contradicts   |                               |
   |   server fact 102, but 102    |                               |
   |   has newer created_at        |                               |
   |   → SKIP local, server wins]  |                               |
   |                               |                               |
   |  [Local fact Z: genuinely     |                               |
   |   new, no match]              |                               |
   |  POST /store(Z) ───────────→  |                               |
   |  ←── success, seq=103 ──────  |                               |
   |                               |                               |
   |  [Update watermark to 103]    |                               |
   |                               |                               |
```

## Appendix B: Sequence Diagram — Reconnection Flow (Subgraph)

```
Agent B                     Chain / Subgraph                  Agent A
   |                               |                               |
   |  [OFFLINE]                    |                               |
   |                               |  ← STORE event (block 1001)   |
   |                               |  ← STORE event (block 1002)   |
   |                               |                               |
   |  [COMES ONLINE]               |                               |
   |                               |                               |
   |  GraphQL query ────────────→  |                               |
   |  (facts where isActive=true)  |                               |
   |  ←── all active facts ──────  |                               |
   |                               |                               |
   |  [Decrypt all]                |                               |
   |  [Compare with local queue]   |                               |
   |                               |                               |
   |  [Fact X: fp match → skip]    |                               |
   |  [Fact Y: contradiction →     |                               |
   |   merge locally]              |                               |
   |                               |                               |
   |  STORE event (merged) ─────→  |  (block 1005)                 |
   |  SUPERSEDE event ──────────→  |  (block 1006)                 |
   |  (Y_old superseded by merged) |                               |
   |                               |                               |
   |  [Subgraph deactivates Y_old] |                               |
   |                               |                               |
   |  [Update watermark to 1006]   |                               |
```
