/**
 * TotalReclaw MCP - quota-exceeded copy from the billing snapshot
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.1 (R-C). The
 * 403 quota_exceeded message must never hardcode a tier cap: Free 250 and
 * the Pro figure both come from /v1/billing/status via the cached snapshot
 * (the relay sources them from Stripe). When the snapshot (or a field) is
 * absent — an older relay, or a quota error right after an account swap —
 * the copy degrades to wording with no number.
 *
 * Pure module: no imports, no I/O. Exact strings are spec-pinned.
 */

/** Subset of the billing snapshot the quota copy reads. */
export interface QuotaSnapshot {
  tier?: string;
  free_writes_limit?: number;
  upgrade_writes_limit?: number;
}

const NO_SNAPSHOT =
  'You have reached your monthly memory limit. The counter resets on the 1st of the month. '
  + 'Call totalreclaw_status for your current cap; on Free, totalreclaw_upgrade raises it.';

/**
 * Build the quota-exceeded message from a (possibly stale or absent)
 * billing snapshot. Any snapshot beats none — callers should not apply a
 * TTL before deciding what to say.
 */
export function quotaExceededMessage(s: QuotaSnapshot | null | undefined): string {
  const limit = s?.free_writes_limit;
  if (typeof limit !== 'number') return NO_SNAPSHOT;
  const lim = limit.toLocaleString('en-US');

  if (s?.tier === 'pro') {
    return `Monthly Pro limit reached (${lim} memories plus grace). The counter resets on the 1st of the month.`;
  }

  const up = s?.upgrade_writes_limit;
  const upgradeTarget = typeof up === 'number'
    ? `for ${up.toLocaleString('en-US')} memories a month with the totalreclaw_upgrade tool.`
    : `for a higher monthly limit with the totalreclaw_upgrade tool.`;
  return `Monthly Free limit reached (${lim} memories). The counter resets on the 1st of the month. Upgrade to Pro ${upgradeTarget}`;
}
