/**
 * Boot-time chain-gate predicate for client-side `executeBatch` UserOp
 * submission. Spec #281 §9 Phase 1 (item imp-16).
 *
 * The gate batches via `executeBatch` on Gnosis (chain 100). After ops-1
 * (2026-06-05) BOTH tiers run on Gnosis, so both now batch — the chain==100
 * check is retained as a safety guard (a non-100 chain, which shouldn't occur
 * after ops-1, falls back to single-fact). The legacy Free ⇒ Base Sepolia
 * (84532) single-fact path is gone with the retired two-tier routing (#402).
 * The `TOTALRECLAW_GNOSIS_BATCH_ENABLED` env var is a hard kill-switch —
 * setting it to `false` disables batching regardless of chain, so ops can
 * revert to single-fact submission without a client redeploy. Behaviour of
 * `shouldBatchOnChain` is unchanged by #402.
 *
 * Read at boot only (module-load time). Per-write reads would re-parse the
 * env on every submission — too expensive for the auto-extraction hot path
 * and pointless because the env doesn't change mid-process.
 *
 * Sibling work-leaves wire `shouldBatchOnChain` into `submitFactBatchOnChain`
 * (TS) and `agent/lifecycle.py` (Python mirror in `batch_gate.py`); this
 * module ships the primitive only.
 *
 * Env read is centralized in entry.ts (env-reading seam, Task 1.3 of the
 * OpenClaw native integration plan, 2026-06-21).
 */

import { isGnosisBatchEnabledAtBoot } from './entry.js';

const GNOSIS_CHAIN_ID = 100;

export function shouldBatchOnChain(chainId: number): boolean {
  if (!isGnosisBatchEnabledAtBoot()) return false;
  return chainId === GNOSIS_CHAIN_ID;
}

export const __testing = {
  readGateForTests(env: NodeJS.ProcessEnv, chainId: number): boolean {
    const enabled = (env.TOTALRECLAW_GNOSIS_BATCH_ENABLED ?? '').toLowerCase() !== 'false';
    if (!enabled) return false;
    return chainId === GNOSIS_CHAIN_ID;
  },
};
