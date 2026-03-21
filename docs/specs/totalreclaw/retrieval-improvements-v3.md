# TotalReclaw Retrieval Improvements v3 — Comprehensive Plan

**Created:** 2026-03-02 (Session 18)
**Branch:** `feature/subgraph`
**Scope:** Both PoC v2 (server mode) and Subgraph mode
**Goal:** Close the subgraph recall gap AND leapfrog state-of-the-art (Mem0, QMD, LangMem, Supermemory)

---

## Context

### Current State (Session 17-18 measurements)

| Metric | Server (PoC v2) | Subgraph |
|--------|----------------|----------|
| Recall@8 | 98.1% | 40.2% |
| Query latency (avg) | ~50-100ms | 94ms |
| Candidate pool | Up to 3,000 | `first: 1000` (hard cap) |
| Sort before LIMIT | `decay_score DESC` | None (insertion order) |
| Search trigger | Every message ≥5 chars | Every message ≥5 chars |
| Relevance threshold | None | None |
| Ranking signals | BM25 + cosine (similarity only) | BM25 + cosine (similarity only) |
| Importance in ranking | NO (display only) | NO (display only) |
| Recency in ranking | NO (display only) | NO (display only) |
| Extraction throttle | Every turn (no throttle) | Every turn (no throttle) |
| Hot cache | 30 facts (client) | 30 facts (fallback only) |
| Rate limiting | Per-user sliding window | None |

### Competitive Landscape (as of March 2026)

- **Mem0**: Cosine threshold 0.3, ADD/UPDATE/DELETE/NOOP write dedup, optional reranker
- **QMD**: Agent-triggered only (no auto-search), local LLM reranker, max 6 results
- **LangMem**: `similarity × importance × recency` scoring, agent decides when to search
- **MemOS**: Cosine threshold 0.45, structured injection (Role/System/Memory/Skill), max 6+6
- **Supermemory**: Profile injection every 50 turns, auto-recall every turn, max 10
- **Emerging**: "Observational Memory" stable prefix for prompt caching (no one has implemented this in OpenClaw yet)

### Root Causes of Subgraph Recall Gap (40.2% vs 98.1%)

1. **`first: 1000` cap** — GraphQL truncates results; GIN returns all matches
2. **No sort before LIMIT** — Server sorts by `decay_score DESC` (important facts survive truncation); subgraph returns arbitrary order
3. **`blindIndices` vs `blindIndexes`** — Entity pluralization bug in `subgraph-search.ts` (fixed in E2E test, not in skill)
4. **Single query for all trapdoors** — `TRAPDOOR_BATCH_SIZE=500` means all ~40 trapdoors in one query; common-word matches drown rare-word matches
5. **Sequential batch execution** — Even if batched, queries run in a `for` loop with `await`, not `Promise.all()`

---

## Improvement Categories

### Category A: Subgraph Recall Fixes (close the 40.2% → ~95%+ gap)

### Category B: Ranking Quality (both modes — outcompete Mem0/LangMem)

### Category C: Search Efficiency (reduce unnecessary queries/cost)

### Category D: Write Path Optimization (reduce gas + extraction cost)

### Category E: Architecture Differentiators (leapfrog state of the art)

---

## Category A: Subgraph Recall Fixes

### A1. Fix `blindIndices` → `blindIndexes` in subgraph-search.ts

**Priority:** CRITICAL (blocking)
**Effort:** 5 minutes
**Files:** `skill/plugin/subgraph-search.ts` lines 39, 73

The GraphQL query uses `blindIndices` but Graph Node pluralizes `BlindIndex` entity as `blindIndexes`. This was fixed in the E2E test but never propagated to the skill.

**Changes:**
- Line 39: `blindIndices(` → `blindIndexes(`
- Line 73: `blindIndices?` → `blindIndexes?`
- Line 77: `json.data.blindIndices` → `json.data.blindIndexes`

### A2. Split trapdoors into small parallel batches

**Priority:** CRITICAL
**Effort:** 30 minutes
**Files:** `skill/plugin/subgraph-search.ts`

**Current:** `TRAPDOOR_BATCH_SIZE = 500` — all ~40 trapdoors go in one query. Common-word trapdoor matches drown rare-word matches within the `first: 1000` cap.

**Change:**
- Lower `TRAPDOOR_BATCH_SIZE` from 500 to 5
- Replace sequential `for` loop (line 33) with `Promise.all()` for parallel execution
- Each batch independently gets up to `first: 1000` results
- Existing dedup logic (line 78, `allResults.has(entry.fact.id)`) already handles cross-batch deduplication

**Expected impact:** 8 parallel queries × 1,000 results each = up to 8,000 blind index entities (after dedup, ~2,000-4,000 unique facts). Rare trapdoor matches no longer drowned.

**Graph Network compatibility:** Works with default indexer settings (`GRAPH_GRAPHQL_MAX_FIRST: 1000`). No server-side changes needed.

**Latency:** Same wall-clock time (~71ms) since queries run in parallel.

### A3. Add `orderBy` and `orderDirection` to GraphQL query

**Priority:** HIGH
**Effort:** 15 minutes
**Files:** `skill/plugin/subgraph-search.ts`

**Current:** No sort → truncation drops facts in arbitrary (insertion) order. Recent facts are systematically lost.

**Change:** Add `orderBy: decayScore, orderDirection: desc` to the GraphQL query:
```graphql
blindIndexes(
  where: { hash_in: $trapdoors, owner: $owner }
  first: $first
  orderBy: decayScore
  orderDirection: desc
) { ... }
```

Wait — `blindIndexes` doesn't have `decayScore`. The `decayScore` is on the `Fact` entity. We need to either:
- Option 1: Query `facts` entity directly with a different filter approach
- Option 2: Add `orderBy: id, orderDirection: desc` to get newest facts first (proxy for recency)
- Option 3: Keep current approach but ensure enough candidates via A2

**Recommended:** Option 2 (`orderBy: id, orderDirection: desc`) as a quick fix. This prioritizes recent facts in each truncated batch. Combined with A2 (parallel batches isolating rare trapdoors), this should get recall close to server baseline.

### A4. Cursor-based pagination for power users

**Priority:** MEDIUM (needed for 1yr+ power users)
**Effort:** 1-2 hours
**Files:** `skill/plugin/subgraph-search.ts`

For power users (18K+ facts), even split batches may truncate. Add cursor pagination:

```typescript
let lastId = "";
while (true) {
  const batch = await query({
    where: { hash_in: trapdoorChunk, owner, id_gt: lastId },
    first: 1000,
    orderBy: "id",
    orderDirection: "asc"
  });
  results.push(...batch);
  if (batch.length < 1000) break;
  lastId = batch[batch.length - 1].id;
}
```

**Gate:** Only activate pagination if first batch returns exactly 1,000 results (saturated). For users with <5,000 facts, single-pass queries suffice.

### A5. Increase `GRAPH_GRAPHQL_MAX_FIRST` for self-hosted deployments

**Priority:** LOW (config-only)
**Effort:** 1 minute
**Files:** `subgraph/docker-compose.yml`

Add env var to Graph Node service:
```yaml
GRAPH_GRAPHQL_MAX_FIRST: 5000
```

Only applies to self-hosted. No effect on The Graph Network indexers.

---

## Category B: Ranking Quality Improvements (Both Modes)

### B1. Add importance + recency to ranking fusion

**Priority:** HIGH
**Effort:** 2-3 hours
**Files:** `skill/plugin/reranker.ts`, `client/src/search/rerank.ts`

**Current:** RRF fuses only BM25 + cosine. Importance and recency are metadata-only.

**Change:** Add two more ranking signals to RRF:
1. **Importance score** (from `decay_score` or `metadata.importance`): Normalize to [0, 1]
2. **Recency score**: `1 / (1 + hours_since_creation / 168)` (half-life of 1 week)

New RRF fusion: 4 rankings instead of 2:
```
RRF(d) = 1/(k + rank_bm25(d)) + 1/(k + rank_cosine(d)) + 1/(k + rank_importance(d)) + 1/(k + rank_recency(d))
```

**Configurable weights** (for different use cases):
- Default: equal weight (0.25 each)
- Medical/safety: importance-heavy (0.2, 0.2, 0.4, 0.2)
- Conversational: recency-heavy (0.2, 0.2, 0.2, 0.4)

**Implementation:** Add `weights` parameter to `rerank()` function. Each RRF term gets multiplied by its weight.

### B2. Minimum relevance threshold gate

**Priority:** HIGH
**Effort:** 1 hour
**Files:** `skill/plugin/index.ts` (both hook and tool paths)

**Current:** Always injects top 8, even if they're irrelevant to "thanks" or "ok sure".

**Change:** After reranking, compute max cosine similarity of top result. If below threshold, return `undefined` (no injection):

```typescript
const topCosineSim = Math.max(...reranked.map(r => r.cosineSimilarity ?? 0));
if (topCosineSim < RELEVANCE_THRESHOLD) return undefined;
```

**Threshold:** 0.3 (same as Mem0). Configurable via env var `TOTALRECLAW_RELEVANCE_THRESHOLD`.

**Impact:** Eliminates noise injection for irrelevant turns. Reduces token usage. Improves prompt cache hit rate.

### B3. Maximal Marginal Relevance (MMR) for result diversity

**Priority:** MEDIUM
**Effort:** 2 hours
**Files:** `skill/plugin/reranker.ts`

**Current:** Top 8 by RRF score may include near-duplicate facts.

**Change:** After RRF fusion, apply MMR to ensure diversity:
```
MMR(d) = λ · relevance(d) - (1-λ) · max_sim(d, selected_docs)
```
Where `λ = 0.7` (favor relevance, penalize redundancy).

Iterate: pick highest MMR, add to selected set, recalculate MMR for remaining, repeat until 8 selected.

**Impact:** Prevents returning 5 variations of "user prefers dark mode" when 1 would suffice.

### B4. Cross-encoder reranker (optional quality tier)

**Priority:** LOW (future)
**Effort:** 4-6 hours
**Files:** New `skill/plugin/cross-encoder.ts`

Add optional cross-encoder reranking (like Mem0's Cohere/ZeroEntropy integration). Use a local ONNX cross-encoder model (`cross-encoder/ms-marco-MiniLM-L-6-v2`).

**Gate:** Only activate for `totalreclaw_recall` tool (explicit search), not for auto-hook. Adds ~150-200ms latency.

---

## Category C: Search Efficiency Improvements

### C1. Two-tier search: lightweight hook vs full tool

**Priority:** HIGH
**Effort:** 3-4 hours
**Files:** `skill/plugin/index.ts`, `skill/plugin/subgraph-search.ts`

**Current:** Both `before_agent_start` hook and `totalreclaw_recall` tool use identical search logic.

**Change:** Differentiate:

| Path | Trigger | Strategy | Cost |
|------|---------|----------|------|
| **Hook (auto)** | Every message | Hot cache first → if stale (>5 min), light vector-only query (1 batch, `first: 1000`) | 0-1 queries |
| **Tool (explicit)** | LLM decides | Full search: all trapdoor batches in parallel, pagination if needed | 4-8+ queries |

**Hook fast path:**
1. Check hot cache age. If <5 minutes old, return cached results immediately (0 queries).
2. If stale, run a SINGLE query with only LSH trapdoors (20 hashes) — this gives semantic matches without the word-token noise.
3. Apply relevance threshold (B2). If no result passes, return `undefined`.
4. Update hot cache.

**Tool full path:**
1. Generate all trapdoors (word + stem + LSH)
2. Split into batches of 5, run in parallel
3. Paginate if any batch saturates
4. Full 4-signal reranking (B1)
5. Optional cross-encoder (B4)

**Impact at 1K users:**
- Current: 60,000 full searches/day
- After: ~6,000 light searches/day (90% cache hits) + ~6,000 full tool searches (10% of turns)
- Graph Network cost drops from ~$19/day to ~$3/day

### C2. Hot cache TTL and semantic similarity skip

**Priority:** MEDIUM
**Effort:** 1-2 hours
**Files:** `skill/plugin/hot-cache-wrapper.ts`, `skill/plugin/index.ts`

**Current:** Hot cache stores 30 facts. No TTL on cache serving. No similarity check.

**Change:**
1. Add `lastQueryEmbedding` to hot cache metadata
2. On new message, compute query embedding, compare cosine similarity with `lastQueryEmbedding`
3. If similarity > 0.85, return cached results without querying (conversation is on the same topic)
4. Add configurable TTL (default: 5 minutes). After TTL, force fresh query.

**Impact:** Eliminates redundant searches during focused conversations where consecutive messages are on the same topic.

### C3. Implement `autoExtractEveryTurns` throttle

**Priority:** MEDIUM
**Effort:** 30 minutes
**Files:** `skill/plugin/index.ts` (agent_end hook, around line 1226)

**Current:** `agent_end` hook extracts facts on EVERY turn. Config field `autoExtractEveryTurns: 5` exists but is unused.

**Change:** Add turn counter. Only call `extractFacts()` every N turns:
```typescript
let turnsSinceLastExtraction = 0;

// In agent_end hook:
turnsSinceLastExtraction++;
if (turnsSinceLastExtraction >= config.autoExtractEveryTurns) {
  const facts = await extractFacts(evt.messages, 'turn');
  // ...
  turnsSinceLastExtraction = 0;
}
```

**Impact:** Reduces LLM extraction calls by 80% (every 5 turns instead of every turn). For the managed service, also reduces on-chain writes by 80%.

### C4. Fact count query optimization

**Priority:** LOW
**Effort:** 30 minutes
**Files:** `skill/plugin/subgraph-search.ts` (getSubgraphFactCount)

**Current:** `getSubgraphFactCount` fetches up to 1,000 fact IDs just to count them. Wasteful.

**Change:** Use `globalStates` entity which tracks `totalFacts`:
```graphql
query { globalStates(first: 1) { totalFacts } }
```
Already exists in the schema. Single lightweight query instead of fetching 1,000 IDs.

---

## Category D: Write Path Optimization

### D1. Batch fact writes (managed service)

**Priority:** MEDIUM
**Effort:** 4-6 hours
**Files:** `contracts/contracts/EventfulDataEdge.sol`, `skill/plugin/subgraph-store.ts`

**Current:** Each fact = 1 transaction. 21K base gas per tx.

**Change:** Add batch function to contract that accepts multiple protobuf-encoded facts in one tx. Amortize 21K gas across N facts.

**Impact:** At 10 facts/batch, per-fact gas drops ~15%. More importantly, reduces tx count (fewer Paymaster UserOps).

### D2. Embedding compression (both modes)

**Priority:** MEDIUM
**Effort:** 2-3 hours
**Files:** `skill/plugin/crypto.ts`, `client/src/crypto/`

**Current:** 1024-dim float32 embedding → encrypted → hex → ~8,248 bytes calldata.

**Change:** Quantize to int8 before encryption: 1024 dims × 1 byte = 1024 bytes (vs 4,096 bytes float32). After encryption + hex encoding: ~2,104 bytes.

**Impact:** Embedding calldata drops from ~3,128 to ~824 bytes. Per-fact gas reduction of ~40K gas. Minimal reranking quality impact for BM25 + cosine fusion (int8 cosine approximation is >0.99 correlated with float32).

### D3. Write-side dedup (Mem0-style ADD/UPDATE/NOOP)

**Priority:** MEDIUM-HIGH
**Effort:** 4-6 hours
**Files:** `skill/plugin/extractor.ts`, `skill/plugin/index.ts`

**Current:** Content fingerprint dedup exists but is purely hash-based (exact match). Near-duplicate facts are stored separately.

**Change:** Before storing a new fact:
1. Generate embedding for the new fact
2. Search existing facts (light query, top 5)
3. If cosine similarity > 0.9 with any existing fact, classify:
   - Same meaning → NOOP (skip)
   - Updated information → UPDATE (soft-delete old + store new)
   - Contradictory → DELETE old + ADD new
4. Otherwise → ADD

**Impact:** Reduces fact bloat over time. Fewer facts = smaller blind index table = better query performance. Matches Mem0's approach.

---

## Category E: Architecture Differentiators

### E1. Stable prefix for prompt caching ("Observational Memory")

**Priority:** HIGH (competitive differentiator — no one else has this)
**Effort:** 6-8 hours
**Files:** New `skill/plugin/stable-prefix.ts`, modify `skill/plugin/index.ts`

**Current:** Every turn injects different memories → every turn is a prompt cache miss.

**Change:** Split context injection into two parts:
1. **Stable prefix** (changes rarely): User profile + top preferences + key facts. Rebuilt every N turns (e.g., 50) or on explicit update. This prefix gets prompt-cached.
2. **Dynamic suffix** (changes per turn): Episodic/contextual memories relevant to current message. Only this part varies.

**Implementation:**
- Store stable prefix in hot cache with version number
- On hook trigger: prepend cached stable prefix + run light search for dynamic memories
- Every 50 turns (or when importance > 8 fact is stored): rebuild stable prefix

**Impact:** 60-90% of context tokens become cache-eligible. At scale, this could cut LLM costs 3-5× for the host application.

### E2. Tiered memory categories with different decay/injection rules

**Priority:** MEDIUM
**Effort:** 3-4 hours
**Files:** `skill/plugin/index.ts`, `skill/plugin/reranker.ts`

**Current:** All facts are treated identically.

**Change:** Categorize facts by type (already in schema: fact, preference, decision, episodic, goal):
- **Preferences**: Low decay, injected in stable prefix (E1), high importance weight
- **Facts**: Normal decay, injected dynamically when relevant
- **Episodic**: High decay, injected only when highly relevant (threshold 0.5 instead of 0.3)
- **Decisions**: Low decay, injected in stable prefix
- **Goals**: No decay until completed, always in stable prefix

### E3. Temporal awareness in ranking (Zep-inspired)

**Priority:** LOW (future)
**Effort:** 6-8 hours

Add temporal metadata to facts: `valid_from`, `valid_until`. When ranking, penalize facts whose validity window doesn't include the current time. This handles preference changes: "User moved from NYC to SF" → old NYC fact gets `valid_until` set, new SF fact becomes active.

---

## Expected Recall After Improvements

### Subgraph Mode

| Fix | Individual Impact | Cumulative |
|-----|------------------|-----------|
| Baseline (current) | — | 40.2% |
| A1: Fix blindIndexes bug | +0% (skill wasn't using subgraph yet) | 40.2% |
| A2: Parallel trapdoor batches (size 5) | +25-35% | ~70% |
| A3: `orderBy: id, orderDirection: desc` | +5-10% | ~78% |
| A2+A3 combined (more candidates, recent-first) | synergistic | ~85% |
| A4: Cursor pagination (power users) | +5-10% (power users only) | ~90% |
| B1: 4-signal ranking (importance + recency) | +3-5% (better top-8 selection) | ~93-95% |
| A5: `MAX_FIRST: 5000` (self-hosted) | +2-3% on top of A2 | ~95-97% |

### Server Mode (PoC v2)

Already at 98.1%. Improvements from B1-B3 may push effective quality higher (better top-8 selection from the same candidate pool), but the recall ceiling is already near 100%.

---

## Can Subgraph Match Server Performance?

**Short answer: Yes, to ~93-97% recall, with the Category A fixes.**

**Why not 98.1%?** The server's GIN `&&` operator returns ALL matching facts in one scan, sorted by decay_score. The subgraph must paginate through GraphQL, which adds:
1. Multiple round trips (8 parallel + possible pagination)
2. No native `decay_score DESC` sort on blind index entities (can only sort by `id`)
3. Dedup overhead (same fact returned by multiple trapdoor batches)

**The remaining ~1-3% gap** is inherent to the GraphQL-over-GIN indirection. It's close enough that ranking improvements (B1-B3) can compensate — better top-8 selection from a slightly smaller candidate pool.

**Where subgraph BEATS server:**
- Decentralization (no single server trust)
- Immutability (on-chain event log)
- Censorship resistance
- Works with The Graph Network (no self-hosted infra needed)

**Where server stays better:**
- Raw recall (98.1% vs ~95%)
- Single round trip latency
- Native `decay_score DESC` sorting
- Simpler client code

---

## Implementation Order

### Phase 1: Subgraph Recall Fixes (1-2 days)
1. A1: Fix blindIndexes → 5 min
2. A2: Parallel trapdoor batches → 30 min
3. A3: orderBy desc → 15 min
4. C4: Fact count query optimization → 30 min
5. Re-run E2E benchmark → 15 min

### Phase 2: Ranking + Efficiency (2-3 days)
6. B1: 4-signal ranking → 2-3 hours
7. B2: Relevance threshold gate → 1 hour
8. C1: Two-tier search (hook vs tool) → 3-4 hours
9. C3: autoExtractEveryTurns throttle → 30 min

### Phase 3: Write Optimization (1-2 days)
10. D2: Embedding compression (int8) → 2-3 hours
11. D3: Write-side dedup → 4-6 hours
12. C2: Hot cache TTL + similarity skip → 1-2 hours

### Phase 4: Differentiators (3-5 days)
13. E1: Stable prefix / Observational Memory → 6-8 hours
14. E2: Tiered memory categories → 3-4 hours
15. B3: MMR diversity → 2 hours
16. A4: Cursor pagination → 1-2 hours
17. D1: Batch writes → 4-6 hours

### Phase 5: Future (deferred)
18. B4: Cross-encoder reranker
19. E3: Temporal awareness
20. A5: MAX_FIRST config (self-hosted only)

---

## Verification Plan

After each phase, re-run:
1. `npm run test:e2e` — recall@8 benchmark (target: >90% after Phase 1, >95% after Phase 2)
2. `npm run test:gas` — gas measurements (target: 30%+ reduction after Phase 3)
3. Manual latency test — verify hook latency stays under 140ms p95
4. Query count audit — verify Graph Network query reduction (target: 80%+ reduction after Phase 2)

---

## Files Changed Summary

| File | Changes |
|------|---------|
| `skill/plugin/subgraph-search.ts` | A1, A2, A3, A4, C4 |
| `skill/plugin/reranker.ts` | B1, B3 |
| `skill/plugin/index.ts` | B2, C1, C3, E1, E2 |
| `skill/plugin/hot-cache-wrapper.ts` | C2, E1 |
| `skill/plugin/crypto.ts` | D2 |
| `skill/plugin/extractor.ts` | D3 |
| `client/src/search/rerank.ts` | B1, B3 (client library mirror) |
| `contracts/contracts/EventfulDataEdge.sol` | D1 |
| `subgraph/docker-compose.yml` | A5 |
| New: `skill/plugin/stable-prefix.ts` | E1 |
| New: `skill/plugin/cross-encoder.ts` | B4 |
