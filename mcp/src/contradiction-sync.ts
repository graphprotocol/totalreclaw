/**
 * TotalReclaw MCP — contradiction detection + auto-resolution (Phase 2 Slice 2d).
 *
 * This is the MCP mirror of `skill/plugin/contradiction-sync.ts` — same
 * pipeline, same decision semantics, same decision-log format. Keeps the MCP
 * write path consistent with the plugin so a fact pinned via OpenClaw and
 * later re-asserted via MCP (or vice versa) produces the same outcome.
 *
 * Runs after store-time dedup and before the canonical v1 claim blob is
 * encrypted + submitted on-chain. For every entity on the new claim, fetches
 * existing active claims that share the same entity trapdoor, decrypts them,
 * and asks the WASM core to detect contradictions in the [0.3, 0.85) band.
 * Each contradicting pair is then resolved via the P2-3 formula; the winner
 * is kept on-chain, the loser is queued for tombstoning.
 *
 * Pinned claims are never touched — a contradiction against a pinned claim
 * always causes the new write to be skipped with reason `existing_pinned`.
 * Pin respect is enforced by the Rust core via `respect_pin_in_resolution`
 * inside `resolve_with_candidates`; the TypeScript side does not need to
 * re-check.
 *
 * Pure functions at the core, I/O behind dependency injection so tests can
 * run the real WASM while stubbing the subgraph + filesystem. The WASM core
 * is NOT mocked in tests — we run against the live @totalreclaw/core bindings
 * so decision-log formats stay byte-for-byte compatible with the plugin.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { computeEntityTrapdoor, isDigestBlob } from './claims-helper.js';

// MCP uses require('@totalreclaw/core') directly (see consolidation.ts,
// claims-helper.ts) instead of createRequire — match that pattern.
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    _wasm = require('@totalreclaw/core');
  }
  return _wasm!;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Internal kill-switch for auto-resolution. Mirrors the plugin's
 * `AutoResolveMode` (see `skill/plugin/claims-helper.ts:432`). Kept local to
 * this module to avoid dragging plugin-specific context into MCP's
 * `claims-helper.ts`.
 *
 * - `active` (default): full detection + auto-resolution
 * - `off`: skip contradiction detection entirely; legacy behaviour
 * - `shadow`: detect + log but do not apply decisions
 *
 * Read per-call from `TOTALRECLAW_AUTO_RESOLVE_MODE` — this is an INTERNAL
 * debug kill-switch, not a user-facing env var. Not documented in the MCP
 * README or SKILL.md.
 */
export type AutoResolveMode = 'active' | 'off' | 'shadow';

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
 * - `tie_leave_both`: the formula scores are within TIE_ZONE_SCORE_TOLERANCE;
 *   treat the "contradiction" as rounding noise and leave both claims active.
 *   The write path ignores this variant.
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
    }
  | {
      action: 'tie_leave_both';
      existingFactId: string;
      entityId: string;
      similarity: number;
      winnerScore: number;
      loserScore: number;
      winnerComponents: ScoreComponents;
      loserComponents: ScoreComponents;
    };

/** Row format for `decisions.jsonl`. Byte-for-byte identical to plugin's. */
export interface DecisionLogEntry {
  ts: number;
  entity_id: string;
  new_claim_id: string;
  existing_claim_id: string;
  similarity: number;
  action: 'supersede_existing' | 'skip_new' | 'shadow' | 'tie_leave_both';
  reason?: 'existing_pinned' | 'existing_wins' | 'new_wins' | 'tie_below_tolerance';
  winner_score?: number;
  loser_score?: number;
  winner_components?: ScoreComponents;
  loser_components?: ScoreComponents;
  /**
   * Full canonical Claim JSON for the formula loser (the existing claim that
   * got tombstoned by `supersede_existing`). Required by the pin-on-tombstone
   * recovery path — see the plugin's `contradiction-sync.ts` for rationale.
   */
  loser_claim_json?: string;
  mode: AutoResolveMode;
}

// ---------------------------------------------------------------------------
// Paths + file I/O — identical file layout as plugin so the same decisions.jsonl
// is compatible across clients.
// ---------------------------------------------------------------------------

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
 * Minimum score gap required to auto-resolve a `supersede_existing` decision.
 * Calibrated against the 2026-04-14 Postgres/DuckDB false-positive (gap 9 ppm).
 * See plugin's `contradiction-sync.ts` for full rationale.
 */
export const TIE_ZONE_SCORE_TOLERANCE = 0.01;

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
    const freshJson = core.defaultWeightsFile(BigInt(Math.floor(nowUnixSeconds)));
    return JSON.parse(freshJson) as WeightsFile;
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
  claim: CanonicalClaim;
  id: string;
  embedding: number[];
}

/**
 * Parse a decrypted Claim blob into a canonical claim object.
 *
 * Accepts the canonical short-key Claim shape (`{t, c, cf, i, sa, ea, ...}`).
 * Returns null for legacy docs, digest blobs, entity-infrastructure claims,
 * or anything that fails to parse — excluded from contradiction detection.
 *
 * Mirrors the plugin's `parseCandidateClaim` byte-for-byte. v1 long-form
 * blobs (`{text, type, entities, ...}`) do not have `t`/`c` and therefore
 * return null here. The Rust core's `Claim` struct only accepts the short-key
 * shape, so v1 blobs are not in-band for contradiction detection in either
 * client. This matches plugin's current behavior — cross-client parity.
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
  if (obj.c === 'dig' || obj.c === 'ent') return null;
  return obj as CanonicalClaim;
}

/** Is this candidate claim pinned (status `p`)? Delegates to WASM core. */
export function isPinnedClaim(claim: CanonicalClaim): boolean {
  try {
    return getWasm().isPinnedClaim(JSON.stringify(claim));
  } catch {
    return typeof claim.st === 'string' && claim.st === 'p';
  }
}

/**
 * Shape of the `existing_json` expected by WASM `detectContradictions` /
 * `resolveWithCandidates`. Matches `DetectContradictionsItem` in
 * `rust/totalreclaw-core/src/wasm.rs`.
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
 * Deduplicates by subgraph fact id across entities. Caps total per entity at
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
 * Uses `core.resolveWithCandidates()` (the full pipeline in one call: detect
 * contradictions + resolve pairs + pin check + tie-zone guard) — introduced in
 * core 1.5.0, shipped live in MCP's `@totalreclaw/core@2.0.0` dependency.
 *
 * If the WASM export is not available (core < 1.5.0) or fails, returns `[]`
 * and the write path falls back to the pre-Phase-2 behaviour. We do NOT ship
 * a legacy `detectContradictions + resolvePair` path — MCP 3.1.0 pins
 * `@totalreclaw/core ^2.0.0` which guarantees the unified export.
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

  // Build the WASM candidates payload. Drop any candidate whose embedding
  // is missing — the core short-circuits on empty vectors but we also drop
  // them from the lookup map so we don't waste a reference.
  const items: WasmExistingItem[] = candidates
    .filter((c) => c.embedding.length > 0)
    .map((c) => ({ claim: c.claim, id: c.id, embedding: c.embedding }));

  if (items.length === 0) return [];

  const byId = new Map<string, CandidateClaim>();
  for (const c of items) byId.set(c.id, { claim: c.claim, id: c.id, embedding: c.embedding });

  if (typeof core.resolveWithCandidates !== 'function') {
    // Unexpected: MCP 3.1.0 depends on core ^2.0.0 which ships this export.
    // If this branch triggers, something is materially wrong with the core
    // package — log and fall back to Phase-1 behaviour (no detection).
    logger.warn('Contradiction: core.resolveWithCandidates missing (core < 1.5.0?) — skipping detection');
    return [];
  }

  let actionsJson: string;
  try {
    actionsJson = core.resolveWithCandidates(
      JSON.stringify(newClaim),
      newClaimId,
      JSON.stringify(newEmbedding),
      JSON.stringify(items),
      weightsJson,
      thresholdLower,
      thresholdUpper,
      BigInt(Math.floor(nowUnixSeconds)),
      TIE_ZONE_SCORE_TOLERANCE,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.warn(`Contradiction: resolveWithCandidates failed: ${msg}`);
    return [];
  }

  let actions: Array<Record<string, unknown>>;
  try {
    actions = JSON.parse(actionsJson);
  } catch {
    return [];
  }
  if (actions.length === 0) return [];

  // Map core ResolutionAction (tagged enum) → local ResolutionDecision.
  const decisions: ResolutionDecision[] = [];
  for (const action of actions) {
    const type = action.type as string;
    if (type === 'supersede_existing') {
      const existing = byId.get(action.existing_id as string);
      decisions.push({
        action: 'supersede_existing',
        existingFactId: action.existing_id as string,
        existingClaim: existing?.claim ?? {},
        entityId: (action.entity_id as string) ?? '',
        similarity: (action.similarity as number) ?? 0,
        winnerScore: (action.winner_score as number) ?? 0,
        loserScore: (action.loser_score as number) ?? 0,
        winnerComponents: action.winner_components as ScoreComponents,
        loserComponents: action.loser_components as ScoreComponents,
      });
    } else if (type === 'skip_new') {
      const reason = action.reason as string;
      decisions.push({
        action: 'skip_new',
        reason: reason === 'existing_pinned' ? 'existing_pinned' : 'existing_wins',
        existingFactId: action.existing_id as string,
        entityId: (action.entity_id as string) ?? '',
        similarity: (action.similarity as number) ?? 0,
        winnerScore: action.winner_score as number | undefined,
        loserScore: action.loser_score as number | undefined,
        winnerComponents: action.winner_components as ScoreComponents | undefined,
        loserComponents: action.loser_components as ScoreComponents | undefined,
      });
    } else if (type === 'tie_leave_both') {
      decisions.push({
        action: 'tie_leave_both',
        existingFactId: action.existing_id as string,
        entityId: (action.entity_id as string) ?? '',
        similarity: (action.similarity as number) ?? 0,
        winnerScore: (action.winner_score as number) ?? 0,
        loserScore: (action.loser_score as number) ?? 0,
        winnerComponents: action.winner_components as ScoreComponents,
        loserComponents: action.loser_components as ScoreComponents,
      });
    }
    // no_contradiction actions are ignored.
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
 *   - empty list: no contradiction, proceed unchanged
 *
 * Never throws. On any failure (subgraph, decrypt, WASM), returns `[]` so the
 * write path falls back to pre-Phase-2 behaviour.
 *
 * Pin respect is handled inside WASM `resolve_with_candidates` via the core's
 * `respect_pin_in_resolution` — the TypeScript side does not need to re-check.
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

  // No entities → nothing to check.
  const entities = Array.isArray(newClaim.e) ? newClaim.e : [];
  if (entities.length === 0) return [];

  const nowUnixSeconds =
    typeof deps.nowUnixSeconds === 'number'
      ? deps.nowUnixSeconds
      : Math.floor(Date.now() / 1000);

  const weightsFile = await loadWeightsFile(nowUnixSeconds);
  const weightsJson = JSON.stringify(weightsFile.weights ?? {});
  const thresholdLower =
    typeof weightsFile.threshold_lower === 'number' ? weightsFile.threshold_lower : 0.3;
  const thresholdUpper =
    typeof weightsFile.threshold_upper === 'number' ? weightsFile.threshold_upper : 0.85;

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
    logger.warn(`Contradiction: candidate retrieval failed: ${msg}`);
    return [];
  }
  if (candidates.length === 0) return [];

  const rawDecisions = resolveWithCore({
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

  // Tie-zone guard: when the formula winner beats the loser by less than
  // TIE_ZONE_SCORE_TOLERANCE, the "contradiction" is rounding noise.
  // Core >= 1.5.0 already applies this inside resolveWithCandidates so this
  // map is typically a no-op; kept defensively in case the core path is
  // bypassed.
  const decisions = rawDecisions.map((d): ResolutionDecision => {
    if (
      d.action === 'supersede_existing' &&
      Math.abs(d.winnerScore - d.loserScore) < TIE_ZONE_SCORE_TOLERANCE
    ) {
      return {
        action: 'tie_leave_both',
        existingFactId: d.existingFactId,
        entityId: d.entityId,
        similarity: d.similarity,
        winnerScore: d.winnerScore,
        loserScore: d.loserScore,
        winnerComponents: d.winnerComponents,
        loserComponents: d.loserComponents,
      };
    }
    return d;
  });

  // Build decision-log entries. Prefer core.buildDecisionLogEntries (>= 1.5.0)
  // so the log format stays byte-for-byte compatible with the plugin.
  const core = getWasm();
  const useCoreDecisionLog = typeof core.buildDecisionLogEntries === 'function';

  if (useCoreDecisionLog) {
    try {
      const coreActions = _decisionsToCoreActions(decisions, newClaimId);
      const existingClaimsMap: Record<string, string> = {};
      for (const d of decisions) {
        if (d.action === 'supersede_existing') {
          try { existingClaimsMap[d.existingFactId] = JSON.stringify(d.existingClaim); } catch { /* skip */ }
        }
      }
      const entriesJson = core.buildDecisionLogEntries(
        JSON.stringify(coreActions),
        JSON.stringify(newClaim),
        JSON.stringify(existingClaimsMap),
        mode === 'shadow' ? 'shadow' : mode,
        BigInt(Math.floor(nowUnixSeconds)),
      );
      const entries: DecisionLogEntry[] = JSON.parse(entriesJson);
      for (const entry of entries) {
        await appendDecisionLog(entry);
        if (entry.action === 'tie_leave_both') {
          logger.info(
            `Contradiction: tie (gap=${Math.abs((entry.winner_score ?? 0) - (entry.loser_score ?? 0)).toFixed(6)} < ${TIE_ZONE_SCORE_TOLERANCE}, sim=${entry.similarity.toFixed(3)}, entity=${entry.entity_id}) — leaving both active`,
          );
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.warn(`Contradiction: buildDecisionLogEntries failed, falling back to inline: ${msg}`);
      await _appendDecisionLogInline(decisions, newClaimId, nowUnixSeconds, mode, logger);
    }
  } else {
    await _appendDecisionLogInline(decisions, newClaimId, nowUnixSeconds, mode, logger);
  }

  // Shadow-mode filtering via core.filterShadowMode (>= 1.5.0).
  if (typeof core.filterShadowMode === 'function') {
    try {
      const coreActions = _decisionsToCoreActions(decisions, newClaimId);
      const filteredJson = core.filterShadowMode(JSON.stringify(coreActions), mode);
      const filteredActions: Array<Record<string, unknown>> = JSON.parse(filteredJson);

      const byExistingId = new Map<string, ResolutionDecision>();
      for (const d of decisions) {
        if (d.action === 'supersede_existing' || d.action === 'skip_new' || d.action === 'tie_leave_both') {
          byExistingId.set(d.existingFactId, d);
        }
      }
      return filteredActions
        .map((a) => byExistingId.get(a.existing_id as string))
        .filter((d): d is ResolutionDecision => d !== undefined);
    } catch {
      // Fall through to local filtering.
    }
  }

  // Local fallback: shadow → empty, active → filter out ties.
  if (mode === 'shadow') return [];
  return decisions.filter((d) => d.action !== 'tie_leave_both');
}

/** Convert ResolutionDecision[] to core ResolutionAction JSON format. */
function _decisionsToCoreActions(
  decisions: ResolutionDecision[],
  newClaimId: string,
): Array<Record<string, unknown>> {
  return decisions.map((d) => {
    if (d.action === 'supersede_existing') {
      return {
        type: 'supersede_existing',
        existing_id: d.existingFactId, new_id: newClaimId,
        similarity: d.similarity, score_gap: Math.abs(d.winnerScore - d.loserScore),
        entity_id: d.entityId, winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
      };
    } else if (d.action === 'skip_new') {
      return {
        type: 'skip_new', reason: d.reason,
        existing_id: d.existingFactId, new_id: newClaimId,
        entity_id: d.entityId, similarity: d.similarity,
        winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
      };
    } else if (d.action === 'tie_leave_both') {
      return {
        type: 'tie_leave_both',
        existing_id: d.existingFactId, new_id: newClaimId,
        similarity: d.similarity, score_gap: Math.abs(d.winnerScore - d.loserScore),
        entity_id: d.entityId, winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
      };
    }
    return { type: 'no_contradiction' };
  });
}

/** Inline decision-log fallback (used when core.buildDecisionLogEntries unavailable). */
async function _appendDecisionLogInline(
  decisions: ResolutionDecision[],
  newClaimId: string,
  nowUnixSeconds: number,
  mode: AutoResolveMode,
  logger: ContradictionLogger,
): Promise<void> {
  for (const d of decisions) {
    if (d.action === 'supersede_existing') {
      let loserClaimJson: string | undefined;
      try { loserClaimJson = JSON.stringify(d.existingClaim); } catch { loserClaimJson = undefined; }
      await appendDecisionLog({
        ts: nowUnixSeconds, entity_id: d.entityId,
        new_claim_id: newClaimId, existing_claim_id: d.existingFactId,
        similarity: d.similarity,
        action: mode === 'shadow' ? 'shadow' : 'supersede_existing',
        reason: 'new_wins',
        winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
        loser_claim_json: loserClaimJson, mode,
      });
    } else if (d.action === 'skip_new') {
      await appendDecisionLog({
        ts: nowUnixSeconds, entity_id: d.entityId,
        new_claim_id: newClaimId, existing_claim_id: d.existingFactId,
        similarity: d.similarity,
        action: mode === 'shadow' ? 'shadow' : 'skip_new',
        reason: d.reason,
        winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
        mode,
      });
    } else if (d.action === 'tie_leave_both') {
      await appendDecisionLog({
        ts: nowUnixSeconds, entity_id: d.entityId,
        new_claim_id: newClaimId, existing_claim_id: d.existingFactId,
        similarity: d.similarity,
        action: 'tie_leave_both', reason: 'tie_below_tolerance',
        winner_score: d.winnerScore, loser_score: d.loserScore,
        winner_components: d.winnerComponents, loser_components: d.loserComponents,
        mode,
      });
      logger.info(
        `Contradiction: tie (gap=${Math.abs(d.winnerScore - d.loserScore).toFixed(6)} < ${TIE_ZONE_SCORE_TOLERANCE}, sim=${d.similarity.toFixed(3)}, entity=${d.entityId}) — leaving both active`,
      );
    }
  }
}
