import { TotalReclaw } from '@totalreclaw/client';
import { FORGET_TOOL_DESCRIPTION } from '../prompts.js';

export interface ForgetIntput {
  fact_id?: string;
  query?: string;
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
        description: 'The ID of the fact to forget',
      },
      query: {
        type: 'string',
        description: 'Or forget by semantic query',
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
