<!--
Product: TotalReclaw
Formerly: tech specs/v0.3 (grok)/TS v0.3: TotalReclaw Skill for OpenClaw.md
Version: 0.3 (1.0)
Last updated: 2026-02-24
-->

# Technical Specification: TotalReclaw Skill for OpenClaw

**Version:** 1.0 (Draft for Coding Agent)
**Date:** February 20, 2026
**Author:** Grok (on behalf of the TotalReclaw team)
**Target:** OpenClaw v0.x (GitHub: openclaw/openclaw)
**Primary Storage:** TotalReclaw E2EE server (server-blind, password-based restore)
**Philosophy Alignment:** Keep OpenClaw's Markdown-first "source of truth" for human readability/auditability while adding structured E2EE facts + graph layer behind the scenes. The Markdown files remain writable/editable by the agent and user; TotalReclaw acts as the private, portable, searchable backend.

---

## 1. Goals & Non-Goals

### Goals

- Automatic client-side fact extraction + importance scoring (Mem0-style) before encryption.
- Lightweight entity-relation graph layer for multi-hop reasoning.
- Built-in importance decay + eviction (from day one).
- Seamless integration with OpenClaw's existing triggers (especially pre-compaction flush).
- Server-blind: all processing on OpenClaw runtime (client), only encrypted blobs + blind indices go to server.
- Maintain <50 ms added latency on hot path; <200 ms on flush path.
- Full portability: user enters master password → restores entire memory on any OpenClaw instance or other agent.

### Non-Goals (v1)

- Full graph query language on server (client-side only for now).
- Replacing Markdown files (they stay as human-readable cache/export).

---

## 2. Data Models (TypeScript / JSON Schemas)

All data is defined in strict TypeScript interfaces (and equivalent JSON Schema for validation). These are used both in the OpenClaw plugin and in the TotalReclaw client library.

```ts
// Core Memory Fact (stored as one encrypted blob per fact)
interface TotalReclawFact {
  id: string;                    // UUID v7 (time-sortable, e.g. ulid or uuid7)
  timestamp: string;             // ISO 8601 with millisecond precision
  source: 'conversation' | 'pre_compaction' | 'explicit' | 'user_edit';
  raw_text: string;              // short snippet from conversation (max 512 chars)
  fact_text: string;             // atomic, concise statement (e.g. "User prefers TypeScript over JavaScript for new projects")
  type: 'fact' | 'preference' | 'decision' | 'episodic' | 'goal';
  importance: number;            // 1-10 (LLM scored, integer)
  confidence: number;            // 0.0-1.0 (LLM confidence)
  entities: Entity[];            // embedded for graph
  relations: Relation[];         // embedded triples
  decay_score: number;           // current 0-1 (updated on access or nightly)
  tags: string[];                // auto-generated + user-added
  version: number;               // for conflict resolution / updates
}

// Entity & Relation (for graph layer)
interface Entity {
  id: string;                    // stable UUID (hashed name+type for deduplication)
  name: string;                  // normalized name (e.g. "PostgreSQL")
  type: 'person' | 'project' | 'tool' | 'preference' | 'concept' | 'location' | string; // extensible
}

interface Relation {
  subject_id: string;
  predicate: string;             // "prefers", "decided_to_use", "works_on", "hates", etc.
  object_id: string;
  confidence: number;            // 0.0-1.0
}

// Global Graph Snapshot (stored as separate encrypted blob, updated every N facts or on demand)
interface MemoryGraph {
  entities: Record<string, Entity>;   // id → Entity
  relations: Relation[];
  last_updated: string;               // ISO 8601
  checksum: string;                   // SHA-256 of sorted content for dedup
}
```

### Blind Indices (for server-side keyword/entity search)

SHA-256 hashes of:

- Every token in `fact_text`
- Every `entity.name`
- Every `predicate + object.name` combination

### Storage on TotalReclaw Server (per user, under one namespace)

- Encrypted fact blobs (AES-256-GCM)
- Encrypted graph snapshot blobs
- Encrypted embeddings (for vector KNN)
- Blind indices (for exact keyword/entity match)

---

## 3. Trigger Mechanisms (OpenClaw Integration)

### Storage Triggers (in priority order)

#### 1. Pre-Compaction Flush (primary hook – reuse OpenClaw's existing `compaction.memoryFlush`)

OpenClaw already fires a silent agentic turn when `totalTokens >= softThresholdTokens`.

**Enhancement:** Replace/inject the default flush prompt with the exact prompt shown in section 4.1 below.

**Frequency:** exactly when OpenClaw triggers it (typically every few hours in long sessions).

#### 2. Post-Turn Lightweight Extraction (every N turns, configurable)

After every `config.memory.autoExtractEveryTurns` (default: 3) user+agent turns:

1. Queue last 3 turns to background worker.
2. Run extraction via OpenClaw's `llm-task` plugin with `temperature: 0` and a JSON schema
   for structured fact output. This makes a separate LLM call independent of the main
   conversation, ensuring deterministic extraction for content fingerprint dedup
   (see TS v0.3.1b §8.2). The `llm-task` plugin must be enabled in OpenClaw config.
3. If any item has `importance ≥ 7` → immediate `store_totalreclaw_batch`.

#### 3. Explicit User / Agent Commands

- Detect in user message (regex + LLM classifier): "remember that…", "I prefer…", "forget…", "note: …"
- Or new tool `remember_fact(text, type?, importance?)` that agent can call.
- Forces extraction + store with importance boosted +1.

#### 4. Periodic Background (OpenClaw cron)

- Daily at 03:00 (or configurable): full nightly consolidation of last 24h Markdown + any pending facts.
- Runs extraction + dedup + graph merge.

#### 5. User Edit Hook

- When user (or agent) edits a Markdown file in `memory/` dir → watcher fires → re-extract facts from diff → update TotalReclaw.

### Retrieval Triggers

#### 1. Context Builder (before every agent turn)

In OpenClaw's agent runtime (before building system prompt + history):

```ts
const relevant = await openMemoryClient.search({
  query: currentUserMessage + lastTurnSummary,
  k: 8,
  minImportance: 5,
  includeGraph: true
});
// Inject into prompt: "Relevant long-term memories: ..." + graph snippets
```

#### 2. Explicit Tool Call

New tool exposed to agent:

```ts
tool: {
  name: "recall_totalreclaw",
  description: "Search long-term E2EE memory. Use when user asks about past preferences/decisions.",
  parameters: { query: string, k?: number }
}
```

Returns top facts + graph context (decrypted client-side).

#### Export & Import Tools

```ts
tool: {
  name: "export_totalreclaw",
  description: "Export memories for backup or portability. Returns plaintext.",
  parameters: { 
    format?: "json" | "markdown",
    namespace?: string 
  }
}

tool: {
  name: "import_totalreclaw",
  description: "Import memories from backup. Supports cross-agent portability.",
  parameters: { 
    content: string,
    format?: "json" | "markdown",
    namespace_mapping?: Record<string, string>,
    merge_strategy?: "skip_existing" | "overwrite" | "merge"
  }
}
```

**Import Behavior**:
- Validates data structure against TotalReclawFact schema
- Re-encrypts all facts with current master password
- Deduplication via semantic similarity (threshold 0.85)
- Namespace remapping for migration scenarios
- Conflict resolution uses optimistic locking + LLM-assisted merge (v0.3.1 §340-357)

#### 3. On-Demand in Canvas / UI

"Memory" tab in OpenClaw web UI calls same search API.

---

## 4. Processing Pipelines

### Storage Pipeline (runs entirely client-side in OpenClaw runtime)

1. **Gather context** (last N turns or full session chunk).
2. **LLM Fact Extraction Call** (one structured output prompt – see exact prompt in appendix if needed, but use the pre-compaction one below).
   - Model: same as agent or smaller (e.g. `claude-3-5-haiku` or local).
   - Output: array of `TotalReclawFact` (JSON mode + strict schema validation).
3. **Deduplication**:
   - Quick recall of top-20 similar facts (vector + blind keyword).
   - LLM judge: "Is this new or update? If update → output UPDATE action".
4. **Importance scoring** (part of extraction prompt) + initial `decay_score = importance`.
5. **Graph merge**:
   - Resolve entities (fuzzy match on name+type → reuse UUID).
   - Add new relations.
   - If >50 changes → create new graph snapshot blob.
6. **For each fact + graph delta**:
   - Generate embedding (local or via TotalReclaw client).
   - Generate blind indices.
   - AES-256-GCM encrypt (master password derived key using Argon2id).
   - Upload batch to TotalReclaw server (or queue if offline).
7. **(Optional)** Append human-readable bullet list to daily Markdown.

### Exact Pre-Compaction Prompt (inject into OpenClaw's `memoryFlush`)

```markdown
Pre-compaction memory flush.

1. Review the last 20 turns of conversation history.
2. Extract atomic facts/preferences/decisions using the exact JSON schema above.
3. For each item: assign importance 1-10, link entities/relations.
4. Deduplicate against existing memories (call recall_totalreclaw with summary of current session).
5. Call store_totalreclaw_batch(facts) – do NOT write to Markdown yet.
6. Then append a human-readable summary to memory/daily/YYYY-MM-DD.md (append only).
```

### Retrieval Pipeline (E2EE two-pass, unchanged from TotalReclaw v0.2)

1. Client: embed query → server KNN on encrypted embeddings → download top 500 candidates.
2. Client: decrypt → BM25 + RRF → return facts + extracted graph subgraph.
3. On access: boost `decay_score += 0.2` (capped at 1.0) and re-upload updated fact.

### Forgetting / Eviction Engine

**Formula** (applied on every retrieval or nightly):

```ts
decay_score = importance * Math.exp(-days_since_last_access / 30) * usage_frequency_factor;
// usage_frequency_factor: 1.0 default, +0.1 per access in last 90 days.
```

**Eviction:** nightly job (or on store if >10k facts) removes facts where `decay_score < 0.3` AND `importance < 6`.

**User override:** "forget X" tool sets `importance=0` and marks deleted.

**Tombstone records** kept 30 days for undo.

---

## 5. OpenClaw Integration Points (Code Locations to Hook)

| File | Integration |
|------|-------------|
| `src/agents/runtime/contextBuilder.ts` | Insert retrieval call |
| `src/compaction/memoryFlush.ts` | Replace/inject enhanced prompt + call store pipeline |
| `src/memory/watch.ts` | Add re-extract on Markdown edit |
| `src/tools/builtin.ts` | Register `recall_totalreclaw` and `remember_fact` |

### Config Extension (`agents.defaults`)

```yaml
memory:
  totalreclaw:
    enabled: true
    serverUrl: "https://api.totalreclaw.xyz"
    autoExtractEveryTurns: 3
    minImportanceForAutoStore: 6
    forgetThreshold: 0.3
    extractionModel: "claude-3-5-haiku-20241022"  # or local
```

### Plugin Manifest

`@totalreclaw/totalreclaw` (NPM package) that registers hooks via OpenClaw's plugin system.

---

## 6. E2EE & Security Specifics

- Master password **never leaves client**; used only for key derivation (Argon2id with high iterations).
- All LLM calls happen **before encryption**.
- Graph stored as one encrypted blob per 100 facts (or on demand) to keep client RAM low.
- **Offline mode:** queue facts locally (encrypted SQLite), sync on reconnect.
- No persistent client storage except temporary in-memory during session.

---

## 7. Configuration & Tunables (all in OpenClaw config)

| Config Key | Description | Default |
|------------|-------------|---------|
| `memory.totalreclaw.enabled` | Enable/disable TotalReclaw | `true` |
| `memory.totalreclaw.serverUrl` | TotalReclaw server URL | `"https://api.totalreclaw.xyz"` |
| `memory.totalreclaw.autoExtractEveryTurns` | Turns between extractions | `3` |
| `memory.totalreclaw.minImportanceForAutoStore` | Minimum importance to auto-store | `6` |
| `memory.totalreclaw.forgetThreshold` | Decay score threshold for eviction | `0.3` |
| `memory.totalreclaw.extractionModel` | LLM for extraction | `"claude-3-5-haiku-20241022"` |
| `memory.totalreclaw.decayHalfLifeDays` | Decay half-life | `30` |
| `memory.totalreclaw.maxFactsBeforeEviction` | Max facts before eviction runs | `10000` |
| `memory.totalreclaw.importMergeStrategy` | Default merge strategy for imports | `"skip_existing"` |
| `memory.totalreclaw.importDedupThreshold` | Similarity threshold for dedup | `0.85` |

---

## 8. Testing & Validation Plan (for coding agent)

| Test Type | Description |
|-----------|-------------|
| **Unit tests** | Fact extraction accuracy on 100 sample conversations |
| **Integration tests** | End-to-end pre-compaction flush → verify encrypted blob on server + Markdown append |
| **Scale tests** | 10k synthetic facts → measure recall, eviction, graph merge time (<2s) |
| **Privacy tests** | Attempt to read server blobs without password → must be impossible |
| **Portability test** | Export with password → import on fresh OpenClaw instance → 100% match on all queries |
| **Forgetting test** | Simulate 90 days → verify low-importance facts evicted |

---

## 9. Implementation Order (for coding agent)

1. TotalReclaw client library wrapper (TypeScript) with batch store/search + graph support.
2. Fact extraction + dedup + graph merge module (with LLM prompt templates).
3. Hook into pre-compaction flush (highest ROI).
4. Add retrieval tool + context injection.
5. Add export/import tools for portability.
6. Importance/decay/eviction background job.
6. Markdown sync layer.
7. Config + plugin manifest.
8. Full test suite.

---

## 10. Expected Deliverables

- NPM package `@totalreclaw/totalreclaw`
- Updated TotalReclaw client library with LSH support (see separate spec)
- Complete README with installation, config examples, and migration guide

---

## 11. Conflict Resolution (Reference)

Conflict resolution for imports and concurrent updates is defined in **v0.3.1 §340-357**:

- **Optimistic locking**: Each fact has a `version` field; updates must include current version
- **LLM-assisted merge**: On version conflict, LLM compares both versions and produces merged result
- **Merge rules**: Preserves higher importance, more recent timestamp, and non-conflicting entity additions
- **User override**: Explicit `overwrite` strategy bypasses merge logic

This mechanism is sufficient for MVP cross-agent portability scenarios.
