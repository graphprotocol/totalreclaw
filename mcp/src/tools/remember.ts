import { TotalReclaw, FactMetadata, RerankedResult } from '@totalreclaw/client';
import {
  REMEMBER_TOOL_DESCRIPTION,
} from '../prompts.js';
import {
  findNearDuplicate,
  shouldSupersede,
  getStoreDedupThreshold,
  STORE_DEDUP_MAX_CANDIDATES,
  type DecryptedCandidate,
} from '../consolidation.js';
import { VALID_MEMORY_TYPES, type MemoryType } from '../memory-types.js';
import {
  VALID_MEMORY_TYPES_V1,
  VALID_MEMORY_SCOPES,
  type MemoryTypeV1,
  type MemoryScope,
  type MemorySource,
  LEGACY_TYPE_TO_V1,
  isValidMemoryTypeV1,
  isValidMemoryScope,
} from '../v1-types.js';

// ── Single-fact input (backward compat) ──────────────────────────────────────

export interface RememberInputSingle {
  fact: string;
  importance?: number;
  metadata?: {
    type?: string;
    expires_at?: string;
  };
}

// ── Batch-fact input (new) ───────────────────────────────────────────────────

export interface BatchFact {
  text: string;
  importance?: number;
  type?: MemoryType | MemoryTypeV1;
  /** v1 scope axis — life-domain tag. */
  scope?: MemoryScope;
  /** v1 reasoning field for decision-style claims. */
  reasoning?: string;
}

export interface RememberInputBatch {
  facts: BatchFact[];
}

// ── Union type for the handler ───────────────────────────────────────────────

export type RememberInput = RememberInputSingle | RememberInputBatch;

export interface RememberOutput {
  success: boolean;
  fact_id: string;
  was_duplicate: boolean;
  action: 'created' | 'updated' | 'skipped';
  superseded_id?: string;
}

export interface BatchRememberOutput {
  success: boolean;
  results: RememberOutput[];
  total: number;
  created: number;
  skipped: number;
  dedup_skipped?: number;
  dedup_superseded?: number;
}

// Union of legacy v0 types + v1 types. The tool accepts both so host agents
// migrating from older MCP versions keep working; the managed-service write
// path normalizes to v1 via LEGACY_TYPE_TO_V1 before encrypting.
const ACCEPTED_TYPE_ENUM = Array.from(
  new Set<string>([...VALID_MEMORY_TYPES, ...VALID_MEMORY_TYPES_V1]),
);

export const rememberToolDefinition = {
  name: 'totalreclaw_remember',
  description: REMEMBER_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      fact: {
        type: 'string',
        description: 'A single fact to remember (atomic, concise). Use this OR the facts array.',
      },
      facts: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            text: {
              type: 'string',
              description: 'The atomic fact text',
            },
            importance: {
              type: 'number',
              minimum: 1,
              maximum: 10,
              description: 'Importance score 1-10',
            },
            type: {
              type: 'string',
              enum: ACCEPTED_TYPE_ENUM,
              description:
                'Memory taxonomy v1 type. One of: claim, preference, directive, commitment, episode, summary. ' +
                'Boundary tests: claim = assertive state-of-world ("lives in Lisbon", "chose PostgreSQL"). ' +
                'preference = expressive taste ("likes dark mode", "prefers coffee over tea"). ' +
                'directive = imperative rule the user wants enforced ("always check d.get(errors)", "never rm -rf the state dir without stopping gateway"). ' +
                'commitment = future intent ("will ship v2 Friday", "planning to migrate to Postgres"). ' +
                'episode = notable past event ("deployed v1.0 on March 15", "had surgery last summer"). ' +
                'summary = session synthesis (only for debrief pipelines). ' +
                'Legacy v0 types (fact, context, decision, rule, goal, episodic) are still accepted and auto-mapped.',
            },
            scope: {
              type: 'string',
              enum: [...VALID_MEMORY_SCOPES],
              description:
                'Life-domain scope. One of: work, personal, health, family, creative, finance, misc, unspecified. ' +
                'Optional — defaults to "unspecified".',
            },
            reasoning: {
              type: 'string',
              description:
                'Optional "why" clause for decision-style claims. Populate when the user expresses a decision with rationale: text="Chose PostgreSQL for the analytics store", reasoning="data is relational and needs ACID guarantees".',
            },
          },
          required: ['text'],
        },
        description: 'Array of facts to store in a single call (preferred for multiple facts)',
      },
      importance: {
        type: 'number',
        minimum: 1,
        maximum: 10,
        default: 5,
        description: 'Importance score 1-10 (only for single-fact mode)',
      },
      scope: {
        type: 'string',
        enum: [...VALID_MEMORY_SCOPES],
        description:
          'Life-domain scope (single-fact mode). One of: work, personal, health, family, creative, finance, misc, unspecified.',
      },
      reasoning: {
        type: 'string',
        description:
          'Optional "why" clause for decision-style claims (single-fact mode). See facts[].reasoning.',
      },
      metadata: {
        type: 'object',
        properties: {
          type: {
            type: 'string',
            enum: ACCEPTED_TYPE_ENUM,
          },
          scope: {
            type: 'string',
            enum: [...VALID_MEMORY_SCOPES],
          },
          reasoning: { type: 'string' },
          expires_at: {
            type: 'string',
            description: 'ISO timestamp for time-limited facts',
          },
        },
      },
    },
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

/**
 * Normalize any accepted `type` value (v1 or legacy v0) to the v1 enum.
 *
 * Returns `'claim'` when the input is missing, unknown, or is a plain string.
 * The MCP host agent typically doesn't know whether it's on a v1 or v0 vault,
 * so we accept both and normalize here — the inner encrypted blob is always
 * written as v1 on the managed service path.
 */
export function normalizeTypeToV1(raw: unknown): MemoryTypeV1 {
  if (typeof raw !== 'string') return 'claim';
  const trimmed = raw.trim().toLowerCase();
  if (isValidMemoryTypeV1(trimmed)) return trimmed;
  if (trimmed in LEGACY_TYPE_TO_V1) return LEGACY_TYPE_TO_V1[trimmed];
  return 'claim';
}

/** Narrow an unknown arg into a {@link MemoryScope} (defaulting to unspecified). */
export function normalizeScope(raw: unknown): MemoryScope {
  if (isValidMemoryScope(raw)) return raw;
  return 'unspecified';
}

// Notify callback type for cache invalidation
export type OnRememberCallback = () => void;

let _onRememberCallback: OnRememberCallback | null = null;

export function setOnRememberCallback(cb: OnRememberCallback): void {
  _onRememberCallback = cb;
}

// Store-time dedup is always ON in v1.
// TOTALRECLAW_STORE_DEDUP user-facing toggle was removed.
const STORE_DEDUP_ENABLED = true;

// ── Internal: search for near-duplicates in HTTP mode ─────────────────────────

/**
 * Search for near-duplicates of a fact using the client's recall method.
 *
 * Uses the fact text as a search query to find similar existing facts,
 * then applies cosine similarity on their embeddings to find near-duplicates.
 *
 * Returns null on any failure (fail-open: store duplicate rather than lose fact).
 */
async function searchForNearDuplicatesHTTP(
  client: TotalReclaw,
  factText: string,
): Promise<{ match: DecryptedCandidate; similarity: number } | null> {
  try {
    // Use the fact text as a query to find similar facts
    const candidates = await client.recall(factText, STORE_DEDUP_MAX_CANDIDATES);

    if (candidates.length === 0) return null;

    // Convert RerankedResult[] to DecryptedCandidate[]
    const decryptedCandidates: DecryptedCandidate[] = candidates.map((r: RerankedResult) => ({
      id: r.fact.id,
      text: r.fact.text,
      embedding: r.fact.embedding && r.fact.embedding.length > 0 ? r.fact.embedding : null,
      importance: Math.round((r.fact.metadata.importance ?? 0.5) * 10),
      decayScore: r.decayAdjustedScore,
      createdAt: r.fact.createdAt.getTime(),
      version: 1,
    }));

    // Find the query embedding from the first candidate's cosine match
    // We need the query embedding for findNearDuplicate, but the client
    // doesn't expose it directly. Instead, we look for exact text matches
    // or very high cosine scores among the candidates.
    //
    // Since the client already does recall with embedding-based search,
    // the candidates are pre-filtered by relevance. We can compare
    // candidate embeddings against each other to find near-duplicates.
    //
    // However, findNearDuplicate needs the NEW fact's embedding. Since
    // we don't have direct access to the embedding generation in HTTP mode
    // (the client handles it internally), we use a different approach:
    // compare the candidates' embeddings against each other to check if
    // any returned candidate is a near-duplicate based on cosine similarity
    // with the query vector score from the reranker.
    //
    // Actually, the simplest approach: if vectorScore is very high (>= threshold),
    // it IS a near-duplicate of the query (which IS the fact text).
    const threshold = getStoreDedupThreshold();

    let bestMatch: { match: DecryptedCandidate; similarity: number } | null = null;

    for (let i = 0; i < candidates.length; i++) {
      const r = candidates[i];
      // vectorScore is the cosine similarity between query embedding and candidate embedding
      const similarity = r.vectorScore;
      if (similarity >= threshold) {
        if (!bestMatch || similarity > bestMatch.similarity) {
          bestMatch = { match: decryptedCandidates[i], similarity };
        }
      }
    }

    return bestMatch;
  } catch {
    // Fail-open: dedup failure should not prevent storing
    return null;
  }
}

// ── Internal: store a single fact with dedup ──────────────────────────────────

async function storeSingleFact(
  client: TotalReclaw,
  text: string,
  importance: number,
  factType: string | undefined,
  isExplicitRemember: boolean,
  expiresAt?: string,
  scope?: string,
  reasoning?: string,
): Promise<RememberOutput> {
  // Store-time dedup check (HTTP mode)
  let supersededId: string | undefined;
  let effectiveImportance = importance;

  if (STORE_DEDUP_ENABLED) {
    const dupResult = await searchForNearDuplicatesHTTP(client, text);

    if (dupResult) {
      if (isExplicitRemember) {
        // Explicit remember: always supersede (user explicitly wants this stored)
        effectiveImportance = Math.max(importance, dupResult.match.importance);
        supersededId = dupResult.match.id;
        try {
          await client.forget(dupResult.match.id);
          console.error(`Store-time dedup: superseded ${dupResult.match.id} (sim=${dupResult.similarity.toFixed(3)})`);
        } catch {
          console.error(`Store-time dedup: failed to delete superseded fact ${dupResult.match.id}`);
          supersededId = undefined;
        }
      } else {
        // Batch mode: apply shouldSupersede logic
        const action = shouldSupersede(importance, dupResult.match);
        if (action === 'skip') {
          console.error(`Store-time dedup: skipping "${text.slice(0, 60)}..." (sim=${dupResult.similarity.toFixed(3)})`);
          return {
            success: true,
            fact_id: '',
            was_duplicate: true,
            action: 'skipped',
          };
        }
        // action === 'supersede'
        effectiveImportance = Math.max(importance, dupResult.match.importance);
        supersededId = dupResult.match.id;
        try {
          await client.forget(dupResult.match.id);
          console.error(`Store-time dedup: superseded ${dupResult.match.id} (sim=${dupResult.similarity.toFixed(3)})`);
        } catch {
          console.error(`Store-time dedup: failed to delete superseded fact ${dupResult.match.id}`);
          supersededId = undefined;
        }
      }
    }
  }

  // v1 tool fields are forwarded via metadata.tags so the self-hosted path
  // retains enough info to surface them in recall (HTTP mode doesn't yet write
  // the v1 blob — managed-service path does; see handleRememberSubgraph).
  const tags: string[] = [];
  if (factType) tags.push(factType);
  if (scope && scope !== 'unspecified') tags.push(`scope:${scope}`);
  if (reasoning && reasoning.trim().length > 0) tags.push(`reasoning:${reasoning.trim().slice(0, 128)}`);

  const metadata: FactMetadata = {
    importance: effectiveImportance / 10,
    source: 'mcp_remember',
    tags,
  };

  if (expiresAt) {
    metadata.timestamp = new Date(expiresAt);
  }

  const factId = await client.remember(text.trim(), metadata);

  return {
    success: true,
    fact_id: factId,
    was_duplicate: !!supersededId,
    action: supersededId ? 'updated' : 'created',
    superseded_id: supersededId,
  };
}

// ── Handler ──────────────────────────────────────────────────────────────────

export async function handleRemember(
  client: TotalReclaw,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;

  // Determine if this is batch or single mode
  const isBatch = Array.isArray(input?.facts) && (input.facts as unknown[]).length > 0;
  const isSingle = typeof input?.fact === 'string';

  if (!isBatch && !isSingle) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Invalid input: provide either a "fact" string or a "facts" array',
        }),
      }],
    };
  }

  // ── Batch mode ─────────────────────────────────────────────────────────────
  if (isBatch) {
    const factsArray = input.facts as BatchFact[];
    const results: RememberOutput[] = [];
    let created = 0;
    let skipped = 0;
    let dedupSkipped = 0;
    let dedupSuperseded = 0;

    for (const f of factsArray) {
      if (!f.text || typeof f.text !== 'string' || f.text.trim().length === 0) {
        results.push({
          success: false,
          fact_id: '',
          was_duplicate: false,
          action: 'skipped',
        });
        skipped++;
        continue;
      }

      const imp = f.importance ?? 5;
      if (typeof imp !== 'number' || imp < 1 || imp > 10) {
        results.push({
          success: false,
          fact_id: '',
          was_duplicate: false,
          action: 'skipped',
        });
        skipped++;
        continue;
      }

      try {
        const result = await storeSingleFact(
          client,
          f.text,
          imp,
          f.type,
          false, // batch mode: not explicit remember
          undefined, // expiresAt (not in batch item)
          f.scope,
          f.reasoning,
        );

        if (result.action === 'skipped' && result.was_duplicate) {
          dedupSkipped++;
          skipped++;
        } else if (result.action === 'updated') {
          dedupSuperseded++;
          created++;
        } else {
          created++;
        }

        results.push(result);
      } catch (error) {
        results.push({
          success: false,
          fact_id: '',
          was_duplicate: false,
          action: 'skipped',
        });
        skipped++;
      }
    }

    const batchResult: BatchRememberOutput = {
      success: created > 0,
      results,
      total: factsArray.length,
      created,
      skipped,
      dedup_skipped: dedupSkipped,
      dedup_superseded: dedupSuperseded,
    };

    if (_onRememberCallback && created > 0) {
      _onRememberCallback();
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(batchResult),
      }],
    };
  }

  // ── Single-fact mode (backward compat) ─────────────────────────────────────
  const singleInput = input as unknown as RememberInputSingle;

  if (!singleInput.fact || typeof singleInput.fact !== 'string' || singleInput.fact.trim().length === 0) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          success: false,
          error: 'Invalid input: fact is required and must be a non-empty string',
        }),
      }],
    };
  }

  if (singleInput.importance !== undefined) {
    if (typeof singleInput.importance !== 'number' || singleInput.importance < 1 || singleInput.importance > 10) {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            success: false,
            error: 'Invalid input: importance must be a number between 1 and 10',
          }),
        }],
      };
    }
  }

  try {
    // Support both top-level and metadata-nested scope/reasoning for flexibility.
    const topLevelScope = typeof (input as Record<string, unknown>).scope === 'string'
      ? (input as Record<string, unknown>).scope as string
      : undefined;
    const topLevelReasoning = typeof (input as Record<string, unknown>).reasoning === 'string'
      ? (input as Record<string, unknown>).reasoning as string
      : undefined;
    const metaScope = typeof (singleInput.metadata as Record<string, unknown> | undefined)?.scope === 'string'
      ? ((singleInput.metadata as Record<string, unknown>).scope as string)
      : undefined;
    const metaReasoning = typeof (singleInput.metadata as Record<string, unknown> | undefined)?.reasoning === 'string'
      ? ((singleInput.metadata as Record<string, unknown>).reasoning as string)
      : undefined;

    const result = await storeSingleFact(
      client,
      singleInput.fact,
      singleInput.importance ?? 5,
      singleInput.metadata?.type,
      true, // single-fact mode: explicit remember, always supersede
      singleInput.metadata?.expires_at,
      topLevelScope ?? metaScope,
      topLevelReasoning ?? metaReasoning,
    );

    if (_onRememberCallback) {
      _onRememberCallback();
    }

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
          success: false,
          error: `Failed to store memory: ${message}`,
        }),
      }],
    };
  }
}
