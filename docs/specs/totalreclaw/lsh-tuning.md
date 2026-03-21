# LSH Parameter Tuning for Multi-Tenant SaaS

**Product:** TotalReclaw (TotalReclaw)
**Version:** PoC v2 with LSH support
**Last updated:** 2026-02-28
**Applies to:** `skill/plugin/lsh.ts`, `skill/plugin/crypto.ts`, `skill/plugin/index.ts`

---

## 1. Current Validated Configuration

| Parameter | Value | File |
|-----------|-------|------|
| Bit width | 32 | `skill/plugin/lsh.ts` |
| Tables | 20 | `skill/plugin/lsh.ts` |
| Candidate pool | 1200 | `skill/plugin/index.ts` |
| Stemming | Porter | `skill/plugin/crypto.ts` |
| Embedding model | Qwen3-Embedding-0.6B (1024-dim) | `client/src/embedding.ts` |

**Validation dataset:** 50 conversations / 415 facts / 140 queries (diverse personal conversations).

**Benchmark results (Session 13):**
- Semantic recall: 24.3% (within 0.4% of LanceDB using OpenAI text-embedding-3-small)
- Factual recall: 27.3%
- Cross-conversation recall: 22.9%
- Overall non-negative recall: 24.8%

**Tuning history:** Started at 64-bit x 12 tables (too strict for 1024-dim embeddings -- ~0% match probability at cosine 0.7). Tested 12-bit x 28 (too coarse, excessive false positives). Settled on 32-bit x 20 as the sweet spot.

---

## 2. The Multi-Tenant SaaS Challenge

TotalReclaw is an end-to-end encrypted memory vault. The server never sees plaintext. This creates a unique tuning challenge:

- **The server cannot analyze content.** It stores opaque SHA-256 hashes in `blind_indices` and returns candidates that match the trapdoors sent by the client. It has no ability to inspect, cluster, or profile user content.
- **Different users have different content profiles.** A tech worker stores facts about code and APIs. A chef stores recipes and ingredients. A student stores lecture notes and exam topics.
- **Different users have different fact counts.** A power user might accumulate 10,000 facts over months. A casual user might have 50.
- **LSH parameters are CLIENT-SIDE.** The hashing (hyperplane generation, signature computation, blind hashing) all happens in the plugin/skill code running on the user's device. The server receives and stores whatever the client sends.

---

## 3. Why Per-User Tuning is NOT Needed

This is the key insight for multi-tenant deployment.

### Bit width and table count are content-type-agnostic

The 32-bit x 20 configuration works because it targets a cosine similarity range (0.5--0.8) that covers most natural language paraphrasing, regardless of domain. A cooking fact and a coding fact both follow the same linguistic patterns when paraphrased:

- "Pedro uses Python for data analysis" vs "he works with Python for analyzing data" -- cosine ~0.7
- "The risotto needs 20 minutes of stirring" vs "stir the risotto for about twenty minutes" -- cosine ~0.7

The LSH match probability depends only on the cosine similarity between the query embedding and the fact embedding, not on the content domain. With 32-bit x 20:

| Cosine sim | P(match in at least 1 of 20 tables) |
|------------|--------------------------------------|
| 0.90 | ~1.000 |
| 0.80 | ~0.990 |
| 0.70 | ~0.820 |
| 0.60 | ~0.470 |
| 0.50 | ~0.190 |

This distribution works well for diverse content types because paraphrased queries typically land in the 0.6--0.8 range for any domain.

### What DOES vary per user is the candidate pool size

A user with 50 facts needs a pool of ~200. A user with 10,000 facts needs a pool of 2,000--3,000. More facts means more false positives from LSH (more facts landing in the same buckets by chance), which means the reranker needs more candidates to find the true positives.

The candidate pool is already a per-request parameter in the search API. The client specifies `max_candidates` in each search call. No server-side configuration or per-user state is needed.

### Dynamic scaling formula

```
pool = min(max(factCount * 3, 400), 5000)
```

| User's fact count | Candidate pool | Rationale |
|-------------------|---------------|-----------|
| 50 | 400 (minimum) | Small vault, few false positives |
| 200 | 600 | Light user |
| 500 | 1500 | Moderate user |
| 1,000 | 3000 | Active user |
| 1,700+ | 5000 (cap) | Power user; beyond 5000 the reranker cost dominates |

### Why the server does not need per-user LSH config

1. LSH hashing happens client-side.
2. The server stores whatever `blind_indices` the client sends.
3. The server returns whatever candidates match the trapdoors.
4. The client controls the pool size via `max_candidates` in each search request.

The server is a dumb (by design) encrypted storage and retrieval layer. It does not know -- and should not know -- anything about LSH parameters.

---

## 4. When to Change Global LSH Parameters

The 32-bit x 20 config should be treated as a global constant baked into the client code. Change it only under these circumstances:

| Trigger | Action | Requires re-indexing? |
|---------|--------|-----------------------|
| New benchmark on very different content (e.g., enterprise/technical knowledge base) shows recall below 20% | Consider 24-bit x 24 or 28-bit x 22 | Yes |
| Embedding model upgrade | Re-validate; similarity distributions may shift | Yes |
| Non-English language support | Re-validate; stemming and word distributions differ | Yes |
| Recall is acceptable but too many false positives overwhelm the reranker | Increase bit width (e.g., 36 or 40) | Yes |

**Re-indexing cost:** All existing facts must be decrypted client-side, re-hashed with new LSH parameters, and the `blind_indices` array updated on the server. This is a client-initiated operation (the server cannot do it -- server-blind). For production, a `PATCH /v1/facts/{fact_id}/indices` endpoint would be needed. For the PoC, a fresh re-ingest is simpler.

---

## 5. Server-Blind Observability

The server can observe the following metrics without breaking the E2EE guarantee. None of these reveal plaintext content.

### Candidate count per search

If a user's searches consistently return the maximum number of candidates (hitting the pool cap), the client should increase the pool size. The server can include `total_candidates_matched` in the search response.

### blind_indices array size per fact

A proxy for fact complexity. With the current config, each fact averages ~36--53 hashes (20 LSH + 8--15 word + 8--15 stem). A fact with significantly more or fewer hashes is unusual but not actionable server-side.

### Search latency

If GIN index lookups slow down for a specific user, they may have too many facts with overlapping buckets. This is a scaling signal, not a tuning signal -- the fix is increasing the candidate pool or (at extreme scale) partitioning.

### Fact count per user

Directly available from the database. The primary input to the dynamic pool sizing formula.

### Exposing metrics to the client

The server can expose a lightweight endpoint that returns per-user operational metrics:

```json
GET /v1/metrics
{
  "fact_count": 1847,
  "avg_candidates_per_search": 892,
  "max_candidates_hit_rate": 0.12,
  "p95_search_latency_ms": 18
}
```

The client uses `fact_count` to auto-tune the pool size. If `max_candidates_hit_rate` is high (e.g., > 0.3), the client increases the pool.

---

## 6. Recommended Auto-Tuning Strategy

### On each search request

```
1. Client knows user's approximate fact count (from last sync/export or cached)
2. pool = min(max(factCount * 3, 400), 5000)
3. Send search with max_candidates = pool
```

### On each ingest

```
1. Use fixed LSH params (32-bit x 20) for all users
2. Use fixed stemming (Porter) for all users
3. These are baked into the client code, not configurable per-user
```

### When the client starts a session

```
1. Fetch user's fact count from server (or use cached value)
2. Initialize candidate pool size
3. No LSH parameter changes needed
```

This strategy requires zero server-side configuration changes. The server does not store or manage LSH parameters. The client handles everything.

---

## 7. Parameter Reference Table

| Parameter | Value | Where Set | Per-User? | When to Change |
|-----------|-------|-----------|-----------|----------------|
| Bit width | 32 | Client (`lsh.ts`) | No -- global | Only if recall drops on new content types or embedding model changes |
| Tables | 20 | Client (`lsh.ts`) | No -- global | Only if recall drops; diminishing returns past ~24-28 |
| Candidate pool | 1200 (dynamic) | Client (`index.ts`) | Yes -- per search | Scale with fact count using formula |
| Stemming | Porter | Client (`crypto.ts`) | No -- global | Only if adding non-English language support |
| Embedding model | Qwen3-Embedding-0.6B | Client (`embedding.ts`) | No -- global | Upgraded from all-MiniLM-L6-v2. 1024-dim, 100+ languages, ~600MB ONNX model |
| Blind index format | SHA-256 hex | Client (`crypto.ts`) | No -- global | No change planned |

---

## 8. Summary

- **Global LSH parameters (32-bit x 20) work for all users.** Content domain does not affect LSH match probabilities -- only cosine similarity matters, and paraphrasing follows similar patterns across domains.
- **The only per-user parameter is candidate pool size.** This scales with fact count and is controlled entirely by the client in each search request.
- **The server remains blind to content.** It stores opaque hashes, returns matching candidates, and exposes operational metrics (fact count, candidate counts, latency) that help the client auto-tune without revealing content.
- **Global parameter changes are rare** (new embedding model, new language, or a benchmark showing poor recall on a new content type) and require client-side re-indexing.
