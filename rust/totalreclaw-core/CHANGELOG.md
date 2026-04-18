# Changelog

All notable changes to `@totalreclaw/core` / `totalreclaw-core` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
