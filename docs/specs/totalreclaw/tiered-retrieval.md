# Tiered Retrieval — source-aware ranking

**Status:** Tier 1 shipped in `@totalreclaw/core@2.0.0` and every v1 client. Tiers 2-4 designed, not yet implemented.
**Related specs:** [memory-taxonomy-v1.md](./memory-taxonomy-v1.md), [retrieval-v2.md](./retrieval-v2.md).
**Audience:** implementers, performance engineers, and anyone tuning recall quality.

---

## Why tiered retrieval

v1 taxonomy introduces three new axes on every memory (`source`, `scope`, `volatility`) plus a closed 6-type enum. The retrieval pipeline in `@totalreclaw/core` has to respect these fields — otherwise the taxonomy work is cosmetic and recall quality does not move.

Four discrete tiers of retrieval improvement were designed. Tier 1 ships with v1 because it is load-bearing for the taxonomy (without it, assistant-regurgitated "facts" score equal to user-authored ones). Tiers 2-4 are optional post-v1 work gated on real-user recall quality data.

---

## Pipeline diagram (v1, Tier 1 active)

```
                                     CLIENT
  query text
      │
      ├── tokenize → blind trapdoors (HMAC-SHA256 word hashes)
      ├── compute query embedding (Harrier-OSS 640d, local)
      ├── compute LSH bucket hashes
      │
      ▼
     HTTP request (blind trapdoors + LSH buckets only — no plaintext)
      │
      ▼
                                   RELAY + SUBGRAPH
  GraphQL query over indexed blind_indices + bucket tables
  returns ~30-200 encrypted candidates (ciphertext + metadata)
      │
      ▼
                                     CLIENT
  decrypt candidates (XChaCha20-Poly1305)
  parse v1 claim payload (text, type, source, scope, volatility, reasoning)
      │
      ├── BM25 over decrypted text (intent-weighted)
      ├── Cosine similarity over decrypted embeddings
      ├── RRF fusion (k=60)
      │
      ▼
  ┌─────────────────────────────────┐
  │  TIER 1: source-weighted rerank │   ← NEW in v1
  │  final_score *= source_weight   │
  │   user        = 1.00            │
  │   user-inf.   = 0.90            │
  │   derived     = 0.70            │
  │   external    = 0.70            │
  │   assistant   = 0.55            │
  │   legacy/null = 0.85            │
  └─────────────────────────────────┘
      │
      ▼
  top-k (default 8) returned to host agent
```

Every step that touches plaintext runs on the client. The relay and subgraph only ever see blind trapdoors, ciphertext, and LSH bucket hashes.

---

## Tier 1 — source-weighted final score (SHIPPED)

**Required for v1 launch.** Without it, the `source` field on every claim is metadata the ranker ignores, and assistant-tagged memories score equal to user-authored ones.

### Scoring formula

After BM25 + cosine + RRF fusion produces a base score per candidate, multiply by source weight:

```
final_score = rrf_score * source_weight(claim.source)
```

Source weights (locked in core 2.0.0):

| Source | Weight | Rationale |
|---|:-:|---|
| `user` | 1.00 | Anchor — user explicitly said it |
| `user-inferred` | 0.90 | Extracted from user signals (high confidence) |
| `derived` | 0.70 | Digests, summaries, consolidation output |
| `external` | 0.70 | Imported from another system |
| `assistant` | 0.55 | Assistant-authored, heavy penalty |
| `legacy` / null | 0.85 | Pre-v1 vault entries (back-compat default) |

### Why these numbers

Calibrated during the v1 development run:

- `user = 1.0` anchors everything else. You never penalize direct user statements.
- Distances between tiers are small enough that a high-BM25 assistant claim can still beat a low-BM25 user claim if the content match is overwhelming (e.g. a specific Marriott number appearing verbatim in the query). This preserves the "Bangkok test": shared content (receipts, screenshots) extracted by the assistant remains recoverable.
- `assistant = 0.55` is aggressive enough to break the Mem0 97.8%-junk failure mode documented in [mem0 Issue #4573](https://github.com/mem0ai/mem0/issues/4573), where assistant-regurgitated inferences polluted recall.
- Never drop to zero — all candidates remain eligible for top-k.
- `0.85 legacy fallback` lets mixed v0/v1 vaults rerank cleanly during migration without silently biasing against pre-v1 data.

### Source code

- Rust core: `rust/totalreclaw-core/src/reranker.rs` — `rerank_with_config`, `source_weight`.
- WASM binding: `@totalreclaw/core` exports `rerankWithConfig`, `sourceWeight`, `legacyClaimFallbackWeight`.
- PyO3 binding: `totalreclaw-core` Python package exposes identical functions.

All 5 clients (OpenClaw plugin, MCP, NanoClaw, Hermes, ZeroClaw) reach Tier 1 via the shared core. No client implements reranking locally.

### Test coverage

- 42 tests on the Tier 1 path in core.
- Cross-language parity: TS plugin + Python + Rust all produce identical top-8 given identical input.
- Scenario tests include the "Bangkok test" (assistant-extracted specific-entity claim wins on high content match) and the "tea vs coffee" test (user-authored claim beats equal-base-score assistant claim).

---

## Tier 2 — scope pre-filter (DESIGNED, NOT SHIPPED)

**Post-v1.** Ship only if recall data shows scope-unaware ranking missing obvious targets.

When the query carries a scope hint ("at work...", "for my health..."), pre-filter candidates to matching scope OR `unspecified`. Keep `unspecified` as escape hatch so legacy data without a scope does not disappear.

```rust
pub struct QueryIntent {
    pub scope_hints: Vec<MemoryScope>,
    pub type_hints: Vec<MemoryType>,
    pub temporal_hint: Option<TemporalHint>,
}
```

Intent extraction is keyword-based at first. Can be upgraded to an LLM call later (at ~1 extra API call per query).

**Risk:** imperfect intent extraction could exclude valid candidates. Mitigation: only pre-filter when confidence is high (multiple keyword matches).

**Effort:** ~1 day.

---

## Tier 3 — volatility-aware decay (DESIGNED, NOT SHIPPED)

**Post-v1.** Ship when user feedback indicates stale facts surfacing in recall ("agent keeps reminding me of the trip I took 2 years ago").

Today's `decay_score` on-chain applies uniformly to every claim. v1 adds `volatility` — some memories should never decay (your name, allergies), others should decay fast (today's task list).

Proposed:

```rust
fn effective_recency(claim: &Claim, now: u64) -> f64 {
    let age_days = (now - claim.created_at) / 86400;
    match claim.volatility {
        Volatility::Stable => 1.0,                                // no decay
        Volatility::Updatable => 0.95_f64.powf(age_days as f64 / 30.0),  // ~5% per month
        Volatility::Ephemeral => {
            if let Some(expires) = claim.expires_at {
                if now > expires { 0.0 }
                else { (expires - now) as f64 / (expires - claim.created_at) as f64 }
            } else {
                0.9_f64.powf(age_days as f64 / 7.0)               // fast decay
            }
        },
    }
}
```

Multiply `final_score` by `effective_recency`.

Interaction with on-chain decay: the on-chain `decay_score` stays (used for storage-level eviction on free tier). Tier 3 is a retrieval-time recency multiplier for ranking only.

**Effort:** ~0.5 day.

---

## Tier 4 — type boost via query intent (DESIGNED, NOT SHIPPED)

**Post-v1.** Lowest priority — add only if retrieval benchmark shows type-mismatch as a significant error class.

When the query signals a desired type ("what rule...", "what do I prefer..."), multiply score by 1.2 for matching-type candidates. Soft boost, not a filter.

Heuristic mapping:

- "what rule" / "best practice" / "gotcha" → `directive`
- "what do I prefer" / "my taste" → `preference`
- "what did I decide" / "why did I choose" → `claim` with `reasoning` present
- "when did I" / "last time" → `episode`
- "what am I working on" → `claim` with `volatility: ephemeral`
- "what will I do" / "plan" → `commitment`

**Effort:** ~0.5 day.

---

## Why tiered (not "ship everything")

Each tier adds complexity; each tier has a calibration cost (wrong weights make recall worse, not better). Shipping in tiers lets us:

1. Lock in the biggest win (Tier 1, source-weighted) with v1.
2. Measure real recall quality for a period of weeks.
3. Add Tier 2-4 only when data shows they would help.

Per the internal roadmap: after 2-4 weeks of production use, revisit Tier 2-4 based on (a) opt-in telemetry of recall quality and (b) specific user feedback. Until then, Tier 1 is the full retrieval change for v1.

---

## Cross-client consistency requirement

All 5 clients share `@totalreclaw/core` — change once, ships everywhere. The taxonomy v1 spec §cross-client-guarantees requires that identical queries against identical vaults return identical top-k ordering regardless of which client is doing the reranking.

Invariants that must hold:

- Source weights: constant across all clients (locked in core).
- Scope filter (Tier 2, future): deterministic extraction rules.
- Volatility decay (Tier 3, future): time-dependent but deterministic given same `now`.
- Type boost (Tier 4, future): constant keyword → type mapping.

No randomness. No per-client config that diverges.

---

## Observable impact

Phase 2 benchmark data (placeholder — will fill in once 500-conv benchmark run completes on 2026-04-18):

- **Baseline (pre-v1):** TBD — clustering ratio, recall@8, precision@1 on Gemini + WildChat corpora.
- **v1 + Tier 1:** TBD — same metrics with source-weighted reranker.
- **v1 Bangkok test:** TBD — user-authored vs assistant-extracted specific-entity claims.

Once the number lands, this section will include the concrete delta. Until then, the qualitative claim is: Tier 1 structurally prevents the Mem0 97.8%-junk failure mode without dropping useful assistant-extracted content (like shared receipts or screenshots).

---

## References

- Full retrieval design: [retrieval-v2.md](./retrieval-v2.md)
- Taxonomy depending on this: [memory-taxonomy-v1.md](./memory-taxonomy-v1.md)
- Reranker implementation: `rust/totalreclaw-core/src/reranker.rs`
- Mem0 audit motivating Tier 1: <https://github.com/mem0ai/mem0/issues/4573>
- Core 2.0.0 CHANGELOG: `rust/totalreclaw-core/CHANGELOG.md`
