"""#639: auto-recall passes a query embedding when the embedder is ready."""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from totalreclaw.agent import recall as recall_mod
from totalreclaw.agent.recall import auto_recall, auto_recall_async

VEC = [0.1] * 640


def _state(results=None):
    state = MagicMock()
    state.is_configured.return_value = True
    state.get_max_candidate_pool.return_value = 100
    client = MagicMock()

    async def _recall(*a, **kw):
        client.kwargs = kw
        return results or []

    client.recall = _recall
    state.get_client.return_value = client
    return state, client


def test_passes_query_embedding_when_available():
    state, client = _state()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", return_value=VEC):
        auto_recall("dietary preference", state)
    assert client.kwargs["query_embedding"] == VEC
    assert client.kwargs["include_embeddings"] is True


def test_falls_back_when_model_not_local_and_never_embeds():
    state, client = _state()
    ge = MagicMock()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", ge):
        auto_recall("dietary preference", state)
    ge.assert_not_called()
    assert "query_embedding" not in client.kwargs
    assert client.kwargs["include_embeddings"] is False


def test_falls_back_when_embedding_raises():
    state, client = _state()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", side_effect=RuntimeError("boom")):
        auto_recall("dietary preference", state)
    assert "query_embedding" not in client.kwargs
    assert client.kwargs["include_embeddings"] is False


def test_falls_back_when_embedding_exceeds_timeout():
    state, client = _state()

    def slow(_t):
        time.sleep(0.5)
        return VEC

    with patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", slow), \
         patch.object(recall_mod, "QUERY_EMBED_TIMEOUT_S", 0.05), \
         patch.object(recall_mod, "_embed_pool", recall_mod.concurrent.futures.ThreadPoolExecutor(1)):
        t0 = time.monotonic()
        # default arg binds at def time; call helper directly with small timeout
        assert recall_mod._embed_query_or_none("q", timeout=0.05) is None
        assert time.monotonic() - t0 < 0.4
        auto_recall("dietary preference", state)  # must not raise


def test_async_passes_query_embedding():
    state, client = _state()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", return_value=VEC):
        asyncio.run(auto_recall_async("dietary preference", state))
    assert client.kwargs["query_embedding"] == VEC
    assert client.kwargs["include_embeddings"] is True


def test_async_falls_back_when_not_ready():
    state, client = _state()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False):
        asyncio.run(auto_recall_async("dietary preference", state))
    assert "query_embedding" not in client.kwargs
    assert client.kwargs["include_embeddings"] is False
