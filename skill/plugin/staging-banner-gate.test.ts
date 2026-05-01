/**
 * staging-banner-gate.test.ts (3.3.4-rc.1)
 *
 * Pins the banner-emit invariant for the RC-staging banner introduced
 * in 3.3.3-rc.1 (PR #165).
 *
 * Bug found in 3.3.3-rc.1 QA (Pedro 2026-04-30): the banner appeared
 * NEVER across an entire conversation despite the build being bound to
 * staging. Root cause was structural: `stagingBannerShown` was set to
 * `true` AS SOON AS the banner block was constructed, but multiple
 * before_agent_start return paths returned `undefined` (e.g. zero
 * memory matches on the first turn), silently dropping the block AND
 * leaving the flag flipped — so subsequent calls never reconstructed
 * the block.
 *
 * Fix: the flag flips ONLY when a return path actually includes the
 * block in its `prependContext`, via `consumeBannerForPrepend()`.
 *
 * This test pins the invariant by simulating the helper's lifecycle
 * (build block → consume → flag flip) and verifying:
 *   - First consume returns the banner string + flips the flag.
 *   - Second consume returns '' (already delivered).
 *   - Build-but-NEVER-consume leaves the flag UNflipped (so the next
 *     before_agent_start invocation reconstructs the block).
 *
 * The test re-implements the helper inline because the helper is a
 * closure over hook-local state in index.ts; pulling it out to a
 * standalone export would broaden the public surface unnecessarily.
 * The test asserts the SHAPE of the fix, not the runtime path.
 */

let passed = 0;
let failed = 0;
function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) { console.log(`ok ${n} - ${name}`); passed++; }
  else { console.log(`not ok ${n} - ${name}`); failed++; }
}

function simulateBannerLifecycle(opts: {
  serverUrl: string;
  serverUrlEnvOverridden: boolean;
  prependContextPathTaken: boolean;
}): { stagingBannerShown: boolean; emittedBlock: string } {
  let stagingBannerShown = false;
  let stagingBannerBlock = '';

  // Simulate the before_agent_start prologue.
  if (!stagingBannerShown) {
    const usingStagingDefault = opts.serverUrl.includes('api-staging.totalreclaw.xyz');
    const userOverrode = opts.serverUrlEnvOverridden;
    if (usingStagingDefault && !userOverrode) {
      stagingBannerBlock = '## staging-banner';
      // Critical: do NOT flip stagingBannerShown here.
    } else {
      // Non-RC artifact OR user override — never fire this lifetime.
      stagingBannerShown = true;
    }
  }

  const consumeBannerForPrepend = (): string => {
    if (stagingBannerBlock === '') return '';
    stagingBannerShown = true;
    return stagingBannerBlock;
  };

  let emittedBlock = '';
  if (opts.prependContextPathTaken) {
    // Simulate a return path that calls `consumeBannerForPrepend()` inline.
    emittedBlock = consumeBannerForPrepend();
  } else {
    // Simulate a return-undefined path — block built but never delivered.
    // No call to consume.
  }

  return { stagingBannerShown, emittedBlock };
}

// ---------------------------------------------------------------------------
// Test 1: staging build + prepend path taken -> banner emitted, flag flipped.
// ---------------------------------------------------------------------------

{
  const r = simulateBannerLifecycle({
    serverUrl: 'https://api-staging.totalreclaw.xyz',
    serverUrlEnvOverridden: false,
    prependContextPathTaken: true,
  });
  assert(r.emittedBlock === '## staging-banner', 'staging + prepend: banner emitted on first call');
  assert(r.stagingBannerShown === true, 'staging + prepend: flag flips on emit');
}

// ---------------------------------------------------------------------------
// Test 2: staging build + RETURN UNDEFINED path -> banner NOT emitted,
//         flag NOT flipped (next before_agent_start can retry).
//
// This is the 3.3.4-rc.1 fix: pre-fix, the flag flipped on build, so the
// next call would skip block construction entirely. Post-fix, the flag
// stays false and the next call reconstructs.
// ---------------------------------------------------------------------------

{
  const r = simulateBannerLifecycle({
    serverUrl: 'https://api-staging.totalreclaw.xyz',
    serverUrlEnvOverridden: false,
    prependContextPathTaken: false,
  });
  assert(r.emittedBlock === '', 'staging + return-undefined: nothing emitted');
  assert(
    r.stagingBannerShown === false,
    '3.3.4-rc.1 fix: staging + return-undefined leaves flag UNflipped — next call retries',
  );
}

// ---------------------------------------------------------------------------
// Test 3: stable build (production URL) -> never builds banner, flag flipped
// to permanently suppress for this gateway-process lifetime.
// ---------------------------------------------------------------------------

{
  const r = simulateBannerLifecycle({
    serverUrl: 'https://api.totalreclaw.xyz',
    serverUrlEnvOverridden: false,
    prependContextPathTaken: true,
  });
  assert(r.emittedBlock === '', 'stable build: no banner emitted');
  assert(r.stagingBannerShown === true, 'stable build: flag flipped to suppress');
}

// ---------------------------------------------------------------------------
// Test 4: staging build BUT user-env override -> never builds banner, flag
// flipped to permanently suppress (operator pinned a custom URL).
// ---------------------------------------------------------------------------

{
  const r = simulateBannerLifecycle({
    serverUrl: 'https://api-staging.totalreclaw.xyz',
    serverUrlEnvOverridden: true,
    prependContextPathTaken: true,
  });
  assert(r.emittedBlock === '', 'staging + user override: no banner emitted');
  assert(r.stagingBannerShown === true, 'staging + user override: flag flipped to suppress');
}

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
