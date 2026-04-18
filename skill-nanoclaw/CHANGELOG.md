# Changelog — @totalreclaw/skill-nanoclaw

## 3.0.0 — 2026-04-18

Memory Taxonomy v1 is now the default (and only) extraction path. The legacy
v0 8-type list (fact, decision, episodic, goal, context, rule, plus preference
and summary) is retired from the write path; legacy v0 tokens stored by pre-v3
NanoClaw are still read-side-compatible (via `normalizeToV1Type` +
`V0_TO_V1_TYPE`) so vault entries continue to round-trip.

### Taxonomy changes

- `VALID_MEMORY_TYPES` now lists the 6 v1 canonical types:
  `claim | preference | directive | commitment | episode | summary`.
- New `VALID_MEMORY_SOURCES` (5 provenance values):
  `user | user-inferred | assistant | external | derived`.
- New `VALID_MEMORY_SCOPES` (8 life-domain values):
  `work | personal | health | family | creative | finance | misc | unspecified`.
- New `VALID_MEMORY_VOLATILITIES`: `stable | updatable | ephemeral`.
- New `normalizeToV1Type(raw)` helper — pass-through for v1, maps legacy v0
  via `V0_TO_V1_TYPE`, defaults unknown to `claim`.
- `ExtractedFact` carries `source`, `scope`, `reasoning`, `volatility` fields
  in addition to the v0 fields.

### Extraction prompts (`extraction/prompts.ts`)

- `BASE_SYSTEM_PROMPT` rewritten for the v1 merged-topic format:
  - PHASE 1 identifies 2-3 topics before extraction
  - PHASE 2 extracts facts anchored to those topics
  - Output now `{ topics: [...], facts: [...] }`; bare `{facts}` and bare
    arrays still accepted for robustness
  - Provenance (`source`) required per fact
  - v1 importance rubric: full 1-10 range, no 7-8-9 clustering
- `validateExtractionResponse` now:
  - Accepts v1 merged shape, bare object, and bare array
  - Coerces legacy v0 types via `normalizeToV1Type`
  - Defaults missing `source` to `user-inferred`, missing `scope` to `unspecified`
  - Drops illegal `type:summary + source:user` combinations per spec
  - Truncates `reasoning` at 256 chars, `text` at 512 chars

### Hooks (`hooks/*.ts`)

- `agent-end.ts` now tags stored facts with `source:X` and (when non-default)
  `scope:Y` in addition to `namespace:N` and the v1 type. Missing `source`
  defaults to `user-inferred` as a write-path safety net.
- `pre-compact.ts` same tagging; debrief items coerce legacy `context` to
  v1 `claim` via `V0_TO_V1_TYPE` and emit `source:derived`.
- `before-agent-start.ts` reads both v1 and v0 type tags for backward
  compatibility (so recall on pre-v3 vaults still surfaces `[rule]` / `[fact]`).

### Dependencies

- `@totalreclaw/core` now `file:../rust/totalreclaw-core/pkg` (core v2.0.0 WASM).
  Published build targets `^2.0.0`.
- `@totalreclaw/mcp-server` now a **peer dependency** at `^3.0.0` — the
  agent-runner spawns it via `npx @totalreclaw/mcp-server`, so runtime
  resolution works without a direct install. NanoClaw 3.0.0 requires MCP 3.0.0
  for the new v1 tools (`totalreclaw_pin`, `totalreclaw_retype`,
  `totalreclaw_set_scope`) to be discoverable by the embedded agent.
- `@totalreclaw/client` bumped to `^1.2.0`.

### MCP v1 tool discovery

The agent-runner (`mcp/nanoclaw-agent-runner.ts`) continues to allowlist the
`mcp__totalreclaw__*` glob, so MCP v3.0.0's new v1 tools (pin, unpin, retype,
set_scope) are automatically reachable by the NanoClaw agent LLM. The
`allowedTools` config and `mcpServers.totalreclaw` spawn block have been
covered by a new static integration test in
`tests/mcp-tool-discovery.test.js`.

### Testing

- New test suite `tests/v1-taxonomy.test.js` — **42 v1 default-path tests**
  covering prompt content, response validation (merged + bare shapes + legacy
  arrays), type coercion, default source/scope/reasoning, illegal-combination
  dropping, v1 hook tag emission, and v1+v0 read-side tag detection.
- New test suite `tests/mcp-tool-discovery.test.js` — 6 static integration
  tests asserting the agent-runner's MCP server spawn + tool allowlist.
- Legacy `tests/hooks.test.js` unchanged (11 of its 36 assertions fail
  pre-existing, independent of this PR; the 25 passing ones continue to pass).

### Not done (deferred)

- End-to-end MCP `tools/list` verification — gated on MCP 3.0.0 publish, at
  which point the cross-client E2E suite in `totalreclaw-internal/e2e` picks
  it up.
- Spawn-a-real-MCP Jest test — requires an MCP-SDK test harness not present
  in NanoClaw's devDependencies. The static allowlist test covers the
  narrow config-regression surface; actual tool invocation is E2E territory.

## 0.3.0 and earlier

See git history: `git log skill-nanoclaw/ --oneline`.
