/**
 * `totalreclaw_retype` — change the v1 type of an existing memory.
 *
 * Implementation pattern mirrors `tools/pin.ts`: fetch the original, decrypt,
 * rebuild a new canonical claim with the type override, submit on-chain as a
 * two-payload batch (tombstone old + write new with `superseded_by` link).
 *
 * Spec: `docs/specs/totalreclaw/memory-taxonomy-v1.md` §3-new-MCP-tools.
 */

import crypto from 'node:crypto';
import type { SubgraphSearchFact } from '../subgraph/search.js';
import { encodeFactProtobuf } from '../subgraph/store.js';
import {
  MEMORY_CLAIM_V1_SCHEMA_VERSION,
  VALID_MEMORY_TYPES_V1,
  type MemoryTypeV1,
  type MemorySource,
  type MemoryScope,
  type MemoryVolatility,
} from '../v1-types.js';
import { buildV1ClaimBlob } from '../claims-helper.js';

// ── Tool definition ──────────────────────────────────────────────────────────

const RETYPE_DESCRIPTION =
  'Reclassify memory to different v1 type.\n' +
  '\nINVOKE WHEN USER SAYS:\n' +
  '- "that was actually a directive, not a preference"\n' +
  '- "more of a decision than a goal" / "retype as claim"\n' +
  '- "file that under directives/commitments"\n' +
  '\nDOES: builds v1 claim with new_type, links via superseded_by, tombstones original. Idempotent.\n' +
  '\nWHEN NOT TO USE:\n' +
  '- text wrong → totalreclaw_forget + totalreclaw_remember\n' +
  '- unsure which memory → totalreclaw_recall first\n' +
  '\nnew_type: claim|preference|directive|commitment|episode|summary.';

export const retypeToolDefinition = {
  name: 'totalreclaw_retype',
  description: RETYPE_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      memory_id: {
        type: 'string',
        description: 'The ID of the memory to retype (from a prior totalreclaw_recall result).',
      },
      new_type: {
        type: 'string',
        enum: [...VALID_MEMORY_TYPES_V1],
        description:
          'New Memory Taxonomy v1 type. One of: claim, preference, directive, commitment, episode, summary.',
      },
    },
    required: ['memory_id', 'new_type'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

// ── Input validation ────────────────────────────────────────────────────────

interface ValidatedArgs {
  ok: boolean;
  memoryId: string;
  newType: MemoryTypeV1;
  error: string;
}

export function validateRetypeArgs(args: unknown): ValidatedArgs {
  if (!args || typeof args !== 'object') {
    return {
      ok: false,
      memoryId: '',
      newType: 'claim',
      error: 'Invalid input: memory_id and new_type are required',
    };
  }
  const record = args as Record<string, unknown>;
  const memoryId = record.memory_id;
  if (typeof memoryId !== 'string' || memoryId.trim().length === 0) {
    return {
      ok: false,
      memoryId: '',
      newType: 'claim',
      error: 'Invalid input: memory_id must be a non-empty string',
    };
  }
  const newType = record.new_type;
  if (
    typeof newType !== 'string' ||
    !(VALID_MEMORY_TYPES_V1 as readonly string[]).includes(newType)
  ) {
    return {
      ok: false,
      memoryId: memoryId.trim(),
      newType: 'claim',
      error: `Invalid input: new_type must be one of ${VALID_MEMORY_TYPES_V1.join(', ')}`,
    };
  }
  return {
    ok: true,
    memoryId: memoryId.trim(),
    newType: newType as MemoryTypeV1,
    error: '',
  };
}

// ── Operation pattern ────────────────────────────────────────────────────────
//
// Rebuilds the blob with a v1 override.

export interface MetadataOpDeps {
  owner: string;
  sourceAgent: string;
  fetchFactById: (factId: string) => Promise<SubgraphSearchFact | null>;
  decryptBlob: (hexEncryptedBlob: string) => string;
  encryptBlob: (plaintext: string) => string; // returns hex
  submitBatch: (protobufPayloads: Buffer[]) => Promise<{ txHash: string; success: boolean }>;
  generateIndices: (text: string, entityNames: string[]) => Promise<{
    blindIndices: string[];
    encryptedEmbedding?: string;
  }>;
  /**
   * Optional read-after-write hook. Called after submitBatch returns success;
   * polls the subgraph for the new fact id. Returning `false` causes the op
   * result to surface `partial: true`. When this dep is undefined the op
   * skips the wait entirely.
   */
  confirmIndexed?: (
    factId: string,
    expect?: 'active' | 'inactive',
  ) => Promise<boolean>;
}

export interface MetadataOpResult {
  success: boolean;
  memory_id: string;
  new_memory_id?: string;
  previous_value?: string;
  new_value?: string;
  idempotent?: boolean;
  tx_hash?: string;
  error?: string;
  /**
   * Set when the chain write succeeded but the subgraph indexer didn't
   * confirm the new fact id within the timeout window. The mutation IS
   * on-chain — `tx_hash` is observable on the explorer — but a follow-up
   * recall/export may briefly surface stale state until the indexer catches
   * up. See `subgraph/confirm-indexed.ts`.
   */
  partial?: boolean;
}

/**
 * Parse a decrypted blob into a MemoryClaimV1-shaped object. Handles three
 * input formats:
 *
 * 1. v1 canonical JSON (`{schema_version: "1.0", text, type, source, ...}`)
 * 2. v0 short-key canonical JSON (`{t, c, i, sa, ea, ...}`) — legacy plugin
 * 3. v0 plugin-legacy (`{text, metadata: {type, importance}}`)
 *
 * Returns a v1 shape that callers can mutate then pass to `buildV1ClaimBlob`.
 * The caller is responsible for overriding the specific field being updated.
 */
export function extractV1Fields(decrypted: string): {
  text: string;
  type: MemoryTypeV1;
  source: MemorySource;
  scope?: MemoryScope;
  volatility?: MemoryVolatility;
  reasoning?: string;
  importance?: number;
  confidence?: number;
  createdAt?: string;
} {
  try {
    const obj = JSON.parse(decrypted) as Record<string, unknown>;

    // v1 canonical path — presence of top-level text+type (closed enum) is
    // the signature; `schema_version` is omitted when equal to the default
    // (Rust `skip_serializing_if`), so we don't require it.
    if (
      typeof obj.text === 'string' &&
      typeof obj.type === 'string' &&
      (VALID_MEMORY_TYPES_V1 as readonly string[]).includes(String(obj.type)) &&
      (typeof obj.schema_version !== 'string' ||
        obj.schema_version === MEMORY_CLAIM_V1_SCHEMA_VERSION)
    ) {
      return {
        text: String(obj.text),
        type: (VALID_MEMORY_TYPES_V1 as readonly string[]).includes(String(obj.type))
          ? (obj.type as MemoryTypeV1)
          : 'claim',
        source: typeof obj.source === 'string' ? (obj.source as MemorySource) : 'user-inferred',
        scope: typeof obj.scope === 'string' ? (obj.scope as MemoryScope) : undefined,
        volatility:
          typeof obj.volatility === 'string' ? (obj.volatility as MemoryVolatility) : undefined,
        reasoning: typeof obj.reasoning === 'string' ? obj.reasoning : undefined,
        importance: typeof obj.importance === 'number' ? obj.importance : undefined,
        confidence: typeof obj.confidence === 'number' ? obj.confidence : undefined,
        createdAt: typeof obj.created_at === 'string' ? obj.created_at : undefined,
      };
    }

    // v0 short-key canonical path — map `c` short-key to v1 type, default source user-inferred
    if (typeof obj.t === 'string') {
      const cMap: Record<string, MemoryTypeV1> = {
        fact: 'claim',
        claim: 'claim',
        ctx: 'claim',
        dec: 'claim',
        pref: 'preference',
        rule: 'directive',
        goal: 'commitment',
        epi: 'episode',
        sum: 'summary',
      };
      return {
        text: String(obj.t),
        type: cMap[String(obj.c)] ?? 'claim',
        source: 'user-inferred',
        importance:
          typeof obj.i === 'number' ? Math.max(1, Math.min(10, Math.round(obj.i))) : undefined,
      };
    }

    // v0 plugin-legacy path
    if (typeof obj.text === 'string') {
      const meta = (obj.metadata as Record<string, unknown>) ?? {};
      const typeLegacyToV1: Record<string, MemoryTypeV1> = {
        fact: 'claim',
        context: 'claim',
        decision: 'claim',
        preference: 'preference',
        rule: 'directive',
        goal: 'commitment',
        episodic: 'episode',
        summary: 'summary',
      };
      const legacyType = typeof meta.type === 'string' ? meta.type : 'fact';
      const impFloat = typeof meta.importance === 'number' ? meta.importance : 0.5;
      return {
        text: String(obj.text),
        type: typeLegacyToV1[legacyType] ?? 'claim',
        source: 'user-inferred',
        importance: Math.max(1, Math.min(10, Math.round(impFloat * 10))),
      };
    }
  } catch {
    // fall through
  }

  // Raw-text fallback
  return {
    text: decrypted,
    type: 'claim',
    source: 'user-inferred',
  };
}

/**
 * Generic supersede operation. Used by retype + set_scope. Builds a fresh v1
 * blob with the caller's field override and submits a two-payload batch
 * (tombstone old + new fact with `superseded_by: old_id`).
 *
 * `buildOverride` produces the v1 claim input given the extracted original.
 * Returns `{ previousValue, newValue }` so the result can surface a readable
 * diff to the tool response.
 */
export async function executeMetadataOp<T>(
  memoryId: string,
  deps: MetadataOpDeps,
  readCurrent: (extracted: ReturnType<typeof extractV1Fields>) => T,
  isNoop: (current: T, next: T) => boolean,
  buildOverride: (
    extracted: ReturnType<typeof extractV1Fields>,
    nextValue: T,
  ) => Parameters<typeof buildV1ClaimBlob>[0],
  nextValue: T,
  opName: string,
): Promise<MetadataOpResult> {
  // 1. Fetch the original fact
  const existing = await deps.fetchFactById(memoryId);
  if (!existing) {
    return {
      success: false,
      memory_id: memoryId,
      error: `Memory not found: ${memoryId}`,
    };
  }

  // 2. Decrypt
  const blobHex = existing.encryptedBlob.startsWith('0x')
    ? existing.encryptedBlob.slice(2)
    : existing.encryptedBlob;
  let plaintext: string;
  try {
    plaintext = deps.decryptBlob(blobHex);
  } catch (err) {
    return {
      success: false,
      memory_id: memoryId,
      error: `Failed to decrypt memory: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  const extracted = extractV1Fields(plaintext);
  const current = readCurrent(extracted);

  // 3. Idempotent early-exit
  if (isNoop(current, nextValue)) {
    return {
      success: true,
      memory_id: memoryId,
      previous_value: String(current),
      new_value: String(nextValue),
      idempotent: true,
    };
  }

  // 4. Build new v1 blob with the override
  let newBlobPlain: string;
  try {
    const input = buildOverride(extracted, nextValue);
    newBlobPlain = buildV1ClaimBlob(input);
  } catch (err) {
    return {
      success: false,
      memory_id: memoryId,
      error: `Failed to build updated claim: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  // 5. Encrypt
  let newBlobHex: string;
  try {
    newBlobHex = deps.encryptBlob(newBlobPlain);
  } catch (err) {
    return {
      success: false,
      memory_id: memoryId,
      error: `Failed to encrypt updated claim: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  // 6. Regenerate trapdoors (text didn't change but we want fresh LSH for the
  //    new fact id so it's findable in recall).
  let trapdoors: { blindIndices: string[]; encryptedEmbedding?: string };
  try {
    trapdoors = await deps.generateIndices(extracted.text, []);
  } catch {
    trapdoors = { blindIndices: [] };
  }

  // 7. Build tombstone + new payloads
  const tombstonePayload: FactPayloadMinimal = {
    id: memoryId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: Buffer.from('tombstone').toString('hex'),
    blindIndices: [],
    decayScore: 0,
    source: `mcp_${opName}`,
    contentFp: '',
    agentId: deps.sourceAgent,
  };

  const newFactId = crypto.randomUUID();
  const newPayload: FactPayloadMinimal = {
    id: newFactId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: newBlobHex,
    blindIndices: trapdoors.blindIndices,
    decayScore: 0.85, // Retype/scope updates land as active with healthy decay
    source: `mcp_${opName}`,
    contentFp: '',
    agentId: deps.sourceAgent,
    encryptedEmbedding: trapdoors.encryptedEmbedding,
  };

  const payloads = [encodeFactProtobuf(tombstonePayload), encodeFactProtobuf(newPayload)];

  // 8. Submit
  try {
    const { txHash, success } = await deps.submitBatch(payloads);
    if (!success) {
      return {
        success: false,
        memory_id: memoryId,
        previous_value: String(current),
        error: 'On-chain batch submission failed',
        tx_hash: txHash,
      };
    }
    // 9. Read-after-write — wait until the new fact id is indexed and
    //    active, OR the host's timeout elapses. On timeout, return success
    //    with `partial: true` so the agent surfaces "chain write submitted,
    //    indexer still propagating" instead of claiming the operation is
    //    fully visible.
    let indexed = true;
    if (deps.confirmIndexed) {
      try {
        indexed = await deps.confirmIndexed(newFactId, 'active');
      } catch {
        // Defensive: a confirm helper that throws does not invalidate the
        // chain write — fall through with `partial: true`.
        indexed = false;
      }
    }
    return {
      success: true,
      memory_id: memoryId,
      new_memory_id: newFactId,
      previous_value: String(current),
      new_value: String(nextValue),
      tx_hash: txHash,
      ...(indexed ? {} : { partial: true }),
    };
  } catch (err) {
    return {
      success: false,
      memory_id: memoryId,
      previous_value: String(current),
      error: `Failed to submit batch: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

interface FactPayloadMinimal {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string;
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
  encryptedEmbedding?: string;
}

// ── Public entry point ───────────────────────────────────────────────────────

/**
 * Execute the retype operation: build a new v1 claim with `new_type` and
 * chain it over the original via `superseded_by`.
 */
export async function executeRetype(
  memoryId: string,
  newType: MemoryTypeV1,
  deps: MetadataOpDeps,
): Promise<MetadataOpResult> {
  return executeMetadataOp<MemoryTypeV1>(
    memoryId,
    deps,
    (e) => e.type,
    (cur, next) => cur === next,
    (e, next) => ({
      text: e.text,
      type: next,
      source: e.source,
      scope: e.scope,
      volatility: e.volatility,
      reasoning: e.reasoning,
      importance: e.importance,
      confidence: e.confidence,
      createdAt: e.createdAt,
      supersededBy: memoryId,
    }),
    newType,
    'retype',
  );
}

// ── Handler wrappers ─────────────────────────────────────────────────────────

export async function handleRetype(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validateRetypeArgs(args);
  if (!validation.ok) {
    return errorResponse(validation.error);
  }
  // HTTP (self-hosted) mode: not yet supported — same policy as pin/unpin.
  return errorResponse(
    'Retype is only supported with the managed service. Self-hosted mode does not yet implement v1 supersession.',
  );
}

export async function handleRetypeWithDeps(
  args: unknown,
  deps: MetadataOpDeps,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validateRetypeArgs(args);
  if (!validation.ok) return errorResponse(validation.error);
  const result = await executeRetype(validation.memoryId, validation.newType, deps);
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

function errorResponse(error: string): { content: Array<{ type: string; text: string }> } {
  return { content: [{ type: 'text', text: JSON.stringify({ success: false, error }) }] };
}
