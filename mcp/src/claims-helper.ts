/**
 * TotalReclaw MCP server — Knowledge Graph helpers for the write + read path.
 *
 * Mirrors `skill/plugin/claims-helper.ts` in behavior. Builds canonical Claim
 * JSON from an MCP-tool fact input, computes entity trapdoors, and decodes
 * decrypted blobs uniformly for both the new Claim shape and the legacy
 * {text, metadata} shape.
 *
 * MCP-specific differences vs the OpenClaw plugin helper:
 *   - No LLM compilation in MCP (plan §15.5): we ship digest READ only.
 *     Compilation is delegated to OpenClaw/Hermes on the shared vault.
 *   - No `hoursSince` / `shouldRecompile` / `DIGEST_CLAIM_CAP` guards.
 *   - No dependency on a plugin-local `ExtractedFact` type — MCP accepts
 *     raw tool arguments, so the builder takes a small `ClaimInput` struct.
 */

import crypto from 'node:crypto';
import type {
  MemoryTypeV1,
  MemorySource,
  MemoryScope,
  MemoryVolatility,
  MemoryEntityV1,
  MemoryClaimV1,
} from './v1-types.js';
import { MEMORY_CLAIM_V1_SCHEMA_VERSION } from './v1-types.js';

// The core WASM package ships as CJS. Jest (ts-jest) compiles MCP sources to
// commonjs so a plain `require` works there; TSC with `NodeNext` rewrites
// this to a dynamic import in ESM. Lazy-init so tests can spin up without
// paying WASM cost at module load.
// eslint-disable-next-line @typescript-eslint/no-var-requires
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    _wasm = require('@totalreclaw/core');
  }
  return _wasm!;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

import type { MemoryType } from './memory-types.js';

/**
 * Legacy alias for `MemoryType`, preserved so existing code that imports
 * `FactType` from this file keeps compiling. New code should import
 * `MemoryType` directly from `./memory-types.js`.
 *
 * @deprecated Use `MemoryType` from `./memory-types.js` instead.
 */
export type FactType = MemoryType;

export type EntityType = 'person' | 'project' | 'tool' | 'company' | 'concept' | 'place';

export interface ExtractedEntity {
  name: string;
  type: EntityType;
  role?: string;
}

export interface ClaimInput {
  /** Human-readable fact text. */
  text: string;
  /** One of the 8 memory types. Defaults to `fact` when absent. */
  type?: MemoryType;
  /** LLM-assessed confidence (0.0-1.0). Defaults to 0.85. */
  confidence?: number;
  /** Optional structured entities that surface on search. */
  entities?: ExtractedEntity[];
}

// ---------------------------------------------------------------------------
// Feature flag — TOTALRECLAW_CLAIM_FORMAT
// ---------------------------------------------------------------------------

export type ClaimFormat = 'claim' | 'legacy';

/**
 * Resolve the claim-format mode from the TOTALRECLAW_CLAIM_FORMAT env var.
 *
 * - `claim`  (default, or unset): new canonical Claim blob, entity trapdoors added.
 * - `legacy`: old {text, metadata} doc shape; entity trapdoors still added.
 *
 * Read on every call so tests can toggle via env without module reload.
 */
export function resolveClaimFormat(): ClaimFormat {
  const raw = (process.env.TOTALRECLAW_CLAIM_FORMAT ?? '').trim().toLowerCase();
  return raw === 'legacy' ? 'legacy' : 'claim';
}

// ---------------------------------------------------------------------------
// Category mapping — lives in memory-types.ts now (single source of truth for
// the MCP package). Phase 2.2.6 eliminated the duplicate that used to be
// defined in this file.
// ---------------------------------------------------------------------------

import { mapTypeToCategory } from './memory-types.js';

// Re-export for backward compat with callers that imported from claims-helper.
export { mapTypeToCategory };

// ---------------------------------------------------------------------------
// Canonical Claim builder
// ---------------------------------------------------------------------------

export interface BuildClaimInput {
  fact: ClaimInput;
  /** Integer 1-10; may differ from fact input after store-time dedup supersede. */
  importance: number;
  /** Who is writing this claim, e.g. `mcp-server` or `mcp-server:debrief`. */
  sourceAgent: string;
  /** ISO 8601 extracted-at timestamp; defaults to now. */
  extractedAt?: string;
}

/**
 * Construct a canonical Claim JSON string from a ClaimInput.
 *
 * The output is byte-identical to what the Rust/Python/plugin clients would
 * produce for the same logical claim (field order, default-omission rules,
 * etc.). Encrypt this string directly — do not re-stringify it.
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
// Legacy {text, metadata} doc shape (unchanged from pre-KG MCP store path).
// ---------------------------------------------------------------------------

export interface BuildLegacyDocInput {
  fact: ClaimInput;
  importance: number;
  source: string;
  createdAt?: string;
}

/**
 * Build the legacy `{text, metadata}` document shape.
 *
 * Kept so the TOTALRECLAW_CLAIM_FORMAT=legacy fallback writes blobs that
 * the existing parseClaimOrLegacy read path has always handled.
 */
export function buildLegacyDoc(input: BuildLegacyDocInput): string {
  const { fact, importance, source, createdAt } = input;
  return JSON.stringify({
    text: fact.text,
    metadata: {
      type: fact.type ?? 'fact',
      importance: importance / 10,
      source,
      created_at: createdAt ?? new Date().toISOString(),
    },
  });
}

// ---------------------------------------------------------------------------
// Digest helpers (read-only in MCP scope)
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
export const DIGEST_SOURCE_AGENT = 'mcp-server-digest';

export type DigestMode = 'on' | 'off' | 'template';

/**
 * Resolve TOTALRECLAW_DIGEST_MODE.
 *
 * - `on` (default, unset, unknown): digest injection when a compiled digest
 *   is available in the vault.
 * - `off`: legacy individual-fact search path, no digest injection.
 * - `template`: same as `on` for MCP (MCP never compiles digests — the flag
 *   is accepted for parity with the plugin so operators can use the same env
 *   var across clients).
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
      const importance =
        typeof obj.i === 'number' ? Math.max(1, Math.min(10, Math.round(obj.i))) : 5;
      return {
        text: obj.t,
        importance,
        category: obj.c,
        metadata: {
          type: obj.c,
          importance: importance / 10,
          source: typeof obj.sa === 'string' ? obj.sa : 'mcp_remember',
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
  digestJson: string;
  compiledAt: string;
}

/**
 * Wrap a serialized Digest JSON as a canonical Claim.
 *
 * MCP does not compile digests (no LLM access). This helper exists solely so
 * tests can round-trip digest claims through the read path, and so any
 * future MCP compilation route would use a shared helper.
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
 * Parse a canonical Claim JSON and, if it is a digest claim, return the
 * wrapped Digest object. Returns null for non-digest claims or parse errors.
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
 * Accepts canonical Claim JSON (`{c:"dig",...}`). Returns false for legacy
 * `{text, metadata}` docs and any parse error.
 */
export function isDigestBlob(decrypted: string): boolean {
  try {
    const obj = JSON.parse(decrypted);
    return obj && typeof obj === 'object' && obj.c === DIGEST_CATEGORY;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Entity trapdoors
// ---------------------------------------------------------------------------

/**
 * Compute a single entity trapdoor: sha256("entity:" + normalized_name) as hex.
 *
 * Uses the same primitive (plain SHA-256, not HMAC) as word / stem trapdoors.
 * The `entity:` prefix namespaces the result so a user called "postgresql"
 * never collides with the word trapdoor for the token "postgresql". The
 * search path must construct queries with the same prefix.
 *
 * Rationale for plain SHA-256 vs HMAC: the existing word trapdoor
 * implementation in `rust/totalreclaw-core/src/blind.rs` uses plain SHA-256
 * of the normalized token. For entity trapdoors to appear in the same
 * blindIndices array and be findable by the current search pipeline, they
 * must use the same primitive. This matches the plugin's decision byte-for-byte.
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
export function computeEntityTrapdoors(
  entities: readonly ExtractedEntity[] | undefined,
): string[] {
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

// ---------------------------------------------------------------------------
// v1 taxonomy — canonical blob builder
// ---------------------------------------------------------------------------

/**
 * Input shape for building a Memory Taxonomy v1 blob. All fields mirror the
 * MemoryClaimV1 schema; the builder fills in required timestamps/UUIDs so
 * callers can pass the subset they know about.
 */
export interface BuildV1ClaimInput {
  text: string;
  type: MemoryTypeV1;
  source: MemorySource;
  id?: string;                     // UUIDv7 (generated when absent)
  createdAt?: string;              // ISO8601 UTC (now when absent)
  scope?: MemoryScope;
  volatility?: MemoryVolatility;
  reasoning?: string;
  entities?: MemoryEntityV1[];
  expiresAt?: string;
  importance?: number;             // 1-10 (optional advisory)
  confidence?: number;             // 0-1
  supersededBy?: string;           // claim id override
}

/**
 * Construct a v1 canonical claim JSON string, validated through the core
 * `validateMemoryClaimV1` WASM export. The output is UTF-8 JSON ready to be
 * encrypted as the inner blob of a protobuf-v4 fact (wrapper `version = 4`).
 *
 * Returns the canonical JSON (not the parsed object) so callers can encrypt
 * it directly without risking non-deterministic key ordering.
 *
 * Throws if the input is malformed (missing required field, invalid enum
 * value). The MCP tool handlers narrow with `isValidMemoryTypeV1` etc. before
 * calling in, so this should be unreachable from the tool schema path.
 */
export function buildV1ClaimBlob(input: BuildV1ClaimInput): string {
  const claim: MemoryClaimV1 = {
    id: input.id ?? crypto.randomUUID(),
    text: input.text,
    type: input.type,
    source: input.source,
    created_at: input.createdAt ?? new Date().toISOString(),
    schema_version: MEMORY_CLAIM_V1_SCHEMA_VERSION,
  };

  if (input.scope && input.scope !== 'unspecified') claim.scope = input.scope;
  if (input.volatility && input.volatility !== 'updatable') claim.volatility = input.volatility;
  if (input.entities && input.entities.length > 0) claim.entities = input.entities;
  if (input.reasoning) claim.reasoning = input.reasoning;
  if (input.expiresAt) claim.expires_at = input.expiresAt;
  if (typeof input.importance === 'number') claim.importance = input.importance;
  if (typeof input.confidence === 'number') claim.confidence = input.confidence;
  if (input.supersededBy) claim.superseded_by = input.supersededBy;

  // Canonicalise + validate via core. Throws on any schema violation.
  return getWasm().validateMemoryClaimV1(JSON.stringify(claim));
}

/**
 * Per-blob read result surfaced through the legacy-friendly reader. Extends
 * `BlobReadResult` with v1-specific fields when present.
 */
export interface V1BlobReadResult {
  /** The decrypted text payload. */
  text: string;
  /** 1-10 normalized importance. Defaults to 5. */
  importance: number;
  /**
   * Category short-key for back-compat reporting (e.g. `pref`, `rule`). For v1
   * blobs this is derived from the v1 `type` via V1_TYPE_TO_SHORT_CATEGORY.
   */
  category: string;
  /** Raw v1 metadata when the blob is a MemoryClaimV1. */
  v1?: {
    type: MemoryTypeV1;
    source: MemorySource;
    scope?: MemoryScope;
    volatility?: MemoryVolatility;
    reasoning?: string;
    expires_at?: string;
    confidence?: number;
    superseded_by?: string;
    entities?: MemoryEntityV1[];
    created_at: string;
  };
  /** Whatever metadata the blob carried (v0 or v1). Used by callers that want raw access. */
  metadata: Record<string, unknown>;
}

/**
 * Parse a decrypted blob into a uniform structure supporting v1, canonical v0
 * (`{t, c, i, ...}`), and legacy plugin (`{text, metadata: {...}}`).
 *
 * The v0 reader at `readClaimFromBlob` is retained as a separate function so
 * the pin-chain logic in `pin.ts` (which mutates short-key claims) keeps its
 * exact behaviour. Callers that need the full v1 surface should use this one.
 */
export function readBlobUnified(decryptedJson: string): V1BlobReadResult {
  try {
    const obj = JSON.parse(decryptedJson) as Record<string, unknown>;

    // v1 path: presence of top-level `text` + `type` (closed 6-value enum)
    // is the v1 signature. `schema_version` is omitted when it equals the
    // default (per skip_serializing_if in the Rust struct), so we can't
    // require it — any v1 enum value distinguishes v1 from legacy.
    const v1Types = new Set<string>([
      'claim',
      'preference',
      'directive',
      'commitment',
      'episode',
      'summary',
    ]);
    if (
      typeof obj.text === 'string' &&
      typeof obj.type === 'string' &&
      v1Types.has(String(obj.type)) &&
      // Optional `schema_version`: when present must be the v1 constant.
      (typeof obj.schema_version !== 'string' ||
        obj.schema_version === MEMORY_CLAIM_V1_SCHEMA_VERSION)
    ) {
      const importance =
        typeof obj.importance === 'number' ? Math.max(1, Math.min(10, Math.round(obj.importance))) : 5;
      const typeStr = String(obj.type);
      const sourceStr = typeof obj.source === 'string' ? obj.source : 'user-inferred';
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const wasm = require('@totalreclaw/core') as typeof import('@totalreclaw/core');
      // `parseMemoryTypeV1` returns the enum string unwrapped (e.g. "directive"),
      // not JSON — do not JSON.parse the return value.
      const v1Type = wasm.parseMemoryTypeV1(typeStr) as MemoryTypeV1;
      const v1Source = wasm.parseMemorySource(sourceStr) as MemorySource;

      // Map v1 type to legacy short-key for recall display. Inline import to
      // avoid a circular dep between v1-types and claims-helper.
      const shortMap: Record<string, string> = {
        claim: 'claim',
        preference: 'pref',
        directive: 'rule',
        commitment: 'goal',
        episode: 'epi',
        summary: 'sum',
      };
      const short = shortMap[v1Type] ?? 'claim';

      return {
        text: obj.text,
        importance,
        category: short,
        v1: {
          type: v1Type,
          source: v1Source,
          scope: typeof obj.scope === 'string' ? (obj.scope as MemoryScope) : undefined,
          volatility:
            typeof obj.volatility === 'string' ? (obj.volatility as MemoryVolatility) : undefined,
          reasoning: typeof obj.reasoning === 'string' ? obj.reasoning : undefined,
          expires_at: typeof obj.expires_at === 'string' ? obj.expires_at : undefined,
          confidence: typeof obj.confidence === 'number' ? obj.confidence : undefined,
          superseded_by:
            typeof obj.superseded_by === 'string' ? obj.superseded_by : undefined,
          entities: Array.isArray(obj.entities) ? (obj.entities as MemoryEntityV1[]) : undefined,
          created_at: typeof obj.created_at === 'string' ? obj.created_at : '',
        },
        metadata: obj as Record<string, unknown>,
      };
    }
  } catch {
    // fall through to v0 / legacy parser
  }

  // Fall back to the legacy parser for v0 canonical + plugin-legacy shapes.
  // This preserves the older category mapping (`fact`, `pref`, etc.).
  return readClaimFromBlob(decryptedJson);
}

