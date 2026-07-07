// ---------------------------------------------------------------------------
// Importance filter for auto-extraction. Extracted from index.ts.
// Pure over its inputs (threshold derives from CONFIG); no plugin session state.
// ---------------------------------------------------------------------------

import { CONFIG } from '../config.js';
import type { ExtractedFact } from './extractor.js';
import type { OpenClawPluginApi } from '../runtime/types.js';

const MIN_IMPORTANCE_THRESHOLD = CONFIG.minImportance;

/**
 * Filter extracted facts by importance threshold.
 * Facts with importance < MIN_IMPORTANCE_THRESHOLD are dropped.
 * Facts with missing/undefined importance are treated as importance=5 (kept).
 */
export function filterByImportance(
  facts: ExtractedFact[],
  logger: OpenClawPluginApi['logger'],
): { kept: ExtractedFact[]; dropped: number } {
  const kept: ExtractedFact[] = [];
  let dropped = 0;

  for (const fact of facts) {
    const importance = fact.importance ?? 5;
    if (importance >= MIN_IMPORTANCE_THRESHOLD) {
      kept.push(fact);
    } else {
      dropped++;
    }
  }

  // Phase 2.2.5: always log the filter outcome so the agent_end path can
  // distinguish "LLM returned 0 facts" from "LLM returned N facts all dropped
  // below threshold" from "LLM returned N facts, all kept". Prior to 2.2.5
  // this only logged on drops, which made empty-input invisible.
  if (facts.length === 0) {
    logger.info('Importance filter: input=0 (nothing to filter)');
  } else if (dropped > 0) {
    logger.info(
      `Importance filter: dropped ${dropped}/${facts.length} facts below threshold ${MIN_IMPORTANCE_THRESHOLD}`,
    );
  } else {
    logger.info(
      `Importance filter: kept all ${facts.length} facts (threshold ${MIN_IMPORTANCE_THRESHOLD})`,
    );
  }

  return { kept, dropped };
}
