/**
 * TotalReclaw NanoClaw Skill - LLM Prompts for Fact Extraction
 *
 * Prompts follow Mem0-style ADD/UPDATE/DELETE/NOOP pattern for
 * intelligent deduplication and conflict resolution.
 *
 * Aligned to the canonical extraction prompt from skill/plugin/extractor.ts.
 */

export type ExtractionAction = 'ADD' | 'UPDATE' | 'DELETE' | 'NOOP';

export type FactType = 'fact' | 'preference' | 'decision' | 'episodic' | 'goal' | 'context' | 'summary' | 'rule';

export interface ExtractedFact {
  text: string;
  type: FactType;
  importance: number; // 1-10
  action: ExtractionAction;
  existingFactId?: string;
}

const BASE_SYSTEM_PROMPT = `You are a memory extraction engine. Analyze the conversation and extract valuable long-term memories.

Rules:
1. Each memory must be a single, self-contained piece of information
2. Focus on user-specific information that would be useful in future conversations
3. Skip generic knowledge, greetings, small talk, and ephemeral task coordination
4. Score importance 1-10 (6+ = worth storing)
5. Only extract memories with importance >= 6

Types:
- fact: Objective information about the user (name, location, job, relationships)
- preference: Likes, dislikes, or preferences ("prefers dark mode", "allergic to peanuts")
- decision: Choices WITH reasoning ("chose PostgreSQL because data is relational and needs ACID")
- episodic: Notable events or experiences ("deployed v1.0 to production on March 15")
- goal: Objectives, targets, or plans ("wants to launch public beta by end of Q1")
- context: Active project/task context ("working on TotalReclaw v1.2, staging on Base Sepolia")
- summary: Key outcome or conclusion from a discussion ("agreed to use phased rollout for migration")
- rule: A reusable operational rule, non-obvious gotcha, debugging shortcut, or convention the user wants to remember for next time. Distinct from decisions (which have reasoning for a specific choice) and preferences (which are personal tastes). Rules are impersonal, actionable, and transferable — they would help anyone in the same situation. Examples: "Always check the systemd unit file for environment pins before wiping state", "The subgraph schema uses sequenceId not seqId", "Don't open large JSON files in Neovim — use jq instead".

Extraction guidance:
- For decisions: ALWAYS include the reasoning. "Chose X" is weak. "Chose X because Y" is strong.
- For context: Capture what the user is actively working on, including versions, environments, and status.
- For summaries: Only extract when a conversation reaches a clear conclusion or agreement.
- For facts: Prefer specific over vague. "Lives in Lisbon" beats "lives in Europe".
- For rules: ALWAYS extract when the user explicitly signals "remember this", "gotcha", "rule of thumb", "always", "never", or describes a non-obvious learning. Importance >= 7 when the rule prevented a real bug or wasted time. Include the specific context (which tool, which error, which version) so the rule is actionable later. The boundary test: would this apply to anyone in the same situation? Rules generalize; decisions and preferences don't.
- Decisions and context should be importance >= 7 (they are high-value for future conversations).

Actions (compare against existing memories if provided):
- ADD: New memory, no conflict with existing
- UPDATE: Refines or corrects an existing memory (provide existingFactId)
- DELETE: Contradicts an existing memory -- the old one is now wrong (provide existingFactId)
- NOOP: Already captured or not worth storing

Return a JSON object with a "facts" array (no markdown, no code fences):
{"facts": [{"text": "...", "type": "...", "importance": N, "action": "ADD|UPDATE|DELETE|NOOP", "existingFactId": "..."}, ...]}

If nothing is worth extracting, return: {"facts": []}`;

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
 * Parse and validate an extraction response.
 *
 * Expects: { facts: [{ text, type, importance, action, existingFactId? }, ...] }
 *
 * Also accepts "factText" as an alias for "text" for robustness.
 * Does NOT filter by importance -- callers are responsible for their own thresholds.
 */
export function validateExtractionResponse(response: unknown): {
  valid: boolean;
  errors: string[];
  facts?: ExtractedFact[];
} {
  const errors: string[] = [];

  if (!response || typeof response !== 'object') {
    return { valid: false, errors: ['Response must be an object'] };
  }

  const obj = response as Record<string, unknown>;

  if (!Array.isArray(obj.facts)) {
    return { valid: false, errors: ['Response must have a "facts" array'] };
  }

  const validTypes: FactType[] = ['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary', 'rule'];
  const validActions: ExtractionAction[] = ['ADD', 'UPDATE', 'DELETE', 'NOOP'];

  const facts: ExtractedFact[] = [];

  for (let i = 0; i < obj.facts.length; i++) {
    const fact = obj.facts[i] as Record<string, unknown>;
    const factErrors: string[] = [];

    // Accept both "text" and "factText" field names for robustness
    const textValue = fact.text ?? fact.factText;
    if (typeof textValue !== 'string' || (textValue as string).length === 0) {
      factErrors.push(`facts[${i}].text must be a non-empty string`);
    }

    if (!validTypes.includes(fact.type as FactType)) {
      factErrors.push(`facts[${i}].type must be one of: ${validTypes.join(', ')}`);
    }

    if (typeof fact.importance !== 'number' || fact.importance < 1 || fact.importance > 10) {
      factErrors.push(`facts[${i}].importance must be a number between 1 and 10`);
    }

    const action = validActions.includes(String(fact.action) as ExtractionAction)
      ? String(fact.action) as ExtractionAction
      : undefined;
    if (!action) {
      factErrors.push(`facts[${i}].action must be one of: ${validActions.join(', ')}`);
    }

    if (factErrors.length > 0) {
      errors.push(...factErrors);
    } else {
      facts.push({
        text: String(textValue).slice(0, 512),
        type: fact.type as FactType,
        importance: Math.max(1, Math.min(10, Math.round(fact.importance as number))),
        action: action!,
        existingFactId: typeof fact.existingFactId === 'string' ? fact.existingFactId : undefined,
      });
    }
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  return { valid: true, errors: [], facts };
}
