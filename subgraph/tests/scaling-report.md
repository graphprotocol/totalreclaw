# TotalReclaw Subgraph Scaling Analysis

**Generated:** 2026-03-02T16:42:38.881Z
**Based on:** E2E validation with 415 facts, 140 queries
**Data sources:** e2e-results-latest.json, gas-report.md, pg-table-sizes

## Measured Baseline

| Metric | Value |
|--------|-------|
| Facts ingested | 415 |
| Facts indexed (Graph Node) | 415 |
| BlindIndex entities (sampled) | >= 1000 |
| Estimated indices/fact | 38.8 |
| Recall@8 | 40.2% |
| Precision@8 | 17.8% |
| MRR | 0.528 |
| Query latency (avg) | 94ms |
| Query latency (p95) | 104ms |
| Ingest latency (avg/fact) | 46ms |
| Gas per fact (Medium 50w, 80 idx, emb) | 379.6K gas, 9.0K bytes calldata |
| Cost per fact (Base L2) | $0.010 |

**Per-category recall:**

| Category | Recall@8 | Precision@8 | MRR | Queries |
|----------|----------|-------------|-----|---------|
| factual | 62.3% | 26.2% | 0.717 | 42 |
| semantic | 44.3% | 16.4% | 0.592 | 42 |
| cross_conversation | 27.5% | 16.7% | 0.450 | 42 |
| negative | 0.0% | 0.0% | 0.000 | 14 |

## Scenario Definitions

| Scenario | Users | Facts/day/user | Duration | Total Facts | Queries/day |
|----------|-------|----------------|----------|-------------|-------------|
| A (6-mo MVP) | 1.0K | 10 | 180 days | 1.8M | 8.4K |
| B (12-mo) | 10.0K | 10 | 365 days | 36.5M | 84.0K |
| C (Power) | 100 | 50 | 365 days | 1.8M | 1.4K |

## 1. Storage Growth Projections

Assumptions: 505 bytes/fact row, 216 bytes/blind_index row, 39 indices/fact.

| Scenario | Total Facts | Blind Index Rows | PG Data Size | PG Index Size (est.) | Total Storage |
|----------|-------------|-----------------|-------------|---------------------|---------------|
| A (6-mo MVP) | 1.8M | 69.8M | 14.9 GB | 5.2 GB | 20.1 GB |
| B (12-mo) | 36.5M | 1.4B | 302.1 GB | 105.7 GB | 407.8 GB |
| C (Power) | 1.8M | 70.8M | 15.1 GB | 5.3 GB | 20.4 GB |

## 2. Write Cost Projections (Base L2)

Assumptions: $0.0010/KB L1 data, 0.001 gwei L2 gas, $3500 ETH, 9.0K bytes calldata/fact, 379.6K gas/fact.

| Scenario | Facts/month | Gas/month | Calldata/month | Monthly Cost | Annual Cost | Paymaster ETH/yr |
|----------|-------------|-----------|---------------|-------------|-------------|------------------|
| A (6-mo MVP) | 300.0K | 113.9B | 2.5 GB | $3.0K | $36.3K | 1.3667 ETH |
| B (12-mo) | 3.0M | 1139.0B | 25.1 GB | $30.3K | $363.1K | 13.6674 ETH |
| C (Power) | 150.0K | 56.9B | 1.3 GB | $1.5K | $18.2K | 0.6834 ETH |

## 3. Query Performance Projections

Baseline: 415 facts, ~16.1K blind index rows, 94ms avg / 104ms p95 query latency.
GIN scan time model: logarithmic growth with B-tree posting list size.

| Scenario | Total Facts | Blind Index Rows | Scale Factor | Est. GIN Scan (p95) | Total Query p95 | Dynamic Pool Size |
|----------|-------------|-----------------|-------------|--------------------|-----------------|--------------------|
| A (6-mo MVP) | 1.8M | 69.8M | 13.1x | 1.4s | 1.6s | 5.0K |
| B (12-mo) | 36.5M | 1.4B | 17.4x | 1.8s | 2.2s | 5.0K |
| C (Power) | 1.8M | 70.8M | 13.1x | 1.4s | 1.6s | 5.0K |

> **Note:** "Total Query p95" includes ~20% overhead for network round-trip, decryption, and reranking on top of the GIN scan estimate.

## 4. Infrastructure Requirements

Baseline: 415 facts requires ~1 CPU, 512 MB for Graph Node + PostgreSQL.

| Scenario | PG Storage | PG shared_buffers | Graph Node CPU | Graph Node Memory | RPC Node Tier | Est. Infra Cost/mo |
|----------|-----------|-------------------|---------------|-------------------|---------------|-------------------|
| A (6-mo MVP) | 20.1 GB | 5148 MB | 1 core(s) | 973 MB | Public (free) | ~$16.76 |
| B (12-mo) | 407.8 GB | 104399 MB | 19 core(s) | 9856 MB | Dedicated ($50-200/mo) | ~$378.91 |
| C (Power) | 20.4 GB | 5220 MB | 1 core(s) | 979 MB | Public (free) | ~$16.82 |

## 5. Concurrency Analysis

Assumptions: 20% users active, 3 sessions/day, 7 queries/session, 12 active hours.
Graph Node throughput: ~100-500 QPS for simple subgraphs (single entity lookups + GIN scan).

| Scenario | Active Users | Peak QPS | Graph Node Capacity (est.) | Headroom | Nodes Needed |
|----------|-------------|----------|---------------------------|----------|-------------|
| A (6-mo MVP) | 200 | 0.1 | ~65 QPS/node | 100% | 1 |
| B (12-mo) | 2.0K | 1.0 | ~50 QPS/node | 98% | 1 |
| C (Power) | 20 | 0.0 | ~65 QPS/node | 100% | 1 |

## 6. Key Bottlenecks

Ordered by expected impact as scale increases:

1. **GIN index scan time** -- The blind_index table grows linearly with facts. At 36.5M facts with ~80 indices each, the GIN index holds ~2.9B entries. PostgreSQL GIN performance degrades when posting lists exceed available shared_buffers, causing disk I/O spikes.

2. **Calldata costs on L1** -- While Base L2 execution is cheap, L1 data posting (the dominant cost component) scales linearly. Batching multiple facts per transaction can amortize the per-tx overhead but does not reduce L1 data volume.

3. **Graph Node indexing throughput** -- Graph Node processes blocks sequentially. High write volumes (Scenario B: 100K facts/day = ~1.2/sec sustained) may cause indexing lag if block processing is slower than block production.

4. **Client-side decryption + reranking** -- The dynamic candidate pool (up to 5,000 facts) requires AES-GCM decryption + BM25/cosine reranking client-side. At 5K candidates, this adds 50-200ms depending on client hardware.

5. **PostgreSQL storage I/O** -- At Scenario B scale (~49 GB total), the working set exceeds typical VPS memory. Queries hitting cold pages incur SSD latency (~0.1ms/page) which compounds with GIN scan fan-out.

6. **RPC node rate limits** -- Public Base L2 RPC endpoints throttle at ~10-50 req/s. Dedicated nodes ($50-200/mo) raise this to 500-1000 req/s but add infrastructure cost.

## 7. Recommendations

### Scenario A (6-mo MVP, 1K users)

- **Infrastructure:** Single Graph Node instance (1 CPU, 1 GB) + PostgreSQL with 512 MB shared_buffers is sufficient.
- **RPC:** Public Base L2 endpoint is adequate for write volume (~300 txs/day per user).
- **Cost:** Write costs are negligible (~$3.0K/mo). Focus on infra hosting cost.
- **Action items:** Deploy with basic monitoring. No optimization needed yet.

### Scenario B (12-mo, 10K users)

- **Infrastructure:** Upgrade to 2-4 CPU cores, 4+ GB RAM for PostgreSQL. Consider read replicas for query load.
- **RPC:** Dedicated Base L2 RPC node required ($50-200/mo).
- **Storage:** Plan for ~50 GB PostgreSQL storage with SSD-backed volumes.
- **Optimization priorities:**
  1. Implement transaction batching (10-50 facts/tx) to reduce per-fact gas overhead.
  2. Add blind_index table partitioning by owner to reduce GIN scan scope.
  3. Consider caching hot blind index lookups in Redis/memcached.
  4. Implement connection pooling (PgBouncer) for Graph Node <-> PostgreSQL.
- **Cost:** Write costs remain low (~$30.3K/mo) but infra costs become the primary expense.

### Scenario C (Power users, 100 users x 50 facts/day)

- **Infrastructure:** Similar to Scenario A in absolute terms (1.8M facts total) but with higher per-user density.
- **Key difference:** Fewer users means less concurrency pressure but more data per user increases candidate pool sizes.
- **Optimization:** Per-user blind_index partitioning is highly effective here -- each user's index stays small (~18K facts, ~1.4M blind index rows).
- **RPC:** Public endpoint is sufficient given low tx volume.
- **Cost:** Very low (~$1.5K/mo write costs).

### Cross-Scenario Recommendations

1. **Batch writes:** Combine 10-50 facts per on-chain transaction to amortize 21,000 base gas. Requires contract upgrade to handle batch protobuf payloads.
2. **Tiered storage:** Archive facts older than 90 days to cold storage; keep active facts in hot GIN index.
3. **Embedding compression:** Quantize 1024-dim float32 embeddings to int8 (4x size reduction: 4,096B -> 1,024B) before encryption. Reranking quality impact is minimal for BM25+cosine fusion.
4. **Index pruning:** Periodically compact blind_index table by removing entries for superseded/deleted facts.
5. **Horizontal scaling:** For >10K users, shard by user prefix (first 2 bytes of owner address) across multiple Graph Node instances.
