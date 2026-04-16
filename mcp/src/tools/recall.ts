import { TotalReclaw, RerankedResult } from '@totalreclaw/client';
import { RECALL_TOOL_DESCRIPTION } from '../prompts.js';

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

    const memories = filtered.map((r: RerankedResult) => {
      const ageMs = Date.now() - r.fact.createdAt.getTime();
      const ageDays = Math.floor(ageMs / (1000 * 60 * 60 * 24));

      // Best-effort category extraction: if the decrypted text is a claim
      // JSON blob (e.g. {"t":"...","c":"rule",...}), extract the category.
      let type = 'fact';
      try {
        const parsed = JSON.parse(r.fact.text) as Record<string, unknown>;
        if (typeof parsed.c === 'string') type = parsed.c;
      } catch {
        // Not a JSON blob — default to 'fact'
      }

      return {
        fact_id: r.fact.id,
        fact_text: r.fact.text,
        type,
        score: r.score,
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
