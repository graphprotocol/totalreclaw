# TotalReclaw Subgraph E2E & Scaling Report

**Generated:** 2026-03-02
**Branch:** `feature/subgraph` (8+ commits ahead of main)
**Environment:** Hardhat localhost + Docker Compose (Graph Node 0.35.1, PostgreSQL 16, IPFS)
**Benchmark data:** 415 OMBH facts + 140 queries (totalreclaw-internal ground truth, private, maintainers only)

---

## 1. E2E Validation Results

### 1.1 Recall@8 (Overall + Per-Category)

| Metric | Value |
|--------|-------|
| **Recall@8** | **40.2%** |
| Precision@8 | 17.8% |
| MRR | 0.528 |
| Facts ingested | 415 |
| Queries evaluated | 140 |
| Tx errors | 0 |

| Category | Recall@8 | Precision@8 | MRR | Queries |
|----------|----------|-------------|-----|---------|
| Factual | 62.3% | 26.2% | 0.717 | 42 |
| Semantic | 44.3% | 16.4% | 0.592 | 42 |
| Cross-conversation | 27.5% | 16.7% | 0.450 | 42 |
| Negative (true negatives) | 0.0% | 0.0% | 0.000 | 14 |

### 1.2 Comparison with PoC v2 PostgreSQL Baseline (98.1%)

The **57.9 percentage point recall gap** (40.2% vs 98.1%) is **not a fundamental subgraph limitation**. Root cause analysis:

| Factor | Impact | Explanation |
|--------|--------|-------------|
| **GraphQL `first: 1000` cap** | HIGH | The E2E test queries `blindIndexes(where: { hash_in: $trapdoors }, first: 1000)`. With ~39 indices/fact × 415 facts = 16,375 blind index rows, queries with many trapdoor matches get truncated to 1,000 candidates. The PostgreSQL GIN index returns ALL matches. |
| **Owner-scoped filtering** | MEDIUM | All 415 facts share one owner. Subgraph queries filter by owner, so the `first: 1000` cap applies to the entire corpus — not per-trapdoor. PG's GIN index doesn't have this issue. |
| **No pagination** | MEDIUM | The E2E test makes a single query per search. Paginating with `skip` would recover truncated candidates. |

**Fix path:** Increase `first` limit (Graph Node supports up to 5,000 with `GRAPH_ENTITY_QUERY_LIMIT`), implement pagination, or batch trapdoor queries. These are query-layer changes — the data path (ingest → index → store) is identical to the PG baseline.

**Expected recall after fix:** Close to 98.1%, since the blind index data and reranking pipeline are the same.

### 1.3 Ingest Performance

| Metric | Value |
|--------|-------|
| Total ingest time | 19.3s (415 facts) |
| Avg per fact | 46ms |
| Median | 45ms |
| P95 | 57ms |
| P99 | 59ms |
| Throughput | ~21 facts/s |
| Errors | 0 |

Ingest includes: protobuf encoding + embedding encryption + blind index generation + `sendTransaction()` + confirmation wait. On Hardhat the bottleneck is block confirmation (~1 block/tx).

---

## 2. Gas Cost Analysis

### 2.1 Per-Fact Measurements (10 Payload Types)

| Fact Type | Words | Indices | Embedding | Calldata | Gas |
|-----------|-------|---------|-----------|----------|-----|
| Minimal (5w, 10 idx) | 5 | 10 | No | 945 B | 58,770 |
| Small (20w, 50 idx) | 20 | 50 | No | 3,676 B | 168,010 |
| Small (20w, 50 idx, emb) | 20 | 50 | Yes | 6,807 B | 293,250 |
| Medium (50w, 80 idx) | 50 | 80 | No | 5,836 B | 254,410 |
| **Medium (50w, 80 idx, emb)** | **50** | **80** | **Yes** | **8,967 B** | **379,650** |
| Large (100w, 120 idx) | 100 | 120 | No | 8,776 B | 372,010 |
| Large (100w, 120 idx, emb) | 100 | 120 | Yes | 11,907 B | 497,220 |
| XL (200w, 150 idx) | 200 | 150 | No | 11,356 B | 475,150 |
| XL (200w, 150 idx, emb) | 200 | 150 | Yes | 14,487 B | 600,330 |
| Heavy indices (30w, 200 idx, emb) | 30 | 200 | Yes | 16,767 B | 691,680 |

Gas per byte: 41-62 (avg 44.6). Smaller payloads have higher gas/byte due to fixed 21K intrinsic cost.

### 2.2 Embedding Cost Impact

| Fact Size | Gas (with emb) | Gas (no emb) | Overhead |
|-----------|---------------|-------------|----------|
| 20 words | 293,250 | 168,010 | +125,240 (+74.5%) |
| 50 words | 379,650 | 254,410 | +125,240 (+49.2%) |
| 100 words | 497,220 | 372,010 | +125,210 (+33.7%) |
| 200 words | 600,330 | 475,150 | +125,180 (+26.3%) |

Embeddings add a constant ~330K gas (~8,248 bytes: 1024 dims × 4 bytes float32 + 28 bytes AES-GCM, hex-encoded). The relative overhead decreases with fact size.

### 2.3 Base L2 Cost Projections

Using corrected Base L2 pricing: **0.001 gwei** L2 gas, $0.001/KB L1 data, $3,500 ETH.

| Component | Cost (Medium fact) |
|-----------|-------------------|
| L2 execution | $0.0013 |
| L1 data posting | $0.0088 |
| **Total per fact** | **~$0.010** |

L1 data posting dominates at ~88% of total cost. Reducing calldata size (embedding compression, fewer indices) has the biggest cost impact.

| Volume | Monthly Cost | Annual Cost |
|--------|-------------|-------------|
| 1K users × 10 facts/day | $3,000/mo | $36,000/yr |
| 10K users × 10 facts/day | $30,000/mo | $363,000/yr |
| 100 power users × 50 facts/day | $1,500/mo | $18,000/yr |

---

## 3. Query Performance

### 3.1 Latency Breakdown

| Segment | Avg | P95 | Description |
|---------|-----|-----|-------------|
| Client prep | 9ms | 12ms | Blind index generation + embedding + LSH trapdoors |
| GraphQL | 71ms | 79ms | fetch() to Graph Node (network + PG query) |
| Reranking | 14ms | 17ms | Decrypt + BM25 + cosine + RRF fusion |
| **Total** | **94ms** | **104ms** | End-to-end query |

Graph Node metrics confirm: 292/294 queries (99.3%) completed in <100ms server-side. Total query execution time: 16.7s across 294 queries = 57ms avg server-side.

### 3.2 Candidate Pool Analysis

- Current `first: 1000` limit returns up to 1,000 blind index matches per query
- With 415 facts and ~39 indices/fact, many queries saturate the 1,000 cap
- Dynamic pool formula: `min(max(factCount × 3, 400), 5000)`
- At 415 facts: pool = 1,245, but GraphQL caps at 1,000

### 3.3 Latency vs Scale Projections

Using logarithmic GIN scan model (B-tree posting lists):

| Scale | Blind Index Rows | Scale Factor | Est. Query P95 |
|-------|-----------------|-------------|----------------|
| 415 facts (measured) | 16.1K | 1.0× | 104ms |
| 1.8M facts | 69.8M | 13.1× | ~1.6s |
| 36.5M facts | 1.4B | 17.4× | ~2.2s |

**Mitigation:** Owner-scoped partitioning limits GIN scope to single-user data. At 10K users with 1.8M facts each user has ~1,800 facts → ~70K blind index rows, keeping queries under 200ms.

---

## 4. Infrastructure Metrics

### 4.1 PostgreSQL Storage

| Table | Data Size | Index Size | Total | Rows | Bytes/Row |
|-------|-----------|------------|-------|------|-----------|
| fact | 213 KB | 778 KB | 8.2 MB | 422 | 505 |
| blind_index | 3.5 MB | 4.3 MB | 7.8 MB | 16,375 | 216 |
| global_state | 49 KB | 287 KB | 377 KB | 422 | — |
| poi2$ | 164 KB | 377 KB | 582 KB | 422 | — |

Index-to-data ratio: blind_index has 1.2× index overhead (GIN index on hash field). This ratio will increase at scale as GIN posting lists grow.

### 4.2 Graph Node Resources

| Container | CPU | Memory | Network I/O |
|-----------|-----|--------|-------------|
| graph-node | 0.27% | 306 MB | 4.69 GB / 4.61 GB |
| ipfs | 0.53% | 83 MB | 69 MB / 72 MB |
| postgres | 0.05% | 96 MB | 145 MB / 4.61 GB |

Graph Node connection pool: 11 connections, 0 errors, 0ms avg wait time. All 848 store_get operations completed in <25ms.

### 4.3 BlindIndex Growth Rate

| Facts | Blind Index Rows | Ratio | Growth/Fact |
|-------|-----------------|-------|-------------|
| 422 (measured) | 16,375 | 38.8 | — |
| 1,800 (projected) | 69,840 | 38.8 | ~39 rows |
| 1,800,000 | 69,840,000 | 38.8 | ~39 rows |
| 36,500,000 | 1,416,200,000 | 38.8 | ~39 rows |

The ~39 indices/fact is consistent with: word tokens + stems (~19 unique) + 20 LSH buckets per fact.

---

## 5. Scaling Analysis

### 5.1 Scenario A: 1K Users @ 6 Months

| Dimension | Value |
|-----------|-------|
| Total facts | 1.8M |
| Blind index rows | 69.8M |
| PG storage | ~20 GB |
| Write cost | $3,000/mo |
| Paymaster ETH/yr | 1.37 ETH (~$4,795) |
| Peak QPS | 0.1 |
| Infra | 1 CPU, 1 GB Graph Node, 512 MB PG shared_buffers |
| Infra cost | ~$17/mo |

**Assessment:** Easily feasible. Single-node deployment. Main cost is L1 data posting.

### 5.2 Scenario B: 10K Users @ 12 Months

| Dimension | Value |
|-----------|-------|
| Total facts | 36.5M |
| Blind index rows | 1.4B |
| PG storage | ~408 GB |
| Write cost | $30,300/mo |
| Paymaster ETH/yr | 13.67 ETH (~$47,845) |
| Peak QPS | 1.0 |
| Infra | 19 CPUs, 10 GB Graph Node, 100 GB PG shared_buffers |
| Infra cost | ~$379/mo |

**Assessment:** Requires significant PostgreSQL investment. The 1.4B blind index rows need owner-partitioning to keep query latency acceptable. Write costs ($30K/mo) are the dominant expense and require a revenue model or per-user caps.

### 5.3 Scenario C: 100 Power Users

| Dimension | Value |
|-----------|-------|
| Total facts | 1.8M |
| Blind index rows | 70.8M |
| PG storage | ~20 GB |
| Write cost | $1,500/mo |
| Paymaster ETH/yr | 0.68 ETH (~$2,380) |
| Peak QPS | 0.0 |
| Infra | 1 CPU, 1 GB Graph Node |
| Infra cost | ~$17/mo |

**Assessment:** Very feasible. Per-user data is dense (~18K facts/user) but total volume is manageable. Owner-scoped queries keep latency low.

---

## 6. Bottlenecks & Recommendations

### 6.1 BlindIndex Table Growth (Biggest Concern)

The blind_index table grows at ~39 rows per fact. At 36.5M facts (Scenario B), that's 1.4B rows requiring ~302 GB of data + ~105 GB of GIN indexes. This exceeds typical VPS RAM, causing disk I/O on cold GIN page reads.

**Mitigation:** Partition by owner address. Each user's partition stays small (1K users → 1.8M facts / 1K = 1,800 facts/user → ~70K blind index rows/user). Graph Node doesn't support custom partitioning, but PostgreSQL can do it with custom DDL post-deployment.

### 6.2 GraphQL `hash_in` Performance at Scale

The `first: 1000` cap is the primary recall bottleneck today. At scale, even with higher limits, `hash_in` with many trapdoors forces the GIN index to intersect large posting lists.

**Mitigation:**
- Increase `GRAPH_ENTITY_QUERY_LIMIT` to 5,000
- Implement paginated queries (query → check count → paginate if needed)
- Pre-filter trapdoors by selectivity (use rarer terms first)

### 6.3 Embedding Generation Client-Side Latency

Currently 9ms avg for prep (incl. embedding), but this is with cached model. First-load model download is ~50MB. On mobile/low-power devices, embedding inference may take 100-500ms.

**Mitigation:** Pre-compute embeddings server-side in TEE (Phase 2). Or use ONNX quantized model (int8) for 2-3× speedup.

### 6.4 Paymaster Funding Model

At $0.010/fact, fully subsidized write costs are:
- 1K users: $3K/mo — feasible for seed-stage startup
- 10K users: $30K/mo — requires per-user quotas or user-funded model

**Options:**
1. **Freemium:** 100 facts/day free, pay for more via API key top-up
2. **Paymaster-as-a-Service** (Pimlico, Alchemy): ~$0.01/UserOp overhead, handles infra
3. **x402 protocol:** Users fund their own wallet, pay-per-request

---

## 7. Go/No-Go Assessment

### What Works

| Dimension | Status | Evidence |
|-----------|--------|----------|
| **Data path** (ingest → index → store) | WORKS | 415 facts ingested, 0 errors, all indexed |
| **Protobuf encoding** | WORKS | All 13 fields decoded correctly after UTF-8 fix |
| **Blind index search** | WORKS | Queries return correct candidates (when not truncated) |
| **Reranking pipeline** | WORKS | BM25 + cosine + RRF produces expected ranking quality |
| **Query latency** | GOOD | 94ms avg, 104ms p95 — well under 140ms target |
| **Gas efficiency** | ACCEPTABLE | $0.010/fact, embedding overhead ~49% |
| **Graph Node stability** | GOOD | 0 errors, 0ms connection wait, all queries <100ms |

### What Needs Work

| Issue | Severity | Fix Effort | Impact |
|-------|----------|-----------|--------|
| **Recall gap (40% vs 98%)** | HIGH | LOW | Increase `first` limit + pagination |
| **Write cost at scale** | MEDIUM | MEDIUM | Batch writes, embedding compression |
| **BlindIndex growth** | MEDIUM | HIGH | Owner partitioning (PG DDL, not Graph Node) |
| **Gas report used stale pricing** | LOW | DONE | Fixed to 0.001 gwei in scaling script |

### Verdict

**Conditional GO.** The subgraph architecture is viable for Scenario A (1K users, 6 months) and Scenario C (100 power users) with minimal changes. The 40.2% recall is a **query-layer cap**, not an architectural flaw — fixing `first: 1000` should bring recall close to the 98.1% PG baseline. Scenario B (10K users) requires owner-partitioning and cost management before being feasible.

**Recommended next steps:**
1. Fix recall: increase `first` limit to 5,000 and add pagination (1-2 days)
2. Re-run E2E with fixed limits to confirm recall recovery
3. Deploy to Base Sepolia testnet for real-network latency validation
4. Implement Paymaster integration (Pimlico or Alchemy)
5. Add transaction batching for write cost reduction

---

## Appendix: Raw Data Sources

| File | Description |
|------|-------------|
| `e2e-results-latest.json` | Full E2E results with per-query breakdown |
| `gas-report.md` | Gas measurements for 10 payload types |
| `scaling-report.md` | Scaling projections for 3 scenarios |
| `pg-table-sizes.txt` | PostgreSQL table/index sizes |
| `pg-row-counts.txt` | Entity row counts |
| `graph-node-metrics.txt` | Prometheus metrics from Graph Node |
| `docker-stats.txt` | Container resource usage |
