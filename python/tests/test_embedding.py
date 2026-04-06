"""Tests for TotalReclaw embedding pipeline.

These tests require model download (~34MB for e5-small, ~164MB for Harrier).
Skipped in CI (no ONNX model cache). Run locally with:
    cd python && python -m pytest tests/test_embedding.py -v
"""
import math
import os

import pytest

# Skip the entire module in CI where model downloads may fail
pytestmark = pytest.mark.skipif(
    os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true",
    reason="Embedding tests require model download; skipped in CI",
)


class TestEmbedding:
    @pytest.fixture(scope="class")
    def embed(self):
        from totalreclaw.embedding import get_embedding
        return get_embedding

    @pytest.fixture(scope="class")
    def dims(self):
        from totalreclaw.embedding import get_embedding_dims
        return get_embedding_dims()

    def test_output_dimensions(self, embed, dims):
        emb = embed("Hello world")
        assert len(emb) == dims

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

    def test_empty_text(self, embed, dims):
        emb = embed("")
        assert len(emb) == dims

    def test_get_embedding_dims(self):
        from totalreclaw.embedding import get_embedding_dims
        dims = get_embedding_dims()
        assert isinstance(dims, int)
        assert dims > 0

    def test_batch(self, embed, dims):
        from totalreclaw.embedding import get_embeddings_batch
        results = get_embeddings_batch(["hello", "world"])
        assert len(results) == 2
        assert all(len(e) == dims for e in results)
