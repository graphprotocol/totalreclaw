/**
 * TotalReclaw MCP Server - Session Debrief Tool (Crystal-shaped, am-1)
 *
 * Stores one Crystal-shaped session summary per session. The Crystal replaces
 * 5x free-form debrief items with a structured v1 summary that carries
 * metadata.subtype="session_crystal" for filtered recall.
 *
 * For MCP: the host agent (Claude, Cursor, etc.) provides the crystal directly
 * — the MCP server does NOT make its own LLM calls.
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

/** Crystal-shaped session summary provided by the host agent. */
export interface CrystalInput {
  narrative: string;
  key_outcomes?: string[];
  files_affected?: string[];
  open_threads?: string[];
  lessons?: string[];
  importance?: number;
  session_id?: string;
  source_message_ids?: string[];
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
// Parser (legacy free-form path — kept for backward compat)
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

/** Parse and validate a Crystal input from the tool args. */
function parseCrystalInput(raw: unknown): CrystalInput | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const d = raw as Record<string, unknown>;
  const narrative = typeof d.narrative === 'string' ? d.narrative.trim() : '';
  if (narrative.length < 10) return null;

  const strList = (key: string): string[] => {
    const v = d[key];
    if (!Array.isArray(v)) return [];
    return v.map((x) => String(x).trim()).filter((x) => x.length > 0).slice(0, 10);
  };

  let importance = typeof d.importance === 'number' ? d.importance : 8;
  importance = Math.max(1, Math.min(10, Math.round(importance)));

  return {
    narrative: narrative.slice(0, 512),
    key_outcomes: strList('key_outcomes'),
    files_affected: strList('files_affected'),
    open_threads: strList('open_threads'),
    lessons: strList('lessons'),
    importance,
    session_id: typeof d.session_id === 'string' ? d.session_id : undefined,
    source_message_ids: strList('source_message_ids'),
  };
}

// ---------------------------------------------------------------------------
// Tool Definition
// ---------------------------------------------------------------------------

export const debriefToolDefinition = {
  name: 'totalreclaw_debrief',
  description:
    'Crystal session summary — one structured summary per session.\n' +
    '\nINVOKE WHEN USER SAYS:\n' +
    '- "goodbye" / "bye" / "thanks"\n' +
    '- "that\'s all" / "I\'m done"\n' +
    '- "wrapping up" / after long debug/plan session\n' +
    '- before detected compaction/reset\n' +
    '\nWHEN NOT TO USE:\n' +
    '- casual chat / Q&A — adds noise\n' +
    '- no prior totalreclaw_remember — no context\n' +
    '- unsure → skip; debriefs rare\n' +
    '\nProvide a Crystal object. narrative is embedded; all other fields go in metadata.',
  inputSchema: {
    type: 'object',
    oneOf: [
      {
        description: 'Crystal-shaped debrief (preferred, am-1)',
        properties: {
          crystal: {
            type: 'object',
            description: 'Structured Crystal session summary',
            properties: {
              narrative: { type: 'string', description: '1-2 sentence session summary (will be embedded)' },
              key_outcomes: { type: 'array', items: { type: 'string' }, description: 'Decisions made, bugs fixed, conclusions reached' },
              files_affected: { type: 'array', items: { type: 'string' }, description: 'File paths worked on (coding sessions)' },
              open_threads: { type: 'array', items: { type: 'string' }, description: 'Unfinished items needing follow-up' },
              lessons: { type: 'array', items: { type: 'string' }, description: 'Patterns, gotchas, insights worth remembering' },
              importance: { type: 'number', description: 'Importance 1-10 (default 8)' },
              session_id: { type: 'string', description: 'Optional session identifier' },
            },
            required: ['narrative'],
          },
        },
        required: ['crystal'],
      },
      {
        description: 'Legacy free-form debrief items (backward compat)',
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
    ],
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
 * Accepts Crystal-shaped input (preferred) or legacy facts array (backward compat).
 */
export async function handleDebrief(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;

  // Crystal path (preferred)
  if (input?.crystal !== undefined) {
    const crystal = parseCrystalInput(input.crystal);
    if (!crystal) {
      return {
        content: [{ type: 'text', text: JSON.stringify({ success: false, error: 'Invalid crystal: narrative (10+ chars) is required' }) }],
      };
    }

    try {
      const metadata: FactMetadata = {
        importance: crystal.importance! / 10,
        source: 'mcp_debrief',
        tags: [
          'subtype:session_crystal',
          'source:derived',
          ...(crystal.files_affected && crystal.files_affected.length > 0 ? ['has:files_affected'] : []),
        ],
      };
      const factId = await client.remember(crystal.narrative, metadata);
      return {
        content: [{ type: 'text', text: JSON.stringify({ success: true, stored: 1, fact_id: factId, crystal: true }) }],
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      return {
        content: [{ type: 'text', text: JSON.stringify({ success: false, error: message }) }],
      };
    }
  }

  // Legacy facts array path
  const factsInput = input?.facts;
  if (!Array.isArray(factsInput) || factsInput.length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({ success: false, error: 'Provide either "crystal" (preferred) or "facts" array' }),
      }],
    };
  }

  const validated = parseDebriefResponse(JSON.stringify(factsInput));
  if (validated.length === 0) {
    return {
      content: [{ type: 'text', text: JSON.stringify({ success: true, stored: 0, message: 'No valid debrief items (filtered)' }) }],
    };
  }

  let stored = 0;
  const results: Array<{ success: boolean; fact_id: string }> = [];
  for (const item of validated) {
    try {
      const metadata: FactMetadata = { importance: item.importance / 10, source: 'mcp_debrief', tags: [item.type] };
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
    content: [{ type: 'text', text: JSON.stringify({ success: stored > 0, stored, total: validated.length, results }) }],
  };
}
