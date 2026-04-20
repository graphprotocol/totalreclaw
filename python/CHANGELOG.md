# Changelog

All notable changes to the `totalreclaw` Python client and the `totalreclaw.hermes`
Hermes Agent plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.2] - 2026-04-20

Wave 2a Hermes fix-up. Three bugs from the 2.2.1 VPS QA
([internal#14](https://github.com/p-diogo/totalreclaw-internal/pull/14),
`docs/notes/QA-hermes-RC-2.2.1-20260420.md`) — each would have been a
ship-stopper if left in a public release:

### Fixed

- **Bug #4 (HIGH) — `auto_extract` reads Hermes `config.yaml`.**
  Pre-2.2.2's `auto_extract` + post-extraction pipeline required
  `OPENAI_MODEL` to be set as an env var even when
  `~/.hermes/config.yaml` already carried `provider: zai` +
  `model: glm-5-turbo`. The 2.0.2 "fix" only wired the Hermes
  reader into the hooks layer; the generic `detect_llm_config` still
  read env vars exclusively, and the YAML reader expected a NESTED
  `model: {provider, model}` shape while Hermes actually writes
  top-level `provider:` + `model:` keys. 2.2.2:
  - `agent/llm_client.py::read_hermes_llm_config` handles BOTH YAML
    shapes and scans `$HERMES_CONFIG` → XDG → `~/.config/hermes/` →
    legacy `~/.hermes/`. Emits a WARN-level log line identifying the
    config path the model came from.
  - `detect_llm_config()` falls through to the Hermes reader when no
    env vars resolve. This is the path `extract_facts_llm` hits when
    no explicit `llm_config` is passed.
- **Bug #7 (SHIP-STOPPER) — `credentials.json` key parity with plugin 3.2.0.**
  Python pre-2.2.2 wrote `{"recovery_phrase": ...}` at
  `~/.totalreclaw/credentials.json`; plugin 3.2.0 writes
  `{"mnemonic": ...}` on the same canonical path. Cross-agent
  portability — a user switching from Hermes to OpenClaw without
  re-onboarding — was silently broken. 2.2.2:
  - `agent/state.py::_extract_mnemonic_from_creds` helper accepts
    BOTH keys on read, prefers canonical `mnemonic` when both present.
  - `configure()` write path now emits canonical `mnemonic` for
    fresh writes. Preserves legacy `recovery_phrase` shape when an
    existing file carries ONLY that key for the same mnemonic — no
    silent migration on touch.
  - Canonical decision documented in
    `docs/specs/totalreclaw/flows/01-identity-setup.md`.
- **Bug #8 (MEDIUM) — `pin_fact` emits v=4 `MemoryClaimV1` with `pin_status`.**
  Pre-2.2.2's `pin_fact()` wrote a v=3 tombstone but no companion v=4
  pinned claim — a pinned fact was invisible on the subgraph, so
  cross-client pin awareness was broken (other clients couldn't see
  the pin and the Tier-1 reranker's pin-aware ranking never fired).
  2.2.2 ports `skill/plugin/pin.ts::executePinOperation`:
  - `claims_helper.py::build_canonical_claim_v1` gains a
    `pin_status` parameter (validated against
    `VALID_PIN_STATUSES = ("pinned", "unpinned")`).
  - `operations.py::_change_claim_status` now always emits a fresh
    v1.1 blob (long-form `text`/`type`/`pin_status`/`superseded_by`)
    regardless of whether the source fact was v0 short-key or v1.
    New `FactPayload.version` is set to `PROTOBUF_VERSION_V4` so the
    outer protobuf tags the write as v1 taxonomy. Tombstone stays at
    v=3 (matches plugin behavior).
  - New `_project_source_to_v1` helper mirrors the plugin's
    `projectToV1` function-for-function — v0 sources upgrade on the
    fly (short-key `c` → v1 `type`, `sa` heuristics → v1 `source`).

### Tests

- `tests/test_wave2a_hermes_fixes.py`: 19 new tests — 7 Bug #4, 7 Bug #7
  (5 parity + 2 cross-client), 3 Bug #8, 2 cross-client portability.
- `tests/test_pin_unpin.py`: 2 tests updated to assert on the new v1.1
  long-form shape (prior assertions encoded the buggy pre-2.2.2
  short-key contract).
- Full Python suite: 678 passing, 10 skipped, 1 xfailed — no regressions.

### Known limitations

- The installed `totalreclaw-core==2.1.0` PyPI wheel doesn't round-trip
  the v1.1 `pin_status` field through `validate_memory_claim_v1` (the
  Rust struct has it; the serde emit drops it). 2.2.2 reattaches
  `pin_status` after validation — same pattern as `schema_version` and
  `volatility` — so the fix ships independently of core. A future
  `totalreclaw-core` release (2.1.1 on npm already; PyPI pending) will
  round-trip the field natively and the reattach becomes a no-op.

## [2.2.1] - 2026-04-19

Wire `auto_extract` to `remember_batch`; realizes the ~8x extraction latency win
from 2.2.0. No new public API — internal call-site change only.

### Changed

- `agent/lifecycle.py::auto_extract` now submits ADD/UPDATE facts via
  `client.remember_batch()` in chunks of 15 instead of looping
  `client.remember()` per fact. DELETE and NOOP actions are unaffected.
  For a 15-fact extraction cycle this drops relay round-trips from 15
  separate UserOperations to 1, matching the ~60s → ~8s latency projection
  from the Gap 3 notes in 2.2.0.
- Per-fact error granularity is preserved: if `remember_batch` returns
  fewer IDs than facts, the missing ones are logged at WARNING level so
  the caller can diagnose. If the whole batch fails, each fact is logged
  individually.
- UPDATE tombstones (`client.forget(existing_fact_id)`) are still issued
  individually after the batch that stored the replacement, preserving the
  same ordering guarantee as the old loop.

### Tests

- +3 new tests (`tests/test_auto_extract_uses_batch.py`):
  `test_auto_extract_5_facts_calls_remember_batch_once`,
  `test_auto_extract_20_facts_calls_remember_batch_twice`,
  `test_auto_extract_partial_failure_logs_failed_facts`.
- Updated `tests/test_v1_hooks_integration.py` and
  `tests/test_hermes_plugin.py` to assert on `remember_batch` instead of
  `remember` for the auto-extraction path.
- Full suite: 640 passing, 9 skipped, 1 xfailed.

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
