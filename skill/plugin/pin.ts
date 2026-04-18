/** Pin/unpin pure operation for OpenClaw plugin — Slice 2e-plugin, Phase 2. */

import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { mapTypeToCategory } from './claims-helper.js';
import {
  findLoserClaimInDecisionLog,
  maybeWriteFeedbackForPin,
  type ContradictionLogger,
} from './contradiction-sync.js';
import { isValidMemoryType } from './extractor.js';
import type { SubgraphSearchFact } from './subgraph-search.js';

// Lazy-load WASM core (mirrors claims-helper.ts pattern — plays nicely under
// both the OpenClaw runtime (CJS-ish tsx) and bare Node ESM used by tests).
const requireWasm = createRequire(import.meta.url);
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) _wasm = requireWasm('@totalreclaw/core');
  return _wasm!;
}

/** Minimal FactPayload shape — kept local so pin.ts doesn't pull in subgraph-store.ts (which uses plain require() and breaks ESM tests). */
export interface FactPayload {
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

/** Encode a FactPayload as the minimal Protobuf wire format via WASM core. */
function encodeFactProtobufLocal(fact: FactPayload): Buffer {
  const json = JSON.stringify({
    id: fact.id,
    timestamp: fact.timestamp,
    owner: fact.owner,
    encrypted_blob_hex: fact.encryptedBlob,
    blind_indices: fact.blindIndices,
    decay_score: fact.decayScore,
    source: fact.source,
    content_fp: fact.contentFp,
    agent_id: fact.agentId,
    encrypted_embedding: fact.encryptedEmbedding || null,
  });
  return Buffer.from(getWasm().encodeFactProtobuf(json));
}

// ─── Status types ─────────────────────────────────────────────────────────────

export type HumanStatus = 'active' | 'pinned' | 'superseded' | 'retracted' | 'contradicted';

const SHORT_TO_HUMAN: Record<string, HumanStatus> = {
  a: 'active',
  p: 'pinned',
  s: 'superseded',
  r: 'retracted',
  c: 'contradicted',
};

const HUMAN_TO_SHORT: Record<HumanStatus, string> = {
  active: 'a',
  pinned: 'p',
  superseded: 's',
  retracted: 'r',
  contradicted: 'c',
};

// ─── Blob parsing ─────────────────────────────────────────────────────────────

/** Result of parsing a decrypted blob for pin/unpin mutation. */
export interface ParsedBlob {
  claim: Record<string, unknown>;
  currentStatus: HumanStatus;
  isLegacy: boolean;
}

/** Parse a decrypted blob into a canonical mutable Claim + current human status. */
export function parseBlobForPin(decrypted: string): ParsedBlob {
  let obj: Record<string, unknown>;
  try {
    obj = JSON.parse(decrypted) as Record<string, unknown>;
  } catch {
    return {
      claim: buildCanonicalObjectFromLegacy(decrypted, {}),
      currentStatus: 'active',
      isLegacy: true,
    };
  }

  // v1 payload (plugin v3.0.0+): long-form fields + schema_version "1.x".
  // Convert to the short-key shape pin.ts operates on so the rest of the
  // pipeline (st, sup, trapdoor regeneration) keeps working unchanged.
  if (
    typeof obj.text === 'string' &&
    typeof obj.type === 'string' &&
    typeof obj.schema_version === 'string' &&
    obj.schema_version.startsWith('1.')
  ) {
    const shortObj = v1ToShortKeyClaim(obj);
    const st = typeof shortObj.st === 'string' ? shortObj.st : 'a';
    const human = SHORT_TO_HUMAN[st] ?? 'active';
    return { claim: shortObj, currentStatus: human, isLegacy: false };
  }

  // v0 canonical Claim — short keys present.
  if (typeof obj.t === 'string' && typeof obj.c === 'string') {
    const st = typeof obj.st === 'string' ? obj.st : 'a';
    const human = SHORT_TO_HUMAN[st] ?? 'active';
    const cloned = JSON.parse(JSON.stringify(obj)) as Record<string, unknown>;
    return { claim: cloned, currentStatus: human, isLegacy: false };
  }

  // Legacy {text, metadata: {importance: 0-1}} shape.
  if (typeof obj.text === 'string') {
    const meta = (obj.metadata as Record<string, unknown>) ?? {};
    return {
      claim: buildCanonicalObjectFromLegacy(obj.text, meta),
      currentStatus: 'active',
      isLegacy: true,
    };
  }

  return {
    claim: buildCanonicalObjectFromLegacy(decrypted, {}),
    currentStatus: 'active',
    isLegacy: true,
  };
}

/**
 * Convert a Memory Taxonomy v1 blob object into the short-key shape that
 * the rest of pin.ts manipulates. Pin operations tombstone the existing
 * fact and write a fresh one with the short-key format; the v1 inner blob
 * is not round-tripped through pin (that would require upgrading every
 * downstream read site). Since pin already rewrites the fact with new
 * indices, round-trip fidelity isn't required.
 */
function v1ToShortKeyClaim(v1: Record<string, unknown>): Record<string, unknown> {
  const text = typeof v1.text === 'string' ? v1.text : '';
  const type = typeof v1.type === 'string' ? v1.type : 'claim';
  // Map v1 type to the short category key used by the v0 format.
  const category = isValidMemoryType(type) ? mapTypeToCategory(type) : 'fact';
  const impNum = typeof v1.importance === 'number' ? v1.importance : 5;
  const importance = Math.max(1, Math.min(10, Math.round(impNum)));
  const confidence = typeof v1.confidence === 'number' ? v1.confidence : 0.85;
  const source = typeof v1.source === 'string' ? v1.source : 'openclaw-plugin';
  const createdAt = typeof v1.created_at === 'string' ? v1.created_at : new Date().toISOString();

  const out: Record<string, unknown> = {
    t: text,
    c: category,
    cf: confidence,
    i: importance,
    sa: source,
    ea: createdAt,
  };

  if (Array.isArray(v1.entities) && v1.entities.length > 0) {
    out.e = (v1.entities as unknown[])
      .map((e) => {
        if (!e || typeof e !== 'object') return null;
        const entity = e as Record<string, unknown>;
        const name = typeof entity.name === 'string' ? entity.name : '';
        const entType = typeof entity.type === 'string' ? entity.type : 'concept';
        if (!name) return null;
        const short: Record<string, unknown> = { n: name, tp: entType };
        if (typeof entity.role === 'string' && entity.role.length > 0) {
          short.r = entity.role;
        }
        return short;
      })
      .filter((e): e is Record<string, unknown> => e !== null);
  }

  return out;
}

function buildCanonicalObjectFromLegacy(
  text: string,
  meta: Record<string, unknown>,
): Record<string, unknown> {
  // Phase 2.2.6: use the single-source-of-truth mapping from claims-helper
  // instead of a local duplicate. Legacy blobs can carry arbitrary strings in
  // `metadata.type`, so we validate via `isValidMemoryType` before mapping —
  // unknown types fall back to 'fact'.
  const typeStr = typeof meta.type === 'string' ? meta.type : 'fact';
  const category = isValidMemoryType(typeStr) ? mapTypeToCategory(typeStr) : 'fact';
  const impFloat = typeof meta.importance === 'number' ? meta.importance : 0.5;
  const importance = Math.max(1, Math.min(10, Math.round(impFloat * 10)));
  const source = typeof meta.source === 'string' ? meta.source : 'openclaw-plugin';
  const createdAt = typeof meta.created_at === 'string' ? meta.created_at : new Date().toISOString();
  return {
    t: text,
    c: category,
    cf: 0.85,
    i: importance,
    sa: source,
    ea: createdAt,
  };
}

// ─── Pure core: executePinOperation ───────────────────────────────────────────

/** Injected dependencies for the pin operation (transport + crypto + indexing). */
export interface PinOpDeps {
  owner: string;
  sourceAgent: string;
  fetchFactById: (factId: string) => Promise<SubgraphSearchFact | null>;
  decryptBlob: (hexEncryptedBlob: string) => string;
  encryptBlob: (plaintext: string) => string; // returns hex
  submitBatch: (protobufPayloads: Buffer[]) => Promise<{ txHash: string; success: boolean }>;
  /**
   * Regenerate blind indices + encrypted embedding for the pinned claim text.
   * The new fact must have its own trapdoors pointing to its content so
   * trapdoor-based recall keeps finding it after the old fact is tombstoned.
   * Returns empty indices / undefined embedding on failure — caller tolerates.
   */
  generateIndices: (text: string, entityNames: string[]) => Promise<{
    blindIndices: string[];
    encryptedEmbedding?: string;
  }>;
  /**
   * Optional logger used by the Slice 2f feedback wiring. When omitted, a
   * silent logger is used so existing callers remain unchanged.
   */
  logger?: ContradictionLogger;
}

export interface PinOpResult {
  success: boolean;
  fact_id: string;
  new_fact_id?: string;
  previous_status?: HumanStatus;
  new_status?: HumanStatus;
  idempotent?: boolean;
  tx_hash?: string;
  reason?: string;
  error?: string;
}

/**
 * Execute a pin or unpin operation on a single fact.
 *
 * The subgraph is append-only, so a status change requires writing a new fact
 * with the updated status and tombstoning the old one. The new fact's `sup`
 * field points to the old fact id, forming a cross-device-visible supersession
 * chain. Matches MCP's `executePinOperation` byte-for-byte on the supersession
 * semantics (short keys, idempotent no-op, decayScore=1.0, trapdoor regen).
 */
export async function executePinOperation(
  factId: string,
  targetStatus: 'pinned' | 'active',
  deps: PinOpDeps,
  reason?: string,
): Promise<PinOpResult> {
  // 1. Fetch the existing fact
  const existing = await deps.fetchFactById(factId);
  if (!existing) {
    return {
      success: false,
      fact_id: factId,
      error: `Fact not found: ${factId}`,
    };
  }

  // 2. Decrypt + parse current status
  const blobHex = existing.encryptedBlob.startsWith('0x')
    ? existing.encryptedBlob.slice(2)
    : existing.encryptedBlob;
  let plaintext: string;
  let recoveredFromDecisionLog = false;
  try {
    plaintext = deps.decryptBlob(blobHex);
  } catch (err) {
    // Phase 2.1 recovery path: if the on-chain blob is a tombstone (1-byte
    // `0x00` written by an auto-resolved supersede), the cipher will fail
    // because the ciphertext is shorter than the auth tag. Fall back to the
    // canonical Claim JSON we stashed in `decisions.jsonl` at supersede time.
    // Without this fallback, the user can never override an auto-resolution
    // and the weight-tuning loop never receives gradient signal.
    const errMsg = err instanceof Error ? err.message : String(err);
    const looksLikeTombstone =
      blobHex === '00' ||
      blobHex === '' ||
      errMsg.includes('Encrypted data too short') ||
      errMsg.includes('too short') ||
      errMsg.includes('Cipher');
    if (!looksLikeTombstone) {
      return {
        success: false,
        fact_id: factId,
        error: `Failed to decrypt fact: ${errMsg}`,
      };
    }
    const recovered = findLoserClaimInDecisionLog(factId);
    if (!recovered) {
      return {
        success: false,
        fact_id: factId,
        error:
          `Failed to decrypt fact and no recovery row in decisions.jsonl: ${errMsg}. ` +
          'The fact may have been tombstoned by an auto-resolution that predates Phase 2.1 ' +
          '(when loser_claim_json was added to the decision log).',
      };
    }
    plaintext = recovered;
    recoveredFromDecisionLog = true;
    deps.logger?.info?.(
      `pin: recovered loser claim from decisions.jsonl for ${factId.slice(0, 10)}…`,
    );
  }

  const parsed = parseBlobForPin(plaintext);
  // Recovered claims always represent a fact the user is trying to override —
  // never short-circuit the operation as idempotent because the `st` field on
  // the recovered loser was whatever the original auto-resolution stored
  // (typically active). Drop the previous status so the targetStatus check
  // below produces a real on-chain write.
  if (recoveredFromDecisionLog) {
    parsed.currentStatus = 'active';
  }

  // 3. Idempotent early-exit
  if (parsed.currentStatus === targetStatus) {
    return {
      success: true,
      fact_id: factId,
      previous_status: parsed.currentStatus,
      new_status: targetStatus,
      idempotent: true,
      reason,
    };
  }

  // 4. Build the new canonical claim with updated status + supersedes link
  const newClaimObj = { ...parsed.claim };
  if (targetStatus === 'active') {
    // Active is the canonical default — omit `st` entirely.
    delete newClaimObj.st;
  } else {
    newClaimObj.st = HUMAN_TO_SHORT[targetStatus];
  }
  newClaimObj.sup = factId;
  // Refresh extraction timestamp so downstream consumers can tell this is a new event.
  newClaimObj.ea = new Date().toISOString();
  // Carry source agent forward if present, else stamp it.
  if (typeof newClaimObj.sa !== 'string' || newClaimObj.sa.length === 0) {
    newClaimObj.sa = deps.sourceAgent;
  }

  let canonicalJson: string;
  try {
    canonicalJson = getWasm().canonicalizeClaim(JSON.stringify(newClaimObj));
  } catch (err) {
    return {
      success: false,
      fact_id: factId,
      error: `Failed to canonicalize updated claim: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  // 5. Encrypt the new blob
  let newBlobHex: string;
  try {
    newBlobHex = deps.encryptBlob(canonicalJson);
  } catch (err) {
    return {
      success: false,
      fact_id: factId,
      error: `Failed to encrypt updated claim: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  // 5b. Regenerate trapdoors so the new fact is findable by the same text.
  const newClaimText = typeof parsed.claim.t === 'string' ? parsed.claim.t : '';
  const entityNames: string[] = Array.isArray(parsed.claim.e)
    ? (parsed.claim.e as unknown[])
        .map((e) => (e && typeof (e as { n?: unknown }).n === 'string' ? (e as { n: string }).n : ''))
        .filter((n): n is string => n.length > 0)
    : [];
  let regenerated: { blindIndices: string[]; encryptedEmbedding?: string };
  try {
    regenerated = await deps.generateIndices(newClaimText, entityNames);
  } catch {
    regenerated = { blindIndices: [] };
  }

  // 6. Build tombstone + new protobuf payloads.
  // Plugin tombstone convention matches the rest of the plugin: `encryptedBlob: '00'`,
  // empty indices, decayScore=0, source='tombstone'.
  const tombstonePayload: FactPayload = {
    id: factId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: '00',
    blindIndices: [],
    decayScore: 0,
    source: 'tombstone',
    contentFp: '',
    agentId: deps.sourceAgent,
  };

  const newFactId = crypto.randomUUID();
  const newPayload: FactPayload = {
    id: newFactId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: newBlobHex,
    blindIndices: regenerated.blindIndices,
    decayScore: 1.0,
    source: targetStatus === 'pinned' ? 'openclaw-plugin-pin' : 'openclaw-plugin-unpin',
    contentFp: '',
    agentId: deps.sourceAgent,
    encryptedEmbedding: regenerated.encryptedEmbedding,
  };

  const payloads = [encodeFactProtobufLocal(tombstonePayload), encodeFactProtobufLocal(newPayload)];

  // 6b. Slice 2f: consult decisions.jsonl to see if this pin/unpin contradicts
  // a prior auto-resolution. If so, append a counterexample to feedback.jsonl
  // so the next digest-compile's tuning loop can nudge the weights. Voluntary
  // pins (no matching decision) produce no feedback row. Never fatal.
  const feedbackLogger: ContradictionLogger = deps.logger ?? {
    info: () => {},
    warn: () => {},
  };
  try {
    await maybeWriteFeedbackForPin(
      factId,
      targetStatus,
      Math.floor(Date.now() / 1000),
      feedbackLogger,
    );
  } catch {
    // Feedback wiring is best-effort — never block the pin op.
  }

  // 7. Submit both in a single batch UserOp.
  try {
    const { txHash, success } = await deps.submitBatch(payloads);
    if (!success) {
      return {
        success: false,
        fact_id: factId,
        previous_status: parsed.currentStatus,
        error: 'On-chain batch submission failed',
        tx_hash: txHash,
      };
    }
    return {
      success: true,
      fact_id: factId,
      new_fact_id: newFactId,
      previous_status: parsed.currentStatus,
      new_status: targetStatus,
      tx_hash: txHash,
      reason,
    };
  } catch (err) {
    return {
      success: false,
      fact_id: factId,
      previous_status: parsed.currentStatus,
      error: `Failed to submit pin batch: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

// ─── Input validation ─────────────────────────────────────────────────────────

export interface PinArgsValid {
  ok: boolean;
  factId: string;
  reason?: string;
  error: string;
}

/** Validate the `{fact_id, reason?}` input shape for pin/unpin tool calls. */
export function validatePinArgs(args: unknown): PinArgsValid {
  if (!args || typeof args !== 'object') {
    return { ok: false, factId: '', error: 'Invalid input: fact_id is required' };
  }
  const record = args as Record<string, unknown>;
  const factId = record.fact_id;
  if (factId === undefined || factId === null) {
    return { ok: false, factId: '', error: 'Invalid input: fact_id is required' };
  }
  if (typeof factId !== 'string') {
    return { ok: false, factId: '', error: 'Invalid input: fact_id must be a non-empty string' };
  }
  if (factId.trim().length === 0) {
    return { ok: false, factId: '', error: 'Invalid input: fact_id must be a non-empty string' };
  }
  const reason = typeof record.reason === 'string' ? record.reason : undefined;
  return { ok: true, factId: factId.trim(), reason, error: '' };
}
