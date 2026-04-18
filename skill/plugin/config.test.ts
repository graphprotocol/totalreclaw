/**
 * Tests for skill/plugin/config.ts.
 *
 * Covers the v1 env var cleanup surface:
 *   - Removed env vars trigger the one-shot warning.
 *   - `resolveTuning(features)` reads from the relay billing response when
 *     fields are present, falls through to env/defaults otherwise.
 *   - Removed env vars (CHAIN_ID, EMBEDDING_MODEL, STORE_DEDUP, LLM_MODEL,
 *     SESSION_ID, TAXONOMY_VERSION, CLAIM_FORMAT, DIGEST_MODE) have NO
 *     effect on CONFIG output.
 *
 * Run with:
 *   npx tsx config.test.ts
 */

import {
  resolveTuning,
  CONFIG,
  __internal,
  setChainIdOverride,
  __resetChainIdOverrideForTests,
} from './config.js';

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

// ---------------------------------------------------------------------------
// REMOVED_ENV_VARS matches the env vars reference doc
// ---------------------------------------------------------------------------

{
  const expected = [
    'TOTALRECLAW_CHAIN_ID',
    'TOTALRECLAW_EMBEDDING_MODEL',
    'TOTALRECLAW_STORE_DEDUP',
    'TOTALRECLAW_LLM_MODEL',
    'TOTALRECLAW_SESSION_ID',
    'TOTALRECLAW_TAXONOMY_VERSION',
    'TOTALRECLAW_CLAIM_FORMAT',
    'TOTALRECLAW_DIGEST_MODE',
  ];
  assertEq(
    [...__internal.REMOVED_ENV_VARS],
    expected,
    'REMOVED_ENV_VARS: matches env vars reference doc',
  );
}

// ---------------------------------------------------------------------------
// warnRemovedEnvVars emits a warning when any removed env var is set
// ---------------------------------------------------------------------------

{
  const originals: Record<string, string | undefined> = {};
  for (const name of __internal.REMOVED_ENV_VARS) {
    originals[name] = process.env[name];
    delete process.env[name];
  }

  try {
    // No removed vars set → no warning.
    let calls = 0;
    __internal.warnRemovedEnvVars(() => {
      calls++;
    });
    assert(calls === 0, 'warnRemovedEnvVars: quiet when no removed vars set');

    // One removed var set → one warning call.
    process.env.TOTALRECLAW_CHAIN_ID = '100';
    let warned: string | null = null;
    __internal.warnRemovedEnvVars((msg) => {
      warned = msg;
    });
    assert(warned !== null, 'warnRemovedEnvVars: warns when CHAIN_ID set');
    assert(
      (warned ?? '').includes('TOTALRECLAW_CHAIN_ID'),
      'warnRemovedEnvVars: warning mentions CHAIN_ID',
    );
    assert(
      (warned ?? '').includes('env-vars-reference'),
      'warnRemovedEnvVars: warning links to env vars reference',
    );
  } finally {
    for (const [name, val] of Object.entries(originals)) {
      if (val === undefined) delete process.env[name];
      else process.env[name] = val;
    }
  }
}

// ---------------------------------------------------------------------------
// Removed env vars have NO effect on CONFIG output
// ---------------------------------------------------------------------------

{
  // CONFIG is frozen at module-load time for most fields, so the test here
  // is that the fields that USED to be env-backed now have hardcoded values
  // regardless of env.
  process.env.TOTALRECLAW_CHAIN_ID = '1234';
  assertEq(CONFIG.chainId, 84532, 'CHAIN_ID env ignored → default 84532');

  // Store dedup flag is always true.
  assertEq(CONFIG.storeDedupEnabled, true, 'STORE_DEDUP env ignored → always true');

  // No llmModel / sessionId / embeddingModel fields on CONFIG.
  assert(
    !('llmModel' in CONFIG),
    'CONFIG.llmModel: removed (no field for TOTALRECLAW_LLM_MODEL)',
  );
  assert(
    !('sessionId' in CONFIG),
    'CONFIG.sessionId: removed (no field for TOTALRECLAW_SESSION_ID)',
  );
  assert(
    !('embeddingModel' in CONFIG),
    'CONFIG.embeddingModel: removed (no field for TOTALRECLAW_EMBEDDING_MODEL)',
  );

  delete process.env.TOTALRECLAW_CHAIN_ID;
}

// ---------------------------------------------------------------------------
// Chain ID auto-detect override
//
// Regression test for the P0 latent bug where Pro-tier users would sign
// UserOps against chain 84532 (Base Sepolia) while the relay routed their
// writes to chain 100 (Gnosis mainnet). The bundler would reject the sig
// with AA23. Fix: CONFIG.chainId is a getter that reads a runtime override
// set from the billing response. See syncChainIdFromTier in index.ts.
// ---------------------------------------------------------------------------

{
  __resetChainIdOverrideForTests();
  assertEq(CONFIG.chainId, 84532, 'chainId: defaults to 84532 with no override');

  setChainIdOverride(100);
  assertEq(CONFIG.chainId, 100, 'chainId: reflects Pro tier override (Gnosis)');

  setChainIdOverride(84532);
  assertEq(CONFIG.chainId, 84532, 'chainId: reflects Free tier override (Base Sepolia)');

  // Pro → Free downgrade flow: override flips back correctly.
  setChainIdOverride(100);
  assertEq(CONFIG.chainId, 100, 'chainId: Pro override set');
  setChainIdOverride(84532);
  assertEq(CONFIG.chainId, 84532, 'chainId: downgrade to Free flips override back');

  __resetChainIdOverrideForTests();
  assertEq(CONFIG.chainId, 84532, 'chainId: reset returns to 84532 default');
}

// ---------------------------------------------------------------------------
// resolveTuning: billing features take priority over local defaults
// ---------------------------------------------------------------------------

{
  const defaults = resolveTuning(null);
  assert(
    typeof defaults.cosineThreshold === 'number',
    'resolveTuning(null): returns number for cosineThreshold',
  );
  assert(
    typeof defaults.minImportance === 'number',
    'resolveTuning(null): returns number for minImportance',
  );

  // Billing features override defaults.
  const withFeatures = resolveTuning({
    cosine_threshold: 0.42,
    relevance_threshold: 0.55,
    semantic_skip_threshold: 0.99,
    min_importance: 9,
    cache_ttl_ms: 60_000,
    trapdoor_batch_size: 8,
    subgraph_page_size: 500,
  });
  assertEq(withFeatures.cosineThreshold, 0.42, 'resolveTuning: cosine from features');
  assertEq(withFeatures.relevanceThreshold, 0.55, 'resolveTuning: relevance from features');
  assertEq(withFeatures.semanticSkipThreshold, 0.99, 'resolveTuning: semantic from features');
  assertEq(withFeatures.minImportance, 9, 'resolveTuning: minImportance from features');
  assertEq(withFeatures.cacheTtlMs, 60_000, 'resolveTuning: cacheTtl from features');
  assertEq(withFeatures.trapdoorBatchSize, 8, 'resolveTuning: trapdoor batch from features');
  assertEq(withFeatures.pageSize, 500, 'resolveTuning: page size from features');

  // Partial features: missing fields fall back to defaults.
  const partial = resolveTuning({ cosine_threshold: 0.5 });
  assertEq(partial.cosineThreshold, 0.5, 'resolveTuning: partial override applied');
  assertEq(
    partial.minImportance,
    CONFIG.minImportance,
    'resolveTuning: missing field falls back to CONFIG default',
  );
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
