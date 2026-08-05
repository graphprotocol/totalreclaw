/**
 * curation-op — the shared pin/unpin/retype/set_scope dispatch + normalizer.
 *
 * WHY THIS FILE EXISTS (#573):
 *   Curation used to live as CLI subcommands only (`tr pin` / `tr retype` /
 *   `tr set_scope`, #563). The pure operations (`executePinOperation` /
 *   `executeRetype` / `executeSetScope`) already lived in memory/pin.ts +
 *   memory/retype-setscope.ts; the CLI added a thin normalizer (`runCurationOp`)
 *   that dispatches to them and flattens their result into one shape.
 *
 *   #573 adds an in-process AGENT TOOL surface for curation (the plugin was the
 *   only client without one — an agent told to "pin that" confabulated a store
 *   and reported a successful pin). The tool needs the SAME dispatch +
 *   normalization the CLI has. Importing `runCurationOp` from cli/tr-cli.ts
 *   would pull the entire CLI bin graph (pairing, onboarding, subgraph-store,
 *   …) into the agent-tool path, so the normalizer is extracted here — a pure
 *   module whose only imports are the execute* operations + their shared types.
 *
 *   Both call sites now import `runCurationOp` from here:
 *     - cli/tr-cli.ts   — the `tr pin|unpin|retype|set_scope` subcommands
 *     - memory/tools.ts — the `memory_pin|unpin|retype|set_scope` agent tools
 *       (via the captured `curate` closure wired in index.ts)
 *
 * SCANNER-CLEAN HARD CONTRACT (env=N net=N):
 *   This file is pure orchestration. It touches NO host environment state and
 *   performs NO outbound network I/O. All transport/crypto work lives in the
 *   injected `deps` object (the execute* functions take a `PinOpDeps` whose
 *   closures the caller binds to the real pipeline). `npm run check-scanner`
 *   must remain 0 flags.
 */

import {
  executePinOperation,
  type PinOpDeps,
} from './pin.js';
import {
  executeRetype,
  executeSetScope,
} from './retype-setscope.js';
import type { MemoryType, MemoryScope } from '../extraction/extractor.js';
import type { ConfirmIndexedOptions } from '../subgraph/confirm-indexed.js';

/** The four curation operations exposed as agent tools + CLI subcommands. */
export type CurationOp = 'pin' | 'unpin' | 'retype' | 'set_scope';

/**
 * A parsed, validated curation request — the shape both `parseCurationCliArgs`
 * (CLI) and the tool handlers (agent tools) produce before calling
 * `runCurationOp`. `reason` is pin-only; `newType` is retype-only;
 * `newScope` is set_scope-only.
 */
export interface CurationParsedArgs {
  op: CurationOp;
  factId: string;
  reason?: string;
  newType?: MemoryType;
  newScope?: MemoryScope;
}

/**
 * The normalized curation result — one shape for all four ops, regardless of
 * which execute* function produced it. Carries the truthful signals the caller
 * (CLI printer / agent tool) relays: `new_fact_id` + `tx_hash` on a real
 * supersession, `idempotent` on a no-op, `partial` when the chain write landed
 * but the indexer hasn't caught up, `error` on failure.
 */
export interface CurationCliResult {
  ok: boolean;
  op: CurationOp;
  fact_id: string;
  new_fact_id?: string;
  previous_status?: string;
  new_status?: string;
  previous_type?: string;
  new_type?: string;
  previous_scope?: string;
  new_scope?: string;
  idempotent?: boolean;
  tx_hash?: string;
  partial?: boolean;
  reason?: string;
  error?: string;
}

/**
 * Run a curation op end-to-end against injected deps. Dispatches to the pure
 * execute* fn and normalizes its result into a `CurationCliResult`. NEVER
 * throws on op failure — surfaces `{ ok: false, error }` so the caller (CLI
 * `die()` / agent tool truthful result) can relay it cleanly.
 *
 * `deps` is `PinOpDeps`, which is structurally compatible with
 * `RetypeSetScopeDeps` (same owner / crypto / transport / generateIndices
 * surface) — pin.ts and retype-setscope.ts both consume the identical shape, so
 * one injected object serves all four ops.
 *
 * `confirmOpts` threads the read-after-write subgraph poll (the production path
 * omits it so the execute* functions apply their default polling; tests stub
 * it fast).
 */
export async function runCurationOp(
  args: CurationParsedArgs,
  deps: PinOpDeps,
  confirmOpts?: ConfirmIndexedOptions,
): Promise<CurationCliResult> {
  const { op, factId: fact_id } = args;
  try {
    if (op === 'pin' || op === 'unpin') {
      const targetStatus = op === 'pin' ? 'pinned' : 'active';
      const reason = op === 'pin' ? args.reason : undefined;
      const r = await executePinOperation(fact_id, targetStatus, deps, reason, confirmOpts);
      return {
        ok: r.success,
        op,
        fact_id,
        new_fact_id: r.new_fact_id,
        previous_status: r.previous_status,
        new_status: r.new_status,
        idempotent: r.idempotent,
        tx_hash: r.tx_hash,
        partial: r.partial,
        reason: r.reason,
        error: r.error,
      };
    }

    if (op === 'retype') {
      const r = await executeRetype(fact_id, args.newType!, deps, confirmOpts);
      return {
        ok: r.success,
        op,
        fact_id,
        new_fact_id: r.new_fact_id,
        previous_type: r.previous_type,
        new_type: r.new_type,
        tx_hash: r.tx_hash,
        partial: r.partial,
        error: r.error,
      };
    }

    // set_scope
    const r = await executeSetScope(fact_id, args.newScope!, deps, confirmOpts);
    return {
      ok: r.success,
      op,
      fact_id,
      new_fact_id: r.new_fact_id,
      previous_scope: r.previous_scope,
      new_scope: r.new_scope,
      tx_hash: r.tx_hash,
      partial: r.partial,
      error: r.error,
    };
  } catch (err) {
    return {
      ok: false,
      op,
      fact_id,
      error: `${op} failed: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}
