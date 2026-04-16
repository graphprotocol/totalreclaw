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

/**
 * The 8 canonical memory types — single source of truth for this package.
 *
 * Any TypeScript consumer in `skill/plugin/` (tool schemas, type mappings,
 * validation whitelists, canonical-claim builders) MUST import this constant
 * — never re-declare the list inline. A cross-file parity check in
 * `memory-types-parity.test.ts` enforces this at test time.
 *
 * When adding a new type, update ALL of:
 *   - This constant
 *   - `mcp/src/memory-types.ts` (`VALID_MEMORY_TYPES` — MCP package equivalent)
 *   - `python/src/totalreclaw/agent/extraction.py` (`VALID_TYPES`)
 *   - `python/src/totalreclaw/claims_helper.py` (`TYPE_TO_CATEGORY`)
 *   - `rust/totalreclaw-core/src/claims.rs` (`ClaimCategory` enum + short-form test)
 *   - `skill/plugin/claims-helper.ts` (`TYPE_TO_CATEGORY`)
 *   - `skill/plugin/pin.ts` (legacy-blob-lift `TYPE_TO_CATEGORY`)
 *   - The `EXTRACTION_SYSTEM_PROMPT` Types: list (both TS + Python)
 *   - Parity fixtures in `tests/parity/kg_phase1_vectors.json`
 */
export const VALID_MEMORY_TYPES = [
  'fact',
  'preference',
  'decision',
  'episodic',
  'goal',
  'context',
  'summary',
  'rule',
] as const;

/** Type alias derived from the single-source-of-truth constant above. */
export type MemoryType = (typeof VALID_MEMORY_TYPES)[number];

/**
 * Runtime type guard — returns whether an unknown value is a valid MemoryType.
 * Prefer this over inline `.includes()` checks on `VALID_MEMORY_TYPES` so the
 * single-source-of-truth invariant is enforced by grep in CI.
 */
export function isValidMemoryType(value: unknown): value is MemoryType {
  return typeof value === 'string' && (VALID_MEMORY_TYPES as readonly string[]).includes(value);
}

export interface ExtractedFact {
  text: string;
  type: MemoryType;
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
4. Score importance 1-10 using the rubric below (6+ = worth storing)
5. Only extract memories with importance >= 6

Importance rubric (use the FULL 1-10 range, not just 7-8):
- 10: Critical, core identity, never-forget content. The user explicitly says "remember this forever", "critical", "never forget", or it's a fundamental fact like name/birthday/relationships that defines who they are.
- 9: Affects many future decisions or interactions. A high-impact rule, a major life decision with reasoning, a deeply held preference that shapes daily work.
- 8: High-value preference, decision-with-reasoning, or operational rule. The user clearly cares about it AND it will be relevant in many future conversations.
- 7: Specific durable fact about the user's setup, project, or context. Useful to remember but not life-changing.
- 6: Borderline — barely passes the "worth storing" threshold. Generic facts, low-signal preferences. If you're hesitating between 5 and 6, prefer 5 (it gets dropped).
- 5 or below: NOT WORTH STORING. Drop these. Casual mentions, ephemeral state, low-signal chatter.

DO NOT cluster every fact at 7-8. Use 9-10 for high-signal content and 5-6 for borderline content. The system depends on the full range working — over-clustering at 7-8 produces tied scores in the contradiction resolver and makes ranking/decay impossible.

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
 * Minimal logger shape accepted by the extraction pipeline. Matches the
 * OpenClaw plugin logger so callers can pass `api.logger` directly.
 *
 * All methods are optional so tests can pass a partial object and callers
 * that don't care about observability can omit the argument entirely.
 */
export interface ExtractorLogger {
  info?: (msg: string) => void;
  warn?: (msg: string) => void;
}

/**
 * Parse the LLM response into structured facts.
 *
 * Hardened in Phase 2.2.5 to handle the three failure modes that previously
 * caused silent empty returns:
 *   1. Thinking-model outputs with `<think>...</think>` or `<thinking>...</thinking>`
 *      prefix — stripped before the JSON parse attempt.
 *   2. Prose-wrapped JSON ("Here are the facts: [...]") — extracted via a
 *      greedy regex match on the first/last `[` / `]` pair.
 *   3. Hard JSON.parse failures — now logged at WARN level with a preview of
 *      the response so operators can diagnose what the LLM actually produced,
 *      instead of silently returning an empty array.
 *
 * The optional logger parameter is used only for observability; passing `undefined`
 * restores the legacy silent behavior for any caller that prefers it.
 */
export function parseFactsResponse(
  response: string,
  logger?: ExtractorLogger,
): ExtractedFact[] {
  const originalPreview = response.trim().slice(0, 200);
  let cleaned = response.trim();

  // Phase 2.2.5: strip <think>...</think> and <thinking>...</thinking> tags
  // before any other cleanup. Thinking models (glm-5/glm-5.1, claude reasoning,
  // gpt-o1) prefix their output with the reasoning trace; the old parser
  // handed that straight to JSON.parse and silently returned []. Both tag
  // variants are matched case-insensitively; nested tags are not supported
  // but neither are they produced by current models.
  cleaned = cleaned
    .replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '')
    .trim();

  // Strip markdown code fences if present
  if (cleaned.startsWith('```')) {
    cleaned = cleaned.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
  }

  // Phase 2.2.5: if the cleaned output is not pure JSON (e.g. a model wrapped
  // the array in conversational prose like "Here are the facts: [...]"), try
  // to extract the JSON array directly via a greedy match on the first/last
  // bracket pair. This is a best-effort fallback — we still prefer the clean
  // path above, but it's better to recover than to silently return [].
  const tryParse = (input: string): unknown => {
    try {
      return JSON.parse(input);
    } catch {
      return undefined;
    }
  };

  let parsed = tryParse(cleaned);
  let recoveryUsed: 'none' | 'bracket-scan' = 'none';
  if (parsed === undefined) {
    // Fallback: scan for a JSON array anywhere in the cleaned output.
    const match = cleaned.match(/\[[\s\S]*\]/);
    if (match) {
      parsed = tryParse(match[0]);
      if (parsed !== undefined) recoveryUsed = 'bracket-scan';
    }
  }

  if (parsed === undefined) {
    logger?.warn?.(
      `parseFactsResponse: could not parse LLM output as JSON. Preview: ${JSON.stringify(
        originalPreview,
      )}`,
    );
    return [];
  }

  if (recoveryUsed === 'bracket-scan') {
    logger?.info?.(
      `parseFactsResponse: recovered JSON array via bracket-scan fallback (original had ${cleaned.length - (cleaned.match(/\[[\s\S]*\]/)?.[0].length ?? 0)} bytes of prose wrapper)`,
    );
  }

  if (!Array.isArray(parsed)) {
    logger?.warn?.(
      `parseFactsResponse: parsed value is not an array (type=${typeof parsed})`,
    );
    return [];
  }

  const facts = (parsed as unknown[])
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
        type: (isValidMemoryType(fact.type) ? fact.type : 'fact') as MemoryType,
        importance: Math.max(1, Math.min(10, Number(fact.importance) || 5)),
        action,
        existingFactId: typeof fact.existingFactId === 'string' ? fact.existingFactId : undefined,
        confidence: normalizeConfidence(fact.confidence),
      };
      if (entities) result.entities = entities;
      return result;
    })
    .filter((f) => f.importance >= 6 || f.action === 'DELETE'); // DELETE actions pass regardless of importance

  return facts;
}

// ---------------------------------------------------------------------------
// Phase 2.2.6: lexical importance bumps
// ---------------------------------------------------------------------------

/**
 * Escape regex metacharacters so a string can be used as a literal pattern.
 */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Compute a lexical importance bump (0-2) for a single fact based on signals
 * in the surrounding conversation text.
 *
 * This is a Phase 2.2.6 quality fix complementing the prompt rubric tightening
 * (item A). Where the rubric tells the LLM to use the full 1-10 range, the
 * bump tells us *as a post-process*: when the user's actual phrasing carries
 * strong "remember this" signals that the LLM may have under-weighted, push
 * the score up.
 *
 * Signals detected (each adds +1, capped at +2 total):
 *
 *   1. **Strong intent phrases** anywhere in the conversation:
 *      "remember this", "never forget", "rule of thumb", "critical",
 *      "don't ever forget", explicit "always X" / "never Y" patterns.
 *   2. **Emphasis markers**: `!!` (double exclamation), or 3+ all-caps words
 *      in a row (e.g. "DO NOT FORGET", "VERY IMPORTANT").
 *   3. **Repetition**: the fact's first ~20 chars appear at least twice in
 *      the conversation text (paraphrased restating).
 *
 * The bump is additive on top of whatever the LLM scored; final importance
 * is capped at 10.
 *
 * Final-importance ceiling: this never makes a fact pass the importance >= 6
 * filter on its own — a fact still needs to have an LLM score >= 5 (because
 * +2 from 5 = 7, above floor; +1 from 5 = 6, above floor). This is intentional:
 * the bump is for "the LLM correctly identified this as worth storing but
 * under-weighted it", not "the LLM said skip but we're overriding."
 */
export function computeLexicalImportanceBump(
  factText: string,
  conversationText: string,
): number {
  let bump = 0;
  const lowerConv = conversationText.toLowerCase();

  // Signal 1: strong intent phrases anywhere in the conversation
  const strongIntent =
    /\b(remember this|never forget|rule of thumb|don't (?:ever )?forget|critical|important|gotcha|note to self)\b/i;
  if (strongIntent.test(lowerConv)) bump += 1;

  // Signal 2: emphasis markers — double exclamation OR 3+ consecutive all-caps words
  // (3+ chars each, to avoid false positives on acronyms like "AWS S3 IAM")
  const doubleExclamation = /!!/;
  const allCapsPhrase = /\b[A-Z]{3,}(?:\s+[A-Z]{3,}){2,}\b/;
  if (doubleExclamation.test(conversationText) || allCapsPhrase.test(conversationText)) {
    bump += 1;
  }

  // Signal 3: repetition — extract content words (length >= 5, not common stop
  // words) from the fact, and check if any single one appears 2+ times in the
  // conversation. This is more robust to LLM paraphrasing than a fingerprint
  // match: "User prefers PostgreSQL" extracted from "I prefer PostgreSQL ...
  // yeah PostgreSQL is right for OLTP" still triggers because "postgresql"
  // appears multiple times even though the leading chars differ.
  const lowerFact = factText.toLowerCase();
  const stopWords = new Set([
    'about', 'after', 'again', 'against', 'because', 'before', 'being',
    'between', 'could', 'doing', 'during', 'every', 'further', 'having',
    'their', 'these', 'those', 'through', 'under', 'until', 'where', 'which',
    'while', 'would', 'should', 'about', 'thing', 'things', 'something',
    'someone', 'always', 'never', 'often', 'still', 'really', 'maybe',
    'using', 'works', 'work', 'user', 'users', 'with', 'from', 'into',
    'like', 'just', 'than', 'them', 'they', 'will', 'when', 'what', 'were',
    'this', 'that', 'have', 'this',
  ]);
  const factWords = lowerFact.split(/[^a-z0-9_]+/).filter((w) => w.length >= 5 && !stopWords.has(w));
  let triggered = false;
  for (const word of factWords) {
    const occurrences = (lowerConv.match(new RegExp(`\\b${escapeRegExp(word)}\\b`, 'g')) || [])
      .length;
    if (occurrences >= 2) {
      triggered = true;
      break;
    }
  }
  if (triggered) bump += 1;

  return Math.min(bump, 2);
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
 * @param logger - Optional logger for Phase 2.2.5 observability. When provided,
 *                 the function logs why it returned an empty array (no LLM,
 *                 no messages, chatCompletion threw, parse failed) instead of
 *                 silently swallowing failures. Pass `api.logger` from the
 *                 OpenClaw plugin runtime, or omit in tests that don't care.
 * @returns Array of extracted facts, or empty array on failure.
 */
export async function extractFacts(
  rawMessages: unknown[],
  mode: 'turn' | 'full',
  existingMemories?: Array<{ id: string; text: string }>,
  profileContext?: string,
  logger?: ExtractorLogger,
): Promise<ExtractedFact[]> {
  const config = resolveLLMConfig();
  if (!config) {
    logger?.info?.('extractFacts: no LLM config resolved (skipping extraction)');
    return [];
  }

  // Parse messages
  const parsed = rawMessages
    .map(messageToText)
    .filter((m): m is { role: string; content: string } => m !== null);

  if (parsed.length === 0) {
    logger?.info?.(`extractFacts: no parseable messages (raw count=${rawMessages.length})`);
    return [];
  }

  // For 'turn' mode, only look at last 6 messages (3 turns)
  // For 'full' mode, use all messages but truncate to fit token budget
  const relevantMessages = mode === 'turn' ? parsed.slice(-6) : parsed;

  // Truncate to ~3000 tokens worth of text
  const conversationText = truncateMessages(relevantMessages, 12_000);

  if (conversationText.length < 20) {
    logger?.info?.(
      `extractFacts: conversation too short (${conversationText.length} chars < 20, parsed=${parsed.length}, mode=${mode})`,
    );
    return [];
  }

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

  let response: string | null | undefined;
  try {
    response = await chatCompletion(config, [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ]);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger?.warn?.(`extractFacts: chatCompletion threw: ${msg}`);
    return []; // Fail gracefully -- hooks must never break the agent
  }

  if (!response) {
    logger?.info?.('extractFacts: chatCompletion returned null/empty response');
    return [];
  }

  logger?.info?.(
    `extractFacts: LLM returned ${response.length} chars; handing to parseFactsResponse`,
  );
  const facts = parseFactsResponse(response, logger);

  // Phase 2.2.6: lexical importance bumps. After the LLM has scored each
  // fact, post-process by scanning the original conversation text for strong
  // intent signals ("never forget", "rule of thumb", "!!", repetition, etc.)
  // and bump the importance score upward (+1 to +2, capped at 10) for facts
  // that the user clearly cared about. The bump is additive on top of the
  // LLM's score and never overrides the importance >= 6 filter on its own.
  for (const f of facts) {
    const bump = computeLexicalImportanceBump(f.text, conversationText);
    if (bump > 0) {
      const oldImportance = f.importance;
      const effectiveBump = f.importance >= 8 ? Math.min(bump, 1) : bump;
      f.importance = Math.min(10, f.importance + effectiveBump);
      logger?.info?.(
        `extractFacts: lexical bump +${bump} for "${f.text.slice(0, 60)}..." (${oldImportance} → ${f.importance})`,
      );
    }
  }

  return facts;
}

// ---------------------------------------------------------------------------
// Compaction-Aware Extraction (Phase 2.3)
// ---------------------------------------------------------------------------

/**
 * Compaction-specific system prompt. This fires when the conversation context
 * is about to be compacted (truncated to fit the context window). It is the
 * LAST CHANCE to capture knowledge before it is lost, so the threshold is
 * lower (importance >= 5 instead of 6) and the prompt is more aggressive
 * about extracting context, decisions, and episodic memories.
 *
 * Key differences from EXTRACTION_SYSTEM_PROMPT:
 *   - Opening framing emphasizes urgency ("last chance")
 *   - Format-agnostic: handles bullet lists, prose, mixed formats
 *   - Importance threshold lowered to 5
 *   - More aggressive on context/episodic/decision types
 *   - Anti-pattern: don't skip content just because it's in a summary
 *   - Two few-shot examples: bullet-list and prose formats
 */
export const COMPACTION_SYSTEM_PROMPT = `You are extracting memories from a conversation that is about to be compacted. The conversation context will be lost after this point — this is your LAST CHANCE to capture everything worth remembering. Be more aggressive than usual: err on the side of storing.

Rules:
1. Each memory must be a single, self-contained piece of information
2. Focus on user-specific information that would be useful in future conversations
3. Skip generic knowledge, greetings, small talk, and ephemeral task coordination
4. Score importance 1-10 using the rubric below (5+ = worth storing for compaction)
5. Only extract memories with importance >= 5

Importance rubric (use the FULL 1-10 range, not just 7-8):
- 10: Critical, core identity, never-forget content. The user explicitly says "remember this forever", "critical", "never forget", or it's a fundamental fact like name/birthday/relationships that defines who they are.
- 9: Affects many future decisions or interactions. A high-impact rule, a major life decision with reasoning, a deeply held preference that shapes daily work.
- 8: High-value preference, decision-with-reasoning, or operational rule. The user clearly cares about it AND it will be relevant in many future conversations.
- 7: Specific durable fact about the user's setup, project, or context. Useful to remember but not life-changing.
- 6: Borderline in normal extraction — but worth storing during compaction since this context will be lost.
- 5: Would normally be dropped, but during compaction we capture it as a safety net. Low-signal context, minor preferences, ephemeral project state that may still be useful if the conversation is lost.
- 4 or below: NOT WORTH STORING even during compaction. Drop these. Greetings, filler, already-known common knowledge.

DO NOT cluster every fact at 7-8. Use 9-10 for high-signal content and 5-6 for borderline content. The system depends on the full range working.

Format-agnostic parsing (IMPORTANT):
The conversation may contain bullet lists, numbered lists, section headers with paragraphs, code snippets, or plain prose. Treat ALL formats as potential sources of extractable memory:
- If bullets/list items: each item is a candidate memory.
- If section headers (Context, Decisions, Key Learnings, Open Questions, etc.): use the header as a type hint (Context → context, Decisions → decision, Learnings → rule, Open Questions → goal).
- If plain prose: parse each distinct assertion as a candidate memory, even if they run together in paragraph form.
- If code snippets: extract any configuration choices, tool versions, or architectural decisions embedded in comments or code structure.
- If mixed format: apply all of the above.

Do NOT skip content just because it appears in a summary. The agent has already done the filtering — your job is to convert the content into structured memories, not to re-evaluate whether each item is worth storing.

Types:
- fact: Objective information about the user (name, location, job, relationships)
- preference: Likes, dislikes, or preferences ("prefers dark mode", "allergic to peanuts")
- decision: Choices WITH reasoning ("chose PostgreSQL because data is relational and needs ACID")
- episodic: Notable events or experiences ("deployed v1.0 to production on March 15")
- goal: Objectives, targets, or plans ("wants to launch public beta by end of Q1")
- context: Active project/task context ("working on TotalReclaw v1.2, staging on Base Sepolia")
- summary: Key outcome or conclusion from a discussion ("agreed to use phased rollout for migration")
- rule: A reusable operational rule, non-obvious gotcha, debugging shortcut, or convention the user wants to remember for next time.

Extraction guidance (compaction-specific):
- Pay special attention to active project context, decisions in progress, and current working state — these are especially valuable to preserve before compaction.
- For decisions: ALWAYS include the reasoning. "Chose X" is weak. "Chose X because Y" is strong.
- For context: Capture what the user is actively working on, including versions, environments, and status. During compaction, even minor project state is worth preserving.
- For summaries: Extract clear conclusions or agreements — compaction is the perfect time since the conversation is being summarized.
- For rules: Extract non-obvious learnings, gotchas, and conventions. Include specific context (which tool, which error, which version).
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
- Entity "role" is optional and describes the entity's role in the claim
- Prefer SPECIFIC product/tool names over umbrella categories. "PostgreSQL" beats "database"; "Neovim" beats "editor".

Confidence:
- Self-assess how sure you are this is a real, durable fact (0.0-1.0)
- Use 0.9-1.0 when the user stated it directly and unambiguously
- Use 0.7-0.9 when you inferred it from context
- Use 0.5-0.7 when it could be a misstatement or temporary state
- Default to 0.85 if unsure

Few-shot example (bullet-list compaction summary):

Input:
User: I think we're ready to wrap up. What did we cover?
Assistant: Here's a condensed summary:

Context:
- Migrating from Heroku to Fly.io for a Django monolith
- Sarah championing the migration, user skeptical about Celery workers

Decisions:
- Will run a 2-week spike on Fly.io with one Celery worker first
- Using Fly Machines for Celery, Fly Apps for the web tier

Key learnings:
- Fly.io's internal DNS doesn't resolve the same way as Heroku's — service discovery needs explicit config
- Celery task routing broke on our Redis setup because Fly Redis uses a different connection pool model

Output:
[
  {"text": "Migrating from Heroku to Fly.io, Django monolith", "type": "context", "importance": 7, "confidence": 0.9, "action": "ADD", "entities": [{"name": "Heroku", "type": "tool"}, {"name": "Fly.io", "type": "tool"}, {"name": "Django", "type": "tool"}]},
  {"text": "Sarah is championing the Fly.io migration; user skeptical about Celery workers", "type": "context", "importance": 6, "confidence": 0.85, "action": "ADD", "entities": [{"name": "Sarah", "type": "person"}, {"name": "Celery", "type": "tool"}]},
  {"text": "Will run a 2-week spike on Fly.io with one Celery worker first", "type": "decision", "importance": 8, "confidence": 0.95, "action": "ADD", "entities": [{"name": "Fly.io", "type": "tool"}, {"name": "Celery", "type": "tool"}]},
  {"text": "Using Fly Machines for Celery, Fly Apps for the web tier", "type": "decision", "importance": 8, "confidence": 0.95, "action": "ADD", "entities": [{"name": "Fly Machines", "type": "tool"}, {"name": "Fly Apps", "type": "tool"}, {"name": "Celery", "type": "tool"}]},
  {"text": "Fly.io internal DNS doesn't resolve the same as Heroku — service discovery needs explicit config", "type": "rule", "importance": 8, "confidence": 1.0, "action": "ADD", "entities": [{"name": "Fly.io", "type": "tool"}]},
  {"text": "Celery task routing broke on Fly Redis because Fly Redis uses a different connection pool model than Heroku Redis", "type": "rule", "importance": 8, "confidence": 1.0, "action": "ADD", "entities": [{"name": "Celery", "type": "tool"}, {"name": "Fly Redis", "type": "tool"}]}
]

Few-shot example (prose compaction summary):

Input:
User: Can you give me a quick summary of what we figured out?
Assistant: Sure. We went through the auth system debugging and landed on a few things. The root cause of the 401 errors was that our JWT tokens weren't being refreshed before expiry because the refresh handler had an off-by-one error in the expiry check. We fixed that and added a 30-second buffer to be safe. You also mentioned that going forward you want to use refresh tokens with sliding expiry rather than fixed expiry. One thing worth remembering for next time: the Flask-JWT-Extended library's default config is fixed expiry, and sliding requires explicit enablement via JWT_REFRESH_TOKEN_EXPIRES_DELTA. For now we're on fixed but should revisit after the Q2 security review.

Output:
[
  {"text": "JWT tokens were producing 401 errors due to an off-by-one error in the refresh handler's expiry check", "type": "episodic", "importance": 7, "confidence": 0.95, "action": "ADD", "entities": [{"name": "JWT", "type": "tool"}]},
  {"text": "Fixed the JWT refresh off-by-one bug and added a 30-second buffer to the expiry check", "type": "decision", "importance": 8, "confidence": 0.95, "action": "ADD", "entities": [{"name": "JWT", "type": "tool"}]},
  {"text": "User wants to move to refresh tokens with sliding expiry rather than fixed expiry going forward", "type": "preference", "importance": 7, "confidence": 0.9, "action": "ADD", "entities": [{"name": "JWT", "type": "tool"}]},
  {"text": "Flask-JWT-Extended defaults to fixed expiry; sliding expiry requires JWT_REFRESH_TOKEN_EXPIRES_DELTA to be set explicitly", "type": "rule", "importance": 8, "confidence": 1.0, "action": "ADD", "entities": [{"name": "Flask-JWT-Extended", "type": "tool"}]},
  {"text": "Revisit the JWT fixed-vs-sliding expiry decision after the Q2 security review", "type": "goal", "importance": 7, "confidence": 0.9, "action": "ADD", "entities": [{"name": "JWT", "type": "tool"}]}
]

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "...", "importance": N, "confidence": 0.9, "action": "ADD|UPDATE|DELETE|NOOP", "existingFactId": "...", "entities": [{"name": "PostgreSQL", "type": "tool", "role": "chosen database"}]}, ...]

If nothing is worth extracting, return: []`;

/**
 * Parse facts for compaction context (importance threshold 5 instead of 6).
 *
 * Identical to `parseFactsResponse` except the importance floor is 5 instead
 * of 6 — compaction is the last chance to capture context, so we accept
 * borderline facts that would normally be dropped.
 */
export function parseFactsResponseForCompaction(
  response: string,
  logger?: ExtractorLogger,
): ExtractedFact[] {
  const originalPreview = response.trim().slice(0, 200);
  let cleaned = response.trim();

  // Strip <think>...</think> and <thinking>...</thinking> tags
  cleaned = cleaned
    .replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '')
    .trim();

  // Strip markdown code fences if present
  if (cleaned.startsWith('```')) {
    cleaned = cleaned.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
  }

  const tryParse = (input: string): unknown => {
    try {
      return JSON.parse(input);
    } catch {
      return undefined;
    }
  };

  let parsed = tryParse(cleaned);
  let recoveryUsed: 'none' | 'bracket-scan' = 'none';
  if (parsed === undefined) {
    const match = cleaned.match(/\[[\s\S]*\]/);
    if (match) {
      parsed = tryParse(match[0]);
      if (parsed !== undefined) recoveryUsed = 'bracket-scan';
    }
  }

  if (parsed === undefined) {
    logger?.warn?.(
      `parseFactsResponseForCompaction: could not parse LLM output as JSON. Preview: ${JSON.stringify(
        originalPreview,
      )}`,
    );
    return [];
  }

  if (recoveryUsed === 'bracket-scan') {
    logger?.info?.(
      `parseFactsResponseForCompaction: recovered JSON array via bracket-scan fallback`,
    );
  }

  if (!Array.isArray(parsed)) {
    logger?.warn?.(
      `parseFactsResponseForCompaction: parsed value is not an array (type=${typeof parsed})`,
    );
    return [];
  }

  const facts = (parsed as unknown[])
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
        : 'ADD';

      let entities: ExtractedEntity[] | undefined;
      if (Array.isArray(fact.entities)) {
        const validEntities = fact.entities
          .map(parseEntity)
          .filter((e): e is ExtractedEntity => e !== null);
        if (validEntities.length > 0) entities = validEntities;
      }

      const result: ExtractedFact = {
        text: String(fact.text).slice(0, 512),
        type: (isValidMemoryType(fact.type) ? fact.type : 'fact') as MemoryType,
        importance: Math.max(1, Math.min(10, Number(fact.importance) || 5)),
        action,
        existingFactId: typeof fact.existingFactId === 'string' ? fact.existingFactId : undefined,
        confidence: normalizeConfidence(fact.confidence),
      };
      if (entities) result.entities = entities;
      return result;
    })
    .filter((f) => f.importance >= 5 || f.action === 'DELETE'); // Compaction: importance >= 5 (not 6)

  return facts;
}

/**
 * Extract facts using the compaction-aware prompt.
 *
 * This is called from the `before_compaction` hook — the LAST CHANCE to
 * capture knowledge before conversation context is lost. Key differences
 * from `extractFacts`:
 *   - Uses `COMPACTION_SYSTEM_PROMPT` (lower threshold, format-agnostic, more aggressive)
 *   - Always processes the full conversation (`mode: 'full'`)
 *   - Importance filter is >= 5 instead of >= 6
 *   - Lexical importance bumps still apply
 *
 * @param rawMessages - The messages array from the hook event (unknown[])
 * @param existingMemories - Optional list of existing memories for dedup context
 * @param logger - Optional logger for observability
 * @returns Array of extracted facts, or empty array on failure.
 */
export async function extractFactsForCompaction(
  rawMessages: unknown[],
  existingMemories?: Array<{ id: string; text: string }>,
  logger?: ExtractorLogger,
): Promise<ExtractedFact[]> {
  const config = resolveLLMConfig();
  if (!config) {
    logger?.info?.('extractFactsForCompaction: no LLM config resolved (skipping extraction)');
    return [];
  }

  // Parse messages
  const parsed = rawMessages
    .map(messageToText)
    .filter((m): m is { role: string; content: string } => m !== null);

  if (parsed.length === 0) {
    logger?.info?.(`extractFactsForCompaction: no parseable messages (raw count=${rawMessages.length})`);
    return [];
  }

  // Always full mode — process entire conversation for compaction
  const conversationText = truncateMessages(parsed, 12_000);

  if (conversationText.length < 20) {
    logger?.info?.(
      `extractFactsForCompaction: conversation too short (${conversationText.length} chars < 20)`,
    );
    return [];
  }

  // Build existing memories context if available
  let memoriesContext = '';
  if (existingMemories && existingMemories.length > 0) {
    const memoriesStr = existingMemories
      .map((m) => `[ID: ${m.id}] ${m.text}`)
      .join('\n');
    memoriesContext = `\n\nExisting memories (use these for dedup — classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n${memoriesStr}`;
  }

  const userPrompt = `Extract ALL valuable long-term memories from this conversation before it is compacted and lost:\n\n${conversationText}${memoriesContext}`;

  let response: string | null | undefined;
  try {
    response = await chatCompletion(config, [
      { role: 'system', content: COMPACTION_SYSTEM_PROMPT },
      { role: 'user', content: userPrompt },
    ]);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger?.warn?.(`extractFactsForCompaction: chatCompletion threw: ${msg}`);
    return [];
  }

  if (!response) {
    logger?.info?.('extractFactsForCompaction: chatCompletion returned null/empty response');
    return [];
  }

  logger?.info?.(
    `extractFactsForCompaction: LLM returned ${response.length} chars; handing to parseFactsResponseForCompaction`,
  );
  const facts = parseFactsResponseForCompaction(response, logger);

  // Lexical importance bumps (same as regular extraction)
  for (const f of facts) {
    const bump = computeLexicalImportanceBump(f.text, conversationText);
    if (bump > 0) {
      const oldImportance = f.importance;
      const effectiveBump = f.importance >= 8 ? Math.min(bump, 1) : bump;
      f.importance = Math.min(10, f.importance + effectiveBump);
      logger?.info?.(
        `extractFactsForCompaction: lexical bump +${bump} for "${f.text.slice(0, 60)}..." (${oldImportance} → ${f.importance})`,
      );
    }
  }

  return facts;
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
