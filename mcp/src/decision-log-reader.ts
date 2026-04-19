/**
 * Decision-log reader for the pin-on-tombstone recovery path.
 *
 * When a user pins fact F1 via one client, later forgets F1 via another client
 * (which writes a 1-byte `0x00` tombstone on-chain), then asks to re-pin F1,
 * the pin tool must reconstruct F1's plaintext Claim from the decision log —
 * the on-chain ciphertext is no longer decryptable.
 *
 * Plugin parity: mirrors `findLoserClaimInDecisionLog` in
 * `skill/plugin/contradiction-sync.ts` (see §Phase 2.1). Both clients resolve
 * `decisions.jsonl` from the same state dir (`$TOTALRECLAW_STATE_DIR`, default
 * `~/.totalreclaw/`), so plugin writes and MCP reads see the same file.
 *
 * Delegates to the Rust-core WASM export `findLoserClaimInDecisionLog` and
 * falls back to a local JSONL walk if the WASM call throws. Matches the
 * plugin's fallback pattern byte-for-byte so the two implementations agree
 * on edge cases.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Lazy-load WASM core (same pattern as tools/pin.ts + claims-helper.ts)
// eslint-disable-next-line @typescript-eslint/no-var-requires
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    _wasm = require('@totalreclaw/core');
  }
  return _wasm!;
}

/** Subset of DecisionLogEntry needed for the local fallback walk. */
interface MinimalDecisionRow {
  action?: string;
  existing_claim_id?: string;
  loser_claim_json?: string;
}

function resolveStateDir(): string {
  const override = process.env.TOTALRECLAW_STATE_DIR;
  if (override && override.length > 0) return override;
  return path.join(os.homedir(), '.totalreclaw');
}

/** Path to `decisions.jsonl` in the active state dir. */
export function decisionsLogPath(): string {
  return path.join(resolveStateDir(), 'decisions.jsonl');
}

/**
 * Walk `decisions.jsonl` in reverse and return the most recent canonical
 * Claim JSON for a fact that was tombstoned by a `supersede_existing`
 * decision. Returns `null` when the log is missing, empty, or has no matching
 * row. Never throws — the recovery path treats any failure as "no recovery
 * available" and lets the caller surface a clean error.
 *
 * Only matches `supersede_existing` rows (not `tie_leave_both` or `skip_new`)
 * because those are the only rows that actually tombstone the existing fact.
 * Only returns rows that have `loser_claim_json` populated — pre-Phase-2.1
 * supersede rows do not carry the field and cannot be recovered from.
 *
 * Mirrors `findLoserClaimInDecisionLog` in
 * `skill/plugin/contradiction-sync.ts:1090`.
 */
export function findLoserClaimInDecisionLog(factId: string): string | null {
  let logContent = '';
  try {
    logContent = fs.readFileSync(decisionsLogPath(), 'utf-8');
  } catch {
    return null;
  }
  if (!logContent || logContent.length === 0) return null;

  try {
    const result = getWasm().findLoserClaimInDecisionLog(factId, logContent);
    return result === 'null' ? null : result;
  } catch {
    // Fallback: local JSONL walk if the WASM call throws. Matches plugin
    // contradiction-sync.ts fallback byte-for-byte.
    const lines = logContent.split('\n').filter((l) => l.length > 0);
    for (let i = lines.length - 1; i >= 0; i--) {
      let entry: MinimalDecisionRow;
      try {
        entry = JSON.parse(lines[i]) as MinimalDecisionRow;
      } catch {
        continue;
      }
      if (entry.action !== 'supersede_existing') continue;
      if (entry.existing_claim_id !== factId) continue;
      if (typeof entry.loser_claim_json !== 'string' || entry.loser_claim_json.length === 0) {
        continue;
      }
      return entry.loser_claim_json;
    }
    return null;
  }
}
