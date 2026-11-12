/**
 * Recall top-k resolution — how many facts a recall returns when the caller
 * does not pass `k`.
 *
 * Precedence (highest first), mirroring the Python client
 * (`AgentState.get_recall_top_k`):
 *   1. `TOTALRECLAW_RECALL_TOP_K` env var (positive integer).
 *   2. Relay `features.recall_top_k` from `/v1/billing/status`, once a
 *      billing response has been seen this process.
 *   3. `DEFAULT_RECALL_TOP_K` (16).
 *
 * An explicit `k` from the caller always wins over all three; this module
 * only supplies the default.
 *
 * 16 is the cross-client default since public #365 (LOCOMO: overall
 * 40% -> 47%, multi-hop 18% -> 35% going from 8 to 16; k=24 was flat).
 */

export const DEFAULT_RECALL_TOP_K = 16;

let serverRecallTopK: number | undefined;

function asPositiveInt(value: unknown): number | undefined {
  const n = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
  if (typeof n !== 'number' || !Number.isInteger(n) || n <= 0) return undefined;
  return n;
}

/**
 * Record the relay-advertised `features.recall_top_k`. Invalid or missing
 * values are ignored and keep the previously seen value.
 */
export function setServerRecallTopK(value: unknown): void {
  const n = asPositiveInt(value);
  if (n !== undefined) serverRecallTopK = n;
}

/** Resolve the default recall top-k (env > relay > 16). */
export function getRecallTopK(env: NodeJS.ProcessEnv = process.env): number {
  const fromEnv = asPositiveInt(env.TOTALRECLAW_RECALL_TOP_K);
  if (fromEnv !== undefined) return fromEnv;
  if (serverRecallTopK !== undefined) return serverRecallTopK;
  return DEFAULT_RECALL_TOP_K;
}

/** Test-only: forget any relay value seen so far. */
export function _resetServerRecallTopKForTests(): void {
  serverRecallTopK = undefined;
}
