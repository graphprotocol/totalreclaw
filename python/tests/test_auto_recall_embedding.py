"""#639: auto-recall passes a query embedding when the embedder is loaded."""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

from totalreclaw.agent import recall as recall_mod
from totalreclaw.agent.recall import auto_recall, auto_recall_async

VEC = [0.1] * 640


def _state():
    state = MagicMock()
    state.is_configured.return_value = True
    state.get_max_candidate_pool.return_value = 100
    client = MagicMock()

    async def _recall(*a, **kw):
        client.kwargs = kw
        return []

    client.recall = _recall
    state.get_client.return_value = client
    return state, client


def _loaded(get_embedding):
    return (
        patch("totalreclaw.embedding.is_model_loaded", return_value=True),
        patch("totalreclaw.embedding.get_embedding", get_embedding),
    )


def _lexical(client):
    assert client.kwargs["query_embedding"] is None
    assert client.kwargs["include_embeddings"] is False


def test_passes_query_embedding_when_loaded():
    state, client = _state()
    p1, p2 = _loaded(lambda t: VEC)
    with p1, p2:
        auto_recall("dietary preference", state)
    assert client.kwargs["query_embedding"] == VEC
    assert client.kwargs["include_embeddings"] is True


def test_not_loaded_falls_back_never_embeds_and_warms_up():
    state, client = _state()
    ge = MagicMock()
    ensure = MagicMock()
    with patch("totalreclaw.embedding.get_embedding", ge), \
         patch("totalreclaw.embedding.is_model_available_locally", return_value=True), \
         patch("totalreclaw.embedding._ensure_loaded", ensure):
        auto_recall("dietary preference", state)
        auto_recall("again", state)
        time.sleep(0.1)
    ge.assert_not_called()
    ensure.assert_called_once()  # one-time background warm-up
    _lexical(client)


def test_no_warmup_when_not_cached():
    state, client = _state()
    ensure = MagicMock()
    with patch("totalreclaw.embedding._ensure_loaded", ensure):
        auto_recall("q", state)
        time.sleep(0.05)
    ensure.assert_not_called()
    _lexical(client)


def test_falls_back_when_embedding_raises():
    state, client = _state()

    def boom(_t):
        raise RuntimeError("boom")

    p1, p2 = _loaded(boom)
    with p1, p2:
        auto_recall("dietary preference", state)
    _lexical(client)


def test_sync_timeout_end_to_end_lexical():
    state, client = _state()
    release = threading.Event()

    def slow(_t):
        release.wait(2)
        return VEC

    p1, p2 = _loaded(slow)
    with p1, p2, patch.object(recall_mod, "QUERY_EMBED_TIMEOUT_S", 0.05), \
         patch.object(recall_mod, "_embed_query_or_none",
                      lambda q, _orig=recall_mod._embed_query_or_none: _orig(q, timeout=0.05)):
        t0 = time.monotonic()
        auto_recall("dietary preference", state)
        assert time.monotonic() - t0 < 1.0
    release.set()
    _lexical(client)


def test_async_timeout_end_to_end_lexical():
    state, client = _state()
    release = threading.Event()

    def slow(_t):
        release.wait(2)
        return VEC

    p1, p2 = _loaded(slow)
    with p1, p2, patch.object(recall_mod, "_embed_query_or_none",
                              lambda q, _orig=recall_mod._embed_query_or_none: _orig(q, timeout=0.05)):
        asyncio.run(auto_recall_async("dietary preference", state))
    release.set()
    _lexical(client)


def test_circuit_breaker_skips_while_previous_embed_running():
    release = threading.Event()
    calls = []

    def slow(t):
        calls.append(t)
        release.wait(2)
        return VEC

    p1, p2 = _loaded(slow)
    with p1, p2:
        assert recall_mod._embed_query_or_none("a", timeout=0.05) is None
        t0 = time.monotonic()
        assert recall_mod._embed_query_or_none("b", timeout=1.0) is None
        assert time.monotonic() - t0 < 0.2  # immediate, no second wait
        assert calls == ["a"]
        release.set()
        time.sleep(0.1)
        assert recall_mod._embed_query_or_none("c", timeout=1.0) == VEC


def test_async_passes_query_embedding():
    state, client = _state()
    p1, p2 = _loaded(lambda t: VEC)
    with p1, p2:
        asyncio.run(auto_recall_async("dietary preference", state))
    assert client.kwargs["query_embedding"] == VEC
    assert client.kwargs["include_embeddings"] is True
