// scanner-sim: allow
/**
 * The `memory_save` store closure — extracted from index.ts's `buildRecallDeps`
 * into a pure factory so the TRUTHFULNESS contract (#499) is directly unit
 * testable, not just asserted against a mock in the tool-wrapper tests
 * (#499 review, Finding 2). index.ts injects the three real dependencies
 * (init / paired-check / store) that close over its module singletons; tests
 * inject fakes and drive every branch.
 *
 * The domain defaulting (type→'claim', importance→8, source→'user',
 * action→'ADD', confidence→1.0) lives here — the historic
 * totalreclaw_remember → storeExtractedFacts wiring (regression-guarded by
 * extraction/store-dedup-wiring.test.ts scenario 6). importance 8 is the
 * explicit-remember weight (above auto-extraction's 6-7 so an explicit
 * remember wins a store-time collision).
 */
import type { ExtractedFact } from '../extraction/extractor.js';
import type { TrMemorySaveFn, TrMemorySaveInput, TrMemorySaveResult } from './memory-runtime.js';

/**
 * The three dependencies the store closure needs, each closing over index.ts's
 * live module state so hot-reload pairing is honored at call time:
 * - `ensureInit`  — resolve the paired-account context (may throw on failure);
 * - `isPaired`    — read the current precondition (encryptionKey/dedupKey/
 *                   authKeyHex/userId/apiClient present AND not needsSetup),
 *                   evaluated AFTER ensureInit so a hot-reload-completed pair
 *                   counts;
 * - `storeFacts`  — the real storeExtractedFacts pipeline; returns the count
 *                   of NEWLY persisted facts (0 on dedup/skip), throws on an
 *                   on-chain/store failure.
 */
export interface NativeStoreCtx {
  ensureInit: () => Promise<void>;
  isPaired: () => boolean;
  storeFacts: (facts: ExtractedFact[]) => Promise<number>;
}

/**
 * Build the truthful `memory_save` store fn. Return semantics (the whole point
 * of #499 — never report success on a non-persist):
 * - init throws            → `{ ok:false, stored:0, error:'setup incomplete: …' }`
 * - not paired             → `{ ok:false, stored:0, error:'not paired — …' }`
 * - storeFacts throws      → `{ ok:false, stored:0, error:<msg> }`
 * - storeFacts returns 0   → `{ ok:true,  stored:0 }` (dedup/skip — agent says
 *                            "duplicate, not stored", never "Saved")
 * - storeFacts returns >=1 → `{ ok:true,  stored:n }`
 */
export function buildNativeStore(ctx: NativeStoreCtx): TrMemorySaveFn {
  return async (input: TrMemorySaveInput): Promise<TrMemorySaveResult> => {
    try {
      await ctx.ensureInit();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return { ok: false, stored: 0, error: `setup incomplete: ${msg}` };
    }

    if (!ctx.isPaired()) {
      return { ok: false, stored: 0, error: 'not paired — complete TotalReclaw setup first' };
    }

    const fact: ExtractedFact = {
      text: input.text,
      type: input.type ?? 'claim',
      importance: input.importance ?? 8,
      action: 'ADD',
      confidence: 1.0,
      source: 'user',
      ...(input.entities ? { entities: input.entities } : {}),
      ...(input.scope ? { scope: input.scope } : {}),
      ...(input.reasoning ? { reasoning: input.reasoning } : {}),
    };

    try {
      const stored = await ctx.storeFacts([fact]);
      return { ok: true, stored };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return { ok: false, stored: 0, error: msg };
    }
  };
}
