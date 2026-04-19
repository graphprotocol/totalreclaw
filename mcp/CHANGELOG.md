# Changelog

## [3.2.0] - 2026-04-19

### Added
- **`totalreclaw_pin` / `totalreclaw_unpin` now emit v1.1 canonical claims** with the new `pin_status` field (`"pinned" | "unpinned"`) instead of the legacy v0 short-key shape (`{t,c,st,sup,...}`). The outer protobuf wrapper is already written at `version = 4` (unchanged since 3.0.1), so the pin path now lines up with the v1 on-chain contract end to end.
  - New helper export from `src/claims-helper.ts`: `buildV1ClaimBlob(input)` accepts an optional `pinStatus` and returns a canonical v1.1 JSON blob validated through `@totalreclaw/core@2.1.1`'s `validateMemoryClaimV1`.
  - `src/tools/pin.ts::executePinOperation` rewritten: source blob is parsed, projected into v1 shape (v0 sources are UPGRADED per the spec's legacy-type map — `fact|context|decision → claim`, `rule → directive`, `goal → commitment`), then a fresh v1.1 blob is built with `pin_status` set and `superseded_by` pointing to the old fact.
  - `parseBlobForPin` now recognizes pinned status on a v1.1 blob via `pin_status == "pinned"` AND on a v0 blob via the legacy `st == "p"` sentinel (back-compat preserved).
  - `readBlobUnified` surfaces `v1.pin_status` on parsed v1 blobs so downstream (recall display, export) can render the pin indicator without re-parsing.

### Fixed
- **BLOCKER bug #2 from RC 3.0.7-rc.1 QA (2026-04-19)** — `totalreclaw_pin` on a v1 vault was writing a v0 short-key blob without `schema_version` and with the v0 type token (`"rule"` instead of v1 `"directive"`). v1 readers would then report the pinned fact with a different `type` from its pre-pin neighbor. Root cause: the pin rewrite path bypassed `buildV1ClaimBlob` and used the legacy `canonicalizeClaim` helper. Fixed — pin/unpin now route through `buildV1ClaimBlob` with `pinStatus` set.
- `schema_version` is now always emitted on v1.1 pin output (re-attached after core's serde-skip default-omission). Matches plugin output byte-for-byte.

### Notes / Compatibility
- Minor-version bump (3.1.0 → 3.2.0). New pin output format is a breaking change for any downstream reader that expected v0 short-key shape from the pin path — but the whole point of v1 is cross-client interoperability on the v1 surface, so this change lines MCP up with what all other clients already expect.
- v0 blobs continue to READ correctly via `parseBlobForPin`'s fall-through (unchanged), so mixed-version vaults produce uniform pin/unpin behavior.
- Cross-client parity: plugin + MCP produce the same v1.1 JSON for identical inputs — verified by new tests in both packages.
- Requires `@totalreclaw/core@^2.1.1` (bumped in parallel — see `rust/totalreclaw-core/CHANGELOG.md`).

### Tests
- `tests/pin-unpin.test.ts` grew from 33 → 38 assertions. Existing v0 assertions updated to v1 equivalents; 5 new v1.1 tests (pin preserves fields, unpin flips pin_status, idempotent detects v1.1 pinned, cross-impl parity, entities round-trip).
- `tests/tool-pin-recovery.test.ts` 2 tests updated to assert v1.1 output on tombstone-recovery pin.
- Full TS suite: 402 passed (up from 397).

### References
- QA: `totalreclaw-internal/docs/notes/QA-openclaw-RC-3.0.7-rc.1-20260420.md` bug #2.
- Audit: `mcp/AUDIT-v1-tools.md` §A2 (deferred gap — now closed).
- Spec: `docs/specs/totalreclaw/memory-taxonomy-v1.md` (bumped to v1.1; additive extension).

## [3.1.0]

### Added
- **Phase 2 contradiction detection + auto-resolution** wired into the subgraph write path (`handleRememberSubgraph`). Mirrors the OpenClaw plugin's `skill/plugin/contradiction-sync.ts` pattern so a fact pinned via OpenClaw and later re-asserted via MCP (or vice versa) produces the same outcome. Closes the cross-client consistency gap called out in Roadmap Audit 2026-04-19 §2 item #1 and §7.2 Agent C.
  - New module `src/contradiction-sync.ts` — candidate fetch + decrypt, pure resolver delegating to `core.resolveWithCandidates`, decision-log writer (format byte-for-byte compatible with the plugin's `~/.totalreclaw/decisions.jsonl`).
  - Pin respect is enforced by the Rust core via `respect_pin_in_resolution` inside `resolve_with_candidates`. When an existing claim is pinned, a contradicting new write is skipped with reason `existing_pinned`. Pinned facts are never silently overridden.
  - Tie-zone guard (`TIE_ZONE_SCORE_TOLERANCE = 0.01`) calibrated against the 2026-04-14 Postgres/DuckDB false-positive; same threshold as the plugin.
  - Env var `TOTALRECLAW_AUTO_RESOLVE_MODE` (values: `active` default | `off` | `shadow`) — INTERNAL kill-switch. Not user-facing, not documented in README or SKILL.md.
- Tests at `tests/contradiction-sync.test.ts` covering non-contradicting writes, contradicting writes (new wins → supersede), and pinned existing (new skipped).

## [3.0.1]

### Fixed
- Outer protobuf wrapper `version` field was hardcoded to `2` in `encodeFactProtobuf`, while all other v1 clients (OpenClaw plugin, Python, Rust `totalreclaw-memory`) write `4` per the Memory Taxonomy v1 contract. MCP now writes `PROTOBUF_VERSION_V4 = 4`. Matches VPS QA Bug #10 in `QA-V1-VPS-20260418.md`.

## [3.0.0]

### Changed
- Compressed tool descriptions to ≤500 chars each to reduce per-turn LLM context cost while preserving auto-invocation triggers.
