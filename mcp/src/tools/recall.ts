import { TotalReclaw, RerankedResult } from '@totalreclaw/client';
import { RECALL_TOOL_DESCRIPTION } from '../prompts.js';
import { MEMORY_CLAIM_V1_SCHEMA_VERSION } from '../v1-types.js';

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
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
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

    // Best-effort category + source extraction. v1 blobs carry `type` +
    // `source` directly; v0 canonical blobs use `c` (short-key); plugin-legacy
    // blobs have `metadata.type`. We surface whatever we find so callers can
    // filter or display it.
    const parsedMap = filtered.map((r: RerankedResult) => {
      let category = 'fact';
      let source: string | undefined;
      let scope: string | undefined;
      try {
        const parsed = JSON.parse(r.fact.text) as Record<string, unknown>;
        const v1Types = new Set<string>([
          'claim',
          'preference',
          'directive',
          'commitment',
          'episode',
          'summary',
        ]);
        const isV1 =
          typeof parsed.text === 'string' &&
          typeof parsed.type === 'string' &&
          v1Types.has(String(parsed.type)) &&
          (typeof parsed.schema_version !== 'string' ||
            parsed.schema_version === MEMORY_CLAIM_V1_SCHEMA_VERSION);
        if (isV1) {
          category = String(parsed.type);
          if (typeof parsed.source === 'string') source = parsed.source;
          if (typeof parsed.scope === 'string') scope = parsed.scope;
        } else if (typeof parsed.c === 'string') {
          category = parsed.c;
        } else if (typeof parsed.metadata === 'object' && parsed.metadata !== null) {
          const meta = parsed.metadata as Record<string, unknown>;
          if (typeof meta.type === 'string') category = meta.type;
        }
      } catch {
        // Not a JSON blob — default to 'fact'
      }
      return { r, category, source, scope };
    });

    // Retrieval v2 Tier 1: multiply final score by source weight from core.
    const core = getWasm();
    const weighted = parsedMap
      .map(({ r, category, source, scope }) => {
        const w = source ? core.sourceWeight(source) : core.legacyClaimFallbackWeight();
        return { r, category, source, scope, weightedScore: r.score * w, sourceWeight: w };
      })
      .sort((a, b) => b.weightedScore - a.weightedScore);

    const memories = weighted.map(({ r, category, source, scope, weightedScore, sourceWeight }) => {
      const ageMs = Date.now() - r.fact.createdAt.getTime();
      const ageDays = Math.floor(ageMs / (1000 * 60 * 60 * 24));
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
