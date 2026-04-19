# Changelog

All notable changes to `@totalreclaw/core` / `totalreclaw-core` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.1] - 2026-04-19

### Added

- **Memory Taxonomy v1.1 — additive `pin_status` field** on
  [`MemoryClaimV1`](./src/claims.rs). New enum `PinStatus` with variants
  `Pinned` / `Unpinned` (serde kebab → `"pinned"` / `"unpinned"`).
  The field is `Option<PinStatus>` with `#[serde(skip_serializing_if =
  "Option::is_none")]` so pre-v1.1 blobs continue to round-trip byte-for-byte.
- **Unified pin detection** — [`is_pinned_json`](./src/claims.rs) now
  recognizes BOTH the legacy v0 short-key sentinel (`st == "p"`) AND the
  new v1.1 field (`pin_status == "pinned"`). Dispatch: try `MemoryClaimV1`
  first; if that parses successfully, it is authoritative (no fall-through
  to the v0 parser). Back-compat: every input accepted by pre-2.1.1
  `is_pinned_json` returns an unchanged result.
- New helper [`is_pinned_memory_claim_v1`](./src/claims.rs) for v1-only
  callers that have already parsed the blob.
- **WASM bindings**: `parsePinStatus`, `isPinnedClaimJson`.
- **PyO3 bindings**: `parse_pin_status`, `is_pinned_claim_json`.
- Spec: `docs/specs/totalreclaw/memory-taxonomy-v1.md` bumped to v1.1
  (additive — on-wire `schema_version` stays `"1.0"` so existing strict
  validators are unaffected).

### Notes / Compatibility

- Patch-version bump (additive serde field only). No breaking changes.
- Motivated by 2026-04-19 RC QA bug #2: `totalreclaw_pin` shipped v0
  short-key blobs at protobuf `version = 3`, breaking the "v1 on-chain"
  contract for new pins. Rust core gains the field; plugin + mcp pin
  paths are rewired in parallel (plugin 3.1.0 + mcp 3.2.0). See
  `mcp/AUDIT-v1-tools.md` §A2 for the original deferred gap note.
- Existing `ClaimStatus::Pinned` sentinel (v0) is UNCHANGED — v0 blobs
  continue to decode and `is_pinned_claim(&Claim)` still returns `true`
  for them.

### References

- Spec: [`docs/specs/totalreclaw/memory-taxonomy-v1.md`](../../docs/specs/totalreclaw/memory-taxonomy-v1.md) §pin-semantics (v1.1)
- QA: `totalreclaw-internal/docs/notes/QA-openclaw-RC-3.0.7-rc.1-20260420.md` bug #2

## [2.1.0] - 2026-04-19

### Added

- **Memory Taxonomy v1 string-level exports** — the six canonical v1 memory
  types and their compact-category mapping are now first-class exports of
  core, eliminating client-side duplication (previously mirrored in
  `skill/plugin/extractor.ts`, `mcp/src/memory-types.ts`,
  `mcp/src/v1-types.ts`, `skill-nanoclaw/src/extraction/prompts.ts`, and
  `python/src/totalreclaw/agent/extraction.py`). New module
  [`memory_types`](./src/memory_types.rs) exposes:
  - `VALID_MEMORY_TYPES: [&str; 6]` — the closed enum in spec order
    (`claim, preference, directive, commitment, episode, summary`).
  - `TYPE_TO_CATEGORY: &[(&str, &str)]` — long-form v1 type → compact
    display short key used by the on-chain `c` field and recall tags.
  - `is_valid_memory_type(&str) -> bool` — case-sensitive runtime guard.
  - `map_type_to_category(&str) -> Option<&'static str>` — lookup helper.
- **WASM bindings** (feature `wasm`): `getValidMemoryTypes`,
  `getTypeToCategory`, `mapTypeToCategory`, `isValidMemoryType`.
- **PyO3 bindings** (feature `python`): `get_valid_memory_types`,
  `get_type_to_category`, `py_map_type_to_category`,
  `py_is_valid_memory_type`.

### Notes / Compatibility

- The existing internal `MemoryTypeV1` enum in `claims.rs` is unchanged;
  tests in `memory_types::tests` guard against drift between the enum
  variants and the new string-level exports. Clients that already wire
  through `parseMemoryTypeV1` / `parse_memory_type_v1` continue to work
  without modification.
- No breaking changes. Minor-version bump.

### References

- Backlog: [Tier 2 items #5, #6, #8](../../docs/plans/core-hoist-backlog.md)
- Audit: `docs/notes/ROADMAP-AUDIT-20260419.md` (internal) §7.1 Agent A

## [2.0.0] - 2026-04-17

### Added

- **Memory Taxonomy v1** — first-class Rust types for the canonical cross-client
  memory model. New types are exposed through both the WASM and PyO3 bindings:
  - `MemoryClaimV1` — canonical claim shape used across all clients.
  - `MemoryTypeV1` — closed enum of memory kinds (fact, preference, decision,
    episodic, goal, context, summary, rule).
  - `MemorySource` — provenance of a claim (user, agent, tool, system,
    inferred, imported).
  - `MemoryScope` — retention/visibility scope (session, user, global).
  - `MemoryVolatility` — expected churn profile (stable, mutable, ephemeral).
  - Validation helpers: `validateMemoryClaimV1` / `validate_memory_claim_v1`,
    `parseMemoryTypeV1` / `parse_memory_type_v1`, and
    `parseMemorySource` / `parse_memory_source` for strict string-to-enum
    parsing with descriptive errors.

- **Retrieval v2 Tier 1 — source-weighted reranker**:
  - `RerankerConfig` — explicit config struct to drive the v1 rerank path
    (BM25 + cosine + RRF with per-source weight multipliers + tunable tie-break
    behaviour).
  - `rerankWithConfig` / `rerank_with_config` — new Tier 1 entry point.
    Accepts `MemoryClaimV1` candidates and a `RerankerConfig`; returns a
    deterministically ordered result set with source-weight scaling applied
    before fusion.
  - `sourceWeight` / `source_weight` — exposes the canonical per-source
    multiplier table used by the v1 path (`user` > `tool` > `agent` > `system`
    > `inferred` > `imported`) so client code can preview/debug scoring.
  - `legacyClaimFallbackWeight` / `legacy_claim_fallback_weight` — the weight
    applied when a candidate lacks a v1 `source` field, allowing mixed
    v0/v1 corpora to rerank cleanly without silent bias.

### Changed

- **BREAKING (reranker API)** — the v1 rerank path requires an explicit
  `RerankerConfig`. The legacy v0 `rerank()` function is preserved for backward
  compatibility; existing callers continue to work unchanged, but new clients
  targeting the v1 taxonomy must migrate to `rerankWithConfig` /
  `rerank_with_config` to opt in to source-weighted ranking. Callers that mix
  `MemoryClaimV1` and legacy claims will see the `legacyClaimFallbackWeight`
  applied to any claim missing `source`.

### Notes / Compatibility

- WASM and PyO3 bindings are fully in sync: every v1 type, helper, and rerank
  entry point is exposed to both TypeScript and Python clients.
- v0 data continues to deserialise without migration; fields added by v1 are
  optional on the wire.
- Test coverage: 455 lib tests (native), 498 tests with `--features wasm`,
  508 tests with `--features python`.

### References

- Spec: [`docs/specs/totalreclaw/memory-taxonomy-v1.md`](../../docs/specs/totalreclaw/memory-taxonomy-v1.md)
- Spec: [`docs/specs/totalreclaw/retrieval-v2.md`](../../docs/specs/totalreclaw/retrieval-v2.md)

## [1.5.0] - 2026-04-14

### Added

- **Core Hoist Tier 1** — orchestration logic lifted out of client adapters
  and into the shared core:
  - Store-time dedup: `find_best_near_duplicate`, `cluster_facts`.
  - Pin semantics: `is_pinned_claim`, `respect_pin_in_resolution`.
  - Contradiction orchestration: `resolve_with_candidates` — full pipeline
    (detect → pin check → resolve → tie-zone).
  - Decision log: `DecisionLogEntry`, `find_loser_claim_in_decision_log`
    (enables pin-on-tombstone recovery).
  - Shadow mode: `filter_shadow_mode` (observer-only validation mode).

## [1.4.0] and earlier

See git history: `git log -- rust/totalreclaw-core/`.
