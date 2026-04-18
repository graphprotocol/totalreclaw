/** Pin/unpin tools for TotalReclaw MCP server — Slice 2e-mcp, Phase 2. */

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import type { SubgraphSearchFact } from '../subgraph/search.js';
import { encodeFactProtobuf } from '../subgraph/store.js';

// Lazy-load WASM core (same pattern as claims-helper.ts)
// eslint-disable-next-line @typescript-eslint/no-var-requires
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    _wasm = require('@totalreclaw/core');
  }
  return _wasm!;
}

// ─── Slice 2f: feedback-log wiring ────────────────────────────────────────────
//
// The MCP server is one of three pin-tool implementations; all three append
// feedback entries to the SAME on-disk log (`~/.totalreclaw/feedback.jsonl`).
// Unlike the OpenClaw plugin, MCP does NOT run the weight-tuning loop — it has
// no digest-compile lifecycle hook and no LLM to compile against. A user who
// pins exclusively through Claude Desktop (MCP-only) will write feedback rows
// but those rows are only consumed when the same user next runs the plugin or
// Hermes against the same vault. Known gap — documented in CLAUDE.md.

/** Per-component score breakdown, mirroring Rust `ScoreComponents`. */
export interface ScoreComponents {
  confidence: number;
  corroboration: number;
  recency: number;
  validation: number;
  weighted_total: number;
}

/** Row format for `decisions.jsonl`, matching the plugin's shape. */
export interface DecisionLogEntry {
  ts: number;
  entity_id: string;
  new_claim_id: string;
  existing_claim_id: string;
  similarity: number;
  action: 'supersede_existing' | 'skip_new' | 'shadow';
  reason?: 'existing_pinned' | 'existing_wins' | 'new_wins';
  winner_score?: number;
  loser_score?: number;
  winner_components?: ScoreComponents;
  loser_components?: ScoreComponents;
  mode?: string;
}

/** A feedback log entry mirroring Rust `FeedbackEntry`. */
export interface FeedbackEntry {
  ts: number;
  claim_a_id: string;
  claim_b_id: string;
  formula_winner: 'a' | 'b';
  user_decision: 'pin_a' | 'pin_b' | 'pin_both' | 'unpin';
  winner_components: ScoreComponents;
  loser_components: ScoreComponents;
}

function resolveStateDir(): string {
  const override = process.env.TOTALRECLAW_STATE_DIR;
  if (override && override.length > 0) return override;
  return path.join(os.homedir(), '.totalreclaw');
}

function ensureStateDir(): string {
  const dir = resolveStateDir();
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* best effort */ }
  return dir;
}

export function decisionsLogPath(): string {
  return path.join(resolveStateDir(), 'decisions.jsonl');
}

export function feedbackLogPath(): string {
  return path.join(resolveStateDir(), 'feedback.jsonl');
}

/** Walk decisions.jsonl in reverse, find most recent supersede matching factId + role. */
export function findDecisionForPin(
  factId: string,
  role: 'loser' | 'winner',
  logContent: string,
): DecisionLogEntry | null {
  if (!logContent || logContent.length === 0) return null;
  const lines = logContent.split('\n').filter((l) => l.length > 0);
  for (let i = lines.length - 1; i >= 0; i--) {
    let entry: DecisionLogEntry;
    try {
      entry = JSON.parse(lines[i]) as DecisionLogEntry;
    } catch {
      continue;
    }
    if (entry.action !== 'supersede_existing') continue;
    if (!entry.winner_components || !entry.loser_components) continue;
    if (role === 'loser' && entry.existing_claim_id === factId) return entry;
    if (role === 'winner' && entry.new_claim_id === factId) return entry;
  }
  return null;
}

/** Build a FeedbackEntry from a matching decision row. */
export function buildFeedbackFromDecision(
  decision: DecisionLogEntry,
  action: 'pin_loser' | 'unpin_winner',
  nowUnixSeconds: number,
): FeedbackEntry | null {
  if (!decision.winner_components || !decision.loser_components) return null;
  return {
    ts: nowUnixSeconds,
    claim_a_id: decision.existing_claim_id,
    claim_b_id: decision.new_claim_id,
    formula_winner: 'b',
    user_decision: action === 'pin_loser' ? 'pin_a' : 'pin_b',
    winner_components: decision.winner_components,
    loser_components: decision.loser_components,
  };
}

/** Append a feedback entry via the WASM core helpers. Best-effort; never throws. */
export async function appendFeedbackLog(entry: FeedbackEntry): Promise<void> {
  try {
    const core = getWasm();
    const dir = ensureStateDir();
    const p = path.join(dir, 'feedback.jsonl');
    let existing = '';
    try {
      existing = fs.readFileSync(p, 'utf-8');
    } catch {
      existing = '';
    }
    const appended = core.appendFeedbackToJsonl(existing, JSON.stringify(entry));
    const rotated = core.rotateFeedbackLog(appended, BigInt(10_000));
    fs.writeFileSync(p, rotated, 'utf-8');
  } catch {
    // best-effort
  }
}

/**
 * On pin/unpin, consult decisions.jsonl and append a feedback row when the
 * user is overriding a prior auto-resolution. Returns the entry written (or
 * null for voluntary pins / missing components).
 */
export async function maybeWriteFeedbackForPin(
  factId: string,
  targetStatus: 'pinned' | 'active',
  nowUnixSeconds: number,
): Promise<FeedbackEntry | null> {
  let logContent = '';
  try {
    logContent = fs.readFileSync(decisionsLogPath(), 'utf-8');
  } catch {
    logContent = '';
  }
  const role: 'loser' | 'winner' = targetStatus === 'pinned' ? 'loser' : 'winner';
  const decision = findDecisionForPin(factId, role, logContent);
  if (!decision) return null;
  const action = targetStatus === 'pinned' ? 'pin_loser' : 'unpin_winner';
  const entry = buildFeedbackFromDecision(decision, action, nowUnixSeconds);
  if (!entry) return null;
  await appendFeedbackLog(entry);
  return entry;
}

// ─── Tool definitions ─────────────────────────────────────────────────────────

const PIN_DESCRIPTION =
  'Pin memory — nothing overrides/supersedes/auto-retracts. Ground truth.\n' +
  '\nINVOKE WHEN USER SAYS:\n' +
  '- "pin that" / "never forget this" / "always remember this"\n' +
  '- "this is definitely true even though I said X" (contradiction)\n' +
  '- "make sure this sticks" / "lock this in"\n' +
  '- "don\'t lose it"\n' +
  '\nDOES: fetches by fact_id, writes pin claim, tombstones original. Idempotent, on-chain.\n' +
  '\nWHEN NOT TO USE:\n' +
  '- no memory ID yet → totalreclaw_recall first\n' +
  '- storing new → totalreclaw_remember importance 9-10';

const UNPIN_DESCRIPTION =
  'Remove pin so auto-resolution can supersede/update again.\n' +
  '\nINVOKE WHEN USER SAYS:\n' +
  '- "unpin that" / "stop pinning that memory"\n' +
  '- "that was pinned by mistake"\n' +
  '- "let that one get overwritten if new info comes in"\n' +
  '\nDOES: flips pin → active via supersession. Takes fact_id. Idempotent. Propagates on-chain.\n' +
  '\nWHEN NOT TO USE:\n' +
  '- wants to DELETE → totalreclaw_forget\n' +
  '- wants to UPDATE → totalreclaw_remember new text (dedup handles it)';

export const pinToolDefinition = {
  name: 'totalreclaw_pin',
  description: PIN_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: {
        type: 'string',
        description: 'The ID of the fact to pin (from a totalreclaw_recall result).',
      },
      // Accept `memory_id` as an alias for `fact_id` to match the v1 taxonomy
      // spec wording (`memory_id` is used by the new retype / set_scope tools).
      memory_id: {
        type: 'string',
        description: 'Alias for fact_id. Prefer fact_id for backward compatibility.',
      },
      reason: {
        type: 'string',
        description: 'Optional human-readable reason for pinning (logged locally for tuning).',
      },
      expires_at: {
        type: 'string',
        description:
          'Optional ISO 8601 timestamp at which the pin should lapse. ' +
          'Recorded on the new claim; enforcement (auto-unpin after expiry) lives in a future revision.',
      },
    },
    required: ['fact_id'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

export const unpinToolDefinition = {
  name: 'totalreclaw_unpin',
  description: UNPIN_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      fact_id: {
        type: 'string',
        description: 'The ID of the fact to unpin (from a totalreclaw_recall result).',
      },
    },
    required: ['fact_id'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

// ─── Status type + helpers ────────────────────────────────────────────────────

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

/** Parse a decrypted blob into a mutable claim object + current human status. */
interface ParsedBlob {
  claim: Record<string, unknown>;
  currentStatus: HumanStatus;
  isLegacy: boolean;
}

export function parseBlobForPin(decrypted: string): ParsedBlob {
  let obj: Record<string, unknown>;
  try {
    obj = JSON.parse(decrypted) as Record<string, unknown>;
  } catch {
    // Raw text — treat as a legacy fact with default metadata
    return {
      claim: buildCanonicalObjectFromLegacy(decrypted, {}),
      currentStatus: 'active',
      isLegacy: true,
    };
  }

  // New canonical Claim — short keys present
  if (typeof obj.t === 'string' && typeof obj.c === 'string') {
    const st = typeof obj.st === 'string' ? obj.st : 'a';
    const human = SHORT_TO_HUMAN[st] ?? 'active';
    // Deep clone so callers can mutate without touching caller copy
    const cloned = JSON.parse(JSON.stringify(obj)) as Record<string, unknown>;
    return { claim: cloned, currentStatus: human, isLegacy: false };
  }

  // Legacy {text, metadata: {importance: 0-1}} shape
  if (typeof obj.text === 'string') {
    const meta = (obj.metadata as Record<string, unknown>) ?? {};
    return {
      claim: buildCanonicalObjectFromLegacy(obj.text, meta),
      currentStatus: 'active',
      isLegacy: true,
    };
  }

  // Unknown shape — fall back to raw-text legacy path
  return {
    claim: buildCanonicalObjectFromLegacy(decrypted, {}),
    currentStatus: 'active',
    isLegacy: true,
  };
}

function buildCanonicalObjectFromLegacy(
  text: string,
  meta: Record<string, unknown>,
): Record<string, unknown> {
  const typeStr = typeof meta.type === 'string' ? meta.type : 'fact';
  const TYPE_TO_CATEGORY: Record<string, string> = {
    fact: 'fact',
    preference: 'pref',
    decision: 'dec',
    episodic: 'epi',
    goal: 'goal',
    context: 'ctx',
    summary: 'sum',
  };
  const category = TYPE_TO_CATEGORY[typeStr] ?? 'fact';
  const impFloat = typeof meta.importance === 'number' ? meta.importance : 0.5;
  const importance = Math.max(1, Math.min(10, Math.round(impFloat * 10)));
  const source = typeof meta.source === 'string' ? meta.source : 'mcp_remember';
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

// ─── Pure core: executePinOperation ──────────────────────────────────────────

export interface PinOpDeps {
  owner: string;
  sourceAgent: string;
  fetchFactById: (factId: string) => Promise<SubgraphSearchFact | null>;
  decryptBlob: (hexEncryptedBlob: string) => string;
  encryptBlob: (plaintext: string) => string; // returns hex
  submitBatch: (protobufPayloads: Buffer[]) => Promise<{ txHash: string; success: boolean }>;
  /**
   * Regenerate blind indices + encrypted embedding for the pinned claim.
   * The new fact needs trapdoors pointing to its content so trapdoor-based
   * recall still finds pinned claims after the old fact is tombstoned.
   * Returns empty indices + undefined embedding on failure — caller tolerates.
   */
  generateIndices: (text: string, entityNames: string[]) => Promise<{
    blindIndices: string[];
    encryptedEmbedding?: string;
  }>;
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
 * Semantics (Phase 2 §P2-4): the subgraph is append-only, so a status change
 * requires writing a new fact with the updated status and tombstoning the old
 * one. The new fact's `supersedes` field points to the old fact id, forming a
 * cross-device-visible supersession chain.
 *
 * Idempotent: pinning an already-pinned claim (or unpinning a non-pinned one)
 * returns success with `idempotent: true` and no on-chain write.
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
  try {
    plaintext = deps.decryptBlob(blobHex);
  } catch (err) {
    return {
      success: false,
      fact_id: factId,
      error: `Failed to decrypt fact: ${err instanceof Error ? err.message : String(err)}`,
    };
  }

  const parsed = parseBlobForPin(plaintext);

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
    // Default — omit the "st" field entirely
    delete newClaimObj.st;
  } else {
    newClaimObj.st = HUMAN_TO_SHORT[targetStatus];
  }
  newClaimObj.sup = factId;
  // Refresh extraction timestamp so downstream consumers can tell this is a new event
  newClaimObj.ea = new Date().toISOString();
  // Carry the source agent forward if present, otherwise stamp it
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
    ? parsed.claim.e
        .map((e: unknown) => (e && typeof (e as { n?: unknown }).n === 'string' ? (e as { n: string }).n : ''))
        .filter((n: string): n is string => n.length > 0)
    : [];
  let regenerated: { blindIndices: string[]; encryptedEmbedding?: string };
  try {
    regenerated = await deps.generateIndices(newClaimText, entityNames);
  } catch {
    regenerated = { blindIndices: [] };
  }

  // 6. Build tombstone + new protobuf payloads
  const tombstonePayload: FactPayloadMinimal = {
    id: factId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: Buffer.from('tombstone').toString('hex'),
    blindIndices: [],
    decayScore: 0,
    source: targetStatus === 'pinned' ? 'mcp_pin' : 'mcp_unpin',
    contentFp: '',
    agentId: deps.sourceAgent,
  };

  const newFactId = crypto.randomUUID();
  const newPayload: FactPayloadMinimal = {
    id: newFactId,
    timestamp: new Date().toISOString(),
    owner: deps.owner,
    encryptedBlob: newBlobHex,
    blindIndices: regenerated.blindIndices,
    decayScore: 1.0, // Pins are top-priority; unpins revert to active at full decay
    source: targetStatus === 'pinned' ? 'mcp_pin' : 'mcp_unpin',
    contentFp: '',
    agentId: deps.sourceAgent,
    encryptedEmbedding: regenerated.encryptedEmbedding,
  };

  const payloads = [encodeFactProtobuf(tombstonePayload), encodeFactProtobuf(newPayload)];

  // 6b. Slice 2f: if this pin/unpin overrides a prior auto-resolution, append
  // a counterexample to feedback.jsonl. MCP does NOT run the weight-tuning
  // loop itself (no digest-compile hook) — the next plugin / Hermes digest
  // compile on the same vault consumes these entries.
  try {
    await maybeWriteFeedbackForPin(factId, targetStatus, Math.floor(Date.now() / 1000));
  } catch {
    // Feedback wiring is never fatal.
  }

  // 7. Submit both in a single batch UserOp
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

/** Minimal FactPayload type mirror — kept local so pin.ts doesn't re-export subgraph store internals. */
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

// ─── Top-level MCP handlers (HTTP mode) ──────────────────────────────────────

/** HTTP (self-hosted) mode handler — not supported in Slice 2e-mcp. */
export async function handlePin(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validatePinArgs(args);
  if (!validation.ok) {
    return errorResponse(validation.error);
  }
  return errorResponse(
    'Pin/unpin is only supported with the managed service. Self-hosted mode does not yet implement the status-flip supersession flow.',
  );
}

/** HTTP (self-hosted) mode handler — not supported in Slice 2e-mcp. */
export async function handleUnpin(
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validatePinArgs(args);
  if (!validation.ok) {
    return errorResponse(validation.error);
  }
  return errorResponse(
    'Pin/unpin is only supported with the managed service. Self-hosted mode does not yet implement the status-flip supersession flow.',
  );
}

interface ValidArgs {
  ok: boolean;
  factId: string;
  reason?: string;
  error: string;
}

function validatePinArgs(args: unknown): ValidArgs {
  if (!args || typeof args !== 'object') {
    return { ok: false, factId: '', error: 'Invalid input: fact_id is required' };
  }
  const record = args as Record<string, unknown>;
  // Accept either `fact_id` (v0) or `memory_id` (v1 spec wording). `fact_id`
  // wins if both are present so existing MCP consumers keep working.
  const rawId =
    typeof record.fact_id === 'string' && record.fact_id.trim().length > 0
      ? record.fact_id
      : typeof record.memory_id === 'string'
        ? record.memory_id
        : undefined;
  if (typeof rawId !== 'string' || rawId.trim().length === 0) {
    return {
      ok: false,
      factId: '',
      error: 'Invalid input: fact_id (or memory_id) must be a non-empty string',
    };
  }
  const reason = typeof record.reason === 'string' ? record.reason : undefined;
  return { ok: true, factId: rawId.trim(), reason, error: '' };
}

/** Dispatch helper for callers that already hold PinOpDeps (used by index.ts subgraph path). */
export async function handlePinSubgraphWithDeps(
  args: unknown,
  deps: PinOpDeps,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validatePinArgs(args);
  if (!validation.ok) return errorResponse(validation.error);
  const result = await executePinOperation(validation.factId, 'pinned', deps, validation.reason);
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

export async function handleUnpinSubgraphWithDeps(
  args: unknown,
  deps: PinOpDeps,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const validation = validatePinArgs(args);
  if (!validation.ok) return errorResponse(validation.error);
  const result = await executePinOperation(validation.factId, 'active', deps, validation.reason);
  return { content: [{ type: 'text', text: JSON.stringify(result) }] };
}

function errorResponse(error: string): { content: Array<{ type: string; text: string }> } {
  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify({ success: false, error }),
      },
    ],
  };
}
