/**
 * TotalReclaw Plugin — Knowledge Graph helpers for the write path.
 *
 * Builds canonical Claim JSON from an ExtractedFact, generates entity
 * trapdoors for blind search, and resolves the claim-format feature flag.
 *
 * The canonical Claim schema uses compact short keys (t, c, cf, i, sa, ea, e, ...)
 * and is produced byte-identically across Rust, WASM, and Python via
 * `canonicalizeClaim()` in @totalreclaw/core.
 */

import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import type { ExtractedEntity, ExtractedFact } from './extractor.js';

// Lazy-load WASM. We use createRequire so this module loads cleanly under
// both the OpenClaw runtime (CJS-ish tsx) and bare Node ESM (used by tests).
const requireWasm = createRequire(import.meta.url);
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm() {
  if (!_wasm) _wasm = requireWasm('@totalreclaw/core');
  return _wasm!;
}

// ---------------------------------------------------------------------------
// Feature flag
// ---------------------------------------------------------------------------

export type ClaimFormat = 'claim' | 'legacy';

/**
 * Resolve the claim-format mode from the TOTALRECLAW_CLAIM_FORMAT env var.
 *
 * - `claim`  (default, or unset): new canonical Claim blob, entity trapdoors added.
 * - `legacy`: old {text, metadata} doc shape; entity trapdoors still added.
 *
 * Read on every call (cheap) so tests can toggle via env without module reload.
 */
export function resolveClaimFormat(): ClaimFormat {
  const raw = (process.env.TOTALRECLAW_CLAIM_FORMAT ?? '').trim().toLowerCase();
  return raw === 'legacy' ? 'legacy' : 'claim';
}

// ---------------------------------------------------------------------------
// Category mapping (ExtractedFact.type → compact Claim category short key)
// ---------------------------------------------------------------------------

const TYPE_TO_CATEGORY: Record<ExtractedFact['type'], string> = {
  fact: 'fact',
  preference: 'pref',
  decision: 'dec',
  episodic: 'epi',
  goal: 'goal',
  context: 'ctx',
  summary: 'sum',
};

export function mapTypeToCategory(type: ExtractedFact['type']): string {
  return TYPE_TO_CATEGORY[type];
}

// ---------------------------------------------------------------------------
// Canonical Claim builder
// ---------------------------------------------------------------------------

export interface BuildClaimInput {
  fact: ExtractedFact;
  importance: number; // 1-10, may differ from fact.importance after store-time dedup supersede
  sourceAgent: string;
  extractedAt?: string; // ISO 8601; defaults to now
}

/**
 * Construct a canonical Claim JSON string from an ExtractedFact.
 *
 * The output is byte-identical to what the Rust/Python clients would produce
 * for the same logical claim (same field order, default omission rules, etc.).
 * Encrypt this string directly — do not re-stringify it.
 */
export function buildCanonicalClaim(input: BuildClaimInput): string {
  const { fact, importance, sourceAgent, extractedAt } = input;

  const claim: Record<string, unknown> = {
    t: fact.text,
    c: mapTypeToCategory(fact.type),
    cf: fact.confidence ?? 0.85,
    i: importance,
    sa: sourceAgent,
    ea: extractedAt ?? new Date().toISOString(),
  };

  if (fact.entities && fact.entities.length > 0) {
    claim.e = fact.entities.map((e) => {
      const entity: Record<string, unknown> = { n: e.name, tp: e.type };
      if (e.role) entity.r = e.role;
      return entity;
    });
  }

  return getWasm().canonicalizeClaim(JSON.stringify(claim));
}

// ---------------------------------------------------------------------------
// Legacy {text, metadata} doc shape (unchanged from pre-KG storeExtractedFacts).
// ---------------------------------------------------------------------------

export interface BuildLegacyDocInput {
  fact: ExtractedFact;
  importance: number;
  source: string;
  createdAt?: string;
}

/**
 * Build the legacy `{text, metadata}` document shape.
 *
 * Kept so the TOTALRECLAW_CLAIM_FORMAT=legacy fallback can write blobs that
 * the existing parseClaimOrLegacy path has always handled.
 */
export function buildLegacyDoc(input: BuildLegacyDocInput): string {
  const { fact, importance, source, createdAt } = input;
  return JSON.stringify({
    text: fact.text,
    metadata: {
      type: fact.type,
      importance: importance / 10,
      source,
      created_at: createdAt ?? new Date().toISOString(),
    },
  });
}

// ---------------------------------------------------------------------------
// Digest helpers (Stage 3b read path)
// ---------------------------------------------------------------------------

/**
 * Well-known blind index marker used to locate digest claims on the subgraph.
 * Computed as plain SHA-256("type:digest") — same primitive as word trapdoors
 * so it lives in the existing `blindIndices` array. The `type:` namespace
 * prefix keeps it distinct from any user word trapdoor.
 */
export const DIGEST_TRAPDOOR: string = crypto
  .createHash('sha256')
  .update('type:digest')
  .digest('hex');

/** Compact category short key for digest claims (ClaimCategory::Digest). */
export const DIGEST_CATEGORY = 'dig';

/** Distinctive source marker so operators can grep for digest writes. */
export const DIGEST_SOURCE_AGENT = 'openclaw-plugin-digest';

/**
 * Hard ceiling on claim count for LLM-assisted digest compilation.
 * Above this, we skip the LLM entirely and use the template path to keep
 * token cost bounded. See plan §9 and Stage 3b design question #3.
 */
export const DIGEST_CLAIM_CAP = 200;

export type DigestMode = 'on' | 'off' | 'template';

/**
 * Resolve TOTALRECLAW_DIGEST_MODE.
 *
 * - `on` (default, unset, unknown): digest injection + LLM compilation when
 *   an LLM is configured, template fallback otherwise.
 * - `off`: legacy individual-fact search path, no digest injection.
 * - `template`: digest injection but skip LLM entirely (template only).
 *
 * Read per-call so tests can toggle via env without module reload.
 */
export function resolveDigestMode(): DigestMode {
  const raw = (process.env.TOTALRECLAW_DIGEST_MODE ?? '').trim().toLowerCase();
  if (raw === 'off') return 'off';
  if (raw === 'template') return 'template';
  return 'on';
}

// ---------------------------------------------------------------------------
// Decrypted blob reader — handles both new Claim ({t,c,i,...}) and
// legacy {text, metadata: {importance: 0-1}} formats transparently.
// Any decrypt site should use this instead of parsing doc.text directly.
// ---------------------------------------------------------------------------

export interface BlobReadResult {
  text: string;
  importance: number; // integer 1-10
  category: string;
  metadata: Record<string, unknown>;
}

export function readClaimFromBlob(decryptedJson: string): BlobReadResult {
  try {
    const obj = JSON.parse(decryptedJson) as Record<string, unknown>;
    // New canonical Claim format: short keys
    if (typeof obj.t === 'string' && typeof obj.c === 'string') {
      const importance = typeof obj.i === 'number' ? Math.max(1, Math.min(10, Math.round(obj.i))) : 5;
      return {
        text: obj.t,
        importance,
        category: obj.c,
        metadata: {
          type: obj.c,
          importance: importance / 10,
          source: typeof obj.sa === 'string' ? obj.sa : 'auto-extraction',
          created_at: typeof obj.ea === 'string' ? obj.ea : '',
        },
      };
    }
    // Legacy plugin {text, metadata: {importance: 0-1}} format
    if (typeof obj.text === 'string') {
      const meta = (obj.metadata as Record<string, unknown>) ?? {};
      const impFloat = typeof meta.importance === 'number' ? meta.importance : 0.5;
      const importance = Math.max(1, Math.min(10, Math.round(impFloat * 10)));
      return {
        text: obj.text,
        importance,
        category: typeof meta.type === 'string' ? meta.type : 'fact',
        metadata: meta,
      };
    }
  } catch {
    // fall through
  }
  return { text: decryptedJson, importance: 5, category: 'fact', metadata: {} };
}

export interface BuildDigestClaimInput {
  /** The full Digest JSON produced by buildTemplateDigest / assembleDigestFromLlm. */
  digestJson: string;
  /** ISO 8601 timestamp the digest was compiled at. Becomes the `ea` field. */
  compiledAt: string;
}

/**
 * Wrap a serialized Digest JSON as a canonical Claim so it can be encrypted
 * and stored on-chain via the same pipeline as regular facts.
 *
 * Stores the raw Digest JSON as the claim's `t` (text) field. Reader path
 * is `parseClaimOrLegacy(decrypted) → extractDigestFromClaim`.
 *
 * Digest claims deliberately carry no entity refs — otherwise entity
 * trapdoors would surface the digest blob in normal recall queries.
 */
export function buildDigestClaim(input: BuildDigestClaimInput): string {
  const { digestJson, compiledAt } = input;
  const claim = {
    t: digestJson,
    c: DIGEST_CATEGORY,
    cf: 1.0,
    i: 10,
    sa: DIGEST_SOURCE_AGENT,
    ea: compiledAt,
  };
  return getWasm().canonicalizeClaim(JSON.stringify(claim));
}

/**
 * Parse a canonical Claim JSON (produced by parseClaimOrLegacy) and, if it is
 * a digest claim, return the wrapped Digest object. Returns null if the claim
 * is not of category `dig` or if the inner JSON fails to parse.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function extractDigestFromClaim(canonicalClaimJson: string): any | null {
  let claim: { c?: string; t?: string };
  try {
    claim = JSON.parse(canonicalClaimJson);
  } catch {
    return null;
  }
  if (claim.c !== DIGEST_CATEGORY || typeof claim.t !== 'string') return null;
  try {
    const digest = JSON.parse(claim.t);
    // Minimal shape check: a Digest must at least have prompt_text.
    if (typeof digest !== 'object' || digest === null) return null;
    if (typeof digest.prompt_text !== 'string') return null;
    return digest;
  } catch {
    return null;
  }
}

/**
 * Lightweight check: does this decrypted blob look like a digest claim?
 * Used to filter digest blobs out of user-facing recall results.
 *
 * Accepts both canonical Claim JSON (`{c:"dig",...}`) and the already-parsed
 * form; returns false for legacy `{text, metadata}` docs and any parse error.
 */
export function isDigestBlob(decrypted: string): boolean {
  try {
    const obj = JSON.parse(decrypted);
    return obj && typeof obj === 'object' && obj.c === DIGEST_CATEGORY;
  } catch {
    return false;
  }
}

/**
 * Hours between two timestamps.
 *
 * Returns `Infinity` when `compiledAtIso` is unparseable (forces a recompile,
 * which is the safe default when we can't trust the stored timestamp). Returns
 * 0 for future dates (clock-skew defensive).
 */
export function hoursSince(compiledAtIso: string, nowMs: number): number {
  const then = Date.parse(compiledAtIso);
  if (Number.isNaN(then)) return Infinity;
  const deltaMs = nowMs - then;
  if (deltaMs <= 0) return 0;
  return deltaMs / (1000 * 60 * 60);
}

/**
 * The digest is stale if new claims have been written since it was compiled.
 * Both inputs are Unix seconds.
 *
 * Falsely-equal or regressing values (clock skew, empty vault) return false —
 * we only recompile on strictly-newer evidence.
 */
export function isDigestStale(
  digestVersion: number,
  currentMaxCreatedAtUnix: number,
): boolean {
  return currentMaxCreatedAtUnix > digestVersion;
}

export interface RecompileGuardInput {
  countNewClaims: number;
  hoursSinceCompilation: number;
}

/**
 * Recompile guard (plan §15.10):
 *   trigger if countNewClaims >= 10 OR hoursSinceCompilation >= 24.
 *
 * The caller is still responsible for the in-memory "in progress" flag
 * (see digest-sync.ts) — this is a pure predicate.
 */
export function shouldRecompile(input: RecompileGuardInput): boolean {
  const { countNewClaims, hoursSinceCompilation } = input;
  return countNewClaims >= 10 || hoursSinceCompilation >= 24;
}

// ---------------------------------------------------------------------------
// Entity trapdoors
// ---------------------------------------------------------------------------

/**
 * Compute a single entity trapdoor: sha256("entity:" + normalized_name) as hex.
 *
 * Uses the same primitive (plain SHA-256, not HMAC) as word / stem trapdoors in
 * `generateBlindIndices()`. The `entity:` prefix namespaces the result so a
 * user called "postgresql" never collides with the word trapdoor for the token
 * "postgresql". The search path must construct queries with the same prefix.
 *
 * Rationale for plain SHA-256 vs HMAC: the existing word trapdoor implementation
 * in `rust/totalreclaw-core/src/blind.rs` uses plain SHA-256 of the normalized
 * token (no dedup_key). For entity trapdoors to appear in the same blindIndices
 * array and be findable by the current search pipeline, they must use the same
 * primitive. Adopting HMAC for entities alone would break search consistency.
 */
export function computeEntityTrapdoor(name: string): string {
  const normalized = getWasm().normalizeEntityName(name);
  return crypto
    .createHash('sha256')
    .update('entity:' + normalized)
    .digest('hex');
}

/**
 * Compute entity trapdoors for every entity on a fact, deduplicated.
 * Returns an empty array when the fact has no entities.
 */
export function computeEntityTrapdoors(entities: readonly ExtractedEntity[] | undefined): string[] {
  if (!entities || entities.length === 0) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const e of entities) {
    const td = computeEntityTrapdoor(e.name);
    if (!seen.has(td)) {
      seen.add(td);
      out.push(td);
    }
  }
  return out;
}
