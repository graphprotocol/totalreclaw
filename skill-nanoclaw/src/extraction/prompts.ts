/**
 * TotalReclaw NanoClaw Skill — LLM Prompts for Fact Extraction (v1 taxonomy).
 *
 * As of NanoClaw 3.1.0:
 *
 *   1. The canonical extraction system prompt is sourced from the Rust core
 *      (`@totalreclaw/core` 2.2.0+) via `getExtractionSystemPrompt()` —
 *      byte-identical to what the Python client consumes from PyO3. One
 *      source of truth eliminates the prompt drift the 2026-04-18 v1 QA
 *      uncovered.
 *   2. Extraction is **ADD-only** on the emitter side. The hoisted prompt
 *      output schema only lists `"action": "ADD"` and the hooks silently
 *      ignore any UPDATE/DELETE/NOOP tokens that leak through. This
 *      aligns with the investigation finding that NanoClaw pre-3.1 never
 *      actually acted on UPDATE/DELETE/NOOP in production paths —
 *      `agent-end.ts` only ever stored ADDs (see `agent-end.ts` line ~108),
 *      and `pre-compact.ts` had branches that could fire but were rare /
 *      untested in practice. See
 *      `docs/notes/NANOCLAW-ACTION-FREQUENCY-20260419.md`.
 *
 * Memory Taxonomy v1 (the 6 canonical types) remains the only extraction
 * taxonomy. Legacy v0 tokens are still accepted on the read/parse side via
 * ``V0_TO_V1_TYPE`` so pre-v1 vault entries still round-trip.
 */

import * as wasm from '@totalreclaw/core';

/**
 * Extraction action tokens. The LLM output schema only emits `"ADD"` now
 * (see the hoisted `getExtractionSystemPrompt` contents), but `validateExtractionResponse`
 * still accepts all four on the parser side — legacy cached LLM outputs or
 * custom model drivers might still produce the wider set, and we prefer to
 * parse them and let the hooks decide to silently ignore than to hard-fail
 * validation.
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
// v1 merged-topic extraction prompt (hoisted canonical source)
//
// The extraction + compaction system prompts live in the Rust core
// (`rust/totalreclaw-core/src/prompts/{extraction,compaction}.md`,
// accessed via `@totalreclaw/core` 2.2.0+). NanoClaw consumes the hoisted
// version so all TotalReclaw clients (plugin, Python, NanoClaw) use
// byte-identical prompt bytes — no more drift.
//
// The canonical prompt is ADD-only on the emitter side (only `"action": "ADD"`
// appears in the output schema). Hooks (`agent-end.ts`, `pre-compact.ts`)
// silently ignore any UPDATE/DELETE/NOOP tokens that might still leak
// through from cached outputs or custom drivers.
// ---------------------------------------------------------------------------

/**
 * Canonical v1 extraction system prompt — sourced from @totalreclaw/core 2.2.0+.
 *
 * Evaluated at module-load time so downstream consumers (the three prompt
 * objects below) get a plain string. Identical bytes across all clients.
 */
export const BASE_SYSTEM_PROMPT: string = wasm.getExtractionSystemPrompt();

/**
 * Canonical v1 compaction system prompt — sourced from @totalreclaw/core 2.2.0+.
 *
 * Variant used on the pre-compaction surface (importance floor 5 instead
 * of 6, format-agnostic parsing section).
 */
export const COMPACTION_SYSTEM_PROMPT: string = wasm.getCompactionSystemPrompt();

export const PRE_COMPACTION_PROMPT = {
  system: COMPACTION_SYSTEM_PROMPT,

  user: `Extract ALL valuable long-term memories from this conversation before it is lost:

{{CONVERSATION_HISTORY}}
{{EXISTING_MEMORIES}}`,

  format(context: {
    conversationHistory: string;
    existingMemories: string;
  }): { system: string; user: string } {
    const memoriesSection = context.existingMemories && context.existingMemories !== '(No existing memories)'
      ? `\n\nExisting memories (skip any fact that is already captured or overlaps with an existing memory):\n${context.existingMemories}`
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
      ? `\n\nExisting memories (skip any fact that is already captured or overlaps with an existing memory):\n${context.existingMemories}`
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
