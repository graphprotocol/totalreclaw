# Changelog

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
