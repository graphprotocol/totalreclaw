# TotalReclaw Gas Cost Report

**Generated:** 2026-03-02T14:13:22.671Z
**Network:** Hardhat (local)
**Contract:** EventfulDataEdge (fallback -> Log event)
**Payload format:** Protobuf-encoded encrypted facts

## Assumptions

| Parameter | Value |
|-----------|-------|
| Base L2 data cost | $0.001000/KB (post-EIP-4844) |
| Base L2 gas price | 0.05 gwei |
| ETH price | $3500.00 |
| Embedding dims | 640 (float32) |
| Encryption overhead | 28 bytes (AES-256-GCM: 12B IV + 16B tag) |

## Per-Fact Gas Measurements

| Fact Type | Words | Blind Indices | Embedding | Calldata (bytes) | Gas Used | Gas/Byte |
|-----------|-------|---------------|-----------|-----------------|----------|----------|
| Small (20w, 50 idx, emb) | 20 | 50 | Yes | 6,807 | 293,250 | 43.1 |
| Small (20w, 50 idx, no emb) | 20 | 50 | No | 3,676 | 168,010 | 45.7 |
| Medium (50w, 80 idx, emb) | 50 | 80 | Yes | 8,967 | 379,650 | 42.3 |
| Medium (50w, 80 idx, no emb) | 50 | 80 | No | 5,836 | 254,410 | 43.6 |
| Large (100w, 120 idx, emb) | 100 | 120 | Yes | 11,907 | 497,220 | 41.8 |
| Large (100w, 120 idx, no emb) | 100 | 120 | No | 8,776 | 372,010 | 42.4 |
| Minimal (5w, 10 idx, no emb) | 5 | 10 | No | 945 | 58,770 | 62.2 |
| Heavy indices (30w, 200 idx, emb) | 30 | 200 | Yes | 16,767 | 691,680 | 41.3 |
| XL fact (200w, 150 idx, emb) | 200 | 150 | Yes | 14,487 | 600,330 | 41.4 |
| XL no emb (200w, 150 idx, no emb) | 200 | 150 | No | 11,356 | 475,150 | 41.8 |

## Gas Per Byte Analysis

| Metric | Gas/Byte |
|--------|----------|
| Average | 44.6 |
| Min | 41.3 |
| Max | 62.2 |

> **Note:** Gas per byte decreases with payload size because the fixed base
> cost (~21,000 gas for tx + ~1,200 for Log event) is amortized over more bytes.

## Embedding Cost Impact

| Comparison | Avg Gas (with emb) | Avg Gas (no emb) | Embedding Overhead |
|------------|-------------------|-----------------|-------------------|
| 20 words | 293,250 | 168,010 | +125,240 (+74.5%) |
| 50 words | 379,650 | 254,410 | +125,240 (+49.2%) |
| 100 words | 497,220 | 372,010 | +125,210 (+33.7%) |
| 200 words | 600,330 | 475,150 | +125,180 (+26.3%) |

## Cost Extrapolation (Base L2)

Estimated costs on Base L2 mainnet for different fact volumes.

**Representative fact:** Medium (50w, 80 idx, emb)
- Calldata: 8,967 bytes (8.76 KB)
- Gas: 379,650

### Per-Fact Cost Breakdown

| Component | Cost |
|-----------|------|
| L2 execution | $0.0664 |
| L1 data posting | $0.008757 |
| **Total per fact** | **$0.0752** |

### Volume Extrapolation

| Volume | Total Gas | Total Calldata | L2 Exec Cost | L1 Data Cost | Total Cost |
|--------|-----------|---------------|-------------|-------------|-----------|
| 5,000 facts | 1,898,250,000 | 42.8 MB | $332.19 | $43.78 | $375.98 |
| 50,000 facts | 18,982,500,000 | 427.6 MB | $3321.94 | $437.84 | $3759.78 |
| 500,000 facts | 189,825,000,000 | 4275.8 MB | $33219.38 | $4378.42 | $37597.79 |
| 5,000,000 facts | 1,898,250,000,000 | 42758.0 MB | $332193.75 | $43784.18 | $375977.93 |
| 50,000,000 facts | 18,982,500,000,000 | 427579.9 MB | $3321937.50 | $437841.80 | $3759779.30 |

### Cost Per Fact Type (Base L2)

| Fact Type | Calldata | L2 Exec | L1 Data | Total |
|-----------|----------|---------|---------|-------|
| Small (20w, 50 idx, emb) | 6,807 B | $0.0513 | $0.006647 | $0.0580 |
| Small (20w, 50 idx, no emb) | 3,676 B | $0.0294 | $0.003590 | $0.0330 |
| Medium (50w, 80 idx, emb) | 8,967 B | $0.0664 | $0.008757 | $0.0752 |
| Medium (50w, 80 idx, no emb) | 5,836 B | $0.0445 | $0.005699 | $0.0502 |
| Large (100w, 120 idx, emb) | 11,907 B | $0.0870 | $0.0116 | $0.0986 |
| Large (100w, 120 idx, no emb) | 8,776 B | $0.0651 | $0.008570 | $0.0737 |
| Minimal (5w, 10 idx, no emb) | 945 B | $0.0103 | $0.000923 | $0.0112 |
| Heavy indices (30w, 200 idx, emb) | 16,767 B | $0.1210 | $0.0164 | $0.1374 |
| XL fact (200w, 150 idx, emb) | 14,487 B | $0.1051 | $0.0141 | $0.1192 |
| XL no emb (200w, 150 idx, no emb) | 11,356 B | $0.0832 | $0.0111 | $0.0942 |

## Key Takeaways

1. **Base gas cost** is dominated by the 21,000 intrinsic transaction gas.
   The Log event itself adds ~1,200 gas plus calldata costs.
2. **Embeddings** (1024-dim float32, encrypted) add ~8,248 bytes of calldata,
   which is the single largest component of most facts.
3. **Blind indices** at 64 hex chars (32 bytes) + protobuf overhead each,
   scale linearly. 100 indices ~ 6.6 KB of calldata.
4. **On Base L2**, costs are extremely low: a medium fact with embedding
   costs approximately $0.0752 per write.
5. **Batching** multiple facts per transaction would amortize the 21,000
   intrinsic gas cost, reducing per-fact cost further.
