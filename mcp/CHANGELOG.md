# Changelog

## [3.5.0] - 2026-08-16

Minor release — R3 of the Option E Phase 2 release train. Additive: the
`derived-bundle-v1` credential adapter (P2-13) plus the `@totalreclaw/core`
floor bump that activates it. Every pre-existing credential path is unchanged.

**Cross-client parity validated (E2E scenario S6, staging, 2026-08-16.)** The
bundle is a cross-language contract, not a Python convention — proven in all
four directions on one vault, each hop decrypting the other client's
ciphertext:

| write | read | result |
|---|---|---|
| Hermes (bundle) | MCP (phrase) | PASS |
| MCP (phrase) | Hermes (bundle) | PASS |
| Hermes (phrase) | MCP (bundle) | PASS |
| MCP (bundle) | Hermes (phrase) | PASS |

**Claim discipline** (design memo §3 — carried by every Phase 2 release note):
Phase 2 removes the recovery phrase from agent hosts. It does **not** improve
memory confidentiality at a compromised host — the encryption key still
materialises in RAM on every recall, and ciphertext is public on the Gnosis
subgraph. Until Phase 3 the provisioned signing key carries full Smart Account
owner authority and is **not revocable**; treat a leaked bundle as you would a
leaked phrase, minus its portability.

### Fixed
- **[#578] `import_from` tool descriptions no longer advertise MemoClaw.** The tool description listed `memoclaw` as an accepted source but the `ImportSource` enum never accepted it, so an agent taking the description at its word got a validation error for a source that was never supported ([#586](https://github.com/p-diogo/totalreclaw/pull/586)). Descriptions now match the enum exactly.


### Added
- **`derived-bundle-v1` credential bundle support (Option E Phase 2 — [#581](https://github.com/p-diogo/totalreclaw/issues/581), P2-13).** The MCP server is the second client (after Hermes) to configure from a bundle instead of a BIP-39 recovery phrase: the four HKDF-derived vault keys plus a signing key, never the phrase or the 64-byte seed. `~/.totalreclaw/credentials.json` `"version": 2` is detected alongside the existing plaintext-mnemonic shape (precedence: env phrase → `version: 2` bundle → legacy mnemonic; an unrecognised `version` is a loud startup error, never a silent downgrade; an env-phrase that shadows an on-disk `version: 2` bundle logs a warning naming both). New modules: `src/subgraph/bundle.ts` (a runtime-feature-detecting adapter over `@totalreclaw/core`'s `parseBundleV1`/`validateBundleV1`/`deriveBundleFromMnemonic` — never assumes the installed dependency exposes them at compile time), `src/subgraph/credentials.ts` (the precedence resolver, plus `isV2BundleCredentialsFile` for the self-hosted-misconfiguration diagnostic below). `src/subgraph/chain-config.ts` gained `fetchChainConfig` — the single billing-fetch path both mnemonic-mode and bundle-mode state construction now share, closing the SKIPPED-billing-call variant of a bug class found in the Hermes round where a bundle client could skip the billing call entirely (which is the ONLY source of the environment's DataEdge address) and silently write to core's production default even against a staging relay. A sibling FAILED-billing-call variant (the call is made but errors/times out/non-200s) is closed separately below. `src/subgraph/store.ts` gained `resolveOwnerAccount`, threading either a mnemonic-derived or bundle-supplied signing key into every ERC-4337 UserOp signer. Bundle-mode `totalreclaw_pair` completion (`payload_type: "derived-bundle-v1"`) is implemented in `pair-remote-client.ts` + `tools/pair.ts` and fixture-tested, but inert until the relay forwards a `payload_type` field (tracked separately, P2-11). Self-hosted mode is unaffected — it is a wholly separate credential system with no BIP-39 root, and `TOTALRECLAW_SELF_HOSTED=true` with a `version: 2` credentials.json now gets an actionable "bundles are managed-service-only" message instead of the misleading "set TOTALRECLAW_RECOVERY_PHRASE" advice. **No OS-keychain unwrap and no `TOTALRECLAW_CREDENTIALS_PROVIDER` external-secret-manager support** (both Python/Hermes-only for now) — see README's "Credential Material" section for the tracked gaps.

### Fixed (pre-release hardening, adversarial review of #618)
- **Bundle mode now fails closed when the billing lookup fails outright.** `fetchChainConfig` gained a `billingResolved` flag (true only on a 200 + parseable response); `initSubgraphStateFromBundle` throws an actionable error (`assertDataEdgeResolvable`, `subgraph/chain-config.ts`) rather than proceeding when the billing call errored/timed-out/non-200'd AND no `TOTALRECLAW_DATA_EDGE_ADDRESS` override is set — a bundle carries no `data_edge_address` of its own, so silently proceeding would fall through to the store's PRODUCTION DataEdge default. This is the failed-call sibling of the skipped-call variant fixed above (both close under the same "Lesson 1" umbrella, but they're two different bugs with two different fixes). Mnemonic mode keeps its pre-existing (#439) fallback-and-proceed behaviour, now with a loud `console.error` naming the production default whenever that fallback engages.
- **Self-hosted mode + a `version: 2` bundle file** now gets a specific, actionable diagnostic (`isV2BundleCredentialsFile`) instead of the generic "set TOTALRECLAW_RECOVERY_PHRASE" message, which was actively misleading in that case.
- **A malformed (present-but-garbage) `data_edge_address` in a 200 billing response** now logs a `console.error` naming the bad value instead of dropping silently.
- **`pair-remote-client.ts`'s bundle-payload handler-existence check now runs BEFORE `parseBundleV1`**, so a call site with no `completePairingBundle` handler always nacks `unsupported_payload_type` (the accurate diagnosis) rather than `invalid_bundle` in the case where the never-parsed payload also happens to be malformed.

### Release gate
- **Canonical f16 embedding writes now active (#479 Part B, no code change).** A side effect of the `@totalreclaw/core` `^2.5.3` -> `^2.6.0` bump above, and an intended one. Core 2.6.0 carries `encodeEmbeddingCanonical`, and `src/embedding-codec.ts`'s write path has always feature-detected it — so MCP silently stops emitting the legacy f32 packing and starts emitting canonical base64(LE-f16), which is smaller and is the cross-client canonical form. Read-side was already universal (canonical / legacy TS JSON / legacy f32 all decode), so mixed-vault reads are unaffected in both directions and no migration is implied for existing facts. The OpenClaw plugin continues to write legacy JSON until its own core dep is bumped (parked client).
- **Requires `@totalreclaw/core>=2.6.0`** — the release exposing `parseBundleV1` / `validateBundleV1` / `deriveBundleFromMnemonic`. That gate is now cleared: core **2.6.0** published 2026-08-16 (npm + PyPI + crates.io from one commit) carries all three in both the `nodejs` and `./web` WASM builds, and `mcp/package.json` has been bumped from `^2.5.3` to `^2.6.0`. Note the earlier `2.6.0-rc.1` (npm, 2026-07-20) predated #587 and carried **no** bundle exports — it is superseded by 2.6.0 and must not be used to satisfy this requirement. During development this adapter was verified against BOTH a bindings-less published core (`tsc` / `npm run build` / `npx jest` clean, bundle-specific tests skipping with a visible reason) and a locally-built WASM with the bindings present (full bundle coverage). The runtime feature-detection in `src/subgraph/bundle.ts` is retained deliberately even now that the floor guarantees the bindings — it keeps a stale-install downgrade loud rather than a crash.

## [3.4.0] - 2026-07-09

Minor release. Bundles the code-health restructure (from the repo-wide desloppify sweep) with the #439 managed-service chain/DataEdge correctness fix. Minor, not major, despite a tool removal — the removed tool was already deprecated + non-functional (see Removed).

### Fixed
- **[#439] Managed-service writes now consume the relay's billing `chain_id` + `data_edge_address` verbatim.** Previously EVERY managed-mode write (free **and** pro) fell through to the `getSubgraphConfig` default of chain `84532` (Base Sepolia, retired at ops-1) and the **production** DataEdge `0xC445…` — the tier→`chainId=100` flip was dead code, never threaded into the write config. On the staging relay this meant writes landed on the prod DataEdge and were invisible on the staging subgraph; more broadly it was a client-consistency violation vs the Python client + OpenClaw plugin. New `src/subgraph/chain-config.ts` (`resolveChainConfig` / `buildSubgraphOverrides`) resolves both fields from `/v1/billing/status`, carries them on `SubgraphState`, and threads them into all 5 managed-write sites via `stateWriteOverrides`. Default chain flipped `84532 → 100`. The `TOTALRECLAW_DATA_EDGE_ADDRESS` env override still wins over billing. Validated by a staging write→read E2E: a fresh account's write hit the **staging** DataEdge `0xE7a4D2…` (on-chain receipt), indexed on the staging subgraph, recalled back. Sibling of plugin `#402` (chain_id) + `#460` (data_edge_address).

### Changed
- **Tool dispatch unified into a single data-driven table** (`src/dispatch.ts` + `src/server-setup.ts`). The two parallel self-hosted/managed `switch` statements + per-tool glue are replaced by one router: `TOOL_MANIFEST` (mode-independent tool list), `SUBGRAPH_POLICY` / `HTTP_POLICY` (per-(tool, mode) cross-cutting behaviour as data), and `createCallToolHandler` with handlers injected as pre-bound bundles. Routing order preserved verbatim; behaviour-preserving.
- **`memory_id` is the canonical id parameter** for pin/unpin/retype/set-scope; `fact_id` is kept as a back-compat alias so existing callers don't break.
- Handlers take a shared `ToolContext` (dependency-injection bundle) instead of per-handler positional/options variance.
- Domain restructure: crypto/subgraph/extraction/tools split into subdirectories; several helpers de-async'd where they never awaited.

### Removed
- **`totalreclaw_migrate` tool deleted.** It was already marked `[DEPRECATED] INVOKE WHEN: never` and was non-functional (the legacy cross-chain migration path it wrapped was retired with single-chain ops-1). No working call site loses functionality — hence a **minor**, not a major, bump. Stale catalogs that still reference it get the standard tool-removed error envelope.

## [3.2.1] - 2026-04-26

### Security
- **[SECURITY] Removed `totalreclaw_setup` tool**; users should follow the URL-driven install flow per [`docs/guides/claude-code-setup.md`](../docs/guides/claude-code-setup.md). Onboarding now sources the recovery phrase out-of-band (OpenClaw / Hermes browser pair flow, an offline BIP-39 generator, or a prior `~/.totalreclaw/credentials.json` cache) and the user pastes it directly into `TOTALRECLAW_RECOVERY_PHRASE` in the MCP host config. The agent never sees the phrase.
- Stale tool catalogs that still call the old name receive a structured `tool_removed` error pointing at the canonical install guide.
- Server `SERVER_INSTRUCTIONS` rewritten to the new phrase-safety stance (no in-chat setup, never echo / print / summarize the phrase, never invoke phrase-touching CLIs via a shell tool).
- Added `tests/phrase-safety-dist.test.ts` — a regression scan over compiled `dist/` JS that fails if any source code emits a phrase string in a response-payload-shaped JSON object.
- Added repo-level companion to the existing markdown-only phrase-safety guard at `scripts/check-phrase-safety.sh`: new `scripts/check-phrase-safety-dist.sh` scans compiled MCP `dist/` JS for source-emitted phrase keys in tool responses.

### Notes / Compatibility
- Patch-version bump (3.2.0 -> 3.2.1). No breaking change for end-users — the MCP onboarding path was already documented as URL-driven in `docs/guides/claude-code-setup.md` (which explicitly forbids the deleted tool). Hosts that retained a stale `totalreclaw_setup` reference get a structured error with a link to the install guide.

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
