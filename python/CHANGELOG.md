# Changelog

All notable changes to the `totalreclaw` Python client and the `totalreclaw.hermes`
Hermes Agent plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-04-19

Hermes parity Gap 3: client-side batching. Drops 15-fact extraction
latency from ~60s to ~8s by submitting one ERC-4337 UserOperation
per extraction cycle instead of N sequential ones.

### Added

- `TotalReclaw.remember_batch(facts)` — public async API that stores up
  to 15 facts in a single UserOperation via
  `SimpleAccount.executeBatch(...)`. Paymaster / bundler / inclusion
  costs paid once.
- `operations.store_fact_batch(facts, ...)` — internal batch path that
  mirrors `store_fact` per-fact (same encryption, trapdoor generation,
  v1 canonical claim, protobuf v4 wrapper), then wraps all N payloads
  into one UserOp.
- `userop.build_and_send_userop_batch(...)` — batched UserOp submitter
  mirroring `build_and_send_userop` with the same AA25/AA10 retry loop.
- `userop.encode_execute_batch_calldata_for_data_edge(payloads)` +
  `userop.MAX_BATCH_SIZE` (= 15) — thin wrappers around the Rust core's
  `totalreclaw_core.encode_batch_call`, byte-identical to the TS
  plugin's `encodeBatchCalls`.
- `tests/test_userop_batch.py` — byte-match parity fixtures for N = 1 /
  3 / 5 / 10 / 15, empty-batch + oversize-batch validation, mocked
  relay retry tests, and an optional staging-integration test (runs
  only when `TOTALRECLAW_STAGING_INTEGRATION=1`).
- `tests/fixtures/batch_calldata_vectors.{py,json}` — fixture generator
  + baked expected-calldata vectors from the shared Rust core.

### Notes

- Part of the Hermes parity roadmap
  ([`docs/plans/2026-04-18-hermes-parity-roadmap.md`][hermes-parity],
  Gap 3). Closes the UX cliff where auto-extraction after a long
  conversation appeared to freeze the agent for 45–75s.
- The `agent/lifecycle.py::auto_extract` store loop is still per-fact
  on disk in this release — wiring it to `remember_batch` lives in a
  separate follow-up so Phase A (Hermes plugin / adapters) and Gap 3
  (batching) could merge independently. The new public API is fully
  shipped and importable today.
- `encode_batch_call` in the Rust core folds a batch of 1 back to
  `execute(...)` rather than `executeBatch(...)`, so a 1-element batch
  is byte-identical to the single-fact path. No correctness penalty
  for callers that unconditionally batch.

## [2.1.0] - 2026-04-19

Phase A of the Hermes parity roadmap
([docs/plans/2026-04-18-hermes-parity-roadmap.md][hermes-parity]). Closes the
three lowest-effort / highest-visibility gaps between the Python client's
Hermes plugin and the OpenClaw + MCP reference implementations. Feature
release per semver (new public tool surface; no breaking changes).

### Added

- **`totalreclaw_upgrade` tool** (Hermes) — creates a Stripe Checkout
  session via `RelayClient.create_checkout()` and returns the URL for the
  user to complete payment for the Pro tier. Mirrors
  `mcp/src/tools/upgrade.ts`, except the Python client already knows its
  own wallet address so the tool schema has no required arguments. The
  description follows the Phase 2 (v2.0.2) style with explicit
  user-utterance hints ("upgrade to Pro", "I hit the free limit",
  "unlimited") to help the agent invoke it correctly.

- **`totalreclaw_debrief` tool** (Hermes) — explicit-invocation form of
  the session-end debrief. Reuses
  `totalreclaw.agent.lifecycle.session_debrief` (the same function the
  auto `on_session_end` hook calls), so the stored summary facts are
  indistinguishable from the auto-flow output (`type=summary`,
  `provenance=derived`, `scope=unspecified`). The tool returns the stored
  count + `fact_ids` so the agent can confirm. Short sessions
  (< 4 turns) short-circuit with a clear `skipped=true` response.

- **Mem0 import adapter** (`totalreclaw.import_adapters.mem0_adapter`) —
  structural port of `skill/plugin/import-adapters/mem0-adapter.ts`.
  Parses the three canonical Mem0 JSON shapes (dashboard export
  `{memories: [...]}`, API response `{results: [...]}`, bare array) and
  emits pre-structured `NormalizedFact`s that flow through the existing
  `ImportEngine` without LLM re-extraction. Category mapping is
  byte-identical to the TS adapter. `get_adapter('mem0')` + `list_sources()`
  now include the new source.

### Changed

- `totalreclaw.agent.lifecycle.session_debrief(state, stored_fact_texts=None)`
  now returns `list[str]` of stored debrief fact ids instead of `None` so
  the new `totalreclaw_debrief` tool can surface them back to the user.
  The auto `on_session_end` hook ignores the return value — this is a
  behaviour-compatible widening.

- `totalreclaw.hermes.plugin.yaml` version bumped `2.0.2` → `2.1.0` and
  the two new tools added to `provides_tools`.

- `hermes/__init__.py::register()` now registers 10 tools (was 8): the
  existing 8 plus `totalreclaw_upgrade` + `totalreclaw_debrief`.

### Tests

- +33 new tests (`test_upgrade_tool.py` 7, `test_debrief_tool.py` 8,
  `test_mem0_adapter.py` 18). Full suite now 637 passing, 4 skipped,
  1 xfailed.

### Notes

- Gap 3 (`remember_batch` + Python batcher) is tracked in a parallel
  agent worktree; those files (`userop.py`, `operations.py`, `client.py`,
  `agent/extraction.py`) are deliberately untouched in this release.
- The Mem0 adapter's optional live-API fetch path is intentionally
  skipped for Phase A — users export JSON from the Mem0 dashboard and
  paste or point-to it. Live-API ingestion is a potential Phase B
  follow-up.

[hermes-parity]: https://github.com/p-diogo/totalreclaw-internal/blob/main/docs/plans/2026-04-18-hermes-parity-roadmap.md

## [2.0.2] - 2026-04-18

Phase 2 of the v1.0.x stabilization wave. Plugin-layer fixes flagged by
the v1.0.0 QA run (`docs/notes/QA-V1CLEAN-VPS-20260418.md`).

### Fixed

- **Event-loop lifecycle** — `RelayClient` now caches `httpx.AsyncClient`
  per event loop so the Python client works both from Hermes's async
  runtime AND its sync-hook sidecars (`pre_llm_call`). Previous behavior
  raised "Event loop is closed" on the second loop.

- **LLM auto-detect surfaces visible errors** — when no LLM config
  resolves, `extract_facts_llm` / `extract_facts_compaction` now warn at
  WARNING level with actionable guidance, and `post_llm_call` surfaces a
  one-time quota-channel warning so the user sees an explanation in
  their next assistant turn.

- **Auto-setup detection** — rewrote `REMEMBER` / `RECALL` tool
  descriptions so the LLM prefers TotalReclaw over Hermes's built-in
  `memory` tool, and added a one-time setup-nudge when a memory-related
  message arrives before `totalreclaw_setup` has run.

- **In-batch cosine dedup** — `deduplicate_facts_by_embedding` now
  collapses near-identical facts both against `existing_memories` AND
  against earlier facts in the same extraction batch.

- **Spurious extraction of setup meta-content** — `is_product_meta_request`
  + `_filter_product_meta_facts` filter "set up TotalReclaw" / "install
  the memory plugin" utterances before they reach the vault as "user
  preferences". Genuine preferences still pass through.

- **Export / session-id / chain-id auto-detect** — export path, session
  header forwarding, and Pro-tier chain-100 auto-detect all stabilized.

## [2.0.1] and earlier

See git history — pre-v1 stabilization patches.
