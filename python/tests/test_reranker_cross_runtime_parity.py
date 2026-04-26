"""Cross-runtime reranker parity check (Hermes side).

Locks in the rc.22 hoist: plugin / Hermes / MCP all delegate ranking to
core::reranker, so the same fixture MUST produce identical top-K orderings
across runtimes.

This file mirrors `skill/plugin/reranker-cross-runtime-parity.test.ts` --
identical fixture, identical query, identical query embedding, identical
expected top-8 ordering. If you change one, change both.

Run with: pytest python/tests/test_reranker_cross_runtime_parity.py
"""
from __future__ import annotations

import pytest

from totalreclaw.reranker import RerankerCandidate, rerank


# Mirror of the plugin fixture (same ids, same texts, same embeddings,
# same sources). Keep these in sync with
# `skill/plugin/reranker-cross-runtime-parity.test.ts`.
FIXTURE: list[RerankerCandidate] = [
    RerankerCandidate(id="g00", text="User set personal best 25:50 in charity 5K run", embedding=[0.9, 0.1, 0.05], source="user"),
    RerankerCandidate(id="g01", text="User completed half marathon in 1:55", embedding=[0.85, 0.15, 0.02], source="user"),
    RerankerCandidate(id="g02", text="Assistant suggested running shoes", embedding=[0.7, 0.3, 0.1], source="assistant"),
    RerankerCandidate(id="g03", text="User trains five days per week", embedding=[0.6, 0.2, 0.2], source="user"),
    RerankerCandidate(id="g04", text="Weather forecast says sunny tomorrow", embedding=[0.0, 0.1, 0.9], source="user"),
    RerankerCandidate(id="g05", text="User prefers PostgreSQL for analytics", embedding=[0.1, 0.9, 0.0], source="user"),
    RerankerCandidate(id="g06", text="Bob enjoys hiking on weekends", embedding=[0.3, 0.0, 0.6], source="user-inferred"),
    RerankerCandidate(id="g07", text="User had pizza for dinner", embedding=[0.0, 0.0, 1.0], source="user"),
    RerankerCandidate(id="g08", text="Marathon training tips and strategy", embedding=[0.5, 0.4, 0.1], source="external"),
    RerankerCandidate(id="g09", text="User runs in Central Park weekly", embedding=[0.7, 0.2, 0.1], source="user"),
    RerankerCandidate(id="g10", text="Charity 5K event raised funds for shelter", embedding=[0.5, 0.3, 0.2], source="derived"),
    RerankerCandidate(id="g11", text="User logged 25 minutes 50 seconds time", embedding=[0.95, 0.05, 0.0], source="user"),
    RerankerCandidate(id="g12", text="Project deadline next Friday", embedding=[0.0, 0.5, 0.5], source="user"),
    RerankerCandidate(id="g13", text="Coffee preference is dark roast", embedding=[0.0, 0.7, 0.3], source="user"),
    RerankerCandidate(id="g14", text="User won 5K race in 25 minutes 50", embedding=[0.92, 0.08, 0.0], source="user"),
    RerankerCandidate(id="g15", text="Assistant noted user enjoys running", embedding=[0.55, 0.4, 0.05], source="assistant"),
    RerankerCandidate(id="g16", text="Total kilometers run last month: 120", embedding=[0.6, 0.3, 0.1], source="derived"),
    RerankerCandidate(id="g17", text="Pace target is 5 minutes per kilometer", embedding=[0.65, 0.25, 0.1], source="user"),
    RerankerCandidate(id="g18", text="User dislikes interval training", embedding=[0.5, 0.4, 0.1], source="user"),
    RerankerCandidate(id="g19", text="Running playlist includes electronic music", embedding=[0.4, 0.3, 0.3], source="user"),
    RerankerCandidate(id="g20", text="Charity event was held on Saturday", embedding=[0.3, 0.5, 0.2], source="external"),
    RerankerCandidate(id="g21", text="User prefers morning runs over evening", embedding=[0.7, 0.2, 0.1], source="user-inferred"),
    RerankerCandidate(id="g22", text="Personal record was set in May", embedding=[0.85, 0.1, 0.05], source="user"),
    RerankerCandidate(id="g23", text="User uses Garmin watch for tracking", embedding=[0.5, 0.4, 0.1], source="user"),
    RerankerCandidate(id="g24", text="Distance was 5 kilometers exact", embedding=[0.7, 0.2, 0.1], source="user-inferred"),
    RerankerCandidate(id="g25", text="Random unrelated note about the weather", embedding=[0.05, 0.05, 0.9], source="user"),
    RerankerCandidate(id="g26", text="User trained six weeks for the race", embedding=[0.65, 0.25, 0.1], source="user"),
    RerankerCandidate(id="g27", text="Recovery routine includes stretching", embedding=[0.3, 0.4, 0.3], source="user"),
    RerankerCandidate(id="g28", text="Goal is sub-25-minute 5K next year", embedding=[0.8, 0.15, 0.05], source="user"),
    RerankerCandidate(id="g29", text="Assistant recommends hydration tips", embedding=[0.4, 0.3, 0.3], source="assistant"),
]

QUERY = "What was my personal best time in the charity 5K run?"
QUERY_EMBEDDING = [0.85, 0.1, 0.05]


# Expected top-8 ordering -- captured from the plugin run on rc.22 hoist.
# If this drifts, EITHER the plugin OR Hermes is no longer routing through
# core::reranker. Investigate, don't blindly update.
EXPECTED_TOP8 = ["g22", "g00", "g14", "g11", "g01", "g28", "g09", "g26"]


def test_top_k_matches_plugin_fixture() -> None:
    """The same fixture + query MUST produce the identical top-8 across runtimes."""
    results = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=8, apply_source_weights=True)
    actual = [r.id for r in results]
    assert actual == EXPECTED_TOP8, (
        f"Cross-runtime drift: plugin top-8 was {EXPECTED_TOP8}, "
        f"Hermes got {actual}. Both runtimes route through "
        f"totalreclaw_core.rerank_with_config -- if these diverge, "
        f"check that the wrapper isn't applying any client-side post-processing."
    )


def test_determinism_two_invocations() -> None:
    a = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=16, apply_source_weights=True)
    b = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=16, apply_source_weights=True)
    assert [r.id for r in a] == [r.id for r in b]
    for ra, rb in zip(a, b):
        assert ra.rrf_score == pytest.approx(rb.rrf_score, abs=1e-12)


def test_top_k_stability_8_is_prefix_of_16() -> None:
    k8 = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=8, apply_source_weights=True)
    k16 = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=16, apply_source_weights=True)
    assert len(k8) == 8
    assert len(k16) == 16
    assert [r.id for r in k8] == [r.id for r in k16[:8]]


def test_source_weighting_keeps_assistant_off_top1() -> None:
    """With source weighting ON, top-1 should not be an assistant-authored claim
    when user-authored claims have comparable BM25 / cosine scores."""
    results = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=8, apply_source_weights=True)
    assert results[0].source != "assistant"


def test_score_changes_when_source_weights_toggle() -> None:
    """SW on vs off MUST produce different score distributions (otherwise the
    flag is a no-op, indicating routing failure)."""
    sw_on = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=16, apply_source_weights=True)
    sw_off = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, top_k=16, apply_source_weights=False)
    sum_on = sum(r.rrf_score for r in sw_on)
    sum_off = sum(r.rrf_score for r in sw_off)
    assert sum_on != sum_off, "Source weighting toggle had no effect"
