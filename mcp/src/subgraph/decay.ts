/**
 * Live-fact decayScore (graphprotocol/totalreclaw#789).
 *
 * The on-chain `decayScore` (protobuf field 6) is a liveness flag, not an
 * importance scale: 0 = tombstone, 1.0 = live. It carries no importance;
 * importance lives only in the encrypted blob (readers take it from there).
 *
 * The subgraph marks any fact with decayScore < 0.3 inactive
 * (`subgraph/src/mapping.ts` INACTIVE_THRESHOLD), so the live value must stay
 * at or above that. NEVER use this for tombstones: decayScore 0 is how
 * forget / supersede delete.
 *
 * The rule is defined once in `totalreclaw-core` (`live_decay_score`); this
 * module calls the WASM export when the installed core has it and otherwise
 * uses the same constant (the installed core may predate the function).
 * `tests/parity/decay-score-parity.test.ts` keeps the two in lockstep.
 */
let coreModule: unknown = {};
try {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  coreModule = require('@totalreclaw/core');
} catch {
  // Core is always installed in the server; a bare-bones test harness may not
  // have it. The local constant below is the same value.
}

/** Local copy of core's `LIVE_DECAY_SCORE`. */
export const LIVE_DECAY_SCORE = 1.0;

/** Subgraph `INACTIVE_THRESHOLD` (drift-tested against mapping.ts). */
export const ACTIVE_DECAY_THRESHOLD = 0.3;

/** decayScore for a LIVE fact: always 1.0, regardless of importance. */
export function liveDecayScore(): number {
  const fn = (coreModule as { liveDecayScore?: unknown }).liveDecayScore;
  return typeof fn === 'function' ? (fn as () => number)() : LIVE_DECAY_SCORE;
}
