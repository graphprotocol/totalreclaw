/**
 * Quota-warning copy for the NanoClaw skill (WQ-B Task 6).
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.3 (R-C). Same
 * shape as the OpenClaw plugin's `billing/quota-warning.ts`: tier-aware,
 * no hardcoded cap, no warning when the relay sends no limit. Pro users
 * get a reset notice with no upgrade pitch. Only the Free-tier tail
 * differs (NanoClaw points at its `totalreclaw_upgrade` tool instead of
 * the pricing URL).
 *
 * Pure module: no imports, so it stays testable without the skill's
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
  return `${head} Use the totalreclaw_upgrade tool to upgrade to Pro.`;
}
