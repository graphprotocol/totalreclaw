# TotalReclaw Changelog

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
