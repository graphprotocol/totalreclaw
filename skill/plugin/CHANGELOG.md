# Changelog

All notable changes to `@totalreclaw/totalreclaw` (the OpenClaw plugin) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.4] — 2026-04-18

### Fixed

- **Pro-tier UserOp signatures now sign against chain 100 (Gnosis).** Before this
  release, `CONFIG.chainId` was a hardcoded literal `84532`, so Pro-tier writes
  were signed for Base Sepolia even though the relay routed them to Gnosis
  mainnet. The bundler rejected the signature with AA23 — a silent failure
  where every `remember()` looked OK but nothing landed on-chain. There are no
  Pro users in production today, so this never hit a user, but any Pro upgrade
  would have broken every subsequent write. (Hermes Gap 2 equivalent — same
  root cause as the Python client bug fixed in `totalreclaw` 2.0.2.)
- `CONFIG.chainId` is now a getter that reads a runtime override set from the
  billing response. `syncChainIdFromTier(tier)` is called on every
  `writeBillingCache` / `readBillingCache` so the chain flips to 100 for Pro
  tier and stays at 84532 for Free. All existing `getSubgraphConfig()` call
  sites pick up the correct chain automatically because they read
  `CONFIG.chainId` at call time, not at module load.
- Added 6 regression tests in `config.test.ts` covering the default, the
  Pro-tier flip, the Free-tier default, the Pro→Free downgrade path, and the
  test reset helper. Full config suite: 27/27 passing.

## [3.0.0] — 2026-04-18

Major release adopting **Memory Taxonomy v1** and **Retrieval v2 Tier 1** source-weighted reranking — now the DEFAULT and ONLY extraction path.

### Breaking changes

- **Memory Taxonomy v1 is the default AND the only write path.** The `TOTALRECLAW_TAXONOMY_VERSION` opt-in env var introduced during the Phase 3 rollout has been REMOVED. Every extraction + canonical-claim write emits v1 JSON blobs unconditionally. The legacy `TOTALRECLAW_CLAIM_FORMAT=legacy` fallback was also removed — there is no longer any way to reach the v0 short-key or `{text, metadata}` write shapes from the plugin.
- **`@totalreclaw/core` bumped to 2.0.0.** Core now ships v1 schema validators (`validateMemoryClaimV1`, `parseMemoryTypeV1`, `parseMemorySource`), the Retrieval v2 Tier 1 source-weighted reranker (`rerankWithConfig`, `sourceWeight`, `legacyClaimFallbackWeight`), and a protobuf encoder that accepts an explicit `version` field (default 3 for legacy callers, 4 for v1 taxonomy writes).
- **`VALID_MEMORY_TYPES` is now the 6-item v1 list** (`claim | preference | directive | commitment | episode | summary`). The former 8-item v0 list is exported as `LEGACY_V0_MEMORY_TYPES` for back-compat reads of pre-v3 vault entries; do not emit these tokens on the write path. `V0_TO_V1_TYPE` maps every v0 token to its v1 equivalent.
- **`MemoryType` is `MemoryTypeV1`.** The `MemoryTypeV1` name is kept as a back-compat alias; the `isValidMemoryTypeV1` and `VALID_MEMORY_TYPES_V1` exports are also aliases. The new `MemoryTypeV0` type covers the legacy 8-item set.
- **`ExtractedFact` shape expanded.** Now carries `source`, `scope`, `reasoning`, and `volatility` as optional v1 fields. On the write path `source` is required — `storeExtractedFacts` supplies `'user-inferred'` as a defensive default when missing.
- **Outer protobuf `version` field is 4 for all plugin writes.** The v3 wrapper format is retained for tombstones only. Clients that read blobs before plugin v3.0.0 will see `version == 4` on new writes; inner blobs are now v1 JSON, not v0 binary envelopes. See `totalreclaw-internal/docs/plans/2026-04-18-protobuf-v4-design.md`.

### Added

- **`buildCanonicalClaim` now unconditionally emits v1.** The legacy v0 short-key builder was deleted from the public API; callers pass the same `BuildClaimInput` shape (fact + importance + sourceAgent + extractedAt) and the helper forwards to `buildCanonicalClaimV1` internally. `sourceAgent` is retained on the interface for signature back-compat but is ignored (provenance lives in `fact.source`).
- **`buildCanonicalClaimV1`** produces a MemoryClaimV1 JSON payload matching `docs/specs/totalreclaw/memory-taxonomy-v1.md`. Validates through core's strict `validateMemoryClaimV1`, then re-attaches plugin-only extras (`schema_version`, `volatility`).
- **`extractFacts` is the v1 G-pipeline.** Renamed from `extractFactsV1`. Single merged-topic LLM call returning `{topics, facts}`, followed by `applyProvenanceFilterLax` (tag-don't-drop, caps assistant-source at 7), `comparativeRescoreV1` (forces re-rank when ≥5 facts), `defaultVolatility` heuristic fallback, and `computeLexicalImportanceBump` post-processing.
- **`parseFactsResponse` accepts both bare-array and merged-object shapes.** The v0 bare JSON array format is still parsed (legacy / test fixtures), wrapped into `{ topics: [], facts: [...] }` before downstream logic. Unknown types coerce via `V0_TO_V1_TYPE`, so pre-v3 extraction-harness responses keep working.
- **`COMPACTION_SYSTEM_PROMPT` rewritten for v1.** Emits v1 types / sources / scopes in its merged output, keeps the importance-floor-5 behavior, plus the format-agnostic / anti-skip-in-summary guidance. `parseFactsResponseForCompaction` now validates the merged v1 object (bracket-scan fallback still works on prose-wrapped JSON).
- **Outer protobuf `version` parameter wired end-to-end.** Rust core (`rust/totalreclaw-core/src/protobuf.rs`) exposes `PROTOBUF_VERSION_V4 = 4`. WASM + PyO3 bindings accept an optional `version` field on `FactPayload` JSON. Plugin's `subgraph-store.ts` surfaces `PROTOBUF_VERSION_V4` as a named const and every call site that writes a real fact now passes `version: PROTOBUF_VERSION_V4`.
- **`totalreclaw_remember` tool schema accepts v1 fields.** The schema now declares `type` (v1 enum + legacy v0 aliases), `source` (5 v1 values), `scope` (8 v1 values), and `reasoning` (for decision-style claims). Legacy v0 tokens pass through `normalizeToV1Type` transparently.
- **Retrieval v2 Tier 1 is always on.** All three `rerank(...)` call sites in the plugin (main recall tool, before-agent-start auto-recall, HTTP hook auto-recall) pass `applySourceWeights: true`. Every `rerankerCandidates.push({...})` site now surfaces `source` from the decrypted blob's metadata so the RRF score is multiplied by the source weight (user=1.0, user-inferred=0.9, derived/external=0.7, assistant=0.55, legacy=0.85).
- **Session debrief emits v1 summaries.** The `before_compaction` and `before_reset` hook handlers map debrief items to `{type: 'summary', source: 'derived'}` so the v1 schema's provenance requirement is satisfied.
- **`parseBlobForPin` handles v1 blobs.** Pin/unpin can now round-trip a v1 payload (converts to short-key shape for the tombstone + new-fact pipeline). Required so a user can pin a v1 fact produced by the default extraction path.

### Removed

- **`TOTALRECLAW_TAXONOMY_VERSION` env var.** Zero runtime references — only documentation / comment strings remain explaining the removal.
- **`TOTALRECLAW_CLAIM_FORMAT=legacy` fallback.** Legacy `{text, metadata}` doc shape is gone from the write path. `buildLegacyDoc` is no longer exported by the plugin (still present in `claims-helper.ts` for potential external use but unused by `storeExtractedFacts`).
- **`resolveTaxonomyVersion()`** (both in `extractor.ts` and `claims-helper.ts`).
- **v0 `EXTRACTION_SYSTEM_PROMPT`, `parseFactsResponse` legacy parser, v0 `extractFacts()` function.** The v1 versions took over these names.
- **`logClaimFormatOnce` helper** in `index.ts`.

### Migration notes

- **Existing vaults decrypt transparently.** `readClaimFromBlob` prefers v1 → v0 short-key → plugin-legacy `{text, metadata}` → raw text, in that order. No data migration required.
- **Client-side feature matrix updates.** All OpenClaw plugin writes are now v1 (schema_version "1.0", outer protobuf v4). Recalls apply source-weighted reranking automatically.
- **Legacy test fixtures.** Tests that asserted v0 short-key output from `buildCanonicalClaim` have been rewritten to assert v1 long-form output. Tests that passed bare JSON arrays to `parseFactsResponse` still work — the parser wraps bare arrays into the merged-topic shape before validating.

### Pre-existing known issues (not introduced by v3.0.0)

- `lsh.test.ts` fails at baseline because it uses `require()` in an ESM context — pre-existing issue unrelated to the v1 refactor.
- `contradiction-sync.test.ts` has 2 assertions (#12 `isPinnedClaim: st=p` and #21 `resolveWithCore: vim-vs-vscode`) that were red in the commit preceding v3.0.0. These are test-fixture / core-WASM compatibility gaps tracked separately.
