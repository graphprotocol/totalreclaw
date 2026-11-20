/**
 * @jest-environment node
 *
 * quota-copy.test.ts
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.1 (R-C). The
 * quota-exceeded message the MCP shows when the relay answers 403
 * quota_exceeded must take its numbers from the cached billing snapshot —
 * never a hardcoded cap. Against an older relay (no snapshot fields) every
 * variant degrades to wording with no number.
 *
 * The exact strings below are the spec's; changing one is a spec change.
 */

import { quotaExceededMessage } from '../src/quota-copy';

describe('quotaExceededMessage (billing snapshot copy, spec §4.1)', () => {
  test('free with figure', () => {
    expect(
      quotaExceededMessage({ tier: 'free', free_writes_limit: 250, upgrade_writes_limit: 3000 }),
    ).toBe(
      'Monthly Free limit reached (250 memories). The counter resets on the 1st of the month. '
      + 'Upgrade to Pro for 3,000 memories a month with the totalreclaw_upgrade tool.',
    );
  });

  test('free without figure', () => {
    expect(quotaExceededMessage({ tier: 'free', free_writes_limit: 250 })).toBe(
      'Monthly Free limit reached (250 memories). The counter resets on the 1st of the month. '
      + 'Upgrade to Pro for a higher monthly limit with the totalreclaw_upgrade tool.',
    );
  });

  test('pro', () => {
    expect(quotaExceededMessage({ tier: 'pro', free_writes_limit: 3000 })).toBe(
      'Monthly Pro limit reached (3,000 memories plus grace). The counter resets on the 1st of the month.',
    );
  });

  test('no snapshot (null)', () => {
    expect(quotaExceededMessage(null)).toBe(
      'You have reached your monthly memory limit. The counter resets on the 1st of the month. '
      + 'Call totalreclaw_status for your current cap; on Free, totalreclaw_upgrade raises it.',
    );
  });

  test('no snapshot (undefined)', () => {
    expect(quotaExceededMessage(undefined)).toBe(
      'You have reached your monthly memory limit. The counter resets on the 1st of the month. '
      + 'Call totalreclaw_status for your current cap; on Free, totalreclaw_upgrade raises it.',
    );
  });

  test('no snapshot (empty object)', () => {
    expect(quotaExceededMessage({})).toBe(
      'You have reached your monthly memory limit. The counter resets on the 1st of the month. '
      + 'Call totalreclaw_status for your current cap; on Free, totalreclaw_upgrade raises it.',
    );
  });

  test('no snapshot (tier but no limit)', () => {
    expect(quotaExceededMessage({ tier: 'pro' })).toBe(
      'You have reached your monthly memory limit. The counter resets on the 1st of the month. '
      + 'Call totalreclaw_status for your current cap; on Free, totalreclaw_upgrade raises it.',
    );
  });
});
