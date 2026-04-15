/**
 * TotalReclaw Plugin - Fact Extractor
 *
 * Uses LLM calls to extract atomic facts from conversation messages.
 * Matches the extraction prompts described in SKILL.md.
 */

import { chatCompletion, resolveLLMConfig } from './llm-client.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ExtractionAction = 'ADD' | 'UPDATE' | 'DELETE' | 'NOOP';

export type EntityType = 'person' | 'project' | 'tool' | 'company' | 'concept' | 'place';

export interface ExtractedEntity {
  name: string;
  type: EntityType;
  role?: string;
}

export interface ExtractedFact {
  text: string;
  type: 'fact' | 'preference' | 'decision' | 'episodic' | 'goal' | 'context' | 'summary' | 'rule';
  importance: number; // 1-10
  action: ExtractionAction;
  existingFactId?: string;
  entities?: ExtractedEntity[];
  confidence?: number; // 0.0-1.0, LLM self-assessed
}

const ALLOWED_ENTITY_TYPES: ReadonlySet<EntityType> = new Set([
  'person',
  'project',
  'tool',
  'company',
  'concept',
  'place',
]);

/**
 * Default confidence when the LLM does not provide one.
 * Mirrors the fallback used by other extraction clients.
 */
export const DEFAULT_EXTRACTION_CONFIDENCE = 0.85;

interface ContentBlock {
  type?: string;
  text?: string;
  thinking?: string;
}

interface ConversationMessage {
  role?: string;
  content?: string | ContentBlock[];
  text?: string;
}

// ---------------------------------------------------------------------------
// Extraction Prompt
// ---------------------------------------------------------------------------

export const EXTRACTION_SYSTEM_PROMPT = `You are a memory extraction engine. Analyze the conversation and extract valuable long-term memories.

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

Entities:
- List the named entities this memory is about (people, projects, tools, companies, concepts, places)
- When a memory is about the user, include the user's own name as a "person" entity
- Entity "type" must be one of: person | project | tool | company | concept | place
- Entity "role" is optional and describes the entity's role in the claim (e.g. "chooser", "employer", "target"); omit if not clear
- If no entities are identifiable, omit the field or use an empty array

Entity specificity (IMPORTANT for contradiction detection):
- Prefer SPECIFIC product/tool names over umbrella categories. "PostgreSQL" beats "database"; "Neovim" beats "editor"; "TypeScript" beats "language".
- Do NOT include umbrella concepts ("database", "editor", "language", "framework", "tool") as separate entities when a specific product is already listed. The specific name is enough.
- When two memories describe different use cases of the same broader category (e.g. Postgres for OLTP and DuckDB for analytics), each memory's entities must be the SPECIFIC products involved in that memory — never a shared umbrella. Sharing an umbrella entity across complementary choices causes false-positive contradictions.
- Examples of ENTITIES TO AVOID as standalone tags: "database", "editor", "IDE", "language", "framework", "library", "tool", "store", "datastore", "server", "client", "app".

Few-shot example (complementary tech — two memories must not share an umbrella entity):

Memory A: "Uses PostgreSQL as the primary OLTP database for user-facing workloads"
  entities: [{"name": "PostgreSQL", "type": "tool", "role": "primary OLTP store"}, {"name": "Pedro", "type": "person"}]
  (NOT: "database", "OLTP", or any umbrella term)

Memory B: "Uses DuckDB for analytics and reporting workloads, roughly 20x faster than Postgres on aggregations"
  entities: [{"name": "DuckDB", "type": "tool", "role": "analytics engine"}, {"name": "Pedro", "type": "person"}]
  (NOT: "database", "analytics", or any umbrella term. PostgreSQL is mentioned as a comparison point and MAY appear as a separate entity, but the memory is about DuckDB.)

These two memories are COMPLEMENTARY, not contradictory — Postgres serves OLTP and DuckDB serves OLAP. Because they do not share an umbrella entity, the contradiction-detection path correctly treats them as independent.

Few-shot examples (rule type — when to use it and when NOT to use it):

Example 1 — rule embedded in a debugging narrative:
  User: "Ugh, spent two hours earlier today because the subgraph query silently failed and I thought we had zero facts on chain. Turns out the schema field is sequenceId, not seqId — my Python wrapper swallowed the GraphQL error and I read it as 'no data'. Note to self: always check d.get('errors') before trusting an empty facts array."
  Extract:
  [{"text": "Subgraph Fact schema uses sequenceId, not seqId — check d.get('errors') before trusting an empty facts array", "type": "rule", "importance": 8, "confidence": 1.0, "entities": [{"name": "subgraph", "type": "tool"}, {"name": "GraphQL", "type": "tool"}]}]

Example 2 — user stating a convention as a rule:
  User: "Convention for the team: before any rm -rf on the VPS state dir, stop the gateway first. Otherwise an async flush can recreate stale files mid-cleanup and you'll chase phantom state."
  Extract:
  [{"text": "Stop the OpenClaw gateway before rm -rf ~/.totalreclaw/ — async flush can recreate stale files mid-cleanup", "type": "rule", "importance": 7, "confidence": 1.0, "entities": [{"name": "OpenClaw gateway", "type": "tool"}]}]

Example 3 — rule vs decision (distinguishing them):
  User: "We chose DuckDB over ClickHouse for analytics because DuckDB fits in a single-file deployment and our scale is small."
  Extract:
  [{"text": "Chose DuckDB over ClickHouse for analytics because single-file deployment fits small-scale use", "type": "decision", "importance": 8, "confidence": 1.0, "entities": [{"name": "DuckDB", "type": "tool", "role": "chosen"}, {"name": "ClickHouse", "type": "tool", "role": "rejected"}]}]
  This is a DECISION, not a rule — it's a specific choice with reasoning, not a transferable pattern. The boundary test: it applies to THIS user's THIS analytics deployment, not to anyone in the same situation.

Confidence:
- Self-assess how sure you are this is a real, durable fact (0.0-1.0)
- Use 0.9-1.0 when the user stated it directly and unambiguously
- Use 0.7-0.9 when you inferred it from context
- Use 0.5-0.7 when it could be a misstatement or temporary state
- Default to 0.85 if unsure

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "...", "importance": N, "confidence": 0.9, "action": "ADD|UPDATE|DELETE|NOOP", "existingFactId": "...", "entities": [{"name": "PostgreSQL", "type": "tool", "role": "chosen database"}]}, ...]

If nothing is worth extracting, return: []`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract text content from a conversation message (handles various formats).
 *
 * OpenClaw AgentMessage objects use content arrays:
 *   { role: "user", content: [{ type: "text", text: "..." }] }
 *   { role: "assistant", content: [{ type: "text", text: "..." }, { type: "toolCall", ... }] }
 *
 * We also handle the simpler { role, content: "string" } format.
 */
function messageToText(msg: unknown): { role: string; content: string } | null {
  if (!msg || typeof msg !== 'object') return null;

  const m = msg as ConversationMessage;
  const role = m.role ?? 'unknown';

  // Only keep user and assistant messages
  if (role !== 'user' && role !== 'assistant') return null;

  let textContent: string;

  if (typeof m.content === 'string') {
    // Simple string content
    textContent = m.content;
  } else if (Array.isArray(m.content)) {
    // OpenClaw AgentMessage format: array of content blocks
    // Extract text from { type: "text", text: "..." } blocks
    const textParts = (m.content as ContentBlock[])
      .filter((block) => block.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text as string);
    textContent = textParts.join('\n');
  } else if (typeof m.text === 'string') {
    // Fallback: { text: "..." } field
    textContent = m.text;
  } else {
    return null;
  }

  if (textContent.length < 3) return null;

  return { role, content: textContent };
}

/**
 * Truncate messages to fit within a token budget (rough estimate: 4 chars per token).
 */
function truncateMessages(messages: Array<{ role: string; content: string }>, maxChars: number): string {
  const lines: string[] = [];
  let totalChars = 0;

  for (const msg of messages) {
    const line = `[${msg.role}]: ${msg.content}`;
    if (totalChars + line.length > maxChars) break;
    lines.push(line);
    totalChars += line.length;
  }

  return lines.join('\n\n');
}

/**
 * Parse a single entity object from LLM output. Returns null if invalid.
 * Invalid entities are silently dropped so a bad entity never fails the whole fact.
 */
export function parseEntity(raw: unknown): ExtractedEntity | null {
  if (!raw || typeof raw !== 'object') return null;
  const e = raw as Record<string, unknown>;
  const name = typeof e.name === 'string' ? e.name.trim() : '';
  if (name.length === 0) return null;
  const type = String(e.type ?? '').toLowerCase() as EntityType;
  if (!ALLOWED_ENTITY_TYPES.has(type)) return null;
  const entity: ExtractedEntity = { name: name.slice(0, 128), type };
  if (typeof e.role === 'string' && e.role.trim().length > 0) {
    entity.role = e.role.trim().slice(0, 128);
  }
  return entity;
}

/**
 * Clamp a raw confidence value to [0, 1]. Returns the default when missing or NaN.
 */
export function normalizeConfidence(raw: unknown): number {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return DEFAULT_EXTRACTION_CONFIDENCE;
  if (raw < 0) return 0;
  if (raw > 1) return 1;
  return raw;
}

/**
 * Parse the LLM response into structured facts.
 */
export function parseFactsResponse(response: string): ExtractedFact[] {
  // Strip markdown code fences if present
  let cleaned = response.trim();
  if (cleaned.startsWith('```')) {
    cleaned = cleaned.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
  }

  try {
    const parsed = JSON.parse(cleaned);
    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter(
        (f: unknown) =>
          f &&
          typeof f === 'object' &&
          typeof (f as ExtractedFact).text === 'string' &&
          (f as ExtractedFact).text.length >= 5,
      )
      .map((f: unknown) => {
        const fact = f as Record<string, unknown>;
        const validActions: ExtractionAction[] = ['ADD', 'UPDATE', 'DELETE', 'NOOP'];
        const action = validActions.includes(String(fact.action) as ExtractionAction)
          ? (String(fact.action) as ExtractionAction)
          : 'ADD'; // Default to ADD for backward compatibility

        let entities: ExtractedEntity[] | undefined;
        if (Array.isArray(fact.entities)) {
          const validEntities = fact.entities
            .map(parseEntity)
            .filter((e): e is ExtractedEntity => e !== null);
          if (validEntities.length > 0) entities = validEntities;
        }

        const result: ExtractedFact = {
          text: String(fact.text).slice(0, 512),
          type: (['fact', 'preference', 'decision', 'episodic', 'goal', 'context', 'summary', 'rule'].includes(String(fact.type))
            ? String(fact.type)
            : 'fact') as ExtractedFact['type'],
          importance: Math.max(1, Math.min(10, Number(fact.importance) || 5)),
          action,
          existingFactId: typeof fact.existingFactId === 'string' ? fact.existingFactId : undefined,
          confidence: normalizeConfidence(fact.confidence),
        };
        if (entities) result.entities = entities;
        return result;
      })
      .filter((f) => f.importance >= 6 || f.action === 'DELETE'); // DELETE actions pass regardless of importance
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Main extraction function
// ---------------------------------------------------------------------------

/**
 * Extract facts from a list of conversation messages using LLM.
 *
 * @param rawMessages - The messages array from the hook event (unknown[])
 * @param mode - 'turn' for agent_end (recent only), 'full' for compaction/reset
 * @param existingMemories - Optional list of existing memories for dedup context
 * @param profileContext - Optional enriched system prompt from smart import (replaces default)
 * @returns Array of extracted facts, or empty array on failure.
 */
export async function extractFacts(
  rawMessages: unknown[],
  mode: 'turn' | 'full',
  existingMemories?: Array<{ id: string; text: string }>,
  profileContext?: string,
): Promise<ExtractedFact[]> {
  const config = resolveLLMConfig();
  if (!config) return []; // No LLM available

  // Parse messages
  const parsed = rawMessages
    .map(messageToText)
    .filter((m): m is { role: string; content: string } => m !== null);

  if (parsed.length === 0) return [];

  // For 'turn' mode, only look at last 6 messages (3 turns)
  // For 'full' mode, use all messages but truncate to fit token budget
  const relevantMessages = mode === 'turn' ? parsed.slice(-6) : parsed;

  // Truncate to ~3000 tokens worth of text
  const conversationText = truncateMessages(relevantMessages, 12_000);

  if (conversationText.length < 20) return [];

  // Build existing memories context if available
  let memoriesContext = '';
  if (existingMemories && existingMemories.length > 0) {
    const memoriesStr = existingMemories
      .map((m) => `[ID: ${m.id}] ${m.text}`)
      .join('\n');
    memoriesContext = `\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n${memoriesStr}`;
  }

  const userPrompt =
    mode === 'turn'
      ? `Extract important facts from these recent conversation turns:\n\n${conversationText}${memoriesContext}`
      : `Extract ALL valuable long-term memories from this conversation before it is lost:\n\n${conversationText}${memoriesContext}`;

  // Use enriched system prompt from smart import if provided, otherwise default
  const systemPrompt = profileContext || EXTRACTION_SYSTEM_PROMPT;

  try {
    const response = await chatCompletion(config, [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ]);

    if (!response) return [];

    return parseFactsResponse(response);
  } catch {
    return []; // Fail silently -- hooks must never break the agent
  }
}

// ---------------------------------------------------------------------------
// Debrief Extraction
// ---------------------------------------------------------------------------

/**
 * Canonical debrief system prompt — must be identical across all clients.
 */
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

/**
 * Extract a session debrief using LLM.
 *
 * @param rawMessages - All messages from the session
 * @param storedFactTexts - Texts of facts already stored in this session (for dedup)
 * @returns Array of debrief items, or empty array on failure
 */
export async function extractDebrief(
  rawMessages: unknown[],
  storedFactTexts: string[],
): Promise<DebriefItem[]> {
  const config = resolveLLMConfig();
  if (!config) return [];

  const parsed = rawMessages
    .map(messageToText)
    .filter((m): m is { role: string; content: string } => m !== null);

  // Minimum 4 turns (8 messages) to warrant a debrief
  if (parsed.length < 8) return [];

  const conversationText = truncateMessages(parsed, 12_000);
  if (conversationText.length < 20) return [];

  const alreadyStored = storedFactTexts.length > 0
    ? storedFactTexts.map((t) => `- ${t}`).join('\n')
    : '(none)';

  const systemPrompt = DEBRIEF_SYSTEM_PROMPT.replace('{already_stored_facts}', alreadyStored);

  try {
    const response = await chatCompletion(config, [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: `Review this conversation and provide a debrief:\n\n${conversationText}` },
    ]);

    if (!response) return [];
    return parseDebriefResponse(response);
  } catch {
    return [];
  }
}
