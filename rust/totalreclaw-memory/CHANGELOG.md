# Changelog — totalreclaw-memory (ZeroClaw)

## 2.0.1 — 2026-04-19

Patch release: tracks `totalreclaw-core` bump to 2.1.0. No behavioural
changes in ZeroClaw itself; rebuild picks up the new string-level v1
taxonomy exports (`VALID_MEMORY_TYPES`, `TYPE_TO_CATEGORY`,
`is_valid_memory_type`) transitively via the core dependency.

### Changed

- `totalreclaw-core` dependency tightened from `"2.0"` to `"^2.1.0"` to
  pull in the Tier 2 hoist additions. No API surface change in
  `totalreclaw-memory`.

## 2.0.0 — 2026-04-18

Memory Taxonomy v1 lands in the ZeroClaw Rust backend. The legacy v0
envelope + v3 outer protobuf remain the default for the existing
`Memory` trait API so pre-2.0 vaults keep round-tripping, but the new
`store_v1()` entry point plus the read-side v1 envelope parser put the
crate on feature parity with plugin v3.0.0 / mcp 3.0.0 / python 2.0.0.

### v1 write path

- **New** `store::V1StoreInput` struct carrying text + v1 type + source
  + scope + volatility + importance (+ optional reasoning).
- **New** `store::build_memory_claim_v1(input)` — constructs a canonical
  `MemoryClaimV1` with UUIDv7 id + RFC 3339 `created_at` + `schema_version`
  ("1.0") per the v1 spec.
- **New** `store::store_fact_v1()` — full v1 pipeline (exact-dedup via
  content fingerprint, best-match near-duplicate supersede, v4 protobuf
  submission). Emits `zeroclaw_v1_{source-token}` as the on-chain source
  tag (e.g. `zeroclaw_v1_user-inferred`) for analytics.
- **New** `TotalReclawMemory::store_v1(input)` trait method — thin
  wrapper around `store_fact_v1` that threads the memory's pre-derived
  keys / embedding provider / relay client and invalidates the hot cache
  after the store.
- **New** `core::store::prepare_fact_v1()` — the pure-computation half:
  encrypts the v1 JSON envelope (no `{t,a,s}` wrapper — the envelope IS
  the ClaimPayload), builds blind indices from the same text, encrypts
  the embedding, emits the protobuf with `version = 4`.

### v4 outer protobuf

- Core `FactPayload` gains a `version: u32` field.
- Core `encode_fact_protobuf` / `encode_tombstone_protobuf` now thread
  `version` onto field 8. `0` is normalized to `DEFAULT_PROTOBUF_VERSION`
  (`3`) for back-compat callers.
- New constants `DEFAULT_PROTOBUF_VERSION = 3` and
  `PROTOBUF_VERSION_V4 = 4` exported from core's `protobuf` module.
- ZeroClaw's `forget()` / `store_tombstone_v1()` use the v4 path;
  legacy `store_tombstone()` stays on v3 for the non-v1 code path.
- WASM + PyO3 bindings: optional `version` field on the JSON /
  kwarg input; v0 callers who omit it still get a valid v3 blob.

### Read path — v1 envelope parsing

- `backend::parse_decrypted_envelope()` now tries v1 ClaimPayload
  (top-level `text` + `type` fields) first, then falls back to the
  legacy v0 `{t, a, s}` short-key envelope.
- v1 `source` field is parsed (kebab-case literals: `user`,
  `user-inferred`, `assistant`, `external`, `derived`) and attached to
  each `reranker::Candidate` so Retrieval v2 Tier 1 source weights
  apply. v0 envelopes leave `source: None`; the reranker applies the
  legacy-claim fallback weight for them.
- v1 type → ZeroClaw `MemoryCategory` mapping:
  - `episode` → Conversation (7-day decay)
  - `claim | preference | directive | commitment | summary` → Core
  - anything else → Core (safe default)

### Retrieval v2 Tier 1 — source-weighted reranking

- `TotalReclawMemory::recall()` now calls
  `reranker::rerank_with_config(apply_source_weights: true, ...)`.
- v1 blobs carry provenance into the reranker; v0 blobs use
  `LEGACY_CLAIM_FALLBACK_WEIGHT` so pre-2.0 vault entries aren't
  penalized during the migration window.

### Deferred operations — pin / retype / set_scope

The new MCP v1 tools (`totalreclaw_pin`, `totalreclaw_retype`,
`totalreclaw_set_scope`) are not yet wired into the ZeroClaw `Memory`
trait. `pin()` / `retype()` / `set_scope()` stubs exist on
`TotalReclawMemory` but currently return an error telling callers to
use the MCP server's equivalent tools from their NEAR AI agent.
Tracked as a Known Gap in CLAUDE.md.

Rationale: ZeroClaw 2.0 ships the storage-layer v1 support; the
supersede-chain semantics for pin/retype/set_scope are defined in the
v1 spec addendum but are best exercised once the MCP v1 tools are
published and validated end-to-end. Doing them in Rust without that
validation ladder would mean shipping untested pin-chain behaviour
that diverges from the TS / Python implementations.

### Bumps

- `totalreclaw-memory` crate version: **0.1.0 → 2.0.0**
- `totalreclaw-core` dependency: **pinned to "2.0"** (already the case
  as of Agent F's core-v2.0.0 branch).

### Testing

- **51 library unit tests pass** (existing behaviour: crypto, LSH,
  blind index, fingerprint, stemmer, hot cache, reranker, wallet,
  userop, store).
- **15 new v1 integration tests** in `tests/v1_taxonomy.rs` covering:
  - `V1StoreInput` + `build_memory_claim_v1` (UUIDv7, RFC3339, scope /
    volatility / reasoning threading)
  - v1 `MemoryClaimV1` JSON round-trip (spec field names, not v0
    short-keys)
  - v4 protobuf encoding (version tag on field 8, v3 vs v4 distinct)
  - tombstone v4 encoding
  - `MemorySource` kebab-case ser/de (for Tier 1 reranker wiring)
  - `MemoryTypeV1::from_str_lossy` case-insensitivity
  - Enum cardinality (6 types, 5 sources, 8 scopes, 3 volatilities)
- **24 spec-compliance tests pass** (updated one `Candidate` literal
  to include the new `source: None` field).
- **Cross-client E2E tests** (3-way, native UserOp, staging) updated
  to set `version: DEFAULT_PROTOBUF_VERSION` on `FactPayload`
  literals. These tests are `#[ignore]`-gated on network availability
  so they compile but only run when the staging relay / local Anvil
  is up.

### Not done (deferred)

- v1 extraction pipeline in ZeroClaw. The current ZeroClaw usage
  pattern is the NEAR AI agent calling `store_v1()` with already-built
  `V1StoreInput` (the agent runs its own extractor). No LLM extraction
  happens inside this crate — that's in the NEAR AI routines engine
  or in the MCP server. If ZeroClaw ever grows a local extractor
  (analogous to plugin's `extractor.ts`), the G-pipeline (merged-topic
  prompt + `apply_provenance_filter_lax` + `comparative_rescore_v1`
  + `default_volatility`) will need porting — at that point core's
  `smart_import` / `digest` modules are the natural reuse points.
- Native pin / retype / set_scope in the Rust trait. Scheduled for
  ZeroClaw 2.1 once MCP 3.0.0 is published and the v1 supersede-chain
  behaviour is validated end-to-end against the staging subgraph.
- Staging E2E smoke test for the v1 write path specifically. The
  `test_three_way_cross_client` integration test still writes v3
  blobs; a v1 variant will land with the ZeroClaw 2.1 pin/retype work.

## 0.1.0 — pre-2.0

Initial ZeroClaw Rust backend — v0 taxonomy, v3 outer protobuf,
Phase 2 KG contradiction detection, hot cache, dynamic candidate pool,
client batching, debrief. See git history.
