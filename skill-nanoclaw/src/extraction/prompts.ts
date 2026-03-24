/**
 * TotalReclaw NanoClaw Skill - LLM Prompts for Fact Extraction
 *
 * Prompts follow Mem0-style ADD/UPDATE/DELETE/NOOP pattern for
 * intelligent deduplication and conflict resolution.
 *
 * Copied from /skill/src/extraction/prompts.ts
 */

export type ExtractionAction = 'ADD' | 'UPDATE' | 'DELETE' | 'NOOP';

export type FactType = 'fact' | 'preference' | 'decision' | 'episodic' | 'goal' | 'context' | 'summary';

export interface Entity {
  id: string;
  name: string;
  type: string;
}

export interface Relation {
  subjectId: string;
  predicate: string;
  objectId: string;
  confidence: number;
}

export interface ExtractedFact {
  factText: string;
  type: FactType;
  importance: number;
  confidence: number;
  action: ExtractionAction;
  existingFactId?: string;
  entities: Entity[];
  relations: Relation[];
}

export const EXTRACTION_RESPONSE_SCHEMA = {
  type: 'object',
  properties: {
    facts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          factText: { type: 'string', maxLength: 512 },
          type: {
            type: 'string',
            enum: ['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary'],
          },
          importance: { type: 'integer', minimum: 1, maximum: 10 },
          confidence: { type: 'number', minimum: 0, maximum: 1 },
          action: {
            type: 'string',
            enum: ['ADD', 'UPDATE', 'DELETE', 'NOOP'],
          },
          existingFactId: { type: 'string' },
          entities: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                id: { type: 'string' },
                name: { type: 'string' },
                type: { type: 'string' },
              },
              required: ['id', 'name', 'type'],
            },
          },
          relations: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                subjectId: { type: 'string' },
                predicate: { type: 'string' },
                objectId: { type: 'string' },
                confidence: { type: 'number', minimum: 0, maximum: 1 },
              },
              required: ['subjectId', 'predicate', 'objectId', 'confidence'],
            },
          },
          reasoning: { type: 'string' },
        },
        required: ['factText', 'type', 'importance', 'confidence', 'action', 'entities', 'relations'],
      },
    },
    metadata: {
      type: 'object',
      properties: {
        totalTurnsAnalyzed: { type: 'integer' },
        extractionTimestamp: { type: 'string' },
      },
    },
  },
  required: ['facts'],
};

const BASE_SYSTEM_PROMPT = `You are a memory extraction engine for an AI assistant. Your job is to analyze conversations and extract structured, atomic facts that should be remembered long-term.

## Extraction Guidelines

1. **Atomicity**: Each fact should be a single, self-contained piece of information
   - GOOD: "User chose PostgreSQL because the data model is relational and needs ACID"
   - BAD: "User likes TypeScript, uses VS Code, and works at Google"

2. **Types**:
   - **fact**: Objective information about the user/world
   - **preference**: User's likes, dislikes, or preferences
   - **decision**: Choices WITH reasoning ("chose X because Y")
   - **episodic**: Event-based memories (what happened when)
   - **goal**: User's objectives or targets
   - **context**: Active project/task context (what the user is working on, versions, environments)
   - **summary**: Key outcome or conclusion from a discussion

3. **Importance Scoring (1-10)**:
   - 1-3: Trivial, unlikely to matter (small talk, pleasantries)
   - 4-6: Useful context (tool preferences, working style)
   - 7-8: Important (key decisions with reasoning, project context, major preferences)
   - 9-10: Critical (core values, non-negotiables, safety info)

4. **Confidence (0-1)**:
   - How certain are you that this is accurate and worth storing?

5. **Extraction quality cues**:
   - Decisions: ALWAYS include reasoning. "Chose X" alone is low value.
   - Context: Include version numbers, environments, status ("v1.2", "staging", "private beta")
   - Summaries: Only when a conversation reaches a clear conclusion or agreement
   - Facts: Prefer specific over vague

6. **Entities**: Extract named entities (people, projects, tools, concepts)
   - Use stable IDs: hash of name+type (e.g., "typescript-tool")
   - Types: person, project, tool, preference, concept, location, etc.

7. **Relations**: Extract relationships between entities
   - Common predicates: prefers, uses, works_on, decided_to_use, dislikes, etc.

8. **Actions (Mem0 pattern)**:
   - **ADD**: New fact, no conflict with existing memories
   - **UPDATE**: Modifies or refines an existing fact (provide existingFactId)
   - **DELETE**: Contradicts and replaces an existing fact
   - **NOOP**: Not worth storing or already captured`;

export const PRE_COMPACTION_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `## Task: Pre-Compaction Memory Extraction

You are reviewing the last 20 turns of conversation before they are compacted. Extract ALL valuable long-term memories.

## Conversation History (last 20 turns):
{{CONVERSATION_HISTORY}}

## Existing Memories (for deduplication):
{{EXISTING_MEMORIES}}

## Instructions:
1. Review each turn carefully for extractable information
2. Extract atomic facts, preferences, decisions, episodic memories, and goals
3. For each fact, determine if it's NEW (ADD), modifies existing (UPDATE), contradicts existing (DELETE), or is redundant (NOOP)
4. Score importance based on long-term relevance
5. Extract entities and relations

## Output Format:
Return a JSON object matching this schema:
${JSON.stringify(EXTRACTION_RESPONSE_SCHEMA, null, 2)}

Focus on quality over quantity. Better to have 5 highly accurate facts than 20 noisy ones.`,

  format(context: {
    conversationHistory: string;
    existingMemories: string;
  }): { system: string; user: string } {
    return {
      system: this.system,
      user: this.user
        .replace('{{CONVERSATION_HISTORY}}', context.conversationHistory)
        .replace('{{EXISTING_MEMORIES}}', context.existingMemories),
    };
  },
};

export const POST_TURN_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `## Task: Quick Turn Extraction

You are doing a lightweight extraction after a few turns. Focus ONLY on high-importance items.

## Recent Turns (last 3):
{{CONVERSATION_HISTORY}}

## Existing Memories (top matches):
{{EXISTING_MEMORIES}}

## Instructions:
1. Extract ONLY items with importance >= 7 (critical preferences, key decisions)
2. Skip trivial information - this is a quick pass
3. Use ADD/UPDATE/DELETE/NOOP appropriately
4. Be aggressive about NOOP for low-value content

## Output Format:
Return a JSON object matching this schema:
${JSON.stringify(EXTRACTION_RESPONSE_SCHEMA, null, 2)}

Remember: Less is more. Only extract what truly matters.`,

  format(context: {
    conversationHistory: string;
    existingMemories: string;
  }): { system: string; user: string } {
    return {
      system: this.system,
      user: this.user
        .replace('{{CONVERSATION_HISTORY}}', context.conversationHistory)
        .replace('{{EXISTING_MEMORIES}}', context.existingMemories),
    };
  },
};

export const EXPLICIT_COMMAND_PROMPT = {
  system: BASE_SYSTEM_PROMPT,

  user: `## Task: Explicit Memory Storage

The user has explicitly requested to remember something. This is a HIGH PRIORITY extraction.

## User's Explicit Request:
{{USER_REQUEST}}

## Conversation Context:
{{CONVERSATION_CONTEXT}}

## Instructions:
1. Parse what the user wants remembered
2. Boost importance by +1 (explicit requests matter more)
3. Extract as atomic fact(s) with appropriate type
4. Check against existing memories for UPDATE/DELETE
5. Set confidence HIGH (user explicitly wants this stored)

## Output Format:
Return a JSON object matching this schema:
${JSON.stringify(EXTRACTION_RESPONSE_SCHEMA, null, 2)}

This is user-initiated storage - ensure accuracy and capture their intent precisely.`,

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

  const facts: ExtractedFact[] = [];

  for (let i = 0; i < obj.facts.length; i++) {
    const fact = obj.facts[i] as Record<string, unknown>;
    const factErrors: string[] = [];

    if (typeof fact.factText !== 'string' || fact.factText.length === 0) {
      factErrors.push(`facts[${i}].factText must be a non-empty string`);
    }

    const validTypes: FactType[] = ['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary'];
    if (!validTypes.includes(fact.type as FactType)) {
      factErrors.push(`facts[${i}].type must be one of: ${validTypes.join(', ')}`);
    }

    if (typeof fact.importance !== 'number' || fact.importance < 1 || fact.importance > 10) {
      factErrors.push(`facts[${i}].importance must be a number between 1 and 10`);
    }

    if (typeof fact.confidence !== 'number' || fact.confidence < 0 || fact.confidence > 1) {
      factErrors.push(`facts[${i}].confidence must be a number between 0 and 1`);
    }

    const validActions: ExtractionAction[] = ['ADD', 'UPDATE', 'DELETE', 'NOOP'];
    if (!validActions.includes(fact.action as ExtractionAction)) {
      factErrors.push(`facts[${i}].action must be one of: ${validActions.join(', ')}`);
    }

    if (!Array.isArray(fact.entities)) {
      factErrors.push(`facts[${i}].entities must be an array`);
    }

    if (!Array.isArray(fact.relations)) {
      factErrors.push(`facts[${i}].relations must be an array`);
    }

    if (factErrors.length > 0) {
      errors.push(...factErrors);
    } else {
      facts.push({
        factText: fact.factText as string,
        type: fact.type as FactType,
        importance: Math.round(fact.importance as number),
        confidence: fact.confidence as number,
        action: fact.action as ExtractionAction,
        existingFactId: fact.existingFactId as string | undefined,
        entities: (fact.entities as Entity[]) || [],
        relations: (fact.relations as Relation[]) || [],
      });
    }
  }

  if (errors.length > 0) {
    return { valid: false, errors };
  }

  return { valid: true, errors: [], facts };
}
