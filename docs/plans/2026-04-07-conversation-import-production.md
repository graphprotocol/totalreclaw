# Conversation Import — Production Design Notes

**Context:** Bulk import of conversation history (Gemini, ChatGPT, Claude) into TotalReclaw vaults. Validated locally with Gemini Takeout (~3,500 conversations → ~7K facts).

---

## Batching for Bulk Imports

The current `MAX_BATCH_SIZE = 15` in `client/src/userop/batcher.ts` is tuned for the live extraction cycle (max 15 facts per `agent_end` hook). For bulk imports, this is too conservative.

### Recommended: Import Batch Size = 50

| Batch Size | UserOps for 7K facts | Gas per UserOp | Total Gas | Pimlico quota % |
|-----------|---------------------|-----------------|-----------|-----------------|
| 15 (current) | 467 | ~47,500 | ~22M | 0.9% of 50K/mo |
| **50** | **140** | ~135,000 | ~19M | **0.3%** |
| 100 | 70 | ~265,000 | ~18.5M | 0.1% |

**Why 50, not 100:**
- Conservative margin for Gnosis block gas limit (30M)
- Pimlico paymaster may have per-UserOp gas caps
- 140 UserOps is already very comfortable (0.3% of monthly quota)
- Diminishing returns — gas savings from 50→100 are minimal (~3%)

### Implementation

Add an `IMPORT_BATCH_SIZE` constant or parameter to the import tool:

```typescript
// In the import tool handler (plugin + MCP):
const batchSize = isImport ? 50 : MAX_BATCH_SIZE; // 50 for imports, 15 for live extraction
```

The existing `sendBatchOnChain()` in `batcher.ts` already supports arbitrary batch sizes up to the validated limit. Just increase `MAX_BATCH_SIZE` for import contexts or add a separate constant.

### Cost to User

For a ~7K fact Gemini import on Gnosis mainnet:
- **Gas**: ~$0.04 (Pimlico-sponsored, $0 to user)
- **Subscription**: $3.99/month Pro tier
- **LLM extraction**: User's own model/key (local Ollama = free)
- **Total**: $3.99/month — the import itself is essentially free

---

## Production Import Flow

```
1. User uploads Gemini/ChatGPT/Claude export file
2. Client-side: parse → chunk into sessions → LLM extraction (user's model)
3. Client-side: encrypt facts (AES-256-GCM) + generate embeddings (Harrier 640d)
4. Client-side: encode protobuf with ORIGINAL conversation timestamps
5. Client-side: batch 50 facts per UserOp → submit via relay/Pimlico
6. Subgraph indexes with per-fact `createdAt` from protobuf field 2
7. Facts are searchable alongside live-extracted facts (same embeddings, same indices)
```

### Key Differences from Live Extraction

| Aspect | Live Extraction | Bulk Import |
|--------|----------------|-------------|
| Batch size | 15 facts/UserOp | 50 facts/UserOp |
| Timestamp | `Date.now()` | Original conversation time |
| Source field | `conversation` / `pre_compaction` | `gemini-import` / `chatgpt-import` |
| Trigger | `agent_end` hook | User-initiated tool call |
| Dedup | Store-time cosine + LLM-guided | Content fingerprint + text dedup (P0). Post-import LLM consolidation (P1). |

### Adapter Pattern

Reuse the existing `BaseImportAdapter` pattern in `skill/plugin/import-adapters/`:
- `gemini-adapter.ts` — HTML parser + temporal session grouping (built, tested)
- `chatgpt-adapter.ts` — already exists
- `claude-adapter.ts` — already exists

Add `'gemini'` to the `ImportSource` type and register in the adapter factory.

---

## Deduplication Strategy for Imports

Bulk imports introduce a dedup challenge that doesn't exist in live extraction:
facts about the same topic appear across months/years of conversation history,
and naively deduping destroys temporal signal.

### Why Cosine Dedup Alone Is Dangerous

Cosine similarity at 0.85 threshold is structurally blind to temporal changes:

- "User lives in Berlin" vs "User lives in Lisbon" → cosine ~0.92+ (identical structure, different city)
- "User prefers Python" vs "User prefers TypeScript" → cosine ~0.90+
- "Learning Rust" vs "Proficient in Rust" → cosine ~0.88+

All of these represent **real changes over time**, not duplicates. Blind cosine
dedup would kill the second fact, losing the life change / preference evolution.

### Safety by Fact Type

| Type | Blind cosine dedup safe? | Why |
|------|--------------------------|-----|
| **Fact** (location, job) | NO | "Lives in X" → "Lives in Y" = life change |
| **Preference** | NO | Preferences evolve over time |
| **Decision** | YES | One-time events with reasoning |
| **Episodic** | YES | Unique events by nature |
| **Goal** | NO | "Learning X" → "Proficient in X" = progress |
| **Context** | NO | Active projects change frequently |
| **Summary** | YES | Summaries are point-in-time snapshots |

Safe for ~40% of fact types. Dangerous for the rest.

### Recommended: Post-Import LLM Consolidation

Instead of per-fact cosine dedup, use a two-phase approach:

**Phase 1: Cluster (cosine similarity for GROUPING, not deletion)**

After all facts are extracted, cluster semantically similar facts:
- Compute pairwise cosine similarity across all extracted facts
- Group facts with cosine >= 0.80 into clusters
- Single-fact clusters pass through unchanged

**Phase 2: LLM Consolidation (per cluster)**

For each multi-fact cluster, ask the LLM to produce the best representation:

```
You have multiple memories about the same topic from different dates.
Produce the single best memory that captures the full picture, including
any changes over time.

Facts:
- [2024-06-15] "User lives in Berlin, Germany"
- [2025-01-20] "User moved to Lisbon, Portugal"
- [2026-01-10] "User lives in Lisbon"

Output: {"text": "User moved from Berlin to Lisbon in January 2025 (still there as of January 2026)", "type": "fact", "importance": 9}
```

**Why this works:**
- Cosine similarity is used for grouping, not deletion — no information loss
- LLM understands temporal progression and life changes
- One LLM call per cluster (not per fact) — efficient
- Produces richer facts that capture evolution ("moved from X to Y")
- Works correctly for ALL fact types

**Cost:** For ~3,000 extracted facts with ~200 clusters of 2+ facts:
- ~200 LLM calls (cheap/fast model, small input)
- ~30 seconds total with local Ollama
- Negligible compared to extraction phase

### Extraction Prompt Tuning

The extraction system prompt should emphasize temporal context:

```
- When extracting facts about the user's situation (location, job, projects),
  capture CHANGES and TRANSITIONS, not just current state.
  BAD:  "User lives in Lisbon"
  GOOD: "User moved to Lisbon in January 2025"
  
- Include temporal markers when available in the conversation.
  BAD:  "User works at a startup"
  GOOD: "User joined a crypto startup in March 2025"
```

This produces more informative facts AND makes them more distinct for cosine
similarity (less likely to be false-positive clustered with related-but-different facts).

All three are required for production launch — no phased rollout.

---

## Control Plane / Quota Protection

Pro users pay $3.99/mo for the managed service. The relay sponsors gas via
Pimlico. Import must work within the existing quota system — no extra billing,
no upsell. The quota protects our Pimlico sponsorship budget from being
burned disproportionately by a single user's import.

### How It Works

1. Pro user initiates import
2. Pre-flight check: parse file, estimate facts/UserOps, check remaining quota
3. If within quota → proceed
4. If import would exceed remaining quota → tell user:
   "This import needs 170 UserOps. You have 50 remaining this month.
   Options: import a smaller batch, or wait for quota reset on the 1st."
5. Import runs, UserOps count against monthly quota (same as live extraction)

**No extra charge. No new tier. Just the existing quota, surfaced transparently.**

### Pre-flight Check (Dry Run)

The `totalreclaw_import_from` tool with `dry_run=true` returns:
- Parsed entry/session count
- Estimated fact count (based on historical extraction ratios)
- Estimated UserOps at denser batching (facts / 50)
- Current quota usage and remaining capacity
- Whether the import fits within remaining quota
- Time estimate for extraction phase

### Relay-Side Protection

- **Signed import token:** The dry-run returns a signed token encoding the approved fact count and wallet address. The relay only accepts import UserOps with a valid, unexpired token. Prevents bypassing the pre-flight check.
- **Rate limiting:** Max 1 active import per wallet. Max 50,000 facts per import (prevents runaway scripts).
- **Source tracking:** `X-TotalReclaw-Import: true` header + `source: gemini-import` in protobuf. Relay tracks import volume separately from live extraction in the admin dashboard.
- **Quota enforcement:** Import UserOps count against the same monthly quota as live extraction. Relay rejects UserOps when quota is exhausted.

### Roadmap: Quota Top-ups

When a user's import exceeds their remaining monthly quota, they're currently
blocked until the next billing cycle. A future enhancement:

- **Top-up credits:** User purchases additional UserOps (e.g., 1,000 for $0.99)
  without changing their subscription tier
- **One-time import pass:** Flat fee for unlimited import UserOps during a
  24-hour window
- **Auto top-up:** Opt-in setting that automatically purchases credits when
  quota is exhausted during an active import

This is NOT required for launch. Track in roadmap for post-launch iteration.
