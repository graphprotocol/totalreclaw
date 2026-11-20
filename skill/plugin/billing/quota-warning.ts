/**
 * Quota-warning copy for the OpenClaw plugin (WQ-B Task 6).
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.3 (R-C). The >80%
 * quota warning is tier-aware and carries no hardcoded cap: the figures
 * come from the billing cache (`free_writes_used` / `free_writes_limit`,
 * which the relay sources from Stripe and reports per tier). A Pro user
 * past the threshold gets a reset notice with NO upgrade pitch — they
 * cannot upgrade past Pro, and the old one-size-fits-all "Visit … to
 * upgrade" line read as a false nudge. When the relay sends no limit
 * (older relay), no warning is produced; a number is never substituted.
 *
 * Pure module: no imports, so it stays testable without the plugin's heavy
 * dependency tree.
 */

/** Usage ratio at which the warning fires (80%, unchanged). */
export const QUOTA_WARNING_THRESHOLD = 0.8;

export interface QuotaWarningInput {
  tier?: string;
  free_writes_used: number;
  free_writes_limit: number;
}

/**
 * Build the >80% quota warning, or `null` when it must not fire
 * (limit unknown/zero, or usage below the threshold).
 */
export function formatQuotaWarning(c: QuotaWarningInput): string | null {
  const used = c?.free_writes_used ?? 0;
  const limit = c?.free_writes_limit ?? 0;
  if (limit <= 0) return null;
  const ratio = used / limit;
  if (ratio < QUOTA_WARNING_THRESHOLD) return null;
  const pct = Math.round(ratio * 100);
  const head = `\n\nTotalReclaw quota warning: ${used}/${limit} memories used this month (${pct}%).`;
  if (c?.tier === 'pro') {
    return `${head} The quota resets on the 1st of next month.`;
  }
  return `${head} Visit https://totalreclaw.xyz/pricing to upgrade.`;
}
