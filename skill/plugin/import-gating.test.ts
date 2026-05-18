/**
 * Tests for the Pro-tier gate in handlePluginImportFrom + handleBatchImport
 * (skill/plugin/index.ts).
 *
 * Pedro authorized 2026-05-18: ALL imports are Pro-only (mem0 + mcp-memory
 * included). The skill-side gate is intentionally fail-CLOSED on a missing
 * billing cache (`cache?.tier !== 'pro'` is true when cache is null) — this
 * differs from the MCP-side gate which is fail-open. Lock both behaviours in
 * so a silent flip would be caught.
 *
 * Run with: npx tsx import-gating.test.ts
 *
 * TAP-style, no jest dependency.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Isolate the on-disk billing cache to a temp dir BEFORE importing modules
// that derive paths from HOME. Same pattern as billing-cache.test.ts.
const TEST_HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-import-gating-test-'));
process.env.HOME = TEST_HOME;

const { writeBillingCache, BILLING_CACHE_PATH } = await import('./billing-cache.js');
const {
  __handlePluginImportFromForTesting: handlePluginImportFrom,
  __handleBatchImportForTesting: handleBatchImport,
} = await import('./index.js');

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

// Sanity: billing cache must redirect under TEST_HOME, otherwise our writes
// would clobber the real ~/.totalreclaw/.
assert(
  BILLING_CACHE_PATH.startsWith(TEST_HOME),
  `BILLING_CACHE_PATH redirects under TEST_HOME (got: ${BILLING_CACHE_PATH})`,
);

const dummyLogger = {
  info: () => {},
  warn: () => {},
  error: () => {},
  debug: () => {},
};

function resetCache(): void {
  try { fs.unlinkSync(BILLING_CACHE_PATH); } catch { /* ignore */ }
}

function setBillingTier(tier: 'free' | 'pro'): void {
  writeBillingCache({
    tier,
    free_writes_used: tier === 'free' ? 3 : 0,
    free_writes_limit: tier === 'free' ? 10 : 0,
    checked_at: Date.now(),
  });
}

const PRO_GATE_FRAGMENT = 'Memory imports are a Pro feature';
const IMPORT_FROM_SOURCES = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini'];
const BATCH_SOURCES = ['mem0', 'mcp-memory', 'chatgpt', 'claude', 'gemini'];

// ---------------------------------------------------------------------------
// handlePluginImportFrom — Pro gate
// ---------------------------------------------------------------------------

for (const source of IMPORT_FROM_SOURCES) {
  resetCache();
  setBillingTier('free');
  const result = await handlePluginImportFrom({ source }, dummyLogger as any);
  assert(
    result.success === false &&
      typeof result.error === 'string' &&
      result.error.includes(PRO_GATE_FRAGMENT) &&
      result.requires === 'pro',
    `handlePluginImportFrom: free tier blocks source=${source}`,
  );
}

for (const source of IMPORT_FROM_SOURCES) {
  resetCache();
  setBillingTier('pro');
  const result = await handlePluginImportFrom({ source }, dummyLogger as any);
  // Adapter may still error on missing input — we only assert the gate did
  // not produce the Pro-tier rejection.
  const isProGateError =
    result.success === false &&
    typeof result.error === 'string' &&
    result.error.includes(PRO_GATE_FRAGMENT);
  assert(
    !isProGateError,
    `handlePluginImportFrom: pro tier proceeds past gate for source=${source}`,
  );
}

// Missing cache → fail-CLOSED (skill side)
{
  resetCache();
  const result = await handlePluginImportFrom({ source: 'chatgpt' }, dummyLogger as any);
  assert(
    result.success === false &&
      typeof result.error === 'string' &&
      result.error.includes(PRO_GATE_FRAGMENT) &&
      result.requires === 'pro',
    'handlePluginImportFrom: missing cache fails closed (blocks)',
  );
}

// Invalid source rejected with a different error (sanity — validation
// fires before the Pro check is reachable for unknown sources, since
// the gate is identical for all sources anyway).
{
  resetCache();
  setBillingTier('pro');
  const result = await handlePluginImportFrom({ source: 'not-a-real-source' }, dummyLogger as any);
  assert(
    result.success === false &&
      typeof result.error === 'string' &&
      result.error.includes('Invalid source'),
    'handlePluginImportFrom: invalid source rejected with validation error',
  );
}

// ---------------------------------------------------------------------------
// handleBatchImport — Pro gate
// ---------------------------------------------------------------------------

for (const source of BATCH_SOURCES) {
  resetCache();
  setBillingTier('free');
  const result = await handleBatchImport({ source }, dummyLogger as any);
  assert(
    result.success === false &&
      typeof result.error === 'string' &&
      result.error.includes(PRO_GATE_FRAGMENT) &&
      result.requires === 'pro',
    `handleBatchImport: free tier blocks source=${source}`,
  );
}

for (const source of BATCH_SOURCES) {
  resetCache();
  setBillingTier('pro');
  const result = await handleBatchImport({ source }, dummyLogger as any);
  const isProGateError =
    result.success === false &&
    typeof result.error === 'string' &&
    result.error.includes(PRO_GATE_FRAGMENT);
  assert(
    !isProGateError,
    `handleBatchImport: pro tier proceeds past gate for source=${source}`,
  );
}

// Missing cache → fail-CLOSED
{
  resetCache();
  const result = await handleBatchImport({ source: 'chatgpt' }, dummyLogger as any);
  assert(
    result.success === false &&
      typeof result.error === 'string' &&
      result.error.includes(PRO_GATE_FRAGMENT) &&
      result.requires === 'pro',
    'handleBatchImport: missing cache fails closed (blocks)',
  );
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
