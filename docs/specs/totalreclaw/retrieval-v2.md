# Retrieval v2 — source + scope + volatility aware ranking

**Version:** 2.0.0
**Status:** **Shipped** — Tier 1 (source-weighted rerank) shipped in `@totalreclaw/core` 2.0.0 (2026-04-18); Tiers 2–4 designed, not implemented.
**Created:** 2026-04-17
**Supersedes:** retrieval logic in `@totalreclaw/core@1.5.0` (source-blind + scope-blind ranking)

---

## Purpose

Current retrieval pipeline doesn't read the new v1 taxonomy fields. If we ship v1 (claim/preference/directive/commitment/episode/summary + source/scope/volatility), the reranker must respect them — otherwise the whole provenance-filter + axis-split architecture is wasted.

This doc specifies the minimum retrieval change required for v1 lock (Tier 1), plus three optional tiers that may follow.

## Dependency gate

Do NOT implement v2 retrieval before:
1. Pipeline F or G validates on both corpora (Gemini + WildChat 100-conv minimum, ideally full 200)
2. Memory Taxonomy v1 spec locks (`docs/specs/totalreclaw/memory-taxonomy-v1.md`)
3. `@totalreclaw/core@2.0.0` schema lands

If any of the above slips, defer this spec.

---

## Current retrieval pipeline (v1.5.0)

Lives in `rust/totalreclaw-core/src/reranker.rs` (shared via WASM/PyO3 across all clients).

```
query_text
  │
  ├─► extract blind trapdoors (word-level HMAC + LSH buckets)
  │
  ├─► server returns encrypted candidates (~30-200)
  │
  ▼
decrypt candidates client-side
  │
  ├─► BM25 on text (intent-weighted)
  ├─► Cosine on embeddings (Harrier 640d)
  ├─► RRF fusion (reciprocal rank fusion, k=60)
  │
  ▼
intent-weighted reranking (query intent → score boost on matching semantic bucket)
  │
  ▼
top N (default 8)
```

**Fields it reads:**
- Decrypted `text` (BM25 input)
- Decrypted `embedding` (cosine input)
- `decay_score` from chain (exclusion threshold, default 0.3)

**Fields it does NOT read:**
- `source` — no ranking effect
- `scope` — no filtering
- `volatility` — no temporal awareness
- `type` — no category boost

---

## Tier 1 — Source-weighted final score (REQUIRED for v1)

**Rationale:** Pipeline F/G's core thesis is "tag + keep all facts; let retrieval rank by provenance." If reranker ignores `source`, assistant-tagged facts score equal to user-authored claims. Mem0 97.8% junk bug returns.

**Change:**

Add a multiplier to the final RRF score based on source. The values below
(v2-lenient) shipped in `@totalreclaw/core@2.4.0` per the kg-1 promote
(tracked internally).
For the historical v1 weight matrix that shipped 2.0.0–2.2.0, see
[Tier 1 v1 (deprecated)](#tier-1-v1-deprecated) below.

```rust
// rust/totalreclaw-core/src/reranker.rs (v2-lenient, core 2.4.0+)

pub const SOURCE_WEIGHTS: &[(MemorySource, f64)] = &[
    (MemorySource::User,          1.00),  // user explicitly said it
    (MemorySource::UserInferred,  0.95),  // extractor inferred from user signals
    (MemorySource::Derived,       0.85),  // digest, summary, consolidation
    (MemorySource::External,      0.85),  // imported from another system
    (MemorySource::Assistant,     0.85),  // assistant-authored (mild penalty)
];

pub fn source_weight(source: MemorySource) -> f64 {
    SOURCE_WEIGHTS.iter()
        .find(|(s, _)| *s == source)
        .map(|(_, w)| *w)
        .unwrap_or(LEGACY_CLAIM_FALLBACK_WEIGHT) // 0.85, for missing-source candidates
}

pub fn rerank(candidates: &[Candidate], query: &Query) -> Vec<ScoredCandidate> {
    // ... existing BM25 + cosine + RRF ...
    for scored in &mut scored_candidates {
        scored.final_score *= source_weight(scored.claim.source);
    }
    scored_candidates.sort_by(|a, b| b.final_score.partial_cmp(&a.final_score).unwrap());
    scored_candidates.truncate(query.top_k);
    scored_candidates
}
```

**Weight calibration (v2-lenient):**

Bench-validated across 3+ corpora (LongMemEval-500 big-vault, Gemini big-vault,
ChatGPT-export big-vault) per the v2-lenient promotion proposal. Net effect
when paired with B1 entity-trapdoors: +3 pp accuracy overall, +50 pp on
assistant-sourced recall, no measured loss on any corpus.

Constraints:
- `user` = 1.0 (anchor — never penalize explicit user statements)
- Distances between tiers small enough that a high-BM25 assistant fact can still beat a low-BM25 user fact IF content match is overwhelming (e.g. specific Marriott number appears verbatim in query)
- Never drop to zero — all facts remain eligible for top-k

**Effort:** ~20 lines + 10 tests. 1 day including calibration on benchmark data.

### Tier 1 v1 (deprecated)

Shipped in `@totalreclaw/core@2.0.0` through `2.2.0`. Replaced by v2-lenient
in core 2.4.0. The legacy weight matrix is preserved as
`SOURCE_WEIGHTS_V1_LEGACY` in `rust/totalreclaw-core/src/reranker.rs` for
back-compat / benchmark spike work; production callers MUST consume the
default `SOURCE_WEIGHTS` (v2-lenient) above.

```rust
// v1 (pre-2.4.0). Retained as SOURCE_WEIGHTS_V1_LEGACY.
pub const SOURCE_WEIGHTS_V1_LEGACY: &[(MemorySource, f64)] = &[
    (MemorySource::User,          1.00),
    (MemorySource::UserInferred,  0.90),
    (MemorySource::Derived,       0.70),
    (MemorySource::External,      0.70),
    (MemorySource::Assistant,     0.55),  // heavy penalty (v1)
];
```

Why v1 was replaced: the heavy penalty on assistant-sourced facts (0.55)
disproportionately penalized recalled facts that were extracted from
assistant turns, even when the underlying content was user-confirmed. v2-lenient
compresses the gap, retaining provenance ordering but letting strong-signal
assistant-source facts surface when the user query targets them.

**Test vectors:**
- Query "Bangkok hotel" against Pipeline F vault. Expected top-1: `source:assistant` claim "Sheraton Grande Sukhumvit" beats user turn "I want to go to Bangkok" (because former has high cosine + specific entity).
- Query "Pedro's Marriott number". Expected top-1: assistant-extracted "Marriott Bonvoy #7758" beats other assistant claims (high entity specificity + match).
- Query "what do I prefer for coffee". Expected top-1: user-authored `preference:coffee=black` beats any inferred.

---

## Tier 2 — Scope pre-filter (OPTIONAL, post-v1)

**Rationale:** If user query carries a scope hint ("at work…", "for my health…"), pre-filtering candidates by scope improves precision. Otherwise, scope is ignored at retrieval time (as today).

**Change:**

Add a query-intent extractor that detects scope mentions:

```rust
pub struct QueryIntent {
    pub scope_hints: Vec<MemoryScope>,
    pub type_hints: Vec<MemoryType>,
    pub temporal_hint: Option<TemporalHint>, // "recently", "last year", etc.
}

fn extract_query_intent(query: &str) -> QueryIntent {
    // Keyword-based detection:
    //   "at work" / "job" / "office" / "colleague" → scope: work
    //   "doctor" / "meds" / "diet" / "allergy" → scope: health
    //   "mom" / "dad" / "kids" / "spouse" → scope: family
    //   "budget" / "salary" / "tax" / "invest" → scope: finance
    //   etc.
}
```

When retrieving with `scope_hints` present, filter candidates to matching scope OR `unspecified`. Keep `unspecified` as escape hatch so legacy-scope-missing data survives.

**Risk:** intent extraction is imperfect. False-positive scope detection excludes valid candidates. Mitigation: only pre-filter when confidence is high (multiple keyword matches).

**Effort:** ~1 day keyword extractor + tests. Could be upgraded to LLM intent extraction later (extra cost).

**Defer decision:** ship Tier 1 first. Only add Tier 2 if E13 or real-user signal shows scope-unaware ranking misses targets.

---

## Tier 3 — Volatility-aware decay (OPTIONAL, post-v1)

**Rationale:** v1 introduces `volatility ∈ {stable, updatable, ephemeral}`. Today's `decay_score` is uniform — applies the same time-based decay to all claims regardless of nature. User's name shouldn't decay; today's task list should decay fast.

**Change:**

At retrieval time, refine `decay_score` check:

```rust
fn effective_recency(claim: &Claim, now: u64) -> f64 {
    let age_days = (now - claim.created_at) / 86400;
    match claim.volatility {
        Volatility::Stable => 1.0, // no decay
        Volatility::Updatable => 0.95_f64.powf(age_days as f64 / 30.0), // ~5% per month
        Volatility::Ephemeral => {
            if let Some(expires) = claim.expires_at {
                if now > expires { 0.0 } else {
                    let time_to_expiry = expires - now;
                    (time_to_expiry as f64 / (expires - claim.created_at) as f64).max(0.0)
                }
            } else {
                0.9_f64.powf(age_days as f64 / 7.0) // fast decay if no explicit expiry
            }
        },
    }
}
```

Multiply `final_score` by `effective_recency`.

**Interaction with existing on-chain `decay_score`:** keep on-chain decay for storage-level eviction (relay-computed, respects free-tier quotas). This Tier 3 decay is layered at retrieval time for relevance only.

**Effort:** ~0.5 day + tests.

**Defer decision:** ship Tier 1. Add Tier 3 when user feedback indicates stale facts surfacing in recall ("agent keeps reminding me of the trip I had 2 years ago").

---

## Tier 4 — Type boost via query intent (OPTIONAL, post-v1)

**Rationale:** "What rule should I remember about Fly.io?" clearly wants `directive` facts. "What do I prefer for music?" wants `preference`. Current retrieval boosts via BM25 text match only; type-awareness is free gain.

**Change:**

Extend `QueryIntent` to include `type_hints`. When present, multiply the score of matching-type candidates by 1.2 (soft boost, not filter).

```rust
let type_boost = query.intent.type_hints.contains(&claim.type) { 1.2 } else { 1.0 };
final_score *= type_boost;
```

**Heuristic mapping:**
- "what rule" / "best practice" / "gotcha" → directive
- "what do I prefer" / "my taste" / "I like" → preference
- "what did I decide" / "why did I choose" → claim w/ reasoning
- "when did I" / "last time" → episode
- "what am I working on" → claim + volatility:ephemeral
- "what will I do" / "plan" → commitment

**Risk:** heuristics are brittle. Low-severity (boost, not filter).

**Effort:** ~0.5 day.

**Defer decision:** low priority. Add only if retrieval benchmark shows type-mismatch as significant error class.

---

## Implementation path

**If Pipeline F or G wins validation:**

1. Land Tier 1 (source-weighted) in `@totalreclaw/core@2.0.0` alongside taxonomy v1 schema.
2. Update 5 client packages (OpenClaw plugin, MCP, NanoClaw, Hermes, ZeroClaw) — all inherit reranker automatically via core bump.
3. Run cross-client parity test: same query on same vault returns same top-8 across clients.
4. Ship v1 + retrieval Tier 1 together as a single migration.
5. After 2-4 weeks of production, decide whether Tier 2-4 are worth building based on real user feedback + opt-in telemetry (e.g. "was the top-1 result useful" signal).

**If Pipeline C (strict provenance, drop assistant) wins instead:**

No retrieval change required. C already drops source:assistant facts at extraction, so ranker never sees them. Retrieval stays v1.5.0.

---

## Cross-client considerations

All 5 clients share `@totalreclaw/core` reranker. Change once, ships everywhere.

**Cross-vault consistency requirement (per taxonomy v1 spec §cross-client guarantees):** same claim retrieved from same vault via two different clients MUST produce identical top-k ordering. Retrieval v2 weights must be deterministic — no randomness, no per-client config that diverges.

**Risk:** clients sometimes call retrieval with different `top_k` or different embedding models (legacy e5-small vs current Harrier). Document invariants:
- Source weights: constant across all clients
- Scope filter (Tier 2): constant extraction rules
- Volatility decay: time-dependent but deterministic given same `now`
- Type boost (Tier 4): constant mapping

---

## Testing strategy

**Unit tests (Tier 1, MUST):**
- Weight application correctness (user score unchanged, assistant score ×0.85 v2-lenient / ×0.55 v1, etc.)
- Total ordering preservation (no equal-score instability)
- Edge case: missing source field defaults to moderate penalty
- Edge case: all candidates source=assistant → ordering still reflects base score

**Parity tests:**
- TS plugin + Python + Rust all produce identical top-8 given identical input (existing parity harness in `tests/parity/` extends here)

**Retrieval benchmark (E13, OPTIONAL):**
- Synthetic queries against extracted vaults per pipeline
- Measure precision@1, precision@5, recall@8 with and without Tier 1 weights
- Validates weight calibration; can tune weights before lock

---

## Open items

1. **Source weight calibration** — values above are starting guess. Tune during E13 or via A/B against small beta cohort.
2. **Tier boundary: where does `derived` sit?** Digests/summaries are authoritative reductions of user facts, not noise. v1 draft = 0.70 (below user-inferred); v2-lenient bumped this to 0.85 (equal to assistant + external) based on bench data — the heavy v1 penalty on derived content was empirically over-aggressive. Alternative still open: equal to user (1.0) since derived items are computed from user content. Decide with concrete bench data when next E13 calibration runs.
3. **Handling legacy vaults post-v1** — users who stored under v0 (no source field) get `source: user-inferred` by default at normalization? Or fail-safe to `assistant` (most penalized)? Need backward-compat decision at migration.
4. **Intent extractor LLM vs regex** — Tier 2-4 can use either. LLM = more accurate but +1 call per query. Regex = free but brittle. Start regex.
5. **Source weight user override** — should users be able to re-weight globally ("I want assistant-authored facts to rank as high as user")? Probably no in v1. Reopen if demand.

---

## References

- `docs/specs/totalreclaw/memory-taxonomy-v1.md` — taxonomy spec this ranker implements against
- `docs/plans/2026-04-17-extraction-efficiency-experiments.md` §E13 — retrieval benchmark proposal
- `docs/plans/2026-04-16-kg-roadmap-and-active-phases.md` §3.0 — MCP Memory Taxonomy v1 phase tracker
- `rust/totalreclaw-core/src/reranker.rs` — current reranker implementation
- [Mem0 junk audit (motivates Tier 1)](https://github.com/mem0ai/mem0/issues/4573)
