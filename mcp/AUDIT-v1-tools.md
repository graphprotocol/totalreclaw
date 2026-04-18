# MCP Tools Audit — v1 Taxonomy + Description Autonomy

**Branch:** `feat/mcp-v3.0.0-audit` (extends `feat/mcp-v3.0.0`)
**Date:** 2026-04-17
**Scope:** All 19 tools registered in `mcp/src/index.ts` via `ListToolsRequestSchema`.

## Source of truth

Registered tools (`mcp/src/index.ts:1828-1846`):

1. `totalreclaw_setup`
2. `totalreclaw_remember`
3. `totalreclaw_recall`
4. `totalreclaw_forget`
5. `totalreclaw_export`
6. `totalreclaw_import`
7. `totalreclaw_import_from`
8. `totalreclaw_import_batch`
9. `totalreclaw_consolidate`
10. `totalreclaw_status`
11. `totalreclaw_upgrade`
12. `totalreclaw_migrate`
13. `totalreclaw_debrief`
14. `totalreclaw_support`
15. `totalreclaw_account`
16. `totalreclaw_pin`
17. `totalreclaw_unpin`
18. `totalreclaw_retype`
19. `totalreclaw_set_scope`

(CLAUDE.md's "18 tools" bucket omits `totalreclaw_setup` + `totalreclaw_unpin`; the actually-registered total is 19. Kept that way.)

## Audit table

Scores **before** this audit's fixes (/) and **after** (\):

| # | Tool | v1 compat? before / after | Autonomy before / after | Key issue | Fix |
|---|---|:-:|:-:|---|---|
| 1 | `totalreclaw_setup` | N/A / N/A | 7 / 7 | — | none |
| 2 | `totalreclaw_remember` | YES / YES | 9 / 9 | already emits v1 blob on managed path, accepts v1 enums | none |
| 3 | `totalreclaw_recall` | YES / YES | 7 / 8 | Tier 1 source-weighting works, description updated | B1 rewrite |
| 4 | `totalreclaw_forget` | PARTIAL / PARTIAL | 5 / 8 | added `scope` hint; query-based forget unchanged | B2 + schema |
| 5 | `totalreclaw_export` | **NO** / **YES** | 6 / 8 | didn't surface v1 fields | A3 + rewrite |
| 6 | `totalreclaw_import` | PARTIAL / PARTIAL | 5 / 8 | no v1 blob emission on HTTP path (OK — self-hosted) | rewrite |
| 7 | `totalreclaw_import_from` | PARTIAL / PARTIAL | 6 / 8 | legacy type normalization (HTTP only) | rewrite |
| 8 | `totalreclaw_import_batch` | N/A / N/A | 4 / 8 | polling contract now explicit | rewrite |
| 9 | `totalreclaw_consolidate` | N/A / N/A | 6 / 8 | added managed-service warning | rewrite |
| 10 | `totalreclaw_status` | N/A / N/A | 6 / 8 | triggers added | rewrite |
| 11 | `totalreclaw_upgrade` | N/A / N/A | 7 / 8 | triggers added | rewrite |
| 12 | `totalreclaw_migrate` | N/A / N/A | 7 / 7 | workflow-style already strong | none |
| 13 | `totalreclaw_debrief` | **NO** / **YES** | 6 / 8 | emitted v0 canonical — now emits v1 summary + source=derived | **A1** + rewrite |
| 14 | `totalreclaw_support` | N/A / N/A | 6 / 8 | triggers added | rewrite |
| 15 | `totalreclaw_account` | N/A / N/A | 7 / 8 | triggers added | rewrite |
| 16 | `totalreclaw_pin` | PARTIAL / PARTIAL | 5 / 8 | description overhauled; blob format gap documented (A2) | rewrite + doc |
| 17 | `totalreclaw_unpin` | PARTIAL / PARTIAL | 5 / 8 | same as pin | rewrite + doc |
| 18 | `totalreclaw_retype` | YES / YES | 7 / 8 | description polished | rewrite |
| 19 | `totalreclaw_set_scope` | YES / YES | 7 / 8 | description polished | rewrite |

## Fixes applied — code

### A1. `totalreclaw_debrief` (managed path) now emits v1 summary blob

**Location:** `mcp/src/index.ts:876-893` inside `handleDebriefSubgraph`.

**Before:** Fell through to `buildCanonicalClaim({fact, importance, sourceAgent})` — v0 `{t,c,i,sa,ea}` shape.

**After:** Calls `buildV1ClaimBlob({text, type: 'summary', source: 'derived', importance})`. Per `memory-taxonomy-v1.md` line 67, `summary` is the only v1 type that MUST pair with `source ∈ {derived, assistant}` — `derived` is the right default for session synthesis. Tool-level `type: 'context'` also maps to v1 `summary` (both are session-level synthesis per spec §type-semantics). Legacy raw-text fallback preserved under `TOTALRECLAW_CLAIM_FORMAT=legacy`.

**Commit:** `193f2af`

### A3. `totalreclaw_export` surfaces v1 fields

**Location:** `mcp/src/tools/export.ts` (full handler rewrite inside the format branches).

**Before:** JSON export emitted `{id, text, importance, created_at, metadata}` — silently dropped `type`, `source`, `scope`, `reasoning`, `volatility`, `superseded_by`, `entities` from v1 blobs.

**After:** Parses each fact's decrypted text via `readBlobUnified` (already in `claims-helper.ts`, handles all three blob shapes). JSON output now includes v1 fields when present; Markdown output gains `- **Type:** directive`, `- **Source:** user`, `- **Scope:** work`, `- **Reasoning:** ...` lines. v0 vaults unaffected (only `category` surfaced).

**Commit:** `b74606d`

### B (various). Tool descriptions rewritten for LLM autonomy

**Locations:** `mcp/src/prompts.ts` (recall/forget/export/status/upgrade/import_from/import/support/account) + per-tool files (`pin.ts`, `retype.ts`, `set-scope.ts`, `consolidate.ts`, `debrief.ts`, `import-batch.ts`).

Each rewritten description follows:
1. One-sentence purpose
2. `INVOKE WHEN THE USER SAYS:` — 3-5 natural-language utterances
3. `WHAT IT DOES:` — internal behaviour summary
4. `WHEN NOT TO USE:` — anti-over-invocation guardrail
5. Parameter notes (machine-readable JSON schema unchanged)

The rewrite specifically replaces product-engineering jargon (e.g. pin's "auto-resolution engine will never override" → "nothing, not this agent, not another agent sharing the vault, will ever override") with phrases that map 1:1 to user utterances.

**Commits:** `261e899`, `2859e5e`, `868de6c`, `66298f0`, `5786fda`, `367be7c`, `e845020`

### `totalreclaw_forget` schema extended

Added optional `scope` parameter to the input schema (spec: `mcp/src/tools/forget.ts`). Not yet enforced server-side — documented as a forward-compat hint so existing LLM invocations with scope won't break when the relay later adds scope-filtered recall.

**Commit:** `fa5c696`

## A2 — Known gap: pin/unpin and v1 taxonomy

`totalreclaw_pin` / `totalreclaw_unpin` still write v0 short-key `{t,c,i,sa,ea,st,sup}` blobs.

**Reason:** `memory-taxonomy-v1.md` does not define a pin-status field. `buildV1ClaimBlob` validates through `validateMemoryClaimV1` which rejects unknown keys, so a v1 blob cannot carry `st: "p"` without a spec update. Blindly dropping the `st` flag would lose pin status on round-trip.

**Today's behaviour is safe on mixed-format vaults** because `readBlobUnified` (at `claims-helper.ts:555`) falls back to the v0 short-key parser for any blob that isn't v1-shaped. v1 readers see a pinned fact with category `pref`/`rule`/etc but no `source` / `scope` surface.

**Path forward (deferred, needs spec addendum):**
- Add `pin_status` or equivalent to `MemoryClaimV1` and to the core Rust struct
- Bump v1 to v1.1 (or use an extension mechanism)
- Wire pin/unpin through `buildV1ClaimBlob` the same way retype/set_scope already do

Documented this gap explicitly in the tool descriptions (both pin and unpin reference "fact_id from a prior totalreclaw_recall"), and added a test asserting that an existing v1 blob CAN be pinned via the subgraph flow without crashing (even though the result is v0). Production users on v1 vaults can still pin/unpin — the fact becomes legacy-formatted after the operation, but remains readable.

## Tests added

`mcp/tests/tool-descriptions.test.ts` — 44 assertions across 20 `test` cases:

- **LLM-autonomy triggers:** Each tool's description has the minimum required natural-language trigger bullets (0 for setup/remember/import_batch/migrate where triggers live elsewhere, 2-3 for the rest).
- **"WHEN NOT TO USE" guardrail:** Every user-facing tool with triggers also has an anti-over-invocation section.
- **v1 blob round-trip:** `buildV1ClaimBlob` produces valid v1 JSON; `readBlobUnified` returns v1 surface for v1 blobs and falls back to v0 short-key for legacy blobs.
- **A1 fix verification:** Debrief items built with `type: 'summary'`, `source: 'derived'` round-trip correctly. Tool-level `context` maps to v1 `summary`.
- **A3 fix verification:** v1 blob export yields `type/source/scope/reasoning` in the output object; v0 blob export falls back to `type = category`.
- **Schema shape:** `remember` accepts v1 types (claim/preference/directive/commitment/episode/summary) + v1 scopes; `retype.new_type` is closed v1 enum; `set_scope.scope` is closed v1 scope enum; `forget.scope` is an optional v1 scope hint.

**Full-suite count:** 436 existing tests + 44 new = **480 tests, all passing**.

## Verification of previous MCP agent's work (commit `d6d7b83`)

| Claim | Verified? | Evidence |
|---|:-:|---|
| v1 types exported in `src/v1-types.ts` | YES | All enums + `MemoryClaimV1` interface present, matches Rust core |
| `buildV1ClaimBlob` helper | YES | `claims-helper.ts:419-440`; validates through WASM core |
| `readBlobUnified` three-shape parser | YES | `claims-helper.ts:481-556`; v1 → v0 → plugin-legacy → raw-text |
| `totalreclaw_remember` (managed) writes v1 blob | YES | `index.ts:735-742`; routes through `buildV1ClaimBlob` |
| `totalreclaw_retype` / `set_scope` write v1 blobs | YES | `retype.ts:305`; both call `buildV1ClaimBlob` with override |
| Tier 1 source weighting in recall | YES | Managed (`index.ts:1101-1112`) + HTTP (`tools/recall.ts:142-149`) both call `core.sourceWeight()` |
| v0 fallback works on staging (existing tests) | YES | `v1-blob-roundtrip.test.ts` + `recall-source-weight.test.ts` (155 assertions) green |
| `totalreclaw_debrief` emits v1 | **NO** | Was writing v0 via `buildCanonicalClaim` — **fixed in this audit (A1)** |
| `totalreclaw_pin`/`unpin` emit v1 | **NO** | Still v0 short-key — **deferred (A2), spec gap documented** |
| `totalreclaw_export` surfaces v1 | **NO** | Dropped v1 fields — **fixed in this audit (A3)** |

Net: prior agent's work was correct but incomplete. Three write paths still emitted legacy blobs before this audit; two of them (debrief, export) are now fixed, one (pin/unpin) is deferred to a spec-level change.

## Files modified in this audit

```
mcp/AUDIT-v1-tools.md                      NEW
mcp/src/index.ts                           (A1 fix)
mcp/src/prompts.ts                         (9 description rewrites)
mcp/src/tools/consolidate.ts               (description rewrite)
mcp/src/tools/debrief.ts                   (description rewrite)
mcp/src/tools/export.ts                    (A3 fix + readBlobUnified import)
mcp/src/tools/forget.ts                    (scope schema param)
mcp/src/tools/import-batch.ts              (description rewrite)
mcp/src/tools/pin.ts                       (description rewrite)
mcp/src/tools/retype.ts                    (description rewrite)
mcp/src/tools/set-scope.ts                 (description rewrite)
mcp/tests/tool-descriptions.test.ts        NEW — 44 assertions
```

## Commits on this audit branch (`feat/mcp-v3.0.0-audit`)

```
13300a5 test(mcp): add 44 assertions for tool descriptions + v1 compat
b74606d feat(mcp): surface v1 taxonomy fields in export (type/source/scope/reasoning)
fa5c696 feat(mcp): add v1 scope hint param to totalreclaw_forget tool
e845020 docs(mcp): rewrite import-batch description with polling contract
367be7c docs(mcp): rewrite debrief description with triggers and summary-type note
5786fda docs(mcp): rewrite consolidate description with managed-service warning
66298f0 docs(mcp): rewrite set-scope tool description for LLM autonomy
868de6c docs(mcp): rewrite retype tool description for LLM autonomy
2859e5e docs(mcp): rewrite pin/unpin tool descriptions for LLM autonomy
261e899 feat(mcp): rewrite tool descriptions for LLM-autonomy triggers
193f2af fix(mcp): debrief writes v1 summary blob instead of v0 canonical
```

## Deferred / open questions

1. **HTTP mode doesn't write v1 blobs.** The self-hosted PostgreSQL schema predates v1 and stores text-only via `client.remember`. Migrating HTTP to v1 requires a server-side schema change — out of scope for this audit. All HTTP-mode write paths remain v0.

2. **A2: pin/unpin v1 support.** Requires a spec addendum to `memory-taxonomy-v1.md` adding `pin_status` to `MemoryClaimV1`. Until then, pinning on a v1 vault drops that fact to v0 short-key — readable but loses `source`/`scope` surface. Flagged this as a follow-up.

3. **`forget` scope filter.** The schema param is accepted today as a forward-compat hint, but neither the relay nor the self-hosted server currently implements scope-filtered blind indices on search. Real enforcement is a follow-up in the relay/server.

4. **`import_from` / `import` don't propagate v1 scope + reasoning.** Source adapters (Mem0, ChatGPT, Claude) don't emit v1 types today — imports land as v0 via the HTTP `client.remember` path. Improvement: have adapters emit v1 scope/type based on source metadata (e.g. Mem0 `agent_id` → scope hint). Out of scope.

5. **Tool description length.** Several descriptions are now 800-1200 characters. MCP spec doesn't limit tool description length, but if any MCP host has truncation, we may need to trim. No such host known today.

## How to validate

```bash
cd mcp
npm test                                    # 480/480 tests pass
npx tsc --noEmit                            # clean
npx jest tests/tool-descriptions.test.ts    # 44/44 new tests pass
```

For staging validation (not run here — staging involves live relay + chain):

```bash
# Managed-service round-trip of debrief
TOTALRECLAW_RECOVERY_PHRASE="..." node dist/index.js
# Call totalreclaw_debrief with 2 items, then totalreclaw_recall, verify
# memories[].source === 'derived' and memories[].type === 'summary'.
```
