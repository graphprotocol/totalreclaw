/**
 * On-chain decayScore for LIVE facts (graphprotocol/totalreclaw#789).
 *
 * decayScore (protobuf field 6) is a liveness flag, not importance:
 * 0 = tombstone, 1.0 = live. It carries no importance; importance lives only
 * in the encrypted blob and readers take it from there.
 *
 * The rule is defined once in `totalreclaw-core` (`live_decay_score`). The
 * plugin is pinned to a core release that predates that function, so this is
 * a verbatim local copy; `tests/parity/decay-score-parity.test.ts` keeps it
 * identical to core, MCP and Python.
 *
 * The subgraph marks a fact inactive when decayScore < 0.3
 * (`subgraph/src/mapping.ts`), so the live value must stay >= 0.3.
 * NEVER use this for tombstones (decayScore 0 deletes).
 */

/** Local copy of core's `LIVE_DECAY_SCORE`. */
export const LIVE_DECAY_SCORE = 1.0;

/** Subgraph `INACTIVE_THRESHOLD` (drift-tested against mapping.ts). */
export const ACTIVE_DECAY_THRESHOLD = 0.3;

/** decayScore for a LIVE fact: always 1.0, regardless of importance. */
export function liveDecayScore(): number {
  return LIVE_DECAY_SCORE;
}
