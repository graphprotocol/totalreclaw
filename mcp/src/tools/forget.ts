import { TotalReclaw } from '@totalreclaw/client';
import { FORGET_TOOL_DESCRIPTION } from '../prompts.js';
import { VALID_MEMORY_SCOPES } from '../v1-types.js';

export interface ForgetIntput {
  fact_id?: string;
  query?: string;
  /**
   * v1 scope hint for query-based forgets. Not yet enforced server-side
   * (the self-hosted recall path doesn't support scope-filtered blind
   * indices). The LLM should still supply it so future versions can
   * restrict deletion to one life-domain — the param is accepted in the
   * schema today to avoid a breaking change later.
   */
  scope?: typeof VALID_MEMORY_SCOPES[number];
}

export interface ForgetOutput {
  deleted_count: number;
  fact_ids: string[];
}

export const forgetToolDefinition = {
  name: 'totalreclaw_forget',
  description: FORGET_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: {
        type: 'string',
        description:
          'The ID of a specific memory to forget (from a prior totalreclaw_recall result). Preferred over `query` — avoids over-deletion.',
      },
      query: {
        type: 'string',
        description:
          'Semantic search string — every matching memory (up to 50) is tombstoned. Use sparingly; confirm with the user first.',
      },
      scope: {
        type: 'string',
        enum: [...VALID_MEMORY_SCOPES],
        description:
          'Optional v1 scope hint for query-based forgets. Not yet enforced server-side, but supplying it lets future versions restrict deletion to one life-domain (e.g. `scope="health"` to forget only diet-related memories). No effect when `fact_id` is given.',
      },
    },
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: true,
    idempotentHint: true,
  },
};

export async function handleForget(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as ForgetIntput;

  if (!input.fact_id && !input.query) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          deleted_count: 0,
          fact_ids: [],
          error: 'Either fact_id or query must be provided',
        }),
      }],
    };
  }

  try {
    const deletedIds: string[] = [];

    if (input.fact_id) {
      await client.forget(input.fact_id);
      deletedIds.push(input.fact_id);
    } else if (input.query) {
      const results = await client.recall(input.query, 50);

      for (const r of results) {
        try {
          await client.forget(r.fact.id);
          deletedIds.push(r.fact.id);
        } catch {
          // Skip failures
        }
      }
    }

    const result: ForgetOutput = {
      deleted_count: deletedIds.length,
      fact_ids: deletedIds,
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
          deleted_count: 0,
          fact_ids: [],
          error: `Failed to forget memories: ${message}`,
        }),
      }],
    };
  }
}
