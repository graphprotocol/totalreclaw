/**
 * Tests for billing-cache.ts (3.0.7 extraction).
 *
 * Covers round-trip read/write, TTL expiry, corrupt-cache fallback, and
 * missing-file fallback. Also verifies the chain-id sync side-effect (#402)
 * and the DataEdge-address sync + env → billing → WASM-default resolution
 * order via getSubgraphConfig (#460), on both read and write paths.
 *
 * Run with: npx tsx billing-cache.test.ts
 *
 * TAP-style output, no jest dependency.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Isolate the on-disk cache location to a temp dir BEFORE importing the
// modules under test. `CONFIG.billingCachePath` is derived from HOME at
// module-load time, so overriding HOME now redirects the cache away from
// the real `~/.totalreclaw/`.
const TEST_HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-billing-cache-test-'));
process.env.HOME = TEST_HOME;

// Dynamic imports AFTER HOME override so CONFIG.billingCachePath picks up
// the test location. Node ESM caches by URL; these are the first imports.
const { readBillingCache, writeBillingCache, BILLING_CACHE_PATH, BILLING_CACHE_TTL } =
  await import('./billing-cache.js');
const { CONFIG, __resetChainIdOverrideForTests, __resetDataEdgeAddressOverrideForTests } =
  await import('./config.js');
// getSubgraphConfig exercises the full env → billing → WASM-default DataEdge
// resolution order (#460). Imported after the HOME override above.
const { getSubgraphConfig } = await import('./subgraph-store.js');
import type { BillingCache } from './billing-cache.js';

// WASM-baked default DataEdge (the PRODUCTION contract). Captured with no env
// override + no billing override so the DataEdge tests below can assert the
// "fall through to WASM default" branch without hardcoding the address.
delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
__resetDataEdgeAddressOverrideForTests();
const WASM_DEFAULT_DATA_EDGE = getSubgraphConfig().dataEdgeAddress;
// Staging DataEdge — what the staging relay returns in `data_edge_address`.
const STAGING_DATA_EDGE = '0xE7a4D2677B686e13775Ba9092631089e35F0BB91';

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function assertEq<T>(actual: T, expected: T, name: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    console.log(`  actual:   ${JSON.stringify(actual)}`);
    console.log(`  expected: ${JSON.stringify(expected)}`);
  }
  assert(ok, name);
}

// Safety: the redirected path must actually live under the tmp dir. If HOME
// override was defeated by caching, we'd clobber the real cache.
assert(
  BILLING_CACHE_PATH.startsWith(TEST_HOME),
  `BILLING_CACHE_PATH redirects under TEST_HOME (got: ${BILLING_CACHE_PATH})`,
);

function resetCache(): void {
  try { fs.unlinkSync(BILLING_CACHE_PATH); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Sanity — constants
// ---------------------------------------------------------------------------

{
  assertEq(BILLING_CACHE_TTL, 2 * 60 * 60 * 1000, 'BILLING_CACHE_TTL is 2 hours in ms');
  assertEq(
    BILLING_CACHE_PATH,
    CONFIG.billingCachePath,
    'BILLING_CACHE_PATH tracks CONFIG.billingCachePath',
  );
}

// ---------------------------------------------------------------------------
// Missing file → null
// ---------------------------------------------------------------------------

{
  resetCache();
  assertEq(readBillingCache(), null, 'readBillingCache: returns null when file missing');
}

// ---------------------------------------------------------------------------
// Round-trip write + read
// ---------------------------------------------------------------------------

{
  resetCache();
  __resetChainIdOverrideForTests();
  const now = Date.now();
  const cache: BillingCache = {
    tier: 'free',
    free_writes_used: 3,
    free_writes_limit: 10,
    features: { llm_dedup: true, extraction_interval: 5 },
    chain_id: 100,
    checked_at: now,
  };
  writeBillingCache(cache);

  const read = readBillingCache();
  assert(read !== null, 'readBillingCache: round-trip read returns non-null');
  assertEq(read?.tier, 'free', 'readBillingCache: tier round-trips');
  assertEq(read?.free_writes_used, 3, 'readBillingCache: free_writes_used round-trips');
  assertEq(read?.free_writes_limit, 10, 'readBillingCache: free_writes_limit round-trips');
  assertEq(read?.features?.llm_dedup, true, 'readBillingCache: features.llm_dedup round-trips');
  assertEq(
    read?.features?.extraction_interval,
    5,
    'readBillingCache: features.extraction_interval round-trips',
  );
  assertEq(read?.chain_id, 100, 'readBillingCache: chain_id round-trips');
  assertEq(read?.checked_at, now, 'readBillingCache: checked_at round-trips');
  assertEq(
    read?.data_edge_address,
    undefined,
    'readBillingCache: data_edge_address absent round-trips as undefined',
  );

  // Side-effect (#402): Free tier no longer flips to 84532 — the relay's
  // authoritative chain_id (100) is applied verbatim.
  assertEq(CONFIG.chainId, 100, 'writeBillingCache: Free tier + chain_id 100 syncs to 100 (no tier flip)');
}

// ---------------------------------------------------------------------------
// chain_id is authoritative and applied verbatim (#402)
// ---------------------------------------------------------------------------

{
  // After ops-1 both tiers are on Gnosis; the relay returns chain_id and the
  // client MUST consume it verbatim. Free + chain_id 100 → 100.
  resetCache();
  __resetChainIdOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    checked_at: Date.now(),
  });
  assertEq(CONFIG.chainId, 100, 'writeBillingCache: free + chain_id 100 → 100 (root-cause fix)');
}

{
  // Missing chain_id (older relay / partial payload) → default to 100, NOT
  // the retired free-tier 84532.
  resetCache();
  __resetChainIdOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    checked_at: Date.now(),
  });
  assertEq(CONFIG.chainId, 100, 'writeBillingCache: missing chain_id defaults to 100 (no 84532 flip)');
}

{
  // Relay is authoritative: chain_id wins over tier. Pro tier + chain_id
  // 84532 → 84532 (NOT the tier-derived 100). Locks the "verbatim" contract,
  // and read path re-syncs the persisted chain_id on a cold process.
  resetCache();
  __resetChainIdOverrideForTests();
  writeBillingCache({
    tier: 'pro',
    free_writes_used: 0,
    free_writes_limit: 0,
    chain_id: 84532,
    checked_at: Date.now(),
  });
  assertEq(CONFIG.chainId, 84532, 'writeBillingCache: chain_id honored verbatim over tier (pro + 84532 → 84532)');

  __resetChainIdOverrideForTests();
  assertEq(CONFIG.chainId, 100, 'pre-read: chain override reset → default 100');
  const read = readBillingCache();
  assertEq(read?.chain_id, 84532, 'readBillingCache: chain_id persists + round-trips');
  assertEq(CONFIG.chainId, 84532, 'readBillingCache: re-syncs chain_id verbatim on cold load');
}

// ---------------------------------------------------------------------------
// data_edge_address is authoritative and consumed verbatim (#460)
//
// The relay routes each environment to its own DataEdge (staging is on-chain
// isolated). Before this fix getSubgraphConfig resolved the contract as
// `CONFIG.dataEdgeAddress || WASM-default` — so against the staging relay,
// writes mined on the PROD DataEdge (WASM default) while reads came from the
// staging subgraph → empty recall + phantom "stored=N". Fix: billing's
// `data_edge_address` is the middle term of env → billing → WASM-default.
// ---------------------------------------------------------------------------

{
  // Sanity: with no env + no billing override, getSubgraphConfig falls through
  // to the WASM-baked (PROD) default.
  resetCache();
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  __resetDataEdgeAddressOverrideForTests();
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    WASM_DEFAULT_DATA_EDGE,
    'getSubgraphConfig: no env + no billing → WASM default (prod DataEdge)',
  );
}

{
  // RED under the old code: billing supplies the staging DataEdge, but
  // getSubgraphConfig ignored it and returned the WASM prod default. After the
  // fix the relay value is consumed verbatim.
  resetCache();
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  __resetChainIdOverrideForTests();
  __resetDataEdgeAddressOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    data_edge_address: STAGING_DATA_EDGE,
    checked_at: Date.now(),
  });
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    STAGING_DATA_EDGE,
    'getSubgraphConfig: billing data_edge_address consumed verbatim (staging, not prod default)',
  );
}

{
  // Missing data_edge_address (older relay / partial payload) → fall through to
  // the WASM default (unchanged behavior — never a stale value).
  resetCache();
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  __resetDataEdgeAddressOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    checked_at: Date.now(),
  });
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    WASM_DEFAULT_DATA_EDGE,
    'getSubgraphConfig: missing data_edge_address → WASM default (no override)',
  );
}

{
  // Malformed data_edge_address (not an 0x-address) is ignored → WASM default,
  // and the override is CLEARED (does not leak a prior valid value).
  resetCache();
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  __resetDataEdgeAddressOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    data_edge_address: 'not-an-address',
    checked_at: Date.now(),
  });
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    WASM_DEFAULT_DATA_EDGE,
    'getSubgraphConfig: malformed data_edge_address ignored → WASM default',
  );
}

{
  // Explicit operator env override wins over billing (#460 item 4).
  resetCache();
  __resetDataEdgeAddressOverrideForTests();
  const ENV_DATA_EDGE = '0x1111111111111111111111111111111111111111';
  process.env.TOTALRECLAW_DATA_EDGE_ADDRESS = ENV_DATA_EDGE;
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    data_edge_address: STAGING_DATA_EDGE,
    checked_at: Date.now(),
  });
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    ENV_DATA_EDGE,
    'getSubgraphConfig: env override wins over billing data_edge_address',
  );
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
}

{
  // Cold-load restart: a persisted data_edge_address re-syncs the override on
  // readBillingCache (fresh process, override reset).
  resetCache();
  delete process.env.TOTALRECLAW_DATA_EDGE_ADDRESS;
  __resetDataEdgeAddressOverrideForTests();
  writeBillingCache({
    tier: 'free',
    free_writes_used: 0,
    free_writes_limit: 250,
    chain_id: 100,
    data_edge_address: STAGING_DATA_EDGE,
    checked_at: Date.now(),
  });
  // Simulate a cold process: clear the in-memory override, then read from disk.
  __resetDataEdgeAddressOverrideForTests();
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    WASM_DEFAULT_DATA_EDGE,
    'pre-read: DataEdge override reset → WASM default',
  );
  const read = readBillingCache();
  assertEq(read?.data_edge_address, STAGING_DATA_EDGE, 'readBillingCache: data_edge_address persists + round-trips');
  assertEq(
    getSubgraphConfig().dataEdgeAddress,
    STAGING_DATA_EDGE,
    'readBillingCache: re-syncs data_edge_address verbatim on cold load',
  );
  __resetDataEdgeAddressOverrideForTests();
}

// ---------------------------------------------------------------------------
// TTL expiry → null (stale entries rejected)
// ---------------------------------------------------------------------------

{
  resetCache();
  __resetChainIdOverrideForTests();
  const stale: BillingCache = {
    tier: 'pro',
    free_writes_used: 0,
    free_writes_limit: 0,
    checked_at: Date.now() - (BILLING_CACHE_TTL + 60_000), // 1 min past TTL
  };
  // Bypass writeBillingCache's chain sync — write directly so we test read
  // behaviour on a stale entry only.
  fs.writeFileSync(BILLING_CACHE_PATH, JSON.stringify(stale));
  __resetChainIdOverrideForTests();

  const read = readBillingCache();
  assertEq(read, null, 'readBillingCache: returns null when checked_at > TTL');
  // Should NOT have synced — the stale entry must not leak its chain_id. With
  // the override reset, CONFIG.chainId is the default 100.
  assertEq(
    CONFIG.chainId,
    100,
    'readBillingCache: stale entry does not sync chain override (stays default 100)',
  );
}

// ---------------------------------------------------------------------------
// checked_at missing → null (defensive — rejects malformed entries)
// ---------------------------------------------------------------------------

{
  resetCache();
  fs.writeFileSync(
    BILLING_CACHE_PATH,
    JSON.stringify({ tier: 'pro', free_writes_used: 0, free_writes_limit: 0 }),
  );
  assertEq(
    readBillingCache(),
    null,
    'readBillingCache: returns null when checked_at missing',
  );
}

// ---------------------------------------------------------------------------
// Corrupt JSON → null (no throw)
// ---------------------------------------------------------------------------

{
  resetCache();
  fs.writeFileSync(BILLING_CACHE_PATH, '{not valid json');
  assertEq(
    readBillingCache(),
    null,
    'readBillingCache: returns null on corrupt JSON (no throw)',
  );
}

// ---------------------------------------------------------------------------
// writeBillingCache creates parent dir if missing
// ---------------------------------------------------------------------------

{
  resetCache();
  // Remove the entire .totalreclaw dir so write must recreate it.
  const parentDir = path.dirname(BILLING_CACHE_PATH);
  try { fs.rmSync(parentDir, { recursive: true, force: true }); } catch { /* ignore */ }
  assert(!fs.existsSync(parentDir), 'precondition: parent dir removed');

  writeBillingCache({
    tier: 'free',
    free_writes_used: 1,
    free_writes_limit: 10,
    checked_at: Date.now(),
  });
  assert(fs.existsSync(BILLING_CACHE_PATH), 'writeBillingCache: creates parent dir + file');
}

// ---------------------------------------------------------------------------
// Cleanup + summary
// ---------------------------------------------------------------------------

resetCache();
try { fs.rmSync(TEST_HOME, { recursive: true, force: true }); } catch { /* ignore */ }

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
