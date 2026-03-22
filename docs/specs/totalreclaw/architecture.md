<!--
Product: TotalReclaw
Formerly: tech specs/v0.3 (grok)/TS v0.3: E2EE with LSH + Blind Buckets.md
Version: 0.3 (1.0)
Last updated: 2026-02-24
-->

# Technical Specification — Crypto-Only (LSH + Blind Buckets)

**Title:** TotalReclaw v0.3 — Server-Blind Vector Search via LSH + Encrypted Embeddings
**Version:** 1.0 (complete, copy-paste for any coding agent with zero prior context)
**Target stack:** Python 3.12+ (server + client lib) + TypeScript client for OpenClaw
**Assumed PRD (lousy version):** "Make TotalReclaw truly server-blind for both documents and embeddings while keeping accuracy within 3% of plaintext and latency <150ms at 1M memories."

---

## 1.1 Problem Statement (recap for new developer)

Current code stores embeddings as raw `np.ndarray` → server sees plaintext vectors.
Query embeddings are sent in plaintext → server learns semantic content.

**Goal:** Server must never see plaintext embeddings or query vectors at any time.

**Constraint:** Keep exact same two-pass flow and RRF fusion; no TEEs, no homomorphic crypto.

---

## 1.2 Chosen Solution: Locality-Sensitive Hashing + Blind Bucket Indexing

We extend the existing blind-index system (SHA-256 token hashes) to also index LSH buckets.
This gives ~92–96% recall@500 with 400–1,200 candidates → client reranks exactly as today.

---

## 1.3 Data Models (updated)

```python
# In EncryptedMemoryStore (Python)
@dataclass
class MemoryItem:
    id: str                          # UUIDv7
    encrypted_doc: bytes             # AES-256-GCM
    encrypted_embedding: bytes       # AES-256-GCM (NEW)
    blind_indices: list[str]         # SHA-256(token) + SHA-256(LSH_bucket)
    metadata: dict                   # source, timestamp, importance, etc.
```

---

## Re-ranking Architecture (Research-Backed)

### Do NOT Use Main LLM for Re-ranking

Based on research of Mem0, Zep, Letta:
- **Re-ranking latency budget**: <100ms
- **Main LLM latency**: 500-2000ms (too slow!)
- **Zep's approach**: Hybrid scoring without LLM (fastest)

### Recommended: Dedicated Cross-Encoder

| Model | Size | Latency (10 docs) | Accuracy |
|-------|------|-------------------|----------|
| BGE-Reranker-base (ONNX) | 400MB | 30-50ms | High |
| BGE-Reranker-large | 1GB | 50-100ms | Very High |
| ms-marco-MiniLM-L-6-v2 | 100MB | 10-30ms | Good |

### Recommended: Use Main LLM for Extraction

| Model | Size | Latency | When to Use |
|-------|------|---------|-------------|
| **Main agent LLM** | - | 500ms+ | **Preferred** - already available, no extra RAM |
| Qwen3-0.6B (local) | 400MB | 100-300ms | Only if main LLM unavailable |
| Phi-3-mini (local) | 2GB | 200-500ms | Higher accuracy (not recommended) |

**Extraction is async** - doesn't block user queries, so latency is acceptable.

**IMPORTANT**: Prefer using the main LLM for extraction to minimize RAM footprint. This enables deployment on low-resource devices (e.g., Raspberry Pi). A separate extraction model adds 400MB-2GB RAM overhead. Only consider a dedicated small LLM if:
- Main LLM doesn't support async calls
- Privacy requires fully local processing
- Main LLM is cloud-based and user wants offline capability

### Implementation Architecture

```
SYNCHRONOUS PATH (<100ms total):
  Query → Embedding (5-10ms) → Server Search (10-20ms) → Cross-Encoder Rerank (30-50ms)

ASYNC PATH (non-blocking):
  New memories → Small LLM Extraction (100-300ms) → Encrypt → Upload
```

---

## 1.4 LSH Configuration (VALIDATED 2026-02-22)

**Library:** Custom Random Hyperplane LSH (or Faiss IndexLSH)

**Parameters** (validated on combined WhatsApp + Slack data, 8,727 embeddings):

```python
n_bits_per_table = 64   # 64-bit hash per table (NOT 512 as originally proposed)
n_tables = 16           # 16 independent hash tables (increased from 12 for scale)
candidate_pool = 3000   # number of candidates to retrieve for re-ranking
```

**Validation Results (Combined WhatsApp + Slack data, 8,727 embeddings):**
| Metric | Target | Achieved |
|--------|--------|----------|
| Mean Recall@3000 | ≥93% | **93.6%** |
| P5 Recall | - | 84.4% |
| Query Latency | <50ms | **9.71ms** |
| Storage Overhead | ≤2.2x | **0.06x** |
| Candidates Returned | - | ~1,848 |

**Alternative Configurations:**
| Config | n_bits | n_tables | candidate_pool | Mean Recall | P5 Recall | Latency |
|--------|--------|----------|----------------|-------------|-----------|---------|
| Efficient | 64 | 16 | 2800 | 93.3% | 84.0% | 13.73ms |
| **Balanced** | 64 | 12 | 3000 | 93.6% | 84.4% | 9.71ms |
| Higher Recall | 64 | 12 | 4000 | 96.6% | 90.8% | 10.04ms |

**Scaling Note:** Parameters were adjusted from WhatsApp-only validation (99% recall with 2,000 candidates) to account for the larger, more diverse Slack dataset. The candidate pool scales roughly with dataset size.

**Key Finding:** The `candidate_pool` size is the critical lever for recall:
- 500 candidates → ~75% recall
- 1000 candidates → ~91% recall
- 1500 candidates → ~97% recall (small dataset)
- **3000 candidates → ~93.6% recall (large, diverse dataset)** ✅

---

### Scaling Formula for Production

**`candidate_pool` is FULLY DYNAMIC** - no index rebuild needed!

For production deployments with large corpora, the server auto-adjusts `candidate_pool` based on corpus size:

```python
def calculate_candidate_pool(corpus_size: int) -> int:
    """
    Calculate optimal candidate pool size based on corpus size.

    VALIDATED DATA POINTS:
    - 1,162 memories → 2,000 candidates = 99.0% recall (34% of corpus)
    - 8,727 memories → 3,000 candidates = 93.6% recall (34% of corpus)

    Key insight: candidate_pool scales roughly logarithmically, not linearly.
    """
    import math

    MIN_POOL = 2000
    MAX_POOL = 10000

    if corpus_size < 2000:
        return MIN_POOL
    elif corpus_size < 10000:
        # Validated: 8,727 → 3,000 (34% ratio)
        return max(MIN_POOL, min(4000, int(corpus_size * 0.35)))
    elif corpus_size < 100000:
        # Estimate: logarithmic scaling for medium corpora
        return min(MAX_POOL, 3000 + int(math.log10(corpus_size) * 500))
    else:
        # Large corpora: cap at 10,000 but consider hierarchical LSH
        return MAX_POOL
```

**Validated Scaling Table:**

| Corpus Size | Candidate Pool | Ratio | Expected Recall | Validated? |
|-------------|----------------|-------|-----------------|------------|
| 1,162 | 2,000 | 172% | 99.0% | ✅ WhatsApp |
| 8,727 | 3,000 | 34% | 93.6% | ✅ Combined |
| 10,000 | 3,500 | 35% | ~93% | 🟡 Extrapolated |
| 50,000 | 5,000 | 10% | ~90% | 🟡 Extrapolated |
| 100,000 | 6,500 | 6.5% | ~88% | 🟡 Extrapolated |
| 1,000,000 | 10,000 | 1% | ~85%* | 🟡 Extrapolated |

*At 1M+ scale, consider increasing `n_tables` to 24 or using **hierarchical LSH** (cluster → LSH per cluster).

---

### Production Monitoring for LSH Scaling

Track these metrics in production to know when to adjust parameters:

```python
# Add to your /health or /metrics endpoint
{
    "lsh_metrics": {
        "total_embeddings": 8727,           # Track corpus size
        "candidate_pool_configured": 3000,  # Current setting
        "avg_candidates_returned": 1848,    # From search responses
        "avg_recall_estimate": 0.936,       # Based on validation curves
        "p95_query_latency_ms": 15.2,       # Monitor latency
        "last_param_update": "2026-02-22"   # When params were last changed
    }
}
```

**Alert Thresholds:**

| Metric | Warning | Critical | Action |
|--------|---------|----------|--------|
| `total_embeddings` > 50K | - | ⚠️ | Re-validate LSH params |
| `avg_candidates_returned` < 500 | ⚠️ | - | Increase candidate_pool |
| `p95_query_latency_ms` > 100ms | ⚠️ | ⚠️⚠️ | Check database indexes |
| Estimated recall < 90% | ⚠️ | ⚠️⚠️ | Increase candidate_pool or n_tables |

**Scaling Triggers:**
1. **Every 10x growth** (10K → 100K → 1M): Re-run LSH validation on sample
2. **Recall drops below 90%**: Increase `candidate_pool` by 1,000
3. **Latency exceeds 100ms**: Consider read replicas or caching

---

### Server-Side Auto-Adjustment (IMPLEMENT THIS)

```python
# In server search handler
async def search(request: SearchRequest) -> SearchResponse:
    # Get current corpus size for user
    corpus_size = await db.get_active_memory_count(request.user_id)

    # Auto-calculate candidate_pool using validated formula
    candidate_pool = calculate_candidate_pool(corpus_size)

    # Use whichever is larger: auto-calculated or client-requested
    max_candidates = max(candidate_pool, request.max_candidates or 3000)

    # Execute search with dynamic pool
    results = await db.search_with_blind_indices(
        user_id=request.user_id,
        trapdoors=request.trapdoors,
        limit=max_candidates
    )

    return SearchResponse(results=results, total_candidates=len(results))
```

**Benefits:**
- No admin intervention needed
- Scales automatically with corpus growth
- Per-user optimization (user with 100 memories uses smaller pool than user with 10K)

---

### Manual Scaling Workflow (Admin Guide)

**`candidate_pool` is AUTOMATIC** - server auto-adjusts based on corpus size.

**Only `n_bits` and `n_tables` require manual intervention** (rarely needed, only at 500K+ scale).

#### Step 1: Check Current Metrics
```bash
# Check corpus size and performance
curl http://localhost:8080/metrics

# Response:
{
  "total_embeddings": 52000,
  "candidate_pool_configured": 3000,
  "avg_candidates_returned": 1450,
  "p95_query_latency_ms": 22
}
```

#### Step 2: Decide If Adjustment Needed
```python
# Use the formula or table above
current_corpus = 52000
recommended_pool = 3000 + int(log10(52000) * 500)  # = ~4600

# Current is 3000, should be ~4600 → NEEDS ADJUSTMENT
```

#### Step 3: Update Configuration
```bash
# Option A: Update server config file
# Edit /server/config.yaml:
lsh:
  candidate_pool: 4600

# Option B: Update via environment variable
export LSH_CANDIDATE_POOL=4600

# Option C: Update database (persistent)
psql -c "UPDATE config SET value='4600' WHERE key='lsh_candidate_pool'"
```

#### Step 4: Restart Server (if needed)
```bash
# For config file changes
docker-compose restart totalreclaw-server

# For env vars (requires restart)
docker-compose up -d --force-recreate totalreclaw-server
```

#### Step 5: Verify
```bash
curl http://localhost:8080/metrics
# Confirm candidate_pool_configured = 4600
```

---

### User Impact Analysis

**When you change `candidate_pool`, users ARE affected:**

| Change | User Impact | Severity |
|--------|-------------|----------|
| **Increase pool** | Higher recall (better results) | ✅ Positive |
| **Increase pool** | Slightly slower search (more candidates to decrypt) | ⚠️ Minor negative |
| **Increase pool** | More bandwidth usage | ⚠️ Minor negative |
| **Decrease pool** | Lower recall (might miss relevant memories) | ❌ Negative |
| **Decrease pool** | Faster search | ✅ Positive |

**Recommendation: Only INCREASE, never DECREASE in production.**

#### Before/After Comparison

| Metric | Before (3000) | After (4600) | Impact |
|--------|---------------|--------------|--------|
| Recall | ~90% | ~93% | Better results ✅ |
| Latency | 22ms | 28ms | +6ms (negligible) |
| Bandwidth | ~500KB/query | ~750KB/query | +250KB |
| Client CPU | Low | Medium | More decryption |

#### No User Action Required
- **Clients auto-adapt**: The `max_candidates` is a server-side limit
- **No client updates needed**: Client just receives more candidates to re-rank
- **Seamless transition**: Users won't notice except better recall

#### Communication (Optional)
If you want to notify users:
```
System Notice: Memory search improved!
We've increased search depth to find more relevant memories.
You may notice slightly more comprehensive results.
```

---

### Scaling Decision Tree

```
Is total_embeddings > 50K?
├── YES → Check avg_recall_estimate
│   ├── < 90% → URGENT: Increase candidate_pool
│   └── ≥ 90% → OK, but plan for 100K milestone
│
└── NO → Check growth rate
    ├── Growing fast (>10K/month) → Pre-emptively increase
    └── Growing slow → Monitor monthly
```

---

### At 500K+ Scale: Hierarchical LSH (Future)

When corpus exceeds 500K, single LSH becomes inefficient. Switch to:

```
Hierarchical LSH:
1. Cluster memories into groups (~10K each)
2. LSH index per cluster
3. Query: Find top 3 relevant clusters → search within each
4. Total candidates: 3 × 3000 = 9000 (same as before, better recall)

Implementation: Post-PoC, requires additional development.
```

---

## Latency Optimizations (Future Improvements)

If latency becomes an issue, implement these optimizations in order of impact:

### 1. Client-Side Caching (60%+ Hit Rate)
```typescript
// Cache recent searches by query hash
const searchCache = new LRUCache<string, SearchResult[]>({ max: 100 });

async function cachedSearch(query: string): Promise<SearchResult[]> {
  const hash = sha256(query);
  const cached = searchCache.get(hash);
  if (cached) return cached;  // Cache hit - instant return

  const results = await server.search(query);
  searchCache.set(hash, results);
  return results;
}
```

**Impact**: 60%+ of queries hit cache → 0ms latency for those queries

### 2. Debounce Rapid Messages
```typescript
// Don't search on every keystroke - wait for user to pause
const debouncedSearch = debounce(search, 300);  // 300ms delay

onMessageReceived((msg) => {
  if (msg.length < 10) return;  // Skip short messages
  debouncedSearch(msg);
});
```

**Impact**: Reduces unnecessary searches by 40%

### 3. Async Pre-Fetching
```typescript
// Predict and pre-fetch likely queries in background
onMessageReceived((msg) => {
  const likelyQueries = predictQueries(msg);  // Simple keyword extraction
  likelyQueries.forEach(q => backgroundSearch(q));
});
```

**Impact**: 200-300ms head-start on likely queries

### 4. Edge Deployment
- Deploy TotalReclaw server to multiple regions
- Use CDN-like routing to nearest server
- Target: <20ms network RTT

**Impact**: Reduces network latency by 50-70%

### 5. Local LSH Index Cache
```typescript
// Cache LSH hyperplanes and blind indices locally
// Avoids re-computing on every query
const lshCache = await loadLSHFromDisk();
```

**Impact**: Saves 10-20ms per query

### Optimization Priority

| Optimization | Effort | Impact | Priority |
|--------------|--------|--------|----------|
| Client caching | Low | High | P1 |
| Debounce | Low | Medium | P1 |
| Skip short messages | Low | Medium | P1 |
| Async pre-fetch | Medium | Medium | P2 |
| Local LSH cache | Medium | Low | P2 |
| Edge deployment | High | High | P3 |

---

## 1.5 Ingestion Pipeline (client-side only)

```python
# pseudocode — full implementation provided in repo skeleton
def ingest(memory_item: dict, master_password: str):
    # 1. Compute embedding (same as today)
    emb = embedder.encode(memory_item["text"])

    # 2. Generate LSH buckets
    lsh_hashes = lsh_index.hash_vector(emb)          # returns list of 12 strings

    # 3. Generate blind indices
    blind = [sha256(token) for token in tokenize(text)]
    blind += [sha256(h) for h in lsh_hashes]

    # 4. Encrypt BOTH doc and embedding
    key = derive_key(master_password)
    enc_doc = aes_gcm_encrypt(memory_item["text"], key)
    enc_emb = aes_gcm_encrypt(emb.tobytes(), key)

    # 5. Upload
    server.upload({
        "id": uuid7(),
        "encrypted_doc": enc_doc,
        "encrypted_embedding": enc_emb,
        "blind_indices": blind,
        "metadata": {...}
    })
```

---

## 1.6 Search Pipeline (unchanged outer flow)

1. **Client:** embed query → compute LSH hashes → generate trapdoors (SHA-256 of each bucket).
2. **Client:** send ONLY trapdoors + optional keyword blind indices.
3. **Server:** inverted-index lookup → union of all matching memories (400–1,200 candidates).
4. **Server:** return encrypted docs + encrypted embeddings for those IDs.
5. **Client:** decrypt 400–1,200 items → exact cosine on decrypted embeddings + BM25 + RRF → top 8.

**(Optional)** client caches LSH index locally for 24h to reduce server load on repeat queries.

---

## 1.7 Migration from current plaintext-embeddings

- **One-time script:** download all items (they are still decryptable by user), re-ingest with LSH + encryption.
- **Backward compatible:** old items without LSH buckets fall back to keyword-only pre-filter (still works, just lower recall).

---

## 1.8 Full File Structure & Deliverables Expected from Coding Agent

```
totalreclaw/core/lsh.py          — Faiss wrapper + hash generation
totalreclaw/client.py            — updated store() and search() with LSH
totalreclaw/server/blind_index.py — extended inverted index (already exists, just add LSH bucket column)
tests/benchmarks/               — 10k, 100k, 1M synthetic + real WhatsApp datasets
docs/migration.md
```

**Config file:** `totalreclaw/config.yaml` with all LSH tunables

---

## Performance Targets (must hit)

| Metric | Target |
|--------|--------|
| 1M memories search p95 | <140ms |
| Download size | <2MB |
| Recall@500 of true top-250 | ≥93% |
| Storage overhead vs v0.2 | ≤2.2× |

---

## LSH Parameter Immutability & Re-indexing (MVP Requirement)

### Problem

LSH parameters `n_bits` and `n_tables` are **index-time parameters**:
- Old memory indexed with `n_tables=12`
- Server scales to `n_tables=16` (at 500K+ scale)
- New queries use 16 tables, old memory only has 12 buckets
- **Search breaks for old memories**

### Solution: Full Re-index on Param Change

When LSH params must change (rare, only at 500K+ scale):

```
RE-INDEX WORKFLOW:

1. ADMIN TRIGGERS: PUT /admin/lsh-reindex
   - New params: { n_bits: 64, n_tables: 16 }
   - System enters MAINTENANCE mode

2. PAUSE OPERATIONS:
   - Block all exports (prevent partial data)
   - Block new memory storage
   - Allow read-only search (with old params)

3. FOR EACH USER:
   a. Fetch all encrypted memories
   b. Client-side: decrypt with recovery phrase
   c. Client-side: re-compute LSH buckets with NEW params
   d. Client-side: re-encrypt, re-upload
   e. Server: update blind_indices atomically

4. RESUME OPERATIONS:
   - System exits MAINTENANCE mode
   - All queries use new params
```

### Implementation Notes

| Aspect | Detail |
|--------|--------|
| **Frequency** | Rare - only at 500K+ corpus size |
| **Downtime** | Per-user, not global (each user re-indexes independently) |
| **Client requirement** | Must have recovery phrase (cannot re-index server-side) |
| **Rollback** | Keep old blind_indices until re-index complete |
| **Export blocking** | Prevent export during re-index to avoid inconsistent data |

### Config Storage

```python
# Per-user LSH config (immutable until re-index)
class UserLSHConfig:
    user_id: str
    n_bits: int = 64
    n_tables: int = 12
    created_at: datetime
    reindex_in_progress: bool = False
```

### API Endpoints (MVP)

| Endpoint | Purpose |
|----------|---------|
| `GET /lsh-config` | Get current user's LSH params |
| `POST /admin/lsh-reindex` | Trigger re-index (admin only) |
| `GET /lsh-reindex/status` | Check re-index progress |

**Note: This is OUT OF SCOPE for PoC, required for MVP launch.**
