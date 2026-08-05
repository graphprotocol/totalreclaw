// scanner-sim: allow
/**
 * The `memory_pin|unpin|retype|set_scope` curate closure — the curation
 * sibling of `memory_save`'s store closure (`memory/native-store.ts`).
 *
 * WHY THIS FILE EXISTS (#573):
 *   Curation used to be CLI-only (`tr pin` / `tr retype` / `tr set_scope`,
 *   #563). With no agent tool surface, an agent asked to "pin that"
 *   confabulated a `memory_save` and reported a successful pin that never
 *   happened (importance ≠ pin — see the CLAUDE.md "Plugin curation" gap +
 *   §3b "Why" in the 2026-08-02 session handoff). #573 adds four in-process
 *   agent tools (memory_pin / memory_unpin / memory_retype / memory_set_scope)
 *   that route through the SAME `runCurationOp` dispatch the CLI uses, so the
 *   agent has a real curation path instead of improvising with memory_save.
 *
 *   The pure dispatch lives in `memory/curation-op.ts` (extracted there so the
 *   tool path does NOT import `cli/tr-cli.ts` and drag the whole CLI bin graph
 *   into the agent-tool surface). This file is the closure that binds that
 *   dispatch to the live paired-account state — the curation analogue of
 *   `buildNativeStore` for the write path.
 *
 *   The closure — NOT the tool — owns the fail-soft precondition
 *   (ensureInitialized + paired check) and the PinOpDeps assembly from
 *   module singletons, so the tool factories stay scanner-trivial
 *   orchestration (mirrors the memory_save split: native-store.ts owns the
 *   truthfulness contract, tools.ts only validates + forwards).
 *
 * TRUTHFULNESS CONTRACT (the bug fix — same shape as native-store.ts):
 *   - init throws            → `{ ok:false, error:'setup incomplete: …' }`
 *   - not paired             → `{ ok:false, error:'not paired — …' }`
 *   - runCurationOp ok:true  → forwarded verbatim (carries new_fact_id + tx_hash
 *                              so the agent reports a REAL supersession, not a
 *                              confabulated "pinned")
 *   - runCurationOp ok:false → forwarded verbatim (agent relays the error; never
 *                              claims a curation op succeeded that did not)
 *
 *   The agent tool descriptions reinforce this in prose (the §6 lesson: a
 *   constraint that must hold lives in the tool description, not SKILL.md), but
 *   the closure is the actual enforcement — it returns the truthful signals the
 *   description tells the agent to relay.
 */
import type { MemoryType, MemoryScope } from '../extraction/extractor.js';
import type { PinOpDeps } from './pin.js';
import type { CurationOp, CurationCliResult } from './curation-op.js';

// ---------------------------------------------------------------------------
// Types — the curate closure contract (mirrors TrMemorySaveFn's shape).
// ---------------------------------------------------------------------------

/**
 * A parsed curation request from a tool handler — the JSON-arg analogue of the
 * CLI's `CurationParsedArgs`. `reason` is pin-only; `newType` is retype-only;
 * `newScope` is set_scope-only. Tool handlers translate the MCP-shaped inputs
 * (`memory_id`/`fact_id`/`new_type`/`scope`/`reason`) into this shape before
 * calling `curate` (MCP is the parity reference for argument shapes — §3b).
 */
export interface TrCurationInput {
  op: CurationOp;
  /** Canonical id field (MCP `memory_id`, with `fact_id` as a back-compat alias). */
  factId: string;
  /** pin-only: human-readable reason logged for tuning. */
  reason?: string;
  /** retype-only: new v1 taxonomy type. */
  newType?: MemoryType;
  /** set_scope-only: new life-domain scope. */
  newScope?: MemoryScope;
}

/**
 * The curate closure result. Structurally identical to `CurationCliResult`
 * (the normalized shape `runCurationOp` returns) — re-aliased here so the
 * agent-tool surface has its own named type the way `TrMemorySaveResult` does
 * for the write path, and so a future divergence (an agent-only field) does
 * not require touching every call site.
 */
export type TrCurationResult = CurationCliResult;

/**
 * curate() runs one curation op end-to-end against the paired vault. Binds
 * `runCurationOp` to the real PinOpDeps (built from module singletons in
 * index.ts's `buildRecallDeps`) and returns the truthful CurationCliResult.
 * NEVER throws on op failure — surfaces `{ ok:false, error }` so the tool
 * handler can relay it cleanly (mirrors `runCurationOp`'s own no-throw contract).
 */
export interface TrCurationFn {
  (input: TrCurationInput): Promise<TrCurationResult>;
}

// ---------------------------------------------------------------------------
// buildNativeCurate — the fail-soft curate factory (mirrors buildNativeStore).
// ---------------------------------------------------------------------------

/**
 * The dependencies the curate closure needs, each closing over index.ts's live
 * module state so hot-reload pairing is honored at call time (same shape as
 * `NativeStoreCtx`):
 * - `ensureInit`  — resolve the paired-account context (may throw on failure);
 * - `isPaired`    — read the current precondition (encryptionKey/authKeyHex/
 *                   subgraphOwner present AND not needsSetup), evaluated AFTER
 *                   ensureInit so a hot-reload-completed pair counts;
 * - `buildDeps`   — assemble the PinOpDeps from the now-resolved module
 *                   singletons (owner / fetch / decrypt / encrypt / submit /
 *                   generateIndices). Factory-not-object because the singletons
 *                   can change across hot-reloads; rebuilt per call, exactly as
 *                   the CLI rebuilds per invocation.
 */
export interface NativeCurateCtx {
  ensureInit: () => Promise<void>;
  isPaired: () => boolean;
  buildDeps: () => PinOpDeps;
}

/**
 * Import `runCurationOp` here (not at the top of the file) so the type-only
 * `PinOpDeps` / `CurationOp` / `CurationCliResult` imports above stay the only
 * static surface this module presents at load time — keeps the scanner-clean
 * posture identical to native-store.ts (one runtime import, the dispatch fn).
 */
import { runCurationOp } from './curation-op.js';

/**
 * Build the truthful curate fn. Return semantics (the whole point of #573 —
 * never report a curation op as done that did not happen):
 * - init throws            → `{ ok:false, error:'setup incomplete: …' }`
 * - not paired             → `{ ok:false, error:'not paired — …' }`
 * - runCurationOp ok:false → forwarded verbatim (agent relays the error)
 * - runCurationOp ok:true  → forwarded verbatim (carries new_fact_id + tx_hash)
 *
 * `runCurationOp` already catches its own op failures and returns
 * `{ ok:false, error }`, so the outer try/catch here only guards the
 * dep-assembly / init path (mirrors native-store.ts's two-stage guard).
 *
 * `confirmOpts` is deliberately omitted: the production path lets the
 * execute* functions apply their default subgraph-read polling, the same way
 * the CLI's `cmdCuration` calls `runCurationOp(args, deps)` with no third arg.
 * (Tests drive the fast path by stubbing the deps directly.)
 */
export function buildNativeCurate(ctx: NativeCurateCtx): TrCurationFn {
  return async (input: TrCurationInput): Promise<TrCurationResult> => {
    try {
      await ctx.ensureInit();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return { ok: false, op: input.op, fact_id: input.factId, error: `setup incomplete: ${msg}` };
    }

    if (!ctx.isPaired()) {
      return {
        ok: false,
        op: input.op,
        fact_id: input.factId,
        error: 'not paired — complete TotalReclaw setup first',
      };
    }

    const deps = ctx.buildDeps();
    return runCurationOp(
      {
        op: input.op,
        factId: input.factId,
        ...(input.reason !== undefined ? { reason: input.reason } : {}),
        ...(input.newType !== undefined ? { newType: input.newType } : {}),
        ...(input.newScope !== undefined ? { newScope: input.newScope } : {}),
      },
      deps,
    );
  };
}
