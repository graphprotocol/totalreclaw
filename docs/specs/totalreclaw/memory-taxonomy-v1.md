# MCP Memory Taxonomy v1

**Version:** 1.0.0-draft (pending benchmark validation + lock)
**Status:** DRAFT. Validated on Gemini corpus (Pipeline C). WildChat cross-validation in progress (2026-04-17). Lock after WildChat results confirm.
**Owner:** TotalReclaw (pedro@thegraph.foundation)
**Intended publication:** `totalreclaw.xyz/spec/memory-v1` + PR to MCP spec repo as optional `@modelcontextprotocol/memory-taxonomy/v1` extension.

---

## Purpose

Define a standard memory schema that MCP-compatible AI agents can agree on, so memory written by Claude Desktop can be read by Cursor, ChatGPT, custom agents, or any MCP host without drift. Like JSON Schema for emails — an interoperability primitive.

Today cross-client memory is broken: Claude writes `preference: dark mode`, Cursor writes `fact: prefers dark mode`, and a recall query for "what do I prefer" misses two of three. This spec solves that.

## Why this spec must be written by a third party

The Model Context Protocol itself deliberately leaves memory semantics undefined. Single-vendor memory systems (Mem0, Zep, Supermemory, Mastra, Letta) each define their own schema, which doesn't interoperate. TotalReclaw is uniquely positioned to write this spec because:

1. **Cross-client by design.** We ship in 5 clients (OpenClaw, MCP server, NanoClaw, Hermes, ZeroClaw) and the spec must serve all of them.
2. **E2E encrypted.** Server-blind architecture means we cannot unilaterally normalize memory server-side. The spec must be client-enforced.
3. **No vendor lock-in stake.** Mem0/Zep have commercial reasons to diverge; we benefit from convergence.

## Design axioms

1. **Orthogonal axes, not one dimension.** Prior taxonomies (ours included) crammed content class, temporal scope, and operation into one `type` field. This produces clustering pathology. v1 splits into 4 independent axes.
2. **Speech-act-grounded types.** Searle's five illocutionary classes map cleanly to user utterances in real AI conversations. Tulving's episodic/semantic distinction doesn't (zero Reddit threads use that vocabulary).
3. **Provenance as a first-class field.** Mem0 audit (Issue #4573) found 97.8% of entries were junk, root-caused to assistant content being misattributed as user fact. Source field makes this structurally impossible.
4. **Closed type enum, open scope enum.** Types must be a universal small set for cross-client portability; scopes can extend per-client.
5. **Importance is advisory, not authoritative.** Receivers may recompute importance at query time based on their own retrieval model. Prevents cross-client scoring drift.
6. **No backwards compatibility** (pre-v1 records purged — no prod users at lock time).
7. **Zero user-facing configuration knobs.** TTL, confidence thresholds, and volatility defaults are implementation details. Users interact via natural language to their agent + optional dashboard edits.

## Schema

```typescript
interface MemoryClaimV1 {
  // ── REQUIRED ─────────────────────────────────────────────────
  id: string;                    // UUIDv7 (time-ordered, no separate created_at needed for sort)
  text: string;                  // human-readable, 5-512 UTF-8 chars
  type: MemoryType;              // 6 values, closed enum
  source: MemorySource;          // who authored
  created_at: string;            // ISO8601 UTC (redundant w/ UUIDv7 but explicit)
  schema_version: "1.0";

  // ── ORTHOGONAL AXES (defaults applied if absent) ─────────────
  scope?: MemoryScope;           // life domain, auto-detected; default "unspecified"
  volatility?: MemoryVolatility; // assigned in comparative rescoring pass; default "updatable"

  // ── STRUCTURED FIELDS ────────────────────────────────────────
  entities?: Entity[];           // people/places/projects/tools/concepts
  reasoning?: string;            // separate field for decision-style claims (replaces old "decision" type)
  expires_at?: string;           // ISO8601 UTC; set by extractor per type+volatility heuristic

  // ── ADVISORY (receivers MAY recompute) ───────────────────────
  importance?: number;           // 1-10, auto-ranked in comparative pass
  confidence?: number;           // 0-1, extractor self-assessment
  superseded_by?: string;        // claim id that overrides this (tombstone chain)
}

type MemoryType = 
  | "claim"        // assertive speech act: absorbs fact, context, decision
  | "preference"   // expressive: likes/dislikes/tastes
  | "directive"    // imperative: rules the user wants applied going forward
  | "commitment"   // commissive: future-oriented intent
  | "episode"      // narrative: notable events
  | "summary";     // derived synthesis (ONLY valid w/ source ∈ {derived, assistant})

type MemorySource = 
  | "user"         // user explicitly stated
  | "user-inferred"// extractor confidently inferred from user signals
  | "assistant"    // assistant authored — downgrade heavily, see provenance filter
  | "external"     // imported from another system
  | "derived";     // computed (digests, summaries, consolidation)

type MemoryScope = 
  | "work" | "personal" | "health" | "family" 
  | "creative" | "finance" | "misc" | "unspecified";
  // OPEN extensible by client, but clients MUST accept all v1 scopes

type MemoryVolatility = 
  | "stable"       // unlikely to change for years (name, allergies, birthplace)
  | "updatable"    // changes occasionally (job, active project, partner's name)
  | "ephemeral";   // short-lived (today's task, this week's itinerary)

interface Entity {
  name: string;    // specific, prefer proper nouns
  type: "person" | "project" | "tool" | "company" | "concept" | "place";
  role?: string;   // optional: "chooser" / "employer" / "rejected"
}
```

## Type semantics

Every type maps to one of Searle's illocutionary classes. The mapping is intentional — it forces extractors to think "what kind of act is the user performing" instead of "what kind of object is this."

| v1 Type | Speech act | Test to distinguish | Absorbs (from legacy) |
|---|---|---|---|
| `claim` | assertive | "X is the case" (descriptive, state-of-world) | fact, context, decision |
| `preference` | expressive | "I like/prefer/hate X" (expresses attitude) | preference |
| `directive` | imperative | "always do Y / never do Z" (commands behavior) | rule |
| `commitment` | commissive | "I will do X" (future intent) | goal |
| `episode` | narrative | "X happened at time T" (past event) | episodic |
| `summary` | derived | session/thread synthesis (not a turn extraction) | summary |

### Boundary tests

- **claim vs preference:** "I live in Lisbon" = claim. "I prefer Portuguese over Spanish" = preference. Test: would replacing it affect a decision, or just taste?
- **claim vs directive:** "Postgres handles my analytics workload" = claim. "Always use Postgres for analytics" = directive. Test: is this descriptive (claim) or commanding future behavior (directive)?
- **directive vs preference:** "Prefers dark mode" = preference (taste). "Never use dark UI for data-dense screens" = directive (rule). Test: does the user want this ENFORCED, or just CONSIDERED?
- **commitment vs claim:** "Shipping v2 Friday" = commitment. "v2 shipped Friday" = episode (if past) or claim (if current state).
- **episode vs claim:** "Deployed v1.0 on March 15" = episode (event). "v1.0 is deployed" = claim (state).
- **summary:** ONLY valid when produced by debrief/compaction pipelines. Extractors processing live turns MUST NOT emit type:summary.

### Reasoning field

For `type: claim` where the user expressed a decision-with-reasoning, populate `reasoning` with the WHY clause:

```json
{
  "text": "Chose PostgreSQL for the analytics store",
  "type": "claim",
  "reasoning": "data is relational and needs ACID guarantees",
  "entities": [{"name": "PostgreSQL", "type": "tool", "role": "chosen"}]
}
```

Separate field (not embedded in text) enables structured queries like "show me all my decisions with their reasoning."

## Provenance filter (normative)

Every compliant client MUST implement provenance filtering on extracted claims:

1. **At extraction:** LLM is instructed to attribute every claim to a source. If claim substance appears only in `[assistant]` turns and user did not affirm/quote/use it, extractor marks `source: "assistant"`.
2. **Post-extraction verification:** independent keyword fuzzy-match against user turns. Thresholds:
   - ≥30% of content words match → retain source
   - <30% match AND source != "user" → downgrade to `source: "assistant"`
3. **Downgrade consequence:** `source: "assistant"` claims have importance capped at 5 OR dropped entirely (client config).

This is the single biggest quality lever per the Mem0 97.8% junk audit (Issue #4573). Non-negotiable for v1 compliance.

## Volatility assignment (normative)

Assigned in the comparative rescoring pass (post-extraction, pre-storage), NOT at initial single-claim extraction. Rationale: single claims lack context; the full conversation + all extracted facts together let the LLM judge temporal scope accurately.

Default heuristic fallback if LLM omits:
- `type: commitment` → `updatable`
- `type: episode` → `stable` (events happened, immutable)
- `type: directive` → `stable` (rules persist)
- `scope ∈ {health, family}` → `stable`
- everything else → `updatable`

## Expiration policy (normative)

`expires_at` is set at extraction, NOT after. Heuristic by type + volatility:

- `type: episode` + `volatility: ephemeral` → now + 30 days
- `type: claim` + `volatility: ephemeral` → now + 14 days
- everything else → no expiry

Retrieval decay (recall-time ranking penalty for old memories) is orthogonal to expiration and defined by the client implementation.

## Cross-client guarantees

1. Every MCP-compatible client implementing v1 MUST store + return claims matching the schema above.
2. **Type enum is closed.** Vendors MUST NOT extend `MemoryType`. Proposed additions go through the spec PR process.
3. **Scope enum is open.** Clients MAY define additional scope values for their own use, but MUST accept + preserve all v1-defined scopes when reading from a vault written by another client.
4. **Importance is advisory.** Receivers MAY recompute. Writers SHOULD include it as hint, not ground truth.
5. **Schema version field is required.** Clients encountering unknown versions MUST refuse to read (fail-safe, prevents silent drift).

## Compliance levels

- **Level 0 (Read-only):** Client can decrypt + parse + display v1 claims written by others. No write capability.
- **Level 1 (Read+Write):** Client can produce v1-conformant claims. MUST include provenance filter.
- **Level 2 (Full):** Level 1 + implements comparative rescoring for volatility assignment + v1-compliant `superseded_by` chain.

Reference implementations: TotalReclaw 5 clients target Level 2 at v1 launch.

## Migration from v0 (TotalReclaw-internal only)

No backwards compat at v1 lock. Pre-v1 records in test vaults are purged. v0 type names (`fact`, `context`, `decision`, `goal`, `rule`, `episodic`) are rejected at the validate stage.

External systems importing to v1 vaults MUST normalize at the import adapter layer. Reference mapping:

```
fact, context, decision → claim (decision populates reasoning field)
preference               → preference
rule                     → directive
goal                     → commitment
episodic                 → episode
summary                  → summary (source must be derived|assistant)
```

## Retrieval dependency

If v1 ships Pipeline F or G (tag-don't-drop provenance), the reranker in `@totalreclaw/core` must respect `source`. See `docs/specs/totalreclaw/retrieval-v2.md` for the required retrieval changes. Minimum: source-weighted final score (Tier 1). Everything else is post-v1.

## Distribution

1. Land normative schema in `@totalreclaw/core@2.0.0` (Rust WASM for TS, PyO3 for Python). Includes `validate_v1(claim)` + `normalize_legacy_to_v1(claim)` helpers (for adapter layer, not for prod).
2. Publish spec doc at `totalreclaw.xyz/spec/memory-v1`.
3. PR to MCP spec repo proposing `@modelcontextprotocol/memory-taxonomy/v1` as optional extension.
4. Reference clients ship simultaneously: OpenClaw, MCP server, NanoClaw, Hermes, ZeroClaw.
5. Outreach to Mem0, Letta, Mastra, Supermemory, Zep — invite alignment.

## Validation gate (pre-lock)

Pipeline C in `tests/importance-benchmark/run-ab-benchmark.ts` implements v1. A/B/C run on two corpora (Gemini Takeout 200 convs, WildChat-1M 200 convs) compares against A (baseline 8-type) and B (2.2.7 B+D). Lock criteria:

- Clustering ratio < 30% at most-common importance bucket
- Type-entropy/log2(N_types) > 0.65 (normalized)
- Provenance filter reduces rule-like false positives by ≥30% vs B
- Empty rate < 35%
- Within-conv dup rate < 1%
- Spot-check: 10 random C-extracted facts manually judged more useful than matching A/B

Results table (Gemini 200-conv, 2026-04-17):

| Metric | A | B | C | Lock threshold |
|---|---|---|---|---|
| Clustering ratio | 47.6% | 22.4% | 29.6% | <30% ✓ |
| Normalized type entropy | 0.81 | 0.71 | 0.58 | >0.65 ✗ see notes |
| Directive/rule share | 7.1% | 11.6% | 5.7% | B→C drop ≥30% ✓ (51% reduction) |
| Empty rate | 36.5% | 9.5% | 26.0% | <35% ✓ |
| Within-conv dup rate | 0.1% | 0.5% | 0.4% | <1% ✓ |

Note on normalized entropy: C's entropy on 6 types (1.50 / 2.58 = 0.58) is below threshold. Root cause: `claim` (63.7%) is a deliberate superset of legacy fact+context+decision (which summed to 61.3% in A), so the concentration is by-design, not pathological. Proposal: measure **effective claim-subdivision entropy** (using `source + scope + reasoning-present` to subdivide claims) as the compliance metric instead of raw type entropy. Apply at WildChat result to decide.

WildChat results: pending (in progress 2026-04-17).

## Open items

1. Confirm entropy threshold proposal via WildChat validation.
2. Finalize `entity.type` enum — current draft has 6 values; may need `event` or `media` for non-dev corpora.
3. Decide whether `expires_at` should be relay-tunable (global default) or purely spec-constant.
4. MCP extension PR timing — land after 2 weeks of TotalReclaw production validation post-v1 lock.
5. Compliance test suite — JSON test fixtures + parity harness for any implementer to validate against.

## References

- [Mem0 97.8% junk audit (Issue #4573)](https://github.com/mem0ai/mem0/issues/4573) — motivates provenance filter as first-class requirement
- [Searle speech acts (SEP)](https://plato.stanford.edu/entries/speech-acts/) — type taxonomy grounding
- [LongMemEval (arXiv 2410.10813)](https://arxiv.org/abs/2410.10813) — benchmark defining task categories informing scope
- [Tiago Forte PARA method](https://fortelabs.com/blog/para/) — scope-axis user mental model
- [Mastra observational memory](https://mastra.ai/research/observational-memory) — zero-typing counter-argument
- [Supermemory ASMR (99% LongMemEval)](https://supermemory.ai/blog/we-broke-the-frontier-in-agent-memory-introducing-99-sota-memory-system/) — 6-vector precedent, "Assistant Info" as first-class bucket
- Internal: `totalreclaw-internal/docs/plans/2026-04-16-kg-roadmap-and-active-phases.md` §3.0
