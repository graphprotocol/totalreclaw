/**
 * Live-fact decayScore floor (graphprotocol/totalreclaw#789, interim).
 *
 * The subgraph marks any fact with decayScore < 0.3 inactive
 * (`subgraph/src/mapping.ts` INACTIVE_THRESHOLD), hiding it from every client.
 * Live-fact writes are therefore floored at this threshold.
 *
 * NEVER apply this to tombstones: decayScore 0 is how forget / supersede delete.
 */
export const ACTIVE_DECAY_THRESHOLD = 0.3;

/**
 * decayScore for a LIVE fact: `importance / 10` (division, not `* 0.1`),
 * floored at ACTIVE_DECAY_THRESHOLD. Non-finite importance is treated as 5.
 *
 * Divergence note: importance 1 -> 0.3 here, while the Python batch path takes
 * values <= 1 as already 0-1 (1 -> 1.0). There is deliberately no upper clamp
 * (10+ stays >= 1.0). The canonical rule lands in the core helper (#789 plan).
 */
export function liveDecayScore(importance: number): number {
  const imp = typeof importance === 'number' && Number.isFinite(importance) ? importance : 5;
  return Math.max(imp / 10, ACTIVE_DECAY_THRESHOLD);
}
