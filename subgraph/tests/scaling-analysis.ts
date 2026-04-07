/**
 * TotalReclaw Subgraph Scaling Analysis
 *
 * Reads measured E2E data (validation results, gas measurements, PostgreSQL
 * table sizes) and generates a scaling-report.md with projections across
 * three user scenarios.
 *
 * Usage (from subgraph/):
 *   npx tsx --tsconfig tsconfig.node.json tests/scaling-analysis.ts
 *
 * Inputs (all optional — uses reasonable defaults when files are missing):
 *   - ./e2e-results/e2e-results-latest.json
 *   - ./gas-report.md
 *   - ./e2e-results/pg-table-sizes.txt
 *   - ./e2e-results/pg-row-counts.txt
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface E2EResults {
  timestamp: string;
  factCount: number;
  queryCount: number;
  txErrors: number;
  indexedCount: number;
  blindIndexSample: number;
  ingest: {
    totalMs: number;
    avgMs: number;
    medianMs: number;
    p95Ms: number;
    p99Ms: number;
  };
  query: {
    totalMs: number;
    avgMs: number;
    medianMs: number;
    p95Ms: number;
    p99Ms: number;
  };
  overall: {
    recall8: number;
    precision8: number;
    mrr: number;
  };
  categories: Record<string, {
    recall: number;
    precision: number;
    mrr: number;
    count: number;
  }>;
}

interface GasData {
  calldataBytes: number;
  gasUsed: number;
  blindIndexCount: number;
}

interface Scenario {
  name: string;
  users: number;
  factsPerDayPerUser: number;
  durationDays: number;
  totalFacts: number;
  queriesPerDay: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BASE_L2_COST_PER_KB_USD = 0.001;
const BASE_L2_GAS_PRICE_GWEI = 0.001; // Base L2 typically 0.001 gwei or less
const ETH_PRICE_USD = 3500;

/** Default bytes-per-row estimates when pg-table-sizes.txt is unavailable. */
const DEFAULT_BYTES_PER_FACT_ROW = 200;
const DEFAULT_BYTES_PER_BLIND_INDEX_ROW = 100;

const SCENARIOS: Scenario[] = [
  {
    name: 'A (6-mo MVP)',
    users: 1_000,
    factsPerDayPerUser: 10,
    durationDays: 180,
    totalFacts: 1_800_000,
    queriesPerDay: 8_400,
  },
  {
    name: 'B (12-mo)',
    users: 10_000,
    factsPerDayPerUser: 10,
    durationDays: 365,
    totalFacts: 36_500_000,
    queriesPerDay: 84_000,
  },
  {
    name: 'C (Power)',
    users: 100,
    factsPerDayPerUser: 50,
    durationDays: 365,
    totalFacts: 1_825_000,
    queriesPerDay: 1_400,
  },
];

// ---------------------------------------------------------------------------
// File Reading Helpers
// ---------------------------------------------------------------------------

function readE2EResults(baseDir: string): E2EResults | null {
  const filePath = path.resolve(baseDir, 'e2e-results', 'e2e-results-latest.json');
  if (!fs.existsSync(filePath)) {
    console.warn(`  WARN: ${filePath} not found — using defaults`);
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch (err) {
    console.warn(`  WARN: Failed to parse ${filePath}: ${err}`);
    return null;
  }
}

function parseGasReport(baseDir: string): GasData | null {
  const filePath = path.resolve(baseDir, 'gas-report.md');
  if (!fs.existsSync(filePath)) {
    console.warn(`  WARN: ${filePath} not found — using defaults`);
    return null;
  }
  try {
    const content = fs.readFileSync(filePath, 'utf-8');
    // Extract the "Medium (50w, 80 idx, emb)" row from the Per-Fact table.
    // Format: | Medium (50w, 80 idx, emb) | 50 | 80 | Yes | 11,234 | 72,456 | 6.4 |
    const mediumRegex = /\|\s*Medium\s*\(50w,\s*80\s*idx,\s*emb\)\s*\|[^|]*\|[^|]*\|[^|]*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|/;
    const match = content.match(mediumRegex);
    if (match) {
      const calldataBytes = parseInt(match[1].replace(/,/g, ''), 10);
      const gasUsed = parseInt(match[2].replace(/,/g, ''), 10);
      return { calldataBytes, gasUsed, blindIndexCount: 80 };
    }
    // Fallback: try to find any row with "Medium" in it
    const fallbackRegex = /\|\s*Medium[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|/;
    const fallbackMatch = content.match(fallbackRegex);
    if (fallbackMatch) {
      return {
        calldataBytes: parseInt(fallbackMatch[1].replace(/,/g, ''), 10),
        gasUsed: parseInt(fallbackMatch[2].replace(/,/g, ''), 10),
        blindIndexCount: 80,
      };
    }
    console.warn('  WARN: Could not parse Medium row from gas-report.md — using defaults');
    return null;
  } catch (err) {
    console.warn(`  WARN: Failed to read ${filePath}: ${err}`);
    return null;
  }
}

interface PgSizes {
  bytesPerFactRow: number;
  bytesPerBlindIndexRow: number;
}

function parsePgTableSizes(baseDir: string): PgSizes {
  const filePath = path.resolve(baseDir, 'e2e-results', 'pg-table-sizes.txt');
  let bytesPerFactRow = DEFAULT_BYTES_PER_FACT_ROW;
  let bytesPerBlindIndexRow = DEFAULT_BYTES_PER_BLIND_INDEX_ROW;

  if (!fs.existsSync(filePath)) {
    console.warn(`  WARN: ${filePath} not found — using default estimates`);
    return { bytesPerFactRow, bytesPerBlindIndexRow };
  }

  try {
    const content = fs.readFileSync(filePath, 'utf-8');
    // psql output format (raw bytes):
    //  sgd1 | fact          |     8192000 |     212992 |      778240 |   422
    //  sgd1 | blind_index   |     7831552 |    3538944 |     4251648 | 16375
    // Columns: schemaname | relname | total_bytes | data_bytes | index_bytes | rows
    // We use data_bytes / rows for per-row size (excludes index overhead).
    const factMatch = content.match(/\|\s*fact\s*\|[^|]*\|\s*(\d+)\s*\|[^|]*\|\s*(\d+)/);
    const blindMatch = content.match(/\|\s*blind_index\s*\|[^|]*\|\s*(\d+)\s*\|[^|]*\|\s*(\d+)/);

    if (factMatch) {
      const dataBytes = parseInt(factMatch[1], 10);
      const rows = parseInt(factMatch[2], 10);
      if (rows > 0) bytesPerFactRow = Math.round(dataBytes / rows);
    }
    if (blindMatch) {
      const dataBytes = parseInt(blindMatch[1], 10);
      const rows = parseInt(blindMatch[2], 10);
      if (rows > 0) bytesPerBlindIndexRow = Math.round(dataBytes / rows);
    }
  } catch (err) {
    console.warn(`  WARN: Failed to parse pg-table-sizes.txt: ${err}`);
  }

  return { bytesPerFactRow, bytesPerBlindIndexRow };
}

// ---------------------------------------------------------------------------
// Projection Models
// ---------------------------------------------------------------------------

function estimateIndicesPerFact(e2e: E2EResults | null, baseDir: string): number {
  // Best source: actual PG row counts (not capped by GraphQL first: limit)
  const rowCountsPath = path.resolve(baseDir, 'e2e-results', 'pg-row-counts.txt');
  if (fs.existsSync(rowCountsPath)) {
    const content = fs.readFileSync(rowCountsPath, 'utf-8');
    const factMatch = content.match(/fact\s*\|\s*(\d+)/);
    const blindMatch = content.match(/blind_index\s*\|\s*(\d+)/);
    if (factMatch && blindMatch) {
      const facts = parseInt(factMatch[1], 10);
      const blinds = parseInt(blindMatch[1], 10);
      if (facts > 0) return blinds / facts;
    }
  }
  // Fallback: E2E sample (may be capped at 1000)
  if (e2e && e2e.blindIndexSample > 0 && e2e.factCount > 0) {
    const ratio = e2e.blindIndexSample / e2e.factCount;
    if (ratio < 1) return 80;
    return Math.max(ratio, 20);
  }
  return 80;
}

function modelGinScanTime(
  baseBlindIndexRows: number,
  baseQueryLatencyMs: number,
  targetBlindIndexRows: number,
): number {
  // GIN index uses B-tree internally for posting lists.
  // Lookup time grows logarithmically with table size.
  // Model: latency = base * (1 + log2(targetSize / baseSize))
  if (baseBlindIndexRows <= 0 || targetBlindIndexRows <= baseBlindIndexRows) {
    return baseQueryLatencyMs;
  }
  const sizeRatio = targetBlindIndexRows / baseBlindIndexRows;
  const scaleFactor = 1 + Math.log2(sizeRatio);
  return baseQueryLatencyMs * scaleFactor;
}

function dynamicCandidatePool(factCount: number): number {
  return Math.min(Math.max(factCount * 3, 400), 5000);
}

function estimatePeakQps(scenario: Scenario): number {
  const activeUserFraction = 0.20;
  const sessionsPerDay = 3;
  const queriesPerSession = 7;
  const activeHours = 12;

  const activeUsers = scenario.users * activeUserFraction;
  const totalQueries = activeUsers * sessionsPerDay * queriesPerSession;
  return totalQueries / (activeHours * 3600);
}

// ---------------------------------------------------------------------------
// Formatting Helpers
// ---------------------------------------------------------------------------

function fmtNum(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return n.toLocaleString('en-US');
}

function fmtBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 ** 2)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function fmtUsd(n: number): string {
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function fmtMs(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms.toFixed(0)}ms`;
}

function padR(s: string, len: number): string {
  return s.padEnd(len);
}

function padL(s: string, len: number): string {
  return s.padStart(len);
}

// ---------------------------------------------------------------------------
// Report Generation
// ---------------------------------------------------------------------------

function generateReport(
  e2e: E2EResults | null,
  gas: GasData | null,
  pgSizes: PgSizes,
): string {
  const lines: string[] = [];
  const baseDir = path.resolve(__dirname);
  const indicesPerFact = estimateIndicesPerFact(e2e, baseDir);

  // Use measured or default values
  const factCount = e2e?.factCount ?? 415;
  const queryCount = e2e?.queryCount ?? 140;
  const recall8 = e2e?.overall.recall8 ?? 0;
  const precision8 = e2e?.overall.precision8 ?? 0;
  const mrr = e2e?.overall.mrr ?? 0;
  const queryAvgMs = e2e?.query.avgMs ?? 150;
  const queryP95Ms = e2e?.query.p95Ms ?? 300;
  const ingestAvgMs = e2e?.ingest.avgMs ?? 500;
  const blindIndexSample = e2e?.blindIndexSample ?? 1000;
  const indexedCount = e2e?.indexedCount ?? factCount;

  const gasCalldataBytes = gas?.calldataBytes ?? 11_000;
  const gasUsed = gas?.gasUsed ?? 72_000;

  const baseBlindIndexRows = factCount * indicesPerFact;

  // Per-fact L2 cost
  const factSizeKB = gasCalldataBytes / 1024;
  const l2ExecPerFact = (gasUsed * BASE_L2_GAS_PRICE_GWEI * 1e-9) * ETH_PRICE_USD;
  const l1DataPerFact = factSizeKB * BASE_L2_COST_PER_KB_USD;
  const costPerFact = l2ExecPerFact + l1DataPerFact;

  // ---- Header ----
  lines.push('# TotalReclaw Subgraph Scaling Analysis');
  lines.push('');
  lines.push(`**Generated:** ${new Date().toISOString()}`);
  lines.push(`**Based on:** E2E validation with ${factCount} facts, ${queryCount} queries`);
  lines.push(`**Data sources:** ${e2e ? 'e2e-results-latest.json' : '(defaults)'}, ${gas ? 'gas-report.md' : '(defaults)'}, pg-table-sizes`);
  lines.push('');

  // ---- Measured Baseline ----
  lines.push('## Measured Baseline');
  lines.push('');
  lines.push('| Metric | Value |');
  lines.push('|--------|-------|');
  lines.push(`| Facts ingested | ${factCount} |`);
  lines.push(`| Facts indexed (Graph Node) | ${indexedCount} |`);
  lines.push(`| BlindIndex entities (sampled) | >= ${blindIndexSample} |`);
  lines.push(`| Estimated indices/fact | ${indicesPerFact.toFixed(1)} |`);
  lines.push(`| Recall@8 | ${(recall8 * 100).toFixed(1)}% |`);
  lines.push(`| Precision@8 | ${(precision8 * 100).toFixed(1)}% |`);
  lines.push(`| MRR | ${mrr.toFixed(3)} |`);
  lines.push(`| Query latency (avg) | ${fmtMs(queryAvgMs)} |`);
  lines.push(`| Query latency (p95) | ${fmtMs(queryP95Ms)} |`);
  lines.push(`| Ingest latency (avg/fact) | ${fmtMs(ingestAvgMs)} |`);
  lines.push(`| Gas per fact (Medium 50w, 80 idx, emb) | ${fmtNum(gasUsed)} gas, ${fmtNum(gasCalldataBytes)} bytes calldata |`);
  lines.push(`| Cost per fact (Base L2) | ${fmtUsd(costPerFact)} |`);
  lines.push('');

  if (e2e?.categories) {
    lines.push('**Per-category recall:**');
    lines.push('');
    lines.push('| Category | Recall@8 | Precision@8 | MRR | Queries |');
    lines.push('|----------|----------|-------------|-----|---------|');
    for (const [cat, metrics] of Object.entries(e2e.categories)) {
      lines.push(`| ${cat} | ${(metrics.recall * 100).toFixed(1)}% | ${(metrics.precision * 100).toFixed(1)}% | ${metrics.mrr.toFixed(3)} | ${metrics.count} |`);
    }
    lines.push('');
  }

  // ---- Scenario Definitions ----
  lines.push('## Scenario Definitions');
  lines.push('');
  lines.push('| Scenario | Users | Facts/day/user | Duration | Total Facts | Queries/day |');
  lines.push('|----------|-------|----------------|----------|-------------|-------------|');
  for (const s of SCENARIOS) {
    lines.push(`| ${s.name} | ${fmtNum(s.users)} | ${s.factsPerDayPerUser} | ${s.durationDays} days | ${fmtNum(s.totalFacts)} | ${fmtNum(s.queriesPerDay)} |`);
  }
  lines.push('');

  // ---- 1. Storage Growth ----
  lines.push('## 1. Storage Growth Projections');
  lines.push('');
  lines.push(`Assumptions: ${pgSizes.bytesPerFactRow} bytes/fact row, ${pgSizes.bytesPerBlindIndexRow} bytes/blind_index row, ${indicesPerFact.toFixed(0)} indices/fact.`);
  lines.push('');
  lines.push('| Scenario | Total Facts | Blind Index Rows | PG Data Size | PG Index Size (est.) | Total Storage |');
  lines.push('|----------|-------------|-----------------|-------------|---------------------|---------------|');

  for (const s of SCENARIOS) {
    const blindRows = Math.round(s.totalFacts * indicesPerFact);
    const dataSize = s.totalFacts * pgSizes.bytesPerFactRow + blindRows * pgSizes.bytesPerBlindIndexRow;
    // PG index overhead: ~30-40% of data for GIN + B-tree indices
    const indexSize = Math.round(dataSize * 0.35);
    const totalStorage = dataSize + indexSize;
    lines.push(`| ${s.name} | ${fmtNum(s.totalFacts)} | ${fmtNum(blindRows)} | ${fmtBytes(dataSize)} | ${fmtBytes(indexSize)} | ${fmtBytes(totalStorage)} |`);
  }
  lines.push('');

  // ---- 2. Write Costs ----
  lines.push('## 2. Write Cost Projections (Base L2)');
  lines.push('');
  lines.push(`Assumptions: ${fmtUsd(BASE_L2_COST_PER_KB_USD)}/KB L1 data, ${BASE_L2_GAS_PRICE_GWEI} gwei L2 gas, $${ETH_PRICE_USD} ETH, ${fmtNum(gasCalldataBytes)} bytes calldata/fact, ${fmtNum(gasUsed)} gas/fact.`);
  lines.push('');
  lines.push('| Scenario | Facts/month | Gas/month | Calldata/month | Monthly Cost | Annual Cost | Paymaster ETH/yr |');
  lines.push('|----------|-------------|-----------|---------------|-------------|-------------|------------------|');

  for (const s of SCENARIOS) {
    const factsPerMonth = s.users * s.factsPerDayPerUser * 30;
    const gasPerMonth = factsPerMonth * gasUsed;
    const calldataPerMonth = factsPerMonth * gasCalldataBytes;
    const monthlyCost = factsPerMonth * costPerFact;
    const annualCost = monthlyCost * 12;
    // Paymaster funding: total ETH needed per year (L2 exec component only, in ETH)
    const annualL2ExecEth = (factsPerMonth * 12 * gasUsed * BASE_L2_GAS_PRICE_GWEI * 1e-9);
    lines.push(`| ${s.name} | ${fmtNum(factsPerMonth)} | ${fmtNum(gasPerMonth)} | ${fmtBytes(calldataPerMonth)} | ${fmtUsd(monthlyCost)} | ${fmtUsd(annualCost)} | ${annualL2ExecEth.toFixed(4)} ETH |`);
  }
  lines.push('');

  // ---- 3. Query Performance ----
  lines.push('## 3. Query Performance Projections');
  lines.push('');
  lines.push(`Baseline: ${factCount} facts, ~${fmtNum(baseBlindIndexRows)} blind index rows, ${fmtMs(queryAvgMs)} avg / ${fmtMs(queryP95Ms)} p95 query latency.`);
  lines.push('GIN scan time model: logarithmic growth with B-tree posting list size.');
  lines.push('');
  lines.push('| Scenario | Total Facts | Blind Index Rows | Scale Factor | Est. GIN Scan (p95) | Total Query p95 | Dynamic Pool Size |');
  lines.push('|----------|-------------|-----------------|-------------|--------------------|-----------------|--------------------|');

  for (const s of SCENARIOS) {
    const blindRows = Math.round(s.totalFacts * indicesPerFact);
    const estimatedP95 = modelGinScanTime(baseBlindIndexRows, queryP95Ms, blindRows);
    const sizeRatio = blindRows / baseBlindIndexRows;
    const scaleFactor = baseBlindIndexRows > 0 && blindRows > baseBlindIndexRows
      ? (1 + Math.log2(sizeRatio))
      : 1;
    const poolSize = dynamicCandidatePool(s.totalFacts);
    lines.push(`| ${s.name} | ${fmtNum(s.totalFacts)} | ${fmtNum(blindRows)} | ${scaleFactor.toFixed(1)}x | ${fmtMs(estimatedP95)} | ${fmtMs(estimatedP95 * 1.2)} | ${fmtNum(poolSize)} |`);
  }
  lines.push('');
  lines.push('> **Note:** "Total Query p95" includes ~20% overhead for network round-trip, decryption, and reranking on top of the GIN scan estimate.');
  lines.push('');

  // ---- 4. Infrastructure ----
  lines.push('## 4. Infrastructure Requirements');
  lines.push('');
  lines.push(`Baseline: ${factCount} facts requires ~1 CPU, 512 MB for Graph Node + PostgreSQL.`);
  lines.push('');
  lines.push('| Scenario | PG Storage | PG shared_buffers | Graph Node CPU | Graph Node Memory | RPC Node Tier | Est. Infra Cost/mo |');
  lines.push('|----------|-----------|-------------------|---------------|-------------------|---------------|-------------------|');

  for (const s of SCENARIOS) {
    const blindRows = Math.round(s.totalFacts * indicesPerFact);
    const dataSize = s.totalFacts * pgSizes.bytesPerFactRow + blindRows * pgSizes.bytesPerBlindIndexRow;
    const totalStorage = Math.round(dataSize * 1.35); // data + index
    // shared_buffers: ~25% of working set, minimum 256MB
    const workingSetMB = totalStorage / (1024 * 1024);
    const sharedBuffersMB = Math.max(256, Math.round(workingSetMB * 0.25));
    // Graph Node CPU: 1 core per ~2M facts at low load
    const cpuCores = Math.max(1, Math.ceil(s.totalFacts / 2_000_000));
    // Graph Node memory: 512MB base + 256MB per million facts
    const gnMemoryMB = 512 + Math.round((s.totalFacts / 1_000_000) * 256);
    // RPC tier
    let rpcTier: string;
    let rpcCost: number;
    if (s.users <= 1000) {
      rpcTier = 'Public (free)';
      rpcCost = 0;
    } else if (s.users <= 10000) {
      rpcTier = 'Dedicated ($50-200/mo)';
      rpcCost = 100;
    } else {
      rpcTier = 'Premium ($200+/mo)';
      rpcCost = 200;
    }
    // Rough infra cost: compute (~$10/core/mo) + storage (~$0.10/GB/mo) + RPC
    const computeCost = cpuCores * 10 + (gnMemoryMB / 1024) * 5;
    const storageCostGBMo = (totalStorage / (1024 ** 3)) * 0.10;
    const infraCost = computeCost + storageCostGBMo + rpcCost;

    lines.push(`| ${s.name} | ${fmtBytes(totalStorage)} | ${sharedBuffersMB} MB | ${cpuCores} core(s) | ${gnMemoryMB} MB | ${rpcTier} | ~${fmtUsd(infraCost)} |`);
  }
  lines.push('');

  // ---- 5. Concurrency ----
  lines.push('## 5. Concurrency Analysis');
  lines.push('');
  lines.push('Assumptions: 20% users active, 3 sessions/day, 7 queries/session, 12 active hours.');
  lines.push('Graph Node throughput: ~100-500 QPS for simple subgraphs (single entity lookups + GIN scan).');
  lines.push('');
  lines.push('| Scenario | Active Users | Peak QPS | Graph Node Capacity (est.) | Headroom | Nodes Needed |');
  lines.push('|----------|-------------|----------|---------------------------|----------|-------------|');

  for (const s of SCENARIOS) {
    const peakQps = estimatePeakQps(s);
    // Estimate Graph Node capacity based on query complexity at this scale
    const blindRows = Math.round(s.totalFacts * indicesPerFact);
    // Capacity decreases with dataset size: base 300 QPS, halves per 10x blind index growth
    const sizeRatio = blindRows / Math.max(baseBlindIndexRows, 1);
    const capacityPerNode = Math.max(50, Math.round(300 / Math.max(1, Math.log10(sizeRatio + 1) + 1)));
    const headroomPct = ((capacityPerNode - peakQps) / capacityPerNode * 100);
    const nodesNeeded = Math.max(1, Math.ceil(peakQps / capacityPerNode));
    const activeUsers = Math.round(s.users * 0.2);

    lines.push(`| ${s.name} | ${fmtNum(activeUsers)} | ${peakQps.toFixed(1)} | ~${capacityPerNode} QPS/node | ${headroomPct > 0 ? `${headroomPct.toFixed(0)}%` : 'OVER'} | ${nodesNeeded} |`);
  }
  lines.push('');

  // ---- 6. Key Bottlenecks ----
  lines.push('## 6. Key Bottlenecks');
  lines.push('');
  lines.push('Ordered by expected impact as scale increases:');
  lines.push('');
  lines.push('1. **GIN index scan time** -- The blind_index table grows linearly with facts. At 36.5M facts with ~80 indices each, the GIN index holds ~2.9B entries. PostgreSQL GIN performance degrades when posting lists exceed available shared_buffers, causing disk I/O spikes.');
  lines.push('');
  lines.push('2. **Calldata costs on L1** -- While Base L2 execution is cheap, L1 data posting (the dominant cost component) scales linearly. Batching multiple facts per transaction can amortize the per-tx overhead but does not reduce L1 data volume.');
  lines.push('');
  lines.push('3. **Graph Node indexing throughput** -- Graph Node processes blocks sequentially. High write volumes (Scenario B: 100K facts/day = ~1.2/sec sustained) may cause indexing lag if block processing is slower than block production.');
  lines.push('');
  lines.push('4. **Client-side decryption + reranking** -- The dynamic candidate pool (up to 5,000 facts) requires AES-GCM decryption + BM25/cosine reranking client-side. At 5K candidates, this adds 50-200ms depending on client hardware.');
  lines.push('');
  lines.push('5. **PostgreSQL storage I/O** -- At Scenario B scale (~49 GB total), the working set exceeds typical VPS memory. Queries hitting cold pages incur SSD latency (~0.1ms/page) which compounds with GIN scan fan-out.');
  lines.push('');
  lines.push('6. **RPC node rate limits** -- Public Base L2 RPC endpoints throttle at ~10-50 req/s. Dedicated nodes ($50-200/mo) raise this to 500-1000 req/s but add infrastructure cost.');
  lines.push('');

  // ---- 7. Recommendations ----
  lines.push('## 7. Recommendations');
  lines.push('');

  lines.push('### Scenario A (6-mo MVP, 1K users)');
  lines.push('');
  lines.push('- **Infrastructure:** Single Graph Node instance (1 CPU, 1 GB) + PostgreSQL with 512 MB shared_buffers is sufficient.');
  lines.push('- **RPC:** Public Base L2 endpoint is adequate for write volume (~300 txs/day per user).');
  lines.push('- **Cost:** Write costs are negligible (~' + fmtUsd(SCENARIOS[0].totalFacts * costPerFact / (SCENARIOS[0].durationDays / 30)) + '/mo). Focus on infra hosting cost.');
  lines.push('- **Action items:** Deploy with basic monitoring. No optimization needed yet.');
  lines.push('');

  lines.push('### Scenario B (12-mo, 10K users)');
  lines.push('');
  lines.push('- **Infrastructure:** Upgrade to 2-4 CPU cores, 4+ GB RAM for PostgreSQL. Consider read replicas for query load.');
  lines.push('- **RPC:** Dedicated Base L2 RPC node required ($50-200/mo).');
  lines.push('- **Storage:** Plan for ~50 GB PostgreSQL storage with SSD-backed volumes.');
  lines.push('- **Optimization priorities:**');
  lines.push('  1. Implement transaction batching (10-50 facts/tx) to reduce per-fact gas overhead.');
  lines.push('  2. Add blind_index table partitioning by owner to reduce GIN scan scope.');
  lines.push('  3. Consider caching hot blind index lookups in Redis/memcached.');
  lines.push('  4. Implement connection pooling (PgBouncer) for Graph Node <-> PostgreSQL.');
  lines.push('- **Cost:** Write costs remain low (~' + fmtUsd(SCENARIOS[1].totalFacts * costPerFact / (SCENARIOS[1].durationDays / 30)) + '/mo) but infra costs become the primary expense.');
  lines.push('');

  lines.push('### Scenario C (Power users, 100 users x 50 facts/day)');
  lines.push('');
  lines.push('- **Infrastructure:** Similar to Scenario A in absolute terms (1.8M facts total) but with higher per-user density.');
  lines.push('- **Key difference:** Fewer users means less concurrency pressure but more data per user increases candidate pool sizes.');
  lines.push('- **Optimization:** Per-user blind_index partitioning is highly effective here -- each user\'s index stays small (~18K facts, ~1.4M blind index rows).');
  lines.push('- **RPC:** Public endpoint is sufficient given low tx volume.');
  lines.push('- **Cost:** Very low (~' + fmtUsd(SCENARIOS[2].totalFacts * costPerFact / (SCENARIOS[2].durationDays / 30)) + '/mo write costs).');
  lines.push('');

  lines.push('### Cross-Scenario Recommendations');
  lines.push('');
  lines.push('1. **Batch writes:** Combine 10-50 facts per on-chain transaction to amortize 21,000 base gas. Requires contract upgrade to handle batch protobuf payloads.');
  lines.push('2. **Tiered storage:** Archive facts older than 90 days to cold storage; keep active facts in hot GIN index.');
  lines.push('3. **Embedding compression:** Quantize 640-dim float32 embeddings to int8 (4x size reduction: 2,560B -> 640B) before encryption. Reranking quality impact is minimal for BM25+cosine fusion.');
  lines.push('4. **Index pruning:** Periodically compact blind_index table by removing entries for superseded/deleted facts.');
  lines.push('5. **Horizontal scaling:** For >10K users, shard by user prefix (first 2 bytes of owner address) across multiple Graph Node instances.');
  lines.push('');

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

function main(): void {
  console.log('');
  console.log('========================================================');
  console.log('  TotalReclaw Subgraph Scaling Analysis');
  console.log('========================================================');
  console.log('');

  const baseDir = path.resolve(__dirname);

  console.log('[1/4] Reading E2E results...');
  const e2e = readE2EResults(baseDir);
  if (e2e) {
    console.log(`  Loaded: ${e2e.factCount} facts, ${e2e.queryCount} queries, recall@8=${(e2e.overall.recall8 * 100).toFixed(1)}%`);
  }

  console.log('[2/4] Parsing gas report...');
  const gas = parseGasReport(baseDir);
  if (gas) {
    console.log(`  Medium fact: ${gas.calldataBytes} bytes calldata, ${gas.gasUsed} gas`);
  }

  console.log('[3/4] Parsing PostgreSQL table sizes...');
  const pgSizes = parsePgTableSizes(baseDir);
  console.log(`  Bytes/fact row: ${pgSizes.bytesPerFactRow}, bytes/blind_index row: ${pgSizes.bytesPerBlindIndexRow}`);

  console.log('[4/4] Generating scaling report...');
  const report = generateReport(e2e, gas, pgSizes);

  const reportPath = path.resolve(__dirname, 'scaling-report.md');
  fs.writeFileSync(reportPath, report);
  console.log(`  Report written to: ${reportPath}`);

  // ---- Console Summary ----
  console.log('');
  console.log('--- Summary ---');
  console.log('');

  const indicesPerFact = estimateIndicesPerFact(e2e, baseDir);
  const gasCalldataBytes = gas?.calldataBytes ?? 11_000;
  const gasUsed = gas?.gasUsed ?? 72_000;
  const factSizeKB = gasCalldataBytes / 1024;
  const costPerFact = (gasUsed * BASE_L2_GAS_PRICE_GWEI * 1e-9) * ETH_PRICE_USD + factSizeKB * BASE_L2_COST_PER_KB_USD;

  console.log(`  Indices/fact:    ${indicesPerFact.toFixed(1)}`);
  console.log(`  Cost/fact:       ${fmtUsd(costPerFact)}`);
  console.log('');

  for (const s of SCENARIOS) {
    const blindRows = Math.round(s.totalFacts * indicesPerFact);
    const factsPerMonth = s.users * s.factsPerDayPerUser * 30;
    const monthlyCost = factsPerMonth * costPerFact;
    const peakQps = estimatePeakQps(s);
    const totalStorage = Math.round((s.totalFacts * pgSizes.bytesPerFactRow + blindRows * pgSizes.bytesPerBlindIndexRow) * 1.35);

    console.log(`  ${s.name}:`);
    console.log(`    Facts: ${fmtNum(s.totalFacts)} | Blind indices: ${fmtNum(blindRows)} | Storage: ${fmtBytes(totalStorage)}`);
    console.log(`    Write cost: ${fmtUsd(monthlyCost)}/mo | Peak QPS: ${peakQps.toFixed(1)}`);
    console.log('');
  }

  console.log('========================================================');
  console.log('  Done.');
  console.log('========================================================');
}

main();
