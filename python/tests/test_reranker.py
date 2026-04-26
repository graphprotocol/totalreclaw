"""Tests for the Hermes reranker public surface.

As of rc.22 the Python client's reranker is a thin wrapper around
``totalreclaw_core.rerank_with_config``. Internal helpers (BM25, RRF, MMR,
recency, importance) are no longer in the client and are tested at the core
level (``rust/totalreclaw-core/src/reranker.rs`` tests + benches). This file
covers the wrapper's public surface only:

  - ``cosine_similarity`` -- delegates to core
  - ``source_weight`` / ``LEGACY_CLAIM_FALLBACK_WEIGHT``
  - ``detect_query_intent`` -- TS-parity heuristic
  - ``rerank`` -- routes candidates through ``rerank_with_config``

Cross-runtime parity assertions live in ``test_v1_taxonomy.py`` (mirror of
the plugin's ``v1-taxonomy.test.ts``) and ``test_v1_parity.py``.
"""
from __future__ import annotations

import pytest

from totalreclaw.reranker import (
    INTENT_WEIGHTS,
    LEGACY_CLAIM_FALLBACK_WEIGHT,
    DEFAULT_WEIGHTS,
    RankingWeights,
    RerankerCandidate,
    RerankerResult,
    cosine_similarity,
    detect_query_intent,
    rerank,
    source_weight,
)


# ---------------------------------------------------------------------------
# cosine_similarity (delegated to core)
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0, abs=1e-6)

    def test_opposite_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]) == pytest.approx(-1.0, abs=1e-6)

    def test_empty_returns_zero(self) -> None:
        assert cosine_similarity([], [1.0, 2.0]) == 0.0
        assert cosine_similarity([1.0, 2.0], []) == 0.0

    def test_mismatched_length_truncates(self) -> None:
        # Truncate to min length -- matches historic behaviour.
        result = cosine_similarity([1.0, 0.0, 99.0], [1.0, 0.0])
        assert result == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# source_weight (delegated to core)
# ---------------------------------------------------------------------------


class TestSourceWeight:
    def test_user_is_one(self) -> None:
        assert source_weight("user") == 1.0

    def test_user_inferred(self) -> None:
        assert source_weight("user-inferred") == pytest.approx(0.9, abs=1e-3)

    def test_derived_and_external(self) -> None:
        assert source_weight("derived") == pytest.approx(0.7, abs=1e-3)
        assert source_weight("external") == pytest.approx(0.7, abs=1e-3)

    def test_assistant(self) -> None:
        assert source_weight("assistant") == pytest.approx(0.55, abs=1e-3)

    def test_none_falls_back_to_legacy(self) -> None:
        assert source_weight(None) == LEGACY_CLAIM_FALLBACK_WEIGHT

    def test_legacy_fallback_value(self) -> None:
        assert LEGACY_CLAIM_FALLBACK_WEIGHT == pytest.approx(0.85, abs=1e-3)


# ---------------------------------------------------------------------------
# detect_query_intent (TS heuristic, kept for callers)
# ---------------------------------------------------------------------------


class TestDetectQueryIntent:
    @pytest.mark.parametrize(
        "query",
        [
            "What did we discuss yesterday?",
            "What happened last week?",
            "Any recent updates?",
            "What was mentioned earlier today?",
        ],
    )
    def test_temporal_queries(self, query: str) -> None:
        assert detect_query_intent(query) == "temporal"

    @pytest.mark.parametrize(
        "query",
        [
            "What's Alex's email?",
            "Who is the project lead?",
            "Where does Sarah live?",
        ],
    )
    def test_factual_queries(self, query: str) -> None:
        assert detect_query_intent(query) == "factual"

    def test_long_factual_pattern_falls_to_semantic(self) -> None:
        long_q = (
            "What are all the different design patterns and architectural decisions "
            "that were discussed in the project?"
        )
        assert detect_query_intent(long_q) == "semantic"

    def test_temporal_beats_factual(self) -> None:
        assert detect_query_intent("What did we discuss yesterday?") == "temporal"


# ---------------------------------------------------------------------------
# rerank (routes through core)
# ---------------------------------------------------------------------------


class TestRerank:
    def test_empty_candidates_returns_empty(self) -> None:
        assert rerank("query", [1.0, 0.0], [], top_k=5) == []

    def test_top_k_greater_than_count_returns_all(self) -> None:
        cands = [RerankerCandidate(id="1", text="only candidate")]
        results = rerank("only", [], cands, top_k=10)
        assert len(results) == 1

    def test_bm25_only_path_ranks_matching_doc_first(self) -> None:
        # No embeddings -> core falls back to BM25-only signal.
        cands = [
            RerankerCandidate(id="1", text="Alex works at Nexus Labs as a senior engineer"),
            RerankerCandidate(id="2", text="The weather today is sunny and warm"),
            RerankerCandidate(id="3", text="Bob enjoys hiking in the mountains on weekends"),
        ]
        results = rerank("Alex Nexus Labs", [], cands, top_k=2)
        assert len(results) == 2
        assert results[0].id == "1"

    def test_irrelevant_doc_ranked_last(self) -> None:
        query_emb = [1.0, 0.0, 0.0, 0.0]
        cands = [
            RerankerCandidate(id="1", text="Alex works at Nexus Labs", embedding=[0.0, 1.0, 0.0, 0.0]),
            RerankerCandidate(id="2", text="career position company staff",
                              embedding=[0.99, 0.1, 0.0, 0.0]),
            RerankerCandidate(id="3", text="sunny weather forecast today",
                              embedding=[0.0, 0.0, 0.0, 1.0]),
        ]
        results = rerank("Alex Nexus Labs", query_emb, cands, top_k=3)
        assert len(results) == 3
        assert results[2].id == "3"

    def test_apply_source_weights_promotes_user(self) -> None:
        emb = [0.1, 0.2, 0.3, 0.4]
        cands = [
            RerankerCandidate(id="asst", text="prefers PostgreSQL for analytics",
                              embedding=emb, source="assistant"),
            RerankerCandidate(id="user", text="prefers PostgreSQL for analytics",
                              embedding=emb, source="user"),
        ]
        results = rerank("PostgreSQL analytics", emb, cands, top_k=2,
                         apply_source_weights=True)
        assert results[0].id == "user"
        assert results[0].source_weight == pytest.approx(1.0, abs=1e-3)
        assert results[1].source_weight == pytest.approx(0.55, abs=1e-3)

    def test_apply_source_weights_false_leaves_weight_none(self) -> None:
        emb = [1.0, 0.0]
        cands = [
            RerankerCandidate(id="a", text="fact", embedding=emb, source="user"),
            RerankerCandidate(id="b", text="fact", embedding=emb, source="assistant"),
        ]
        results = rerank("fact", emb, cands, top_k=2, apply_source_weights=False)
        assert all(r.source_weight is None for r in results)

    def test_legacy_candidate_gets_fallback_weight(self) -> None:
        cands = [
            RerankerCandidate(id="legacy", text="fact one", embedding=[1.0, 0.0]),
            RerankerCandidate(id="user", text="fact two", embedding=[1.0, 0.0],
                              source="user"),
        ]
        results = rerank("fact", [1.0, 0.0], cands, top_k=2,
                         apply_source_weights=True)
        legacy_result = next(r for r in results if r.id == "legacy")
        assert legacy_result.source_weight == LEGACY_CLAIM_FALLBACK_WEIGHT

    def test_metadata_round_trips(self) -> None:
        # importance / created_at / category are no longer used by the
        # ranker but consumers (agent/contradiction.py) still read them off
        # the result. Verify the wrapper round-trips them.
        cands = [
            RerankerCandidate(
                id="fact-1",
                text="user prefers tea over coffee",
                embedding=[1.0, 0.5],
                importance=0.7,
                created_at=1700000000.0,
                category="preference",
                source="user",
            ),
        ]
        results = rerank("tea coffee", [1.0, 0.5], cands, top_k=1)
        assert results[0].importance == 0.7
        assert results[0].created_at == 1700000000.0
        assert results[0].category == "preference"
        assert results[0].source == "user"


# ---------------------------------------------------------------------------
# Compat shim (legacy ranking-weights argument is accepted but ignored)
# ---------------------------------------------------------------------------


class TestLegacyWeightsCompat:
    def test_intent_weights_dict_present(self) -> None:
        assert "factual" in INTENT_WEIGHTS
        assert "temporal" in INTENT_WEIGHTS
        assert "semantic" in INTENT_WEIGHTS

    def test_default_weights_is_RankingWeights_instance(self) -> None:
        assert isinstance(DEFAULT_WEIGHTS, RankingWeights)

    def test_passing_weights_does_not_break_rerank(self) -> None:
        cands = [RerankerCandidate(id="1", text="fact text here")]
        # Pass a non-default weights object -- core ignores it but the
        # wrapper must accept it for backwards compat.
        weights = RankingWeights(bm25=0.4, cosine=0.2, importance=0.25, recency=0.15)
        results = rerank("fact", [], cands, top_k=1, weights=weights)
        assert len(results) == 1
