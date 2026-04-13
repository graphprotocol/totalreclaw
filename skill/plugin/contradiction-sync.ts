/**
 * TotalReclaw Plugin — contradiction detection + auto-resolution (Phase 2 Slice 2d).
 *
 * Runs after store-time dedup (>= 0.85 cosine) and before the canonical Claim
 * is encrypted + written. For every entity on the new claim, fetches existing
 * active claims that reference the same entity via the entity trapdoor, decrypts
 * them, and asks the WASM core to detect contradictions in the [0.3, 0.85) band.
 * Each contradicting pair is then resolved via the P2-3 formula; the winner is
 * kept on-chain, the loser is queued for tombstoning. Pinned claims are never
 * touched — a contradiction against a pinned claim always causes the new write
 * to be skipped with reason `existing_pinned`.
 *
 * This module mirrors the structure of `digest-sync.ts`: pure functions at the
 * core, I/O behind dependency injection so the test file can run the real WASM
 * while stubbing subgraph + filesystem.
 */

import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  computeEntityTrapdoor,
  isDigestBlob,
  type AutoResolveMode,
} from './claims-helper.js';

const requireWasm = createRequire(import.meta.url);
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm() {
  if (!_wasm) _wasm = requireWasm('@totalreclaw/core');
  return _wasm!;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ContradictionLogger {
  info: (msg: string) => void;
  warn: (msg: string) => void;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type CanonicalClaim = Record<string, any>;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type WeightsFile = Record<string, any>;

/** Per-component score breakdown, mirroring Rust `ScoreComponents`. */
export interface ScoreComponents {
  confidence: number;
  corroboration: number;
  recency: number;
  validation: number;
  weighted_total: number;
}

/**
 * What action the write path should take for a given candidate existing claim.
 *
 * - `no_contradiction`: nothing to do, proceed with the normal write.
 * - `supersede_existing`: the new claim wins, tombstone the named existing fact.
 * - `skip_new`: an existing claim wins (or is pinned) — skip the new write entirely.
 */
export type ResolutionDecision =
  | { action: 'no_contradiction' }
  | {
      action: 'supersede_existing';
      existingFactId: string;
      existingClaim: CanonicalClaim;
      entityId: string;
      similarity: number;
      winnerScore: number;
      loserScore: number;
      winnerComponents: ScoreComponents;
      loserComponents: ScoreComponents;
    }
  | {
      action: 'skip_new';
      reason: 'existing_pinned' | 'existing_wins';
      existingFactId: string;
      entityId: string;
      similarity: number;
      winnerScore?: number;
      loserScore?: number;
      winnerComponents?: ScoreComponents;
      loserComponents?: ScoreComponents;
    };

/** Row format for `decisions.jsonl`. */
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
  /**
   * Per-component score breakdown for the formula winner. Added in Slice 2f
   * so the feedback-tuning loop can reconstruct counterexamples from the log.
   * Optional for backwards-compat with pre-2f log rows.
   */
  winner_components?: ScoreComponents;
  /** Per-component score breakdown for the formula loser. See winner_components. */
  loser_components?: ScoreComponents;
  mode: AutoResolveMode;
}

// ---------------------------------------------------------------------------
// Paths + file I/O
// ---------------------------------------------------------------------------

/** Where feedback, decisions, and weights live. `~/.totalreclaw/` by default. */
function resolveStateDir(): string {
  const override = process.env.TOTALRECLAW_STATE_DIR;
  if (override && override.length > 0) return override;
  return path.join(os.homedir(), '.totalreclaw');
}

function ensureStateDir(): string {
  const dir = resolveStateDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    // Caller handles downstream read/write failures.
  }
  return dir;
}

export function decisionsLogPath(): string {
  return path.join(resolveStateDir(), 'decisions.jsonl');
}

export function weightsFilePath(): string {
  return path.join(resolveStateDir(), 'weights.json');
}

/** Cap on the decisions.jsonl log — oldest lines are dropped above this. */
export const DECISION_LOG_MAX_LINES = 10_000;

/** Soft cap on candidates fetched per entity during contradiction detection. */
export const CONTRADICTION_CANDIDATE_CAP = 20;

/**
 * Append one entry to the decision log, rotating if it grows past the cap.
 * Never throws — logging is best-effort.
 */
export async function appendDecisionLog(entry: DecisionLogEntry): Promise<void> {
  try {
    const dir = ensureStateDir();
    const p = path.join(dir, 'decisions.jsonl');
    let existing = '';
    try {
      existing = fs.readFileSync(p, 'utf-8');
    } catch {
      existing = '';
    }
    const line = JSON.stringify(entry);
    let next = existing;
    if (next.length === 0) {
      next = line + '\n';
    } else if (next.endsWith('\n')) {
      next = next + line + '\n';
    } else {
      next = next + '\n' + line + '\n';
    }
    // Rotate via the WASM core helper (same primitive used by feedback.jsonl).
    // `rotateFeedbackLog` expects a BigInt for max_lines.
    const rotated = getWasm().rotateFeedbackLog(next, BigInt(DECISION_LOG_MAX_LINES));
    fs.writeFileSync(p, rotated, 'utf-8');
  } catch {
    // Logging failures are never fatal.
  }
}

/**
 * Load the per-user weights file, falling back to defaults when the file
 * does not exist or is malformed. Never throws.
 */
export async function loadWeightsFile(nowUnixSeconds: number): Promise<WeightsFile> {
  const core = getWasm();
  const p = weightsFilePath();
  try {
    const raw = fs.readFileSync(p, 'utf-8');
    const parsedJson = core.parseWeightsFile(raw);
    return JSON.parse(parsedJson) as WeightsFile;
  } catch {
    // File missing / malformed / wrong version → return fresh defaults.
    const freshJson = core.defaultWeightsFile(BigInt(Math.floor(nowUnixSeconds)));
    return JSON.parse(freshJson) as WeightsFile;
  }
}

export async function saveWeightsFile(file: WeightsFile): Promise<void> {
  const core = getWasm();
  try {
    const dir = ensureStateDir();
    const p = path.join(dir, 'weights.json');
    const serialized = core.serializeWeightsFile(JSON.stringify(file));
    fs.writeFileSync(p, serialized, 'utf-8');
  } catch {
    // best-effort
  }
}

// ---------------------------------------------------------------------------
// Candidate fetching (subgraph + decrypt)
// ---------------------------------------------------------------------------

export interface SubgraphSearchFn {
  (
    owner: string,
    trapdoors: string[],
    maxCandidates: number,
    authKeyHex?: string,
  ): Promise<Array<{
    id: string;
    encryptedBlob: string;
    encryptedEmbedding?: string | null;
    timestamp?: string;
    isActive?: boolean;
  }>>;
}

export interface DecryptFn {
  (hexBlob: string, key: Buffer): string;
}

export interface CandidateClaim {
  /** The canonical Claim JSON object (parsed from the decrypted blob). */
  claim: CanonicalClaim;
  /** Subgraph fact id — the existing-fact id we would tombstone on supersede. */
  id: string;
  /** Embedding if we could recover it (plain JSON array of numbers). */
  embedding: number[];
}

/**
 * Parse a decrypted Claim blob into a `{claim, status}` pair.
 *
 * Accepts the canonical short-key Claim shape (`{t, c, cf, i, sa, ea, ...}`).
 * Returns null for legacy docs, digest blobs, entity-infrastructure claims,
 * or anything that fails to parse cleanly — these are all excluded from
 * contradiction detection.
 */
export function parseCandidateClaim(decryptedJson: string): CanonicalClaim | null {
  if (isDigestBlob(decryptedJson)) return null;
  let obj: Record<string, unknown>;
  try {
    obj = JSON.parse(decryptedJson) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (typeof obj.t !== 'string' || typeof obj.c !== 'string') return null;
  // Filter out infra categories — digests and entity rollup claims are not
  // user-facing knowledge and must never be considered contradictions.
  if (obj.c === 'dig' || obj.c === 'ent') return null;
  return obj as CanonicalClaim;
}

/** Is this candidate claim pinned (status `p`)? */
export function isPinnedClaim(claim: CanonicalClaim): boolean {
  return typeof claim.st === 'string' && claim.st === 'p';
}

/**
 * Shape of the `existing_json` expected by the WASM `detectContradictions` call.
 *
 * Matches `DetectContradictionsItem` in `rust/totalreclaw-core/src/wasm.rs`.
 */
interface WasmExistingItem {
  claim: CanonicalClaim;
  id: string;
  embedding: number[];
}

/**
 * Collect active claim candidates for every entity on the new claim.
 *
 * For each entity:
 *   1. Compute its trapdoor (same primitive as the write path).
 *   2. Query the subgraph for facts matching that single-element trapdoor.
 *   3. Decrypt + parse each row, filtering infra claims and bad blobs.
 *   4. Recover the embedding (reusing the stored one when possible).
 *
 * Deduplicates by subgraph fact id across entities so the same claim is
 * never processed twice. Caps the total number of candidates per entity at
 * `CONTRADICTION_CANDIDATE_CAP` to keep the write-path cost bounded.
 */
export async function collectCandidatesForEntities(
  newClaim: CanonicalClaim,
  newClaimId: string,
  subgraphOwner: string,
  authKeyHex: string,
  encryptionKey: Buffer,
  deps: {
    searchSubgraph: SubgraphSearchFn;
    decryptFromHex: DecryptFn;
  },
  logger: ContradictionLogger,
): Promise<CandidateClaim[]> {
  const entities = Array.isArray(newClaim.e) ? newClaim.e : [];
  if (entities.length === 0) return [];

  const seenIds = new Set<string>();
  const out: CandidateClaim[] = [];

  for (const entity of entities) {
    const name = typeof entity?.n === 'string' ? entity.n : null;
    if (!name) continue;
    const trapdoor = computeEntityTrapdoor(name);
    let rows: Awaited<ReturnType<SubgraphSearchFn>> = [];
    try {
      rows = await deps.searchSubgraph(
        subgraphOwner,
        [trapdoor],
        CONTRADICTION_CANDIDATE_CAP,
        authKeyHex,
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Contradiction: subgraph query failed for entity "${name}": ${msg}`);
      continue;
    }

    for (const row of rows) {
      if (!row || !row.id || row.id === newClaimId) continue;
      if (row.isActive === false) continue;
      if (seenIds.has(row.id)) continue;
      seenIds.add(row.id);

      let decrypted: string;
      try {
        decrypted = deps.decryptFromHex(row.encryptedBlob, encryptionKey);
      } catch {
        continue;
      }
      const parsed = parseCandidateClaim(decrypted);
      if (!parsed) continue;

      let embedding: number[] = [];
      if (row.encryptedEmbedding) {
        try {
          const emb = JSON.parse(deps.decryptFromHex(row.encryptedEmbedding, encryptionKey));
          if (Array.isArray(emb) && emb.every((x) => typeof x === 'number')) {
            embedding = emb;
          }
        } catch {
          embedding = [];
        }
      }

      out.push({ claim: parsed, id: row.id, embedding });
    }
  }

  return out;
}

// ---------------------------------------------------------------------------
// Pure resolver: given new claim + candidates + weights, return decisions
// ---------------------------------------------------------------------------

export interface ResolveWithCoreInput {
  newClaim: CanonicalClaim;
  newClaimId: string;
  newEmbedding: number[];
  candidates: CandidateClaim[];
  weightsJson: string;
  thresholdLower: number;
  thresholdUpper: number;
  nowUnixSeconds: number;
  mode: AutoResolveMode;
  logger: ContradictionLogger;
}

/**
 * Run WASM contradiction detection + resolution on in-memory data only.
 *
 * Split out from `detectAndResolveContradictions` so the write path can test
 * the decision logic without mocking file I/O or the subgraph.
 */
export function resolveWithCore(input: ResolveWithCoreInput): ResolutionDecision[] {
  const {
    newClaim,
    newClaimId,
    newEmbedding,
    candidates,
    weightsJson,
    thresholdLower,
    thresholdUpper,
    nowUnixSeconds,
    mode,
    logger,
  } = input;

  if (mode === 'off') return [];
  if (candidates.length === 0) return [];
  if (newEmbedding.length === 0) {
    logger.warn('Contradiction: new claim has no embedding; skipping detection');
    return [];
  }

  const core = getWasm();

  // Build the WASM existing_json payload. Drop any candidate whose embedding
  // is missing — detectContradictions already short-circuits on empty vectors
  // but we also drop them from the decrypt-map so we don't waste a lookup.
  const items: WasmExistingItem[] = candidates
    .filter((c) => c.embedding.length > 0)
    .map((c) => ({ claim: c.claim, id: c.id, embedding: c.embedding }));

  if (items.length === 0) return [];

  let contradictionsJson: string;
  try {
    contradictionsJson = core.detectContradictions(
      JSON.stringify(newClaim),
      newClaimId,
      JSON.stringify(newEmbedding),
      JSON.stringify(items),
      thresholdLower,
      thresholdUpper,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Contradiction: detectContradictions failed: ${msg}`);
    return [];
  }

  let contradictions: Array<{
    claim_a_id: string;
    claim_b_id: string;
    entity_id: string;
    similarity: number;
  }>;
  try {
    contradictions = JSON.parse(contradictionsJson);
  } catch {
    return [];
  }
  if (contradictions.length === 0) return [];

  // Index candidates by id for fast lookup during resolve.
  const byId = new Map<string, CandidateClaim>();
  for (const c of items) byId.set(c.id, { claim: c.claim, id: c.id, embedding: c.embedding });

  const decisions: ResolutionDecision[] = [];
  const nowSecondsBig = BigInt(Math.floor(nowUnixSeconds));

  for (const contradiction of contradictions) {
    const existing = byId.get(contradiction.claim_b_id);
    if (!existing) continue;

    // Pinned existing claims are untouchable. Skip the new write and record
    // a decision so the caller can log it.
    if (isPinnedClaim(existing.claim)) {
      decisions.push({
        action: 'skip_new',
        reason: 'existing_pinned',
        existingFactId: existing.id,
        entityId: contradiction.entity_id,
        similarity: contradiction.similarity,
      });
      continue;
    }

    let outcomeJson: string;
    try {
      outcomeJson = core.resolvePair(
        JSON.stringify(newClaim),
        newClaimId,
        JSON.stringify(existing.claim),
        existing.id,
        nowSecondsBig,
        weightsJson,
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Contradiction: resolvePair failed for ${existing.id.slice(0, 10)}…: ${msg}`);
      continue;
    }

    let outcome: {
      winner_id: string;
      loser_id: string;
      winner_score: number;
      loser_score: number;
      winner_components: ScoreComponents;
      loser_components: ScoreComponents;
    };
    try {
      outcome = JSON.parse(outcomeJson);
    } catch {
      continue;
    }

    if (outcome.winner_id === newClaimId) {
      decisions.push({
        action: 'supersede_existing',
        existingFactId: existing.id,
        existingClaim: existing.claim,
        entityId: contradiction.entity_id,
        similarity: contradiction.similarity,
        winnerScore: outcome.winner_score,
        loserScore: outcome.loser_score,
        winnerComponents: outcome.winner_components,
        loserComponents: outcome.loser_components,
      });
    } else {
      decisions.push({
        action: 'skip_new',
        reason: 'existing_wins',
        existingFactId: existing.id,
        entityId: contradiction.entity_id,
        similarity: contradiction.similarity,
        winnerScore: outcome.winner_score,
        loserScore: outcome.loser_score,
        winnerComponents: outcome.winner_components,
        loserComponents: outcome.loser_components,
      });
    }
  }

  return decisions;
}

// ---------------------------------------------------------------------------
// Top-level entry point for the write path
// ---------------------------------------------------------------------------

export interface DetectAndResolveDeps {
  searchSubgraph: SubgraphSearchFn;
  decryptFromHex: DecryptFn;
  /** Now in Unix seconds — overridable for deterministic tests. */
  nowUnixSeconds?: number;
}

export interface DetectAndResolveInput {
  newClaim: CanonicalClaim;
  newClaimId: string;
  newEmbedding: number[];
  subgraphOwner: string;
  authKeyHex: string;
  encryptionKey: Buffer;
  deps: DetectAndResolveDeps;
  logger: ContradictionLogger;
}

/**
 * Write-path entry point. See Slice 2d of the Phase 2 design doc.
 *
 * Returns a list of `ResolutionDecision`s:
 *   - `supersede_existing`: caller queues a tombstone for `existingFactId`
 *   - `skip_new`: caller skips the new write (existing wins or is pinned)
 *   - `no_contradiction`: never explicitly returned — an empty list means this
 *
 * Never throws. On any failure (subgraph, decrypt, WASM), returns `[]` so the
 * write path falls back to Phase 1 behaviour.
 */
export async function detectAndResolveContradictions(
  input: DetectAndResolveInput,
): Promise<ResolutionDecision[]> {
  const {
    newClaim,
    newClaimId,
    newEmbedding,
    subgraphOwner,
    authKeyHex,
    encryptionKey,
    deps,
    logger,
  } = input;

  // Read env per-call so tests can toggle without module reload.
  const raw = (process.env.TOTALRECLAW_AUTO_RESOLVE_MODE ?? '').trim().toLowerCase();
  const mode: AutoResolveMode =
    raw === 'off' ? 'off' : raw === 'shadow' ? 'shadow' : 'active';

  if (mode === 'off') return [];

  // No entities → nothing to check (same contract as detect_contradictions).
  const entities = Array.isArray(newClaim.e) ? newClaim.e : [];
  if (entities.length === 0) return [];

  const nowUnixSeconds =
    typeof deps.nowUnixSeconds === 'number'
      ? deps.nowUnixSeconds
      : Math.floor(Date.now() / 1000);

  // Load per-user weights file (defaults if missing/malformed).
  const weightsFile = await loadWeightsFile(nowUnixSeconds);
  const weightsJson = JSON.stringify(weightsFile.weights ?? {});
  const thresholdLower =
    typeof weightsFile.threshold_lower === 'number' ? weightsFile.threshold_lower : 0.3;
  const thresholdUpper =
    typeof weightsFile.threshold_upper === 'number' ? weightsFile.threshold_upper : 0.85;

  // Fetch + decrypt candidates.
  let candidates: CandidateClaim[];
  try {
    candidates = await collectCandidatesForEntities(
      newClaim,
      newClaimId,
      subgraphOwner,
      authKeyHex,
      encryptionKey,
      deps,
      logger,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Contradiction: candidate fetch failed: ${msg}`);
    return [];
  }
  if (candidates.length === 0) return [];

  const decisions = resolveWithCore({
    newClaim,
    newClaimId,
    newEmbedding,
    candidates,
    weightsJson,
    thresholdLower,
    thresholdUpper,
    nowUnixSeconds,
    mode,
    logger,
  });

  // Append decision log rows. One row per non-trivial decision.
  for (const d of decisions) {
    if (d.action === 'supersede_existing') {
      await appendDecisionLog({
        ts: nowUnixSeconds,
        entity_id: d.entityId,
        new_claim_id: newClaimId,
        existing_claim_id: d.existingFactId,
        similarity: d.similarity,
        action: mode === 'shadow' ? 'shadow' : 'supersede_existing',
        reason: 'new_wins',
        winner_score: d.winnerScore,
        loser_score: d.loserScore,
        winner_components: d.winnerComponents,
        loser_components: d.loserComponents,
        mode,
      });
    } else if (d.action === 'skip_new') {
      await appendDecisionLog({
        ts: nowUnixSeconds,
        entity_id: d.entityId,
        new_claim_id: newClaimId,
        existing_claim_id: d.existingFactId,
        similarity: d.similarity,
        action: mode === 'shadow' ? 'shadow' : 'skip_new',
        reason: d.reason,
        winner_score: d.winnerScore,
        loser_score: d.loserScore,
        winner_components: d.winnerComponents,
        loser_components: d.loserComponents,
        mode,
      });
    }
  }

  // Shadow mode: never apply decisions, always return empty. The log above
  // captures what would have happened so operators can inspect it.
  if (mode === 'shadow') return [];

  return decisions;
}

// ---------------------------------------------------------------------------
// Slice 2f: feedback wiring (pin path) + weight-tuning loop
// ---------------------------------------------------------------------------

/** Path to `~/.totalreclaw/feedback.jsonl` honouring TOTALRECLAW_STATE_DIR. */
export function feedbackLogPath(): string {
  return path.join(resolveStateDir(), 'feedback.jsonl');
}

/** Cap on feedback.jsonl lines; oldest dropped above this. */
export const FEEDBACK_LOG_MAX_LINES = 10_000;

/** Minimum seconds between consecutive tuning-loop runs (rate limit). */
export const TUNING_LOOP_MIN_INTERVAL_SECONDS = 3600;

/** A single row from feedback.jsonl — matches Rust `FeedbackEntry`. */
export interface FeedbackEntry {
  ts: number;
  claim_a_id: string;
  claim_b_id: string;
  formula_winner: 'a' | 'b';
  user_decision: 'pin_a' | 'pin_b' | 'pin_both' | 'unpin';
  winner_components: ScoreComponents;
  loser_components: ScoreComponents;
}

/**
 * Walk `decisions.jsonl` in reverse and find the most recent `supersede_existing`
 * entry that the target fact participated in. Slice 2f uses this to decide
 * whether a pin/unpin call is a real counterexample (gradient signal) or a
 * voluntary pin (no signal).
 *
 * `role` selects which side of the decision to match: `'loser'` finds entries
 * where the fact was tombstoned by the formula (regular pin-after-override),
 * `'winner'` finds entries where the fact was the formula's pick (reverse
 * unpin-the-winner path).
 *
 * Returns null if the log is absent, empty, or has no matching entry with the
 * Slice 2f component breakdown.
 */
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

/**
 * Build a `FeedbackEntry` from a matching decision-log row + pin action.
 *
 * For `supersede_existing`, the formula's winner is always the new claim
 * (`new_claim_id`) and the loser is the existing claim. If the user later
 * pins the loser, `user_decision = 'pin_a'` with `claim_a` = the loser
 * (so claim_a is what the user wants kept). If the user later unpins the
 * winner — an inverse override — we record `user_decision = 'pin_b'` to
 * keep the schema symmetrical.
 */
export function buildFeedbackFromDecision(
  decision: DecisionLogEntry,
  action: 'pin_loser' | 'unpin_winner',
  nowUnixSeconds: number,
): FeedbackEntry | null {
  if (!decision.winner_components || !decision.loser_components) return null;
  if (action === 'pin_loser') {
    return {
      ts: nowUnixSeconds,
      claim_a_id: decision.existing_claim_id,
      claim_b_id: decision.new_claim_id,
      formula_winner: 'b',
      user_decision: 'pin_a',
      winner_components: decision.winner_components,
      loser_components: decision.loser_components,
    };
  }
  return {
    ts: nowUnixSeconds,
    claim_a_id: decision.existing_claim_id,
    claim_b_id: decision.new_claim_id,
    formula_winner: 'b',
    user_decision: 'pin_b',
    winner_components: decision.winner_components,
    loser_components: decision.loser_components,
  };
}

/**
 * Append one feedback entry to `~/.totalreclaw/feedback.jsonl`, rotating if
 * over the cap. Uses the WASM core bindings so the file format is byte-for-byte
 * compatible with the Rust + Python clients. Never throws.
 */
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
    const rotated = core.rotateFeedbackLog(appended, BigInt(FEEDBACK_LOG_MAX_LINES));
    fs.writeFileSync(p, rotated, 'utf-8');
  } catch {
    // Best-effort; feedback logging is never fatal.
  }
}

/**
 * Slice 2f glue: on pin/unpin, consult `decisions.jsonl` and write a feedback
 * row if the user override contradicts a prior formula decision.
 *
 * Returns the entry that was appended, or null when the pin/unpin was
 * voluntary (no matching decision row). Logs info-level on voluntary pins
 * and debug-level on each counterexample written.
 */
export async function maybeWriteFeedbackForPin(
  factId: string,
  targetStatus: 'pinned' | 'active',
  nowUnixSeconds: number,
  logger: ContradictionLogger,
): Promise<FeedbackEntry | null> {
  let logContent = '';
  try {
    logContent = fs.readFileSync(decisionsLogPath(), 'utf-8');
  } catch {
    logContent = '';
  }
  // For pin: the user is saying the loser was right → match loser.
  // For unpin: the user is flipping the winner back → match winner.
  const role: 'loser' | 'winner' = targetStatus === 'pinned' ? 'loser' : 'winner';
  const decision = findDecisionForPin(factId, role, logContent);
  if (!decision) {
    logger.info(
      targetStatus === 'pinned'
        ? `Pin feedback: no matching auto-resolution for ${factId.slice(0, 10)}… (voluntary pin, no tuning signal)`
        : `Unpin feedback: no matching auto-resolution for ${factId.slice(0, 10)}… (voluntary unpin, no tuning signal)`,
    );
    return null;
  }
  const action = targetStatus === 'pinned' ? 'pin_loser' : 'unpin_winner';
  const entry = buildFeedbackFromDecision(decision, action, nowUnixSeconds);
  if (!entry) return null;
  await appendFeedbackLog(entry);
  logger.info(
    `Pin feedback: recorded counterexample (${entry.user_decision}) for ${factId.slice(0, 10)}…`,
  );
  return entry;
}

/**
 * Result of running the weight-tuning loop — exposed for tests and logging.
 */
export interface TuningLoopResult {
  processed: number;
  gradientSteps: number;
  skipped: 'rate-limited' | 'no-new-entries' | 'no-weights' | null;
  lastTuningTs: number;
}

/**
 * Core of the weight-tuning loop. Pure enough to test in isolation: reads
 * `feedback.jsonl`, replays every entry newer than `weightsFile.last_tuning_ts`
 * through the WASM `feedbackToCounterexample` + `applyFeedback` pair, writes
 * back the adjusted weights. Idempotent: re-running with the same feedback
 * file does nothing because the timestamp advances each pass.
 *
 * Rate-limited: if the current `updated_at` is within
 * `TUNING_LOOP_MIN_INTERVAL_SECONDS` of `nowUnixSeconds`, returns early with
 * `skipped: 'rate-limited'`. Never throws.
 */
export async function runWeightTuningLoop(
  nowUnixSeconds: number,
  logger: ContradictionLogger,
): Promise<TuningLoopResult> {
  const core = getWasm();
  let weightsFile: WeightsFile;
  try {
    weightsFile = await loadWeightsFile(nowUnixSeconds);
  } catch {
    return { processed: 0, gradientSteps: 0, skipped: 'no-weights', lastTuningTs: 0 };
  }

  // Rate limit: if the weights file was touched very recently and there is a
  // last_tuning_ts, skip — protects against hot loops during rapid debugging.
  const updatedAt = typeof weightsFile.updated_at === 'number' ? weightsFile.updated_at : 0;
  const priorTuningTs =
    typeof weightsFile.last_tuning_ts === 'number' ? weightsFile.last_tuning_ts : 0;
  if (
    priorTuningTs > 0 &&
    updatedAt > 0 &&
    nowUnixSeconds - updatedAt < TUNING_LOOP_MIN_INTERVAL_SECONDS
  ) {
    return {
      processed: 0,
      gradientSteps: 0,
      skipped: 'rate-limited',
      lastTuningTs: priorTuningTs,
    };
  }

  // Read feedback.jsonl.
  let feedbackContent = '';
  try {
    feedbackContent = fs.readFileSync(feedbackLogPath(), 'utf-8');
  } catch {
    feedbackContent = '';
  }
  if (!feedbackContent || feedbackContent.length === 0) {
    return {
      processed: 0,
      gradientSteps: 0,
      skipped: 'no-new-entries',
      lastTuningTs: priorTuningTs,
    };
  }

  let parsed: { entries: FeedbackEntry[]; warnings: string[] };
  try {
    parsed = JSON.parse(core.readFeedbackJsonl(feedbackContent)) as {
      entries: FeedbackEntry[];
      warnings: string[];
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Tuning loop: failed to parse feedback.jsonl: ${msg}`);
    return {
      processed: 0,
      gradientSteps: 0,
      skipped: 'no-new-entries',
      lastTuningTs: priorTuningTs,
    };
  }

  for (const w of parsed.warnings) logger.warn(`Tuning loop: ${w}`);

  const newEntries = parsed.entries.filter((e) => e.ts > priorTuningTs);
  if (newEntries.length === 0) {
    return {
      processed: 0,
      gradientSteps: 0,
      skipped: 'no-new-entries',
      lastTuningTs: priorTuningTs,
    };
  }

  let weightsJson = JSON.stringify(weightsFile.weights ?? {});
  let gradientSteps = 0;
  let maxTs = priorTuningTs;
  for (const entry of newEntries) {
    if (entry.ts > maxTs) maxTs = entry.ts;
    let cxJson: string;
    try {
      cxJson = core.feedbackToCounterexample(JSON.stringify(entry));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Tuning loop: feedbackToCounterexample failed: ${msg}`);
      continue;
    }
    if (cxJson === 'null') continue;
    try {
      weightsJson = core.applyFeedback(weightsJson, cxJson);
      gradientSteps += 1;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Tuning loop: applyFeedback failed: ${msg}`);
    }
  }

  let adjustedWeights: unknown;
  try {
    adjustedWeights = JSON.parse(weightsJson);
  } catch {
    return {
      processed: newEntries.length,
      gradientSteps,
      skipped: 'no-weights',
      lastTuningTs: maxTs,
    };
  }

  const nextFile: WeightsFile = {
    ...weightsFile,
    weights: adjustedWeights,
    updated_at: nowUnixSeconds,
    last_tuning_ts: maxTs,
    feedback_count:
      (typeof weightsFile.feedback_count === 'number' ? weightsFile.feedback_count : 0) +
      newEntries.length,
  };
  await saveWeightsFile(nextFile);
  logger.info(
    `Tuning loop: processed ${newEntries.length} feedback entries, applied ${gradientSteps} gradient steps`,
  );
  return {
    processed: newEntries.length,
    gradientSteps,
    skipped: null,
    lastTuningTs: maxTs,
  };
}
