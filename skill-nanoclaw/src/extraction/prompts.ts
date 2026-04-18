/**
 * TotalReclaw NanoClaw Skill — LLM Prompts for Fact Extraction (v1 taxonomy).
 *
 * As of NanoClaw 3.0.0, Memory Taxonomy v1 is the only extraction path.
 * The 6 canonical types (claim, preference, directive, commitment, episode,
 * summary) replace the legacy v0 8-type list. Legacy v0 tokens are still
 * accepted on the read/parse side via ``V0_TO_V1_TYPE`` so pre-v1 vault
 * entries still round-trip.
 *
 * Aligned with the canonical extraction prompt from
 * ``skill/plugin/extractor.ts`` (plugin v3.0.0).
 */

export type ExtractionAction = 'ADD' | 'UPDATE' | 'DELETE' | 'NOOP';

// ---------------------------------------------------------------------------
// Memory Taxonomy v1 — 6 canonical memory types. Single source of truth.
// Keep in sync with: skill/plugin/extractor.ts, mcp/src/v1-types.ts,
// python/src/totalreclaw/agent/extraction.py, rust/totalreclaw-core/src/claims.rs.
// ---------------------------------------------------------------------------

export const VALID_MEMORY_TYPES = [
  'claim',
  'preference',
  'directive',
  'commitment',
  'episode',
  'summary',
] as const;

export type MemoryType = (typeof VALID_MEMORY_TYPES)[number];

/** @deprecated Use {@link VALID_MEMORY_TYPES} directly. Back-compat alias only. */
export const VALID_MEMORY_TYPES_V1: readonly MemoryType[] = VALID_MEMORY_TYPES;
/** @deprecated Use {@link MemoryType}. Back-compat alias only. */
export type MemoryTypeV1 = MemoryType;

/** Runtime type guard for the 6 v1 types. */
export function isValidMemoryType(value: unknown): value is MemoryType {
  return typeof value === 'string' && (VALID_MEMORY_TYPES as readonly string[]).includes(value);
}

/**
 * Legacy v0 memory types — retained so pre-v1 vault entries still decode,
 * and so v0 LLM outputs are coerced rather than dropped.
 */
export const LEGACY_V0_MEMORY_TYPES = [
  'fact',
  'preference',
  'decision',
  'episodic',
  'goal',
  'context',
  'summary',
  'rule',
] as const;
export type MemoryTypeV0 = (typeof LEGACY_V0_MEMORY_TYPES)[number];

/** Back-compat alias used by downstream hooks; `FactType` was the v0 name. */
export type FactType = MemoryType | MemoryTypeV0;

/** Legacy v0 → v1 type mapping. Mirrors plugin / Python / core. */
export const V0_TO_V1_TYPE: Record<MemoryTypeV0, MemoryType> = {
  fact: 'claim',
  preference: 'preference',
  decision: 'claim',
  episodic: 'episode',
  goal: 'commitment',
  context: 'claim',
  summary: 'summary',
  rule: 'directive',
};

/** Normalise any incoming type token (v1 or legacy v0) to a v1 type. */
export function normalizeToV1Type(raw: unknown): MemoryType {
  const token = String(raw ?? '').toLowerCase();
  if (isValidMemoryType(token)) return token;
  if ((LEGACY_V0_MEMORY_TYPES as readonly string[]).includes(token)) {
    return V0_TO_V1_TYPE[token as MemoryTypeV0];
  }
  return 'claim';
}

export type MemorySource =
  | 'user'
  | 'user-inferred'
  | 'assistant'
  | 'external'
  | 'derived';

export const VALID_MEMORY_SOURCES: readonly MemorySource[] = [
  'user',
  'user-inferred',
  'assistant',
  'external',
  'derived',
];

export type MemoryScope =
  | 'work'
  | 'personal'
  | 'health'
  | 'family'
  | 'creative'
  | 'finance'
  | 'misc'
  | 'unspecified';

export const VALID_MEMORY_SCOPES: readonly MemoryScope[] = [
  'work',
  'personal',
  'health',
  'family',
  'creative',
  'finance',
  'misc',
  'unspecified',
];

export type MemoryVolatility = 'stable' | 'updatable' | 'ephemeral';

export const VALID_MEMORY_VOLATILITIES: readonly MemoryVolatility[] = [
  'stable',
  'updatable',
  'ephemeral',
];

/**
 * Extracted fact carrying full v1 taxonomy fields. The write path (hooks)
 * defaults `source` to `'user-inferred'` when upstream omits it.
 */
export interface ExtractedFact {
  text: string;
  type: MemoryType;
  importance: number; // 1-10
  action: ExtractionAction;
  existingFactId?: string;
  source?: MemorySource;
  scope?: MemoryScope;
  reasoning?: string;
  volatility?: MemoryVolatility;
}

// ---------------------------------------------------------------------------
// v1 merged-topic extraction prompt
// ---------------------------------------------------------------------------

const BASE_SYSTEM_PROMPT = `You are a memory extraction engine using Memory Taxonomy v1. Work in TWO explicit phases within one response:

PHASE 1 — Topic identification.
Before extracting any fact, identify the 2-3 main topics the user was engaging with. Topics should be short phrases (2-5 words each). If the conversation has no clear user-focused topic, use an empty topics array.

PHASE 2 — Fact extraction anchored to those topics.
Extract valuable memories. Prefer facts that directly relate to the identified topics (importance 7-9 range). Tangential facts may still be extracted but score lower (6-7 range).

Rules:
1. Each memory = single self-contained piece of information
2. Focus on user-specific info useful in future conversations
3. Skip generic knowledge, greetings, small talk, ephemeral task coordination
4. Score importance 1-10 (6+ = worth storing)
5. Every memory MUST attribute a source (provenance critical)

Importance rubric (use FULL 1-10 range):
- 10: Critical, core identity, never-forget content
- 9: Affects many future decisions
- 8: High-value preference/decision/rule
- 7: Specific durable fact
- 6: Borderline
- 5 or below: NOT worth storing — drop

DO NOT cluster everything at 7-8-9.

═══════════════════════════════════════════════════════════════
TYPE (6 values)
═══════════════════════════════════════════════════════════════
- claim: factual assertion (absorbs fact/context/decision; decisions populate reasoning field)
- preference: likes/dislikes/tastes
- directive: imperative rule ("always X", "never Y")
- commitment: future intent ("will do X")
- episode: notable event
- summary: derived synthesis (source must be derived|assistant) — do NOT emit for turn-extraction

═══════════════════════════════════════════════════════════════
SOURCE (provenance, CRITICAL)
═══════════════════════════════════════════════════════════════
- user: user explicitly stated it (in [user]: turns)
- user-inferred: extractor inferred from user signals
- assistant: assistant authored content — DOWNGRADE unless user affirmed/quoted/used it
- external, derived: rare

IF fact substance appears ONLY in [assistant]: turns without user affirmation → source:assistant

═══════════════════════════════════════════════════════════════
SCOPE (life domain)
═══════════════════════════════════════════════════════════════
work | personal | health | family | creative | finance | misc | unspecified

═══════════════════════════════════════════════════════════════
REASONING (only for claims that are decisions)
═══════════════════════════════════════════════════════════════
For type=claim where the user expressed a decision-with-reasoning, populate "reasoning" with the WHY clause.

Actions (compare against existing memories if provided):
- ADD: New memory, no conflict with existing
- UPDATE: Refines or corrects an existing memory (provide existingFactId)
- DELETE: Contradicts an existing memory — the old one is now wrong (provide existingFactId)
- NOOP: Already captured or not worth storing

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT (no markdown, no code fences)
═══════════════════════════════════════════════════════════════
{
  "topics": ["topic 1", "topic 2"],
  "facts": [
    {
      "text": "...",
      "type": "claim|preference|directive|commitment|episode|summary",
      "source": "user|user-inferred|assistant",
      "scope": "work|personal|health|...",
      "importance": N,
      "action": "ADD|UPDATE|DELETE|NOOP",
      "reasoning": "...",      // optional, only for claim+decision
      "existingFactId": "..."  // only for UPDATE/DELETE
    }
  ]
}

If nothing worth extracting: {"topics": [], "facts": []}`;

export const PRE_COMPACTION_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `Extract ALL valuable long-term memories from this conversation before it is lost:

{{CONVERSATION_HISTORY}}
{{EXISTING_MEMORIES}}`,

  format(context: {
    conversationHistory: string;
    existingMemories: string;
  }): { system: string; user: string } {
    const memoriesSection = context.existingMemories && context.existingMemories !== '(No existing memories)'
      ? `\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n${context.existingMemories}`
      : '';
    return {
      system: this.system,
      user: this.user
        .replace('{{CONVERSATION_HISTORY}}', context.conversationHistory)
        .replace('{{EXISTING_MEMORIES}}', memoriesSection),
    };
  },
};

export const POST_TURN_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `Extract important facts from these recent conversation turns:

{{CONVERSATION_HISTORY}}
{{EXISTING_MEMORIES}}`,

  format(context: {
    conversationHistory: string;
    existingMemories: string;
  }): { system: string; user: string } {
    const memoriesSection = context.existingMemories && context.existingMemories !== '(No existing memories)'
      ? `\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n${context.existingMemories}`
      : '';
    return {
      system: this.system,
      user: this.user
        .replace('{{CONVERSATION_HISTORY}}', context.conversationHistory)
        .replace('{{EXISTING_MEMORIES}}', memoriesSection),
    };
  },
};

export const EXPLICIT_COMMAND_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `The user has explicitly requested to remember something. This is a HIGH PRIORITY extraction — boost importance by +1.

User's explicit request:
{{USER_REQUEST}}

Conversation context:
{{CONVERSATION_CONTEXT}}`,

  format(context: {
    userRequest: string;
    conversationContext: string;
  }): { system: string; user: string } {
    return {
      system: this.system,
      user: this.user
        .replace('{{USER_REQUEST}}', context.userRequest)
        .replace('{{CONVERSATION_CONTEXT}}', context.conversationContext),
    };
  },
};

// ---------------------------------------------------------------------------
// Debrief Prompt (canonical — must be identical across all implementations)
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

export interface DebriefItem {
  text: string;
  /**
   * Debrief items are always high-level wrap-up. `summary` is the v1 canonical
   * type; `context` is legacy v0 — at write time it is coerced to `claim` via
   * {@link V0_TO_V1_TYPE}. The literal set is kept for prompt fidelity.
   */
  type: 'summary' | 'context';
  importance: number;
}

/**
 * Parse a debrief response into validated DebriefItems.
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
          typeof (item as Record<string, unknown>).text === 'string' &&
          ((item as Record<string, unknown>).text as string).length >= 5,
      )
      .map((item: unknown) => {
        const d = item as Record<string, unknown>;
        const type: 'summary' | 'context' = d.type === 'summary' ? 'summary' : 'context';
        const rawImportance = typeof d.importance === 'number' ? d.importance : 7;
        const importance = Math.max(1, Math.min(10, rawImportance));
        return { text: String(d.text).slice(0, 512), type, importance };
      })
      .filter((d) => d.importance >= 6)
      .slice(0, 5);
  } catch {
    return [];
  }
}

export function formatConversationHistory(
  turns: Array<{ role: 'user' | 'assistant'; content: string; timestamp: Date }>
): string {
  return turns
    .map((turn, index) => {
      const timestamp = turn.timestamp.toISOString();
      return `[${index + 1}] ${turn.role.toUpperCase()} (${timestamp}):\n${turn.content}`;
    })
    .join('\n\n');
}

/**
 * Parse and validate an extraction response (v1 merged-topic shape).
 *
 * Accepts either:
 *   - v1 merged object: `{ topics: [...], facts: [...] }`
 *   - legacy bare object: `{ facts: [...] }`
 *   - legacy bare array: `[ {...}, {...} ]` (wrapped as `{ topics: [], facts: [...] }`)
 *
 * Each fact is coerced to v1 via {@link normalizeToV1Type}. `source` defaults
 * to `'user-inferred'`, `scope` to `'unspecified'`. Legacy `factText` is
 * still accepted as a fallback for `text` for robustness.
 *
 * Does NOT filter by importance — callers apply their own thresholds.
 */
export function validateExtractionResponse(response: unknown): {
  valid: boolean;
  errors: string[];
  facts?: ExtractedFact[];
  topics?: string[];
} {
  const errors: string[] = [];

  if (!response || (typeof response !== 'object' && !Array.isArray(response))) {
    return { valid: false, errors: ['Response must be an object or array'] };
  }

  // Normalise to { topics, facts } shape.
  let topics: string[] = [];
  let rawFacts: unknown[];

  if (Array.isArray(response)) {
    rawFacts = response;
  } else {
    const obj = response as Record<string, unknown>;
    if (Array.isArray(obj.topics)) {
      topics = (obj.topics as unknown[])
        .filter((t): t is string => typeof t === 'string' && t.length > 0)
        .slice(0, 10);
    }
    if (!Array.isArray(obj.facts)) {
      return { valid: false, errors: ['Response must have a "facts" array'] };
    }
    rawFacts = obj.facts as unknown[];
  }

  const validActions: ExtractionAction[] = ['ADD', 'UPDATE', 'DELETE', 'NOOP'];

  const facts: ExtractedFact[] = [];

  for (let i = 0; i < rawFacts.length; i++) {
    const raw = rawFacts[i];
    if (!raw || typeof raw !== 'object') continue;
    const fact = raw as Record<string, unknown>;
    const factErrors: string[] = [];

    // Accept both "text" and "factText" field names for robustness
    const textValue = fact.text ?? fact.factText;
    if (typeof textValue !== 'string' || (textValue as string).length === 0) {
      factErrors.push(`facts[${i}].text must be a non-empty string`);
    }

    // Accept v1 tokens directly; coerce legacy v0 tokens via V0_TO_V1_TYPE.
    const type = normalizeToV1Type(fact.type);

    if (typeof fact.importance !== 'number' || fact.importance < 1 || fact.importance > 10) {
      factErrors.push(`facts[${i}].importance must be a number between 1 and 10`);
    }

    const action = validActions.includes(String(fact.action) as ExtractionAction)
      ? String(fact.action) as ExtractionAction
      : undefined;
    if (!action) {
      factErrors.push(`facts[${i}].action must be one of: ${validActions.join(', ')}`);
    }

    // v1 provenance fields
    const rawSource = String(fact.source ?? 'user-inferred').toLowerCase();
    const source: MemorySource = (VALID_MEMORY_SOURCES as readonly string[]).includes(rawSource)
      ? (rawSource as MemorySource)
      : 'user-inferred';

    const rawScope = String(fact.scope ?? 'unspecified').toLowerCase();
    const scope: MemoryScope = (VALID_MEMORY_SCOPES as readonly string[]).includes(rawScope)
      ? (rawScope as MemoryScope)
      : 'unspecified';

    const reasoning = typeof fact.reasoning === 'string'
      ? fact.reasoning.slice(0, 256)
      : undefined;

    if (factErrors.length > 0) {
      errors.push(...factErrors);
      continue;
    }

    // Reject illegal type:summary + source:user combinations (per v1 spec).
    if (type === 'summary' && source === 'user') {
      continue;
    }

    facts.push({
      text: String(textValue).slice(0, 512),
      type,
      importance: Math.max(1, Math.min(10, Math.round(fact.importance as number))),
      action: action!,
      existingFactId: typeof fact.existingFactId === 'string' ? fact.existingFactId : undefined,
      source,
      scope,
      reasoning,
    });
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  return { valid: true, errors: [], facts, topics };
}
