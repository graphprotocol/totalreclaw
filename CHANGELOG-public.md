# TotalReclaw Changelog

> **Note:** This file lists releases promoted to the public registries' stable tags. Active release-candidate work (`@rc` dist-tag on npm, `rcN` on PyPI, etc.) is tracked in the internal release-pipeline tracker, not here.

## 3.3.1 / 2.3.1 / mcp-server 3.2.1 — Stable promote (2026-04-27)

Consolidated stable line covering plugin 3.2.0 → 3.3.1, Python client 2.2.1 → 2.3.1, MCP server 3.2.0 → 3.2.1. Highlights from the rc.1 → rc.27 line:

### `@totalreclaw/totalreclaw` (OpenClaw plugin) → 3.3.1

- **URL-driven install flow.** The canonical install path is now a single chat message with the setup-guide URL — the agent fetches it, runs the install commands, and walks the user through browser-based account setup. No terminal commands required for the user. (rc.1, rc.18)
- **Lazy CDN embedder.** The MiniLM ONNX model now loads from CDN on first use rather than being bundled in the plugin tarball — install footprint shrinks from ~210 MB to under 5 MB, and the model is fetched + cached locally on the first real conversation. Closes the rc.21 OOM ship-stoppers. (rc.22)
- **Reranker hoist to `@totalreclaw/core::reranker`.** Tier 1 source-weighted reranker logic moved out of the plugin and into the Rust core (with WASM bindings). All clients now share a single byte-identical implementation. (rc.22)
- **`confirm_indexed` read-after-write primitive.** New core primitive lets clients confirm an on-chain write is indexed by the subgraph before continuing. Closes a class of race conditions where recall ran before the write was visible. (rc.22)
- **Phrase-safety CI guard.** New CI check forbids any code path that emits recovery-phrase material on stdout/stderr from agent-callable contexts. (rc.22)
- **rc.18 dist-tag promote bug fix.** Earlier RCs published under wrong dist-tags; rc.18 + rc.19 fixes ensure stable promote actually moves the `latest` pointer. (rc.18, rc.19)
- **Topology-agnostic restart instructions.** Setup guides now use `<your-container-name>` placeholders instead of hardcoded `tr-openclaw` references; works for any Docker / managed-service topology. (rc.19)
- **Recall miss fix for short queries.** Short queries (1-2 tokens) were under-recalling due to over-aggressive cosine threshold. Threshold now adapts to query length. (rc.18 follow-up)
- **`pin_status` preservation across retype / set_scope.** Pin state now correctly survives type or scope changes. (rc.18 follow-up)

### `totalreclaw` (Python client + Hermes plugin) → 2.3.1

- **`hermes plugins install p-diogo/totalreclaw-hermes`** is now the canonical Hermes plugin install path, alongside `pip install totalreclaw` for the Python tool implementations into Hermes' venv. (rc.16+)
- **Auto-disable Hermes built-in memory.** The TotalReclaw setup flow now calls `hermes tools disable memory` on install — running both Hermes' built-in `memory` tool AND TotalReclaw simultaneously creates a silent intent-stealing bug where memories land in `MEMORY.md` instead of TotalReclaw's encrypted vault. Documented as unsupported. Companion path B: tool-description bias steers the LLM toward `totalreclaw_remember` even when the built-in tool can't be disabled. (rc.25, rc.26)
- **Retype + set_scope on-chain operations.** Hermes now supports natural-language retype ("that's actually a directive, not a preference") and set_scope ("file that under work") via on-chain UserOps. Tool parity with the OpenClaw plugin. (rc.23)
- **F2 same-provider cheap-model selection.** Auto-extraction now resolves a cheap model from the SAME provider as the user's main agent (previously could spin up an unrelated provider's model and fail on missing key). (rc.24)
- **Pair AttributeError + venv install path fixes.** Closes #151 + #152 — the install/setup path now resolves correctly under Hermes-supervised venvs. (rc.10)
- **Pending-queue drain accepts EOA OR SA owner-key.** Auto-extract drains correctly across mixed key states. (rc.26)
- **Persist + drain auto-extract on interpreter-shutdown race.** Pending facts now survive a shutdown mid-extract. (rc.20)
- **Graceful fallback when core wheel lacks `confirm_indexed` bindings.** Hermes degrades to write-and-hope rather than crashing on older wheels. (rc.20)

### `@totalreclaw/mcp-server` (Claude Desktop / Cursor / Windsurf / IronClaw) → 3.2.1

- **[SECURITY] Removed `totalreclaw_setup` tool.** This tool generated a recovery phrase and returned it via MCP tool-output JSON, which crossed the LLM context. Phrase-safety violation. The MCP server now has no phrase-touching tool surface; users source phrases from another client's `~/.totalreclaw/credentials.json`, the OpenClaw / Hermes browser account-setup flow, or an offline BIP-39 generator. (3.2.1)

### Cross-cutting

- **Terminology normalization.** User-facing prose, agent-verbatim messages, and tool descriptions now consistently say "set up your TotalReclaw account" / "account setup" instead of mixing "pair", "pairing", "QR-pair", and "setup". The technical tool name `totalreclaw_pair` is unchanged for backward compatibility — it remains the canonical agent-facilitated account-setup tool. API endpoints (`/pair/p/...`, `/pair/session/open`) and code identifiers are unchanged.
- **Hermes `hermes chat -q` setup caveat documented.** One-shot `hermes chat -q "..."` invocations cannot complete account setup because the process exits before the browser handshake — the WebSocket dies and the browser POST returns 404. The Hermes setup guide now documents this explicitly with daemon-mode + standalone-CLI workarounds. Daily operations (chat-q, --resume) work normally once the account is set up. Tracked at #170.

### Known carry-overs

- The `~/.totalreclaw/credentials.json` schema is unchanged across this stable line. Existing vaults decrypt transparently.
- Plugin 3.3.1 + Hermes 2.3.1 require core 2.1.1+ (bundled as a dependency).
- ZeroClaw (Rust crate) is unchanged at 2.0.0; v2.1+ retry is on the roadmap once `confirm_indexed` bindings stabilize.

---

## rc.19 — Setup-agnostic instructions (2026-04-25)

- **Fix:** setup instructions agnostic to container names. Both shipped SKILL.md files (`skill/plugin/SKILL.md`, `python/src/totalreclaw/hermes/SKILL.md`) and both user-facing guides (`docs/guides/openclaw-setup.md`, `docs/guides/hermes-setup.md`) replaced 12 hardcoded `docker restart tr-openclaw` / `docker restart tr-hermes` literals with topology-agnostic `docker restart <your-container-name>` placeholders + a three-pattern fork (native / Docker self-host / managed service) so users whose container is not named `tr-*` get correct instructions.
- **Add:** "Managed OpenClaw service" and "Managed Hermes service" subsections in both user guides documenting the no-terminal install path (web-UI plugin install + service-restart control). Both shipped SKILL.md files gained a managed-service fallback branch: if the agent's shell can't run `openclaw plugins install` / `hermes plugins install` / `pip install` (ENOENT, command not found, not authorized), it tells the user to install via the service's plugins UI and reply `done` instead of looping.
- Bug-fix only — no API changes, no install-command changes, no version bumps. Resolves the rc.18 audit at `docs/notes/AUDIT-tr-setup-agnostic-2026-04-25.md` (commit `705ac21`, internal repo).

## Wave 1 -- v1 Stabilization (2026-04-19)

Post-v1-launch stabilization wave. The v1 taxonomy (shipped 2026-04-18 in core 2.0.0 and the matching client set) revealed a cluster of UX and protocol gaps once real users started onboarding. Wave 1 closes that cluster across core, MCP, OpenClaw plugin, and the Python client.

### `@totalreclaw/core` (Rust + WASM + PyO3)

- **2.1.0** -- Tier 2 hoist: `VALID_MEMORY_TYPES`, `TYPE_TO_CATEGORY`, `isValidMemoryType` now exported from Rust core with WASM + PyO3 bindings. Single source of truth for the v1 taxonomy across all 5 client implementations.
- **2.1.1** -- Additive `pin_status` field on `MemoryClaimV1` + new `PinStatus` enum. Unified `is_pinned_json` now detects pin on both v0 and v1.1 claims. Ships spec v1.1 addendum (see `docs/specs/totalreclaw/memory-taxonomy-v1.md`). Fully backward compatible.

### `@totalreclaw/mcp-server` (Claude Desktop / Cursor / Windsurf)

- **3.1.0** -- Wires Phase 2 contradiction detection via core WASM (no more TS-side re-implementation). Adds pin-on-tombstone recovery so `totalreclaw_pin` works against entries that were already superseded.
- **3.2.0** -- `totalreclaw_pin` and `totalreclaw_unpin` now emit v1 blobs with `pin_status` (protobuf v=4). Pin state is on-chain, portable, and survives re-indexing.

### `@totalreclaw/totalreclaw` (OpenClaw plugin)

- **3.0.5** -- OpenClaw scanner false-positive fix: reword `billing fetch` to `billing lookup` in JSDoc; no behavior change. Adds a `scanner-sim` CI check to prevent regressions.
- **3.0.6** -- `consolidation.ts` now delegates to the Rust core via WASM. Removes the last TS-side dedup duplication.
- **3.0.7 / 3.0.8** -- Not published standalone; folded into 3.1.0. Isolated billing-cache reads and consolidated `fs.read*` helpers to clear two additional scanner warnings.
- **3.1.0** (npm `latest` -- the version users install) -- 4-bug stabilization wave:
  - Bug #1: schema dedup fix (v1 canonical claims de-duplicated correctly before store).
  - Bug #3: digest stub filter (digest-only placeholder facts no longer leak into recall).
  - Bug #4: auto-bootstrap credentials on first run via a `prependContext` banner (with LLM-leak caveat disclosed in setup guides).
  - Consolidation of 3.0.6 / 3.0.7 / 3.0.8 refactors into one release.

### `totalreclaw` (Python client + Hermes)

- **2.0.2** -- Stabilization wave: event-loop lifecycle fix (Hermes `Event loop is closed` on status/export), `totalreclaw_export` return value, session_id restoration, Pro-tier `chain_id` auto-detect.
- **2.1.0** -- Hermes Phase A parity: upgrade flow, session-end debrief, Mem0 import adapter.
- **2.2.0** -- Gap 3 closed: `remember_batch` UserOp batching. A 15-fact extraction batch drops from ~60s to ~8s on-chain.
- **2.2.1** -- Lifecycle `auto_extract` now routes through `remember_batch` by default; extraction latency benefit now applies to the hot path, not just explicit batches.

### Infrastructure

- New public guide: [`docs/guides/release-process.md`](docs/guides/release-process.md) documenting the RC / promote flow.
- All `publish-*.yml` workflows now accept `release-type: stable | rc` + `rc-number` inputs.
- New `promote-rc.yml` workflow for promoting a validated RC to stable.

### Known issues

- **Plugin 3.1.0 auto-bootstrap UX flaw**: the recovery phrase is surfaced to the user via an LLM-rendered banner. This has a theoretical leak risk and the LLM may silently omit the banner. Plugin 3.2.0 (in flight) replaces this with a CLI onboarding wizard. If you install 3.1.0 and don't see the recovery phrase in your first session, run `totalreclaw_status` to print it directly.
- **Plugin tarballs bundled with older core**: 3.0.x bundled core 2.0.0; 3.1.0 may bundle 2.0.0 or 2.1.0 depending on the refresh state at install time. If `pin_status` isn't working on a fresh install, force-refresh the plugin. A 3.1.1 patch is planned to pin `^2.1.1` as the minimum core dep.
- **`totalreclaw-memory@2.0.1` (Rust crate, crates.io) deferred**: the publish was attempted but blocked at the cargo dry-run sanity check. ZeroClaw users can continue with 2.0.0 -- no functional regression. Publish retry is on the Wave 2 list.

## plugin v3.0.5 (April 2026)
- Fix OpenClaw scanner false-positive from JSDoc "fetch" wording in `config.ts`. No behavior change.
- Added `scanner-sim` CI check to prevent regressions.

## v1.0-beta (March 2026) -- Private Beta
- End-to-end encrypted memory vault for AI agents (AES-256-GCM, HKDF key derivation)
- Dual-chain storage: Free tier on Base Sepolia testnet, Pro tier on Gnosis mainnet
- Stripe billing integration ($5/month Pro tier, 500 free memories/month)
- OpenClaw plugin with automatic memory extraction via lifecycle hooks
- MCP server for Claude Desktop, Cursor, and other MCP-compatible agents
- NanoClaw integration with automatic hooks and CLAUDE.md sync
- IronClaw support via MCP server (routine-based extraction)
- Import adapters: Mem0, ChatGPT, Claude, MCP Memory Server
- Testnet-to-mainnet migration tool (`totalreclaw_migrate`)
- Harrier-OSS-v1-270M embedding model (640d, ~164MB)
- BM25 + Cosine + RRF fusion reranking with dynamic candidate pool sizing
- Store-time near-duplicate dedup (cosine) and LLM-guided dedup (Pro)
- Client batching: multiple facts per UserOp via ERC-4337 executeBatch
- 9/9 E2E integration test journeys passing

## v0.2.0 (March 2026)
- End-to-end encrypted memory vault for AI agents
- OpenClaw skill with automatic memory extraction via lifecycle hooks
- MCP server for Claude Desktop, Cursor, and other MCP-compatible agents
- Stripe billing integration
- On-chain storage via Gnosis Chain with Pimlico gas sponsorship
- 78/78 E2E integration test assertions passing
- Free tier: 500 memories/month, unlimited reads

## v0.1.0 (February 2026) -- PoC
- Initial proof of concept
- AES-256-GCM encryption with blind-index search
- LSH-based fuzzy matching (98.1% Recall@8)
- BM25 + Cosine + RRF fusion reranking
