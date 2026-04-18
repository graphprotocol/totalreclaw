/**
 * Memory Taxonomy v1 — client-side TypeScript mirrors of the canonical
 * enums defined in `rust/totalreclaw-core/src/claims.rs`.
 *
 * Single source of truth for MCP code that accepts / validates v1 fields on
 * the way in from the tool schema. The actual (de)serialization goes through
 * `@totalreclaw/core`'s `validateMemoryClaimV1` / `parseMemoryTypeV1` /
 * `parseMemorySource` — this module only covers the string enumerations
 * so callers can narrow `unknown` tool args without round-tripping WASM.
 *
 * Spec: `docs/specs/totalreclaw/memory-taxonomy-v1.md`.
 */

/** Closed enum of v1 speech-act-grounded memory types. */
export const VALID_MEMORY_TYPES_V1 = [
  'claim',
  'preference',
  'directive',
  'commitment',
  'episode',
  'summary',
] as const;

export type MemoryTypeV1 = (typeof VALID_MEMORY_TYPES_V1)[number];

/** Provenance source for a memory claim (Tier 1 retrieval signal). */
export const VALID_MEMORY_SOURCES = [
  'user',
  'user-inferred',
  'assistant',
  'external',
  'derived',
] as const;

export type MemorySource = (typeof VALID_MEMORY_SOURCES)[number];

/**
 * Life-domain scope for a memory claim. The enum is open-extensible per the
 * v1 spec, but every v1-compliant client MUST accept these eight values when
 * reading from a vault written by another client.
 */
export const VALID_MEMORY_SCOPES = [
  'work',
  'personal',
  'health',
  'family',
  'creative',
  'finance',
  'misc',
  'unspecified',
] as const;

export type MemoryScope = (typeof VALID_MEMORY_SCOPES)[number];

/** Temporal stability of a memory claim (assigned in comparative rescoring pass). */
export const VALID_MEMORY_VOLATILITIES = ['stable', 'updatable', 'ephemeral'] as const;

export type MemoryVolatility = (typeof VALID_MEMORY_VOLATILITIES)[number];

/** Entity reference inside a v1 claim. */
export interface MemoryEntityV1 {
  name: string;
  type: 'person' | 'project' | 'tool' | 'company' | 'concept' | 'place';
  role?: string;
}

/**
 * The encrypted-blob payload for protobuf v4. Written as UTF-8 JSON inside
 * the XChaCha20-Poly1305 envelope. Matches `MemoryClaimV1` in the Rust core.
 */
export interface MemoryClaimV1 {
  // REQUIRED
  id: string;
  text: string;
  type: MemoryTypeV1;
  source: MemorySource;
  created_at: string;
  schema_version: '1.0';

  // ORTHOGONAL AXES (defaults applied if absent)
  scope?: MemoryScope;
  volatility?: MemoryVolatility;

  // STRUCTURED FIELDS
  entities?: MemoryEntityV1[];
  reasoning?: string;
  expires_at?: string;

  // ADVISORY
  importance?: number;
  confidence?: number;
  superseded_by?: string;
}

/** v1 inner-blob schema version — must match the Rust core constant. */
export const MEMORY_CLAIM_V1_SCHEMA_VERSION = '1.0' as const;

/** Outer protobuf wrapper version that signals "inner blob is v1 JSON". */
export const PROTOBUF_WRAPPER_VERSION_V1 = 4 as const;

// ── Runtime type guards ──────────────────────────────────────────────────────

export function isValidMemoryTypeV1(value: unknown): value is MemoryTypeV1 {
  return typeof value === 'string' && (VALID_MEMORY_TYPES_V1 as readonly string[]).includes(value);
}

export function isValidMemorySource(value: unknown): value is MemorySource {
  return typeof value === 'string' && (VALID_MEMORY_SOURCES as readonly string[]).includes(value);
}

export function isValidMemoryScope(value: unknown): value is MemoryScope {
  return typeof value === 'string' && (VALID_MEMORY_SCOPES as readonly string[]).includes(value);
}

export function isValidMemoryVolatility(value: unknown): value is MemoryVolatility {
  return typeof value === 'string' && (VALID_MEMORY_VOLATILITIES as readonly string[]).includes(value);
}

// ── Legacy-type migration helper (for import adapters) ──────────────────────

/**
 * Map a legacy v0 type (8-type taxonomy) to its v1 replacement. Reference
 * table from `docs/specs/totalreclaw/memory-taxonomy-v1.md` §migration-from-v0.
 *
 * Callers should inspect the source fact and populate `reasoning` separately
 * when they see a legacy `decision` type (v1 merges decision → claim with the
 * rationale captured in the `reasoning` field).
 */
export const LEGACY_TYPE_TO_V1: Record<string, MemoryTypeV1> = {
  fact: 'claim',
  context: 'claim',
  decision: 'claim',
  preference: 'preference',
  rule: 'directive',
  goal: 'commitment',
  episodic: 'episode',
  summary: 'summary',
};

/**
 * Map a v1 type back to its legacy short-form category key used in v0 blobs
 * (`{c: "pref"}` etc). The ZeroClaw + plugin read paths still emit these
 * short keys for back-compat; we expose the same mapping so MCP reports
 * uniform category strings in recall output regardless of blob version.
 */
export const V1_TYPE_TO_SHORT_CATEGORY: Record<MemoryTypeV1, string> = {
  claim: 'claim',
  preference: 'pref',
  directive: 'rule', // legacy short-form is `rule`
  commitment: 'goal',
  episode: 'epi',
  summary: 'sum',
};
