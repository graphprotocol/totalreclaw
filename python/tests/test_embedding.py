"""Tests for TotalReclaw embedding pipeline.

These tests require model download (~600MB first time).
Mark with pytest.mark.slow if needed.
"""
import math
import pytest


class TestEmbedding:
    @pytest.fixture(scope="class")
    def embed(self):
        from totalreclaw.embedding import get_embedding
        return get_embedding

    def test_output_dimensions(self, embed):
        emb = embed("Hello world")
        assert len(emb) == 1024

    def test_unit_norm(self, embed):
        emb = embed("Hello world")
        norm = math.sqrt(sum(x * x for x in emb))
        assert abs(norm - 1.0) < 1e-4

    def test_deterministic(self, embed):
        emb1 = embed("Hello world")
        emb2 = embed("Hello world")
        for a, b in zip(emb1, emb2):
            assert abs(a - b) < 1e-6

    def test_different_texts_different_embeddings(self, embed):
        emb1 = embed("The cat sat on the mat")
        emb2 = embed("Quantum physics is fascinating")
        dot = sum(a * b for a, b in zip(emb1, emb2))
        assert dot < 0.95

    def test_similar_texts_high_similarity(self, embed):
        emb1 = embed("Pedro is the founder of TotalReclaw")
        emb2 = embed("Pedro founded TotalReclaw")
        dot = sum(a * b for a, b in zip(emb1, emb2))
        assert dot > 0.7

    def test_empty_text(self, embed):
        emb = embed("")
        assert len(emb) == 1024

    def test_get_embedding_dims(self):
        from totalreclaw.embedding import get_embedding_dims
        assert get_embedding_dims() == 1024

    def test_batch(self, embed):
        from totalreclaw.embedding import get_embeddings_batch
        results = get_embeddings_batch(["hello", "world"])
        assert len(results) == 2
        assert all(len(e) == 1024 for e in results)
