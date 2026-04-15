/**
 * The 8 canonical memory types — single source of truth for the MCP server package.
 *
 * Must stay in sync with `skill/plugin/extractor.ts` (`VALID_MEMORY_TYPES`),
 * `python/src/totalreclaw/agent/extraction.py` (`VALID_TYPES`), and the Rust
 * `ClaimCategory` enum in `rust/totalreclaw-core/src/claims.rs`.
 *
 * A cross-package parity test at `tests/parity/memory-types-parity.test.ts`
 * asserts this list matches the plugin's equivalent. When adding a new type,
 * update ALL the places listed in the plugin's equivalent file.
 *
 * Any MCP-side consumer (tool schemas, type mappings, validation whitelists)
 * MUST import from this constant — never re-declare the list inline.
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
 * single-source-of-truth invariant is enforced.
 */
export function isValidMemoryType(value: unknown): value is MemoryType {
  return typeof value === 'string' && (VALID_MEMORY_TYPES as readonly string[]).includes(value);
}

/**
 * Compact short-form keys used in the on-chain canonical Claim `c` field.
 * Must match the Rust `ClaimCategory` serde_rename values in
 * `rust/totalreclaw-core/src/claims.rs` byte-for-byte. Cross-language parity
 * tests in `tests/parity/` enforce this.
 */
export const TYPE_TO_CATEGORY: Record<MemoryType, string> = {
  fact: 'fact',
  preference: 'pref',
  decision: 'dec',
  episodic: 'epi',
  goal: 'goal',
  context: 'ctx',
  summary: 'sum',
  rule: 'rule',
};

/**
 * Map a `MemoryType` to its compact short-form key for the on-chain blob.
 * Accepts `undefined` (falls back to `'fact'`) for call sites that pass an
 * optional type.
 */
export function mapTypeToCategory(type: MemoryType | undefined): string {
  if (!type) return 'fact';
  return TYPE_TO_CATEGORY[type];
}
