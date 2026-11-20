/**
 * Tests for quota-warning.ts (WQ-B Task 6).
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.3 (R-C). The
 * >80% quota warning must be tier-aware and number-free: a Pro user past
 * the threshold gets a reset notice with NO upgrade pitch (they cannot
 * upgrade past Pro), and the copy hardcodes no cap — the figures come from
 * the billing cache, which the relay sources from Stripe.
 *
 * Run with: npx tsx billing/quota-warning.test.ts
 *
 * TAP-style output, no jest dependency.
 */

import assert from 'node:assert/strict';
import { formatQuotaWarning, QUOTA_WARNING_THRESHOLD } from './quota-warning.js';

let passed = 0;
let failed = 0;

function check(name: string, fn: () => void): void {
  const n = passed + failed + 1;
  try {
    fn();
    console.log(`ok ${n} - ${name}`);
    passed++;
  } catch (err) {
    console.log(`not ok ${n} - ${name}`);
    console.log(`  ${err instanceof Error ? err.message : String(err)}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// Pro: reset notice, no upgrade pitch
// ---------------------------------------------------------------------------

check('pro 2700/3000 warns at 90% with a reset notice and no upgrade pitch', () => {
  const w = formatQuotaWarning({ tier: 'pro', free_writes_used: 2700, free_writes_limit: 3000 });
  assert.ok(w !== null, 'expected a warning');
  assert.match(w!, /90%/);
  assert.match(w!, /resets on the 1st/);
  assert.ok(!/upgrade/i.test(w!), 'Pro warning must not pitch an upgrade');
});

// ---------------------------------------------------------------------------
// Free: keeps the existing upgrade text
// ---------------------------------------------------------------------------

check('free 210/250 warns at 84% with the pricing upgrade line', () => {
  const w = formatQuotaWarning({ tier: 'free', free_writes_used: 210, free_writes_limit: 250 });
  assert.ok(w !== null, 'expected a warning');
  assert.match(w!, /84%/);
  assert.ok(
    w!.includes('Visit https://totalreclaw.xyz/pricing to upgrade.'),
    'Free warning keeps the existing upgrade text',
  );
});

// ---------------------------------------------------------------------------
// Below threshold / unknown limit → no warning
// ---------------------------------------------------------------------------

check('free 100/250 (40%) returns null', () => {
  assert.equal(formatQuotaWarning({ tier: 'free', free_writes_used: 100, free_writes_limit: 250 }), null);
});

check('limit 0 (relay sent no limit) returns null', () => {
  assert.equal(formatQuotaWarning({ tier: 'free', free_writes_used: 1, free_writes_limit: 0 }), null);
});

check('exactly 200/250 (>= threshold) still warns', () => {
  const w = formatQuotaWarning({ tier: 'free', free_writes_used: 200, free_writes_limit: 250 });
  assert.ok(w !== null, '>= 0.8 must warn, as before');
  assert.match(w!, /80%/);
});

check('threshold constant is 0.8', () => {
  assert.equal(QUOTA_WARNING_THRESHOLD, 0.8);
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
