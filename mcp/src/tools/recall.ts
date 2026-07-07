import { TotalReclaw, RerankedResult } from '@totalreclaw/client';
import type { ToolContext } from './types.js';
import { RECALL_TOOL_DESCRIPTION } from '../prompts.js';
import { readBlobUnified } from '../claims-helper.js';

// eslint-disable-next-line @typescript-eslint/no-var-requires
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) _wasm = require('@totalreclaw/core') as typeof import('@totalreclaw/core');
  return _wasm!;
}

export interface RecallInput {
  query: string;
  k?: number;
  min_importance?: number;
  include_decay?: boolean;
}

export interface RecallOutput {
  memories: Array<{
    fact_id: string;
    fact_text: string;
    score: number;
    importance: number;
    age_days: number;
    decay_score: number;
  }>;
  latency_ms: number;
}

export const recallToolDefinition = {
  name: 'totalreclaw_recall',
  description: RECALL_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      query: {
        type: 'string',
        description: 'The search query',
      },
      k: {
        type: 'number',
        default: 8,
        description: 'Number of results to return',
      },
      min_importance: {
        type: 'number',
        minimum: 1,
        maximum: 10,
        description: 'Filter by minimum importance',
      },
      include_decay: {
        type: 'boolean',
        default: true,
        description: 'Apply decay scoring',
      },
    },
    required: ['query'],
  },
  annotations: {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
  },
};

export async function handleRecall(
  ctx: ToolContext,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const client = ctx.client as TotalReclaw;
  const input = args as RecallInput;
  const startTime = Date.now();

  if (!input.query || typeof input.query !== 'string' || input.query.trim().length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          memories: [],
          latency_ms: 0,
          error: 'Invalid input: query is required and must be a non-empty string',
        }),
      }],
    };
  }

  try {
    let k = input.k ?? 8;
    if (k < 1) k = 8;
    if (k > 50) k = 50;

    const results = await client.recall(input.query.trim(), k);

    let filtered = results;
    if (input.min_importance !== undefined) {
      const minImp = input.min_importance / 10;
      filtered = results.filter((r: RerankedResult) =>
        (r.fact.metadata.importance ?? 0.5) >= minImp
      );
    }

    // Category + source + scope via the canonical `readBlobUnified` decoder,
    // the same one the managed (subgraph) read path uses in
    // `handleRecallSubgraph`. Keeping both paths on one decoder means a v1
    // claim gets an identical short-key `type` label ('pref', 'rule', ...) and
    // provenance regardless of storage mode.
    const parsedMap = filtered.map((r: RerankedResult) => {
      const doc = readBlobUnified(r.fact.text);
      return {
        r,
        category: doc.category ?? 'fact',
        source: doc.v1?.source,
        scope: doc.v1?.scope,
      };
    });

    // Source-weights off (recall alignment 2026-06-08; tie-or-worse on shipped path).
    // Preserve source/source_weight fields in output for observability but do not
    // multiply into the ranking score.
    const core = getWasm();
    const weighted = parsedMap
      .map(({ r, category, source, scope }) => {
        const sourceWeight = source ? core.sourceWeight(source) : core.legacyClaimFallbackWeight();
        return { r, category, source, scope, weightedScore: r.score, sourceWeight };
      })
      .sort((a, b) => b.weightedScore - a.weightedScore);

    const memories = weighted.map(({ r, category, source, scope, weightedScore, sourceWeight }) => {
      const ageMs = Date.now() - r.fact.createdAt.getTime();
      const ageDays = Math.floor(ageMs / (1000 * 60 * 60 * 24));
      const createdAtUnix = BigInt(Math.floor(r.fact.createdAt.getTime() / 1000));
      return {
        fact_id: r.fact.id,
        fact_text: r.fact.text,
        type: category,
        source,
        scope,
        score: weightedScore,
        base_score: r.score,
        source_weight: sourceWeight,
        importance: Math.round((r.fact.metadata.importance ?? 0.5) * 10),
        age_days: ageDays,
        decay_score: r.decayAdjustedScore,
        date: core.formatMemoryDate(createdAtUnix),
      };
    });

    const result: RecallOutput = {
      memories,
      latency_ms: Date.now() - startTime,
    };

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(result),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error occurred';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          memories: [],
          latency_ms: Date.now() - startTime,
          error: `Failed to recall memories: ${message}`,
        }),
      }],
    };
  }
}
