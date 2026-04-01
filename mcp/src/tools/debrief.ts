/**
 * TotalReclaw MCP Server - Session Debrief Tool
 *
 * Stores end-of-conversation summaries that capture broader context,
 * outcomes, and conclusions that individual memory storage may have missed.
 *
 * For MCP: the host agent (Claude, Cursor, etc.) provides the facts array
 * directly — the MCP server does NOT make its own LLM calls.
 */

import { TotalReclaw, FactMetadata } from '@totalreclaw/client';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DebriefItem {
  text: string;
  type: 'summary' | 'context';
  importance: number;
}

// ---------------------------------------------------------------------------
// Canonical Debrief Prompt (exported for use by host agents and other clients)
// ---------------------------------------------------------------------------

export const DEBRIEF_SYSTEM_PROMPT = `You are reviewing a conversation that just ended. The following facts were
already extracted and stored during this conversation:

{already_stored_facts}

Your job is to capture what turn-by-turn extraction MISSED. Focus on:

1. **Broader context** — What was the conversation about overall? What project,
   problem, or topic tied the discussion together?
2. **Outcomes & conclusions** — What was decided, agreed upon, or resolved?
3. **What was attempted** — What approaches were tried? What worked, what didn't, and why?
4. **Relationships** — How do topics discussed relate to each other or to things
   from previous conversations?
5. **Open threads** — What was left unfinished or needs follow-up?

Do NOT repeat facts already stored. Only add genuinely new information that provides
broader context a future conversation would benefit from.

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "summary|context", "importance": N}]

- Use type "summary" for conclusions, outcomes, and decisions-of-the-session
- Use type "context" for broader project context, open threads, and what-was-tried
- Importance 7-8 for most debrief items (they are high-value by definition)
- Maximum 5 items (debriefs should be concise, not exhaustive)
- Each item should be 1-3 sentences, self-contained

If the conversation was too short or trivial to warrant a debrief, return: []`;

// ---------------------------------------------------------------------------
// Parser
// ---------------------------------------------------------------------------

/**
 * Parse and validate a debrief response (JSON array of debrief items).
 *
 * - Strips markdown code fences if present
 * - Validates each item: text (string, 5+ chars, max 512), type, importance
 * - Filters items with importance < 6
 * - Defaults type to "context" if invalid, importance to 7 if missing
 * - Caps at 5 items max
 */
export function parseDebriefResponse(response: string): DebriefItem[] {
  let cleaned = response.trim();
  if (cleaned.startsWith('```')) {
    cleaned = cleaned.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
  }

  try {
    const parsed = JSON.parse(cleaned);
    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter(
        (item: unknown) =>
          item &&
          typeof item === 'object' &&
          typeof (item as DebriefItem).text === 'string' &&
          (item as DebriefItem).text.length >= 5,
      )
      .map((item: unknown) => {
        const d = item as Record<string, unknown>;
        const type = d.type === 'summary' ? 'summary' : 'context';
        const importance =
          typeof d.importance === 'number'
            ? Math.max(1, Math.min(10, d.importance))
            : 7;
        return {
          text: String(d.text).slice(0, 512),
          type,
          importance,
        } as DebriefItem;
      })
      .filter((d) => d.importance >= 6)
      .slice(0, 5);
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Tool Definition
// ---------------------------------------------------------------------------

export const debriefToolDefinition = {
  name: 'totalreclaw_debrief',
  description:
    'Store a session debrief — broader context, outcomes, and conclusions that ' +
    'individual memory storage may have missed. Call this at the END of substantive ' +
    'conversations (not casual chat). Pass the key takeaways as facts.',
  inputSchema: {
    type: 'object',
    properties: {
      facts: {
        type: 'array',
        description: 'Array of debrief items to store',
        items: {
          type: 'object',
          properties: {
            text: { type: 'string', description: 'The debrief summary text (1-3 sentences)' },
            type: { type: 'string', enum: ['summary', 'context'], description: 'summary=conclusion/outcome, context=broader project context' },
            importance: { type: 'number', description: 'Importance 1-10 (typically 7-8 for debriefs)' },
          },
          required: ['text'],
        },
      },
    },
    required: ['facts'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/**
 * Handle debrief in HTTP/self-hosted mode: validate, store via TotalReclaw client.
 */
export async function handleDebrief(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const factsInput = input?.facts;

  if (!Array.isArray(factsInput) || factsInput.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Invalid input: "facts" array is required and must not be empty',
        }),
      }],
    };
  }

  // Validate through the parser
  const validated = parseDebriefResponse(JSON.stringify(factsInput));

  if (validated.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: true,
          stored: 0,
          message: 'No valid debrief items to store (filtered by validation)',
        }),
      }],
    };
  }

  let stored = 0;
  const results: Array<{ success: boolean; fact_id: string }> = [];

  for (const item of validated) {
    try {
      const metadata: FactMetadata = {
        importance: item.importance / 10,
        source: 'mcp_debrief',
        tags: [item.type],
      };

      const factId = await client.remember(item.text, metadata);
      results.push({ success: true, fact_id: factId });
      stored++;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      results.push({ success: false, fact_id: '' });
      console.error(`Failed to store debrief item: ${message}`);
    }
  }

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        success: stored > 0,
        stored,
        total: validated.length,
        results,
      }),
    }],
  };
}

