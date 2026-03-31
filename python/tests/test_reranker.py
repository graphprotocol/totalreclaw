"""Tests for TotalReclaw reranker module."""
import math
import time
from unittest.mock import patch

import pytest

from totalreclaw.reranker import (
    INTENT_WEIGHTS,
    STOP_WORDS,
    RankedItem,
    RankingWeights,
    RerankerCandidate,
    RerankerResult,
    _recency_score,
    apply_mmr,
    bm25_score,
    cosine_similarity,
    detect_query_intent,
    rerank,
    rrf_fuse,
    tokenize,
    weighted_rrf_fuse,
)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_basic_tokenization(self):
        tokens = tokenize("Hello World", remove_stop_words=False)
        assert len(tokens) == 2
        # Tokens are lowercased and stemmed
        assert all(isinstance(t, str) for t in tokens)

    def test_removes_stop_words(self):
        tokens = tokenize("the cat is on the mat")
        # "the", "is", "on" are stop words; "cat" and "mat" remain (stemmed)
        assert len(tokens) == 2

    def test_keeps_stop_words_when_disabled(self):
        tokens = tokenize("the cat is on the mat", remove_stop_words=False)
        # "the" (x2), "cat", "is", "on", "mat" — all >= 2 chars
        assert len(tokens) == 6

    def test_removes_punctuation(self):
        tokens = tokenize("hello, world! foo-bar", remove_stop_words=False)
        # "hello", "world", "foo", "bar" (punctuation replaced with space)
        assert len(tokens) == 4

    def test_removes_underscores(self):
        tokens = tokenize("foo_bar baz_qux", remove_stop_words=False)
        # "foo", "bar", "baz", "qux"
        assert len(tokens) == 4

    def test_filters_short_tokens(self):
        tokens = tokenize("I a am ok go", remove_stop_words=False)
        # "I" -> "i" (1 char, filtered), "a" (1 char), "am" (2 chars), "ok" (2 chars), "go" (2 chars)
        assert all(len(t) >= 2 for t in tokens)

    def test_unicode_preserved(self):
        tokens = tokenize("caf\u00e9 na\u00efve r\u00e9sum\u00e9", remove_stop_words=False)
        assert len(tokens) == 3

    def test_empty_string(self):
        assert tokenize("") == []

    def test_only_stop_words(self):
        tokens = tokenize("the and but or")
        assert tokens == []

    def test_stemming(self):
        tokens = tokenize("running games played", remove_stop_words=False)
        # Porter stemmer: "running" -> "run", "games" -> "game", "played" -> "play"
        assert "run" in tokens
        assert "game" in tokens
        assert "play" in tokens

    def test_numbers_preserved(self):
        tokens = tokenize("version 42 released", remove_stop_words=False)
        assert "42" in tokens


class TestStopWords:
    def test_stop_words_match_typescript(self):
        """Stop words set must match the canonical TypeScript list."""
        expected = {
            "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for",
            "from", "had", "has", "have", "he", "her", "him", "his", "how", "if",
            "in", "into", "is", "it", "its", "me", "my", "no", "not", "of", "on",
            "or", "our", "out", "she", "so", "than", "that", "the", "their", "them",
            "then", "there", "these", "they", "this", "to", "up", "us", "was", "we",
            "were", "what", "when", "where", "which", "who", "whom", "why", "will",
            "with", "you", "your",
        }
        assert STOP_WORDS == expected


# ---------------------------------------------------------------------------
# BM25 Scoring
# ---------------------------------------------------------------------------


class TestBM25:
    def test_standard_case(self):
        query = ["cat", "dog"]
        doc = ["cat", "cat", "fish", "dog"]
        score = bm25_score(query, doc, avg_doc_len=4.0, doc_count=10,
                           term_doc_freqs={"cat": 3, "dog": 2, "fish": 5})
        assert score > 0

    def test_no_overlap(self):
        query = ["cat"]
        doc = ["dog", "fish"]
        score = bm25_score(query, doc, avg_doc_len=3.0, doc_count=10,
                           term_doc_freqs={"dog": 5, "fish": 3})
        assert score == 0.0

    def test_empty_doc(self):
        score = bm25_score(["cat"], [], avg_doc_len=3.0, doc_count=10,
                           term_doc_freqs={"cat": 3})
        assert score == 0.0

    def test_empty_query(self):
        score = bm25_score([], ["cat", "dog"], avg_doc_len=3.0, doc_count=10,
                           term_doc_freqs={"cat": 3})
        assert score == 0.0

    def test_zero_doc_count(self):
        score = bm25_score(["cat"], ["cat"], avg_doc_len=3.0, doc_count=0,
                           term_doc_freqs={"cat": 1})
        assert score == 0.0

    def test_zero_avg_doc_len(self):
        score = bm25_score(["cat"], ["cat"], avg_doc_len=0.0, doc_count=10,
                           term_doc_freqs={"cat": 1})
        assert score == 0.0

    def test_multi_term_query(self):
        query = ["cat", "dog", "bird"]
        doc = ["cat", "dog", "cat"]
        freq = {"cat": 2, "dog": 3, "bird": 1}
        score = bm25_score(query, doc, avg_doc_len=4.0, doc_count=10,
                           term_doc_freqs=freq)
        # "bird" not in doc, should not contribute
        score_no_bird = bm25_score(["cat", "dog"], doc, avg_doc_len=4.0,
                                   doc_count=10, term_doc_freqs=freq)
        assert score == score_no_bird

    def test_higher_idf_for_rarer_term(self):
        """A rarer term should produce higher IDF and therefore higher score."""
        doc = ["rare", "common"]
        # "rare" appears in 1 doc, "common" in 9 out of 10
        score_rare = bm25_score(["rare"], doc, avg_doc_len=2.0, doc_count=10,
                                term_doc_freqs={"rare": 1, "common": 9})
        score_common = bm25_score(["common"], doc, avg_doc_len=2.0, doc_count=10,
                                  term_doc_freqs={"rare": 1, "common": 9})
        assert score_rare > score_common


# ---------------------------------------------------------------------------
# Cosine Similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_parallel_vectors(self):
        assert cosine_similarity([1, 0], [2, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_identical_vectors(self):
        v = [0.3, 0.4, 0.5]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_both_zero_vectors(self):
        assert cosine_similarity([0, 0], [0, 0]) == 0.0

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_one_empty(self):
        assert cosine_similarity([1, 2], []) == 0.0

    def test_different_lengths(self):
        # Uses min(len(a), len(b)) — matches TS behavior
        result = cosine_similarity([1, 0, 0], [1, 0])
        assert result == pytest.approx(1.0)

    def test_known_value(self):
        a = [1, 2, 3]
        b = [4, 5, 6]
        expected = (4 + 10 + 18) / (math.sqrt(14) * math.sqrt(77))
        assert cosine_similarity(a, b) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------


class TestRRFFuse:
    def test_basic_fusion(self):
        r1 = [RankedItem("a", 10), RankedItem("b", 5)]
        r2 = [RankedItem("b", 10), RankedItem("a", 5)]
        fused = rrf_fuse([r1, r2])
        # Both a and b appear in both lists — scores should be equal
        assert len(fused) == 2
        assert fused[0].score == pytest.approx(fused[1].score)

    def test_single_ranking(self):
        r = [RankedItem("x", 10), RankedItem("y", 5)]
        fused = rrf_fuse([r])
        assert fused[0].id == "x"
        assert fused[1].id == "y"
        assert fused[0].score > fused[1].score

    def test_partial_overlap(self):
        r1 = [RankedItem("a", 10), RankedItem("b", 5)]
        r2 = [RankedItem("c", 10), RankedItem("a", 5)]
        fused = rrf_fuse([r1, r2])
        ids = {item.id for item in fused}
        assert ids == {"a", "b", "c"}
        # "a" appears in both lists, should have highest score
        assert fused[0].id == "a"

    def test_empty_rankings(self):
        assert rrf_fuse([]) == []
        assert rrf_fuse([[]]) == []

    def test_k_parameter(self):
        r = [RankedItem("a", 10)]
        fused_k1 = rrf_fuse([r], k=1)
        fused_k100 = rrf_fuse([r], k=100)
        # Smaller k gives higher scores
        assert fused_k1[0].score > fused_k100[0].score

    def test_rrf_score_formula(self):
        """Verify the exact RRF score calculation."""
        r1 = [RankedItem("a", 10)]  # rank 0 -> 1-based rank 1
        fused = rrf_fuse([r1], k=60)
        assert fused[0].score == pytest.approx(1.0 / (60 + 1))


# ---------------------------------------------------------------------------
# Weighted RRF
# ---------------------------------------------------------------------------


class TestWeightedRRF:
    def test_equal_weights(self):
        """Equal weights should produce same ordering as unweighted RRF."""
        r1 = [RankedItem("a", 10), RankedItem("b", 5)]
        r2 = [RankedItem("b", 10), RankedItem("a", 5)]
        fused_unweighted = rrf_fuse([r1, r2])
        fused_weighted = weighted_rrf_fuse([r1, r2], [1.0, 1.0])
        assert [f.id for f in fused_unweighted] == [f.id for f in fused_weighted]

    def test_high_weight_dominates(self):
        r1 = [RankedItem("a", 10), RankedItem("b", 5)]
        r2 = [RankedItem("b", 10), RankedItem("a", 5)]
        # Weight r1 heavily — "a" should win (it's #1 in r1)
        fused = weighted_rrf_fuse([r1, r2], [100.0, 0.01])
        assert fused[0].id == "a"

    def test_zero_weight(self):
        r1 = [RankedItem("a", 10)]
        r2 = [RankedItem("b", 10)]
        fused = weighted_rrf_fuse([r1, r2], [1.0, 0.0])
        # b has 0 weight contribution — a should rank higher
        assert fused[0].id == "a"
        assert fused[1].score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Query Intent Detection
# ---------------------------------------------------------------------------


class TestDetectQueryIntent:
    def test_temporal_yesterday(self):
        assert detect_query_intent("What happened yesterday?") == "temporal"

    def test_temporal_recently(self):
        assert detect_query_intent("What did we discuss recently?") == "temporal"

    def test_temporal_last_week(self):
        assert detect_query_intent("Show me last week's decisions") == "temporal"

    def test_temporal_since(self):
        assert detect_query_intent("Changes since Monday") == "temporal"

    def test_factual_what(self):
        assert detect_query_intent("What is the server URL?") == "factual"

    def test_factual_who(self):
        assert detect_query_intent("Who manages the database?") == "factual"

    def test_factual_is(self):
        assert detect_query_intent("Is the feature enabled?") == "factual"

    def test_factual_too_long(self):
        """Factual pattern with >80 chars should fall through to semantic."""
        long_query = "What is the full configuration of the server including all environment variables and settings?"
        assert len(long_query) >= 80
        assert detect_query_intent(long_query) == "semantic"

    def test_semantic_default(self):
        assert detect_query_intent("Tell me about memory encryption") == "semantic"

    def test_semantic_statement(self):
        assert detect_query_intent("Memory encryption architecture") == "semantic"

    def test_temporal_beats_factual(self):
        """Temporal check is first — "What did we discuss yesterday?" is temporal."""
        assert detect_query_intent("What did we discuss yesterday?") == "temporal"

    def test_intent_weights_exist(self):
        for intent in ("factual", "temporal", "semantic"):
            assert intent in INTENT_WEIGHTS
            w = INTENT_WEIGHTS[intent]
            total = w.bm25 + w.cosine + w.importance + w.recency
            assert total == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Recency Score
# ---------------------------------------------------------------------------


class TestRecencyScore:
    def test_just_created(self):
        """A fact created right now should score ~1.0."""
        score = _recency_score(time.time())
        assert score == pytest.approx(1.0, abs=0.01)

    def test_one_week_ago(self):
        """A fact created one week ago should score ~0.5."""
        one_week = 7 * 24 * 3600
        score = _recency_score(time.time() - one_week)
        assert score == pytest.approx(0.5, abs=0.01)

    def test_two_weeks_ago(self):
        """A fact created two weeks ago should score ~0.33."""
        two_weeks = 14 * 24 * 3600
        score = _recency_score(time.time() - two_weeks)
        assert score == pytest.approx(1.0 / 3.0, abs=0.01)

    def test_monotonic_decay(self):
        """More recent facts should have higher scores."""
        now = time.time()
        scores = [_recency_score(now - i * 3600) for i in range(10)]
        for i in range(len(scores) - 1):
            assert scores[i] > scores[i + 1]


# ---------------------------------------------------------------------------
# MMR Diversity
# ---------------------------------------------------------------------------


class TestApplyMMR:
    def test_empty(self):
        assert apply_mmr([]) == []

    def test_single_candidate(self):
        c = RerankerResult(id="a", text="hello", rrf_score=1.0)
        result = apply_mmr([c])
        assert len(result) == 1
        assert result[0].id == "a"

    def test_top_k_limit(self):
        candidates = [
            RerankerResult(id=str(i), text=f"text {i}", rrf_score=1.0 - i * 0.1)
            for i in range(20)
        ]
        result = apply_mmr(candidates, top_k=5)
        assert len(result) == 5

    def test_promotes_diversity(self):
        """MMR should promote diversity when candidates have identical embeddings."""
        # Two clusters: embeddings [1,0,0] and [0,1,0]
        cluster_a = [
            RerankerResult(id=f"a{i}", text=f"a {i}", embedding=[1.0, 0.0, 0.0],
                           rrf_score=0.9 - i * 0.01)
            for i in range(4)
        ]
        cluster_b = [
            RerankerResult(id=f"b{i}", text=f"b {i}", embedding=[0.0, 1.0, 0.0],
                           rrf_score=0.8 - i * 0.01)
            for i in range(4)
        ]
        # Interleave: all cluster_a first (higher RRF), then cluster_b
        candidates = cluster_a + cluster_b

        result = apply_mmr(candidates, lam=0.7, top_k=4)
        result_ids = [r.id for r in result]

        # MMR should not just take the top 4 from cluster_a — it should pull
        # in at least one from cluster_b for diversity
        b_count = sum(1 for rid in result_ids if rid.startswith("b"))
        assert b_count >= 1, f"Expected diversity: {result_ids}"

    def test_no_embeddings(self):
        """Without embeddings, MMR falls back to relevance-only ordering."""
        candidates = [
            RerankerResult(id="a", text="first", rrf_score=1.0),
            RerankerResult(id="b", text="second", rrf_score=0.8),
            RerankerResult(id="c", text="third", rrf_score=0.6),
        ]
        result = apply_mmr(candidates, top_k=3)
        assert result[0].id == "a"


# ---------------------------------------------------------------------------
# Full Rerank Pipeline
# ---------------------------------------------------------------------------


class TestRerank:
    @pytest.fixture
    def synthetic_candidates(self):
        """Create synthetic candidates with varied characteristics."""
        now = time.time()
        return [
            RerankerCandidate(
                id="exact",
                text="The user prefers dark mode for all applications",
                embedding=[0.9, 0.1, 0.0, 0.0],
                importance=0.8,
                created_at=now - 3600,  # 1 hour ago
            ),
            RerankerCandidate(
                id="related",
                text="Application theme settings and UI preferences stored",
                embedding=[0.7, 0.3, 0.1, 0.0],
                importance=0.6,
                created_at=now - 86400,  # 1 day ago
            ),
            RerankerCandidate(
                id="unrelated",
                text="The project uses PostgreSQL for data storage",
                embedding=[0.0, 0.0, 0.9, 0.1],
                importance=0.5,
                created_at=now - 604800,  # 1 week ago
            ),
            RerankerCandidate(
                id="partial",
                text="Dark chocolate is the user's favorite snack",
                embedding=[0.4, 0.0, 0.0, 0.8],
                importance=0.3,
                created_at=now - 172800,  # 2 days ago
            ),
        ]

    def test_empty_candidates(self):
        assert rerank("test query", [1.0, 0.0], []) == []

    def test_returns_reranker_results(self, synthetic_candidates):
        results = rerank(
            "dark mode preference",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
        )
        assert all(isinstance(r, RerankerResult) for r in results)

    def test_top_k_respected(self, synthetic_candidates):
        results = rerank(
            "dark mode",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
            top_k=2,
        )
        assert len(results) <= 2

    def test_rrf_scores_positive(self, synthetic_candidates):
        results = rerank(
            "dark mode preference",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
        )
        for r in results:
            assert r.rrf_score > 0

    def test_cosine_sim_attached(self, synthetic_candidates):
        results = rerank(
            "dark mode preference",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
        )
        # All candidates have embeddings, so all should have cosine_sim
        for r in results:
            assert r.cosine_sim is not None

    def test_relevant_candidate_ranks_high(self, synthetic_candidates):
        results = rerank(
            "dark mode preference",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
        )
        # "exact" should be in the top 2 (strong BM25 + cosine match)
        top_ids = [r.id for r in results[:2]]
        assert "exact" in top_ids

    def test_custom_weights(self, synthetic_candidates):
        """Custom weights should alter rankings."""
        # Heavily weight recency — the most recent candidate should win
        results = rerank(
            "dark mode preference",
            [0.85, 0.15, 0.0, 0.0],
            synthetic_candidates,
            weights=RankingWeights(bm25=0.01, cosine=0.01, importance=0.01, recency=0.97),
        )
        # "exact" was created 1 hour ago (most recent)
        assert results[0].id == "exact"

    def test_no_embeddings(self):
        """Candidates without embeddings should still rank via BM25 + importance + recency."""
        now = time.time()
        candidates = [
            RerankerCandidate(id="a", text="cat dog fish", created_at=now),
            RerankerCandidate(id="b", text="bird plane", created_at=now - 86400),
        ]
        results = rerank("cat dog", [], candidates)
        assert len(results) == 2
        # "a" has BM25 overlap, should rank first
        assert results[0].id == "a"

    def test_missing_importance_and_created_at(self):
        """Candidates without importance/createdAt should get neutral scores."""
        candidates = [
            RerankerCandidate(id="a", text="hello world"),
            RerankerCandidate(id="b", text="hello earth"),
        ]
        results = rerank("hello world", [1.0, 0.0], candidates)
        assert len(results) == 2

    def test_single_candidate(self):
        c = RerankerCandidate(
            id="only",
            text="the only fact",
            embedding=[1.0, 0.0],
            importance=0.9,
            created_at=time.time(),
        )
        results = rerank("only fact", [1.0, 0.0], [c])
        assert len(results) == 1
        assert results[0].id == "only"
