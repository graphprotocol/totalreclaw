"""Regression tests for issue #389 — Gemini import ~58min per 25-chunk batch.

Root cause: ``totalreclaw_import_batch`` (hermes/tools.py) instantiated a fresh
``ImportEngine`` on every tool call, so the engine's smart-import context
(``_smart_ctx``, ``_smart_import_attempted``) and session-grouping caches
(``_session_assignments``, ``_session_ids``, ``_processed_chunk_indices``, ...)
were thrown away after every batch. Large imports re-ran the 17-25 min
profile pass on each batch → ~52 hour total for a 1344-chunk file.

Fix: cache ``ImportEngine`` instances on ``PluginState._import_engines``,
keyed by ``(source, file_path)``. Content-only imports (no ``file_path``)
remain uncached because they lack a stable cache key.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def _bare_state():
    """A minimal stand-in for PluginState that avoids hermes-config disk reads."""
    state = MagicMock()
    state._import_engines = None  # exercise the lazy-init branch
    state.get_client = MagicMock(return_value=MagicMock())
    return state


def test_engine_is_cached_by_source_and_file_path(monkeypatch):
    """Two calls with identical (source, file_path) return the same engine."""
    from totalreclaw.hermes import tools

    state = _bare_state()
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    engine1 = tools._get_or_create_import_engine(state, "gemini", "/tmp/a.html")
    engine2 = tools._get_or_create_import_engine(state, "gemini", "/tmp/a.html")

    assert engine1 is engine2, "same (source, file_path) must reuse the engine"


def test_different_file_paths_get_different_engines(monkeypatch):
    from totalreclaw.hermes import tools

    state = _bare_state()
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    e_a = tools._get_or_create_import_engine(state, "gemini", "/tmp/a.html")
    e_b = tools._get_or_create_import_engine(state, "gemini", "/tmp/b.html")

    assert e_a is not e_b


def test_different_sources_get_different_engines(monkeypatch):
    from totalreclaw.hermes import tools

    state = _bare_state()
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    e_gemini = tools._get_or_create_import_engine(state, "gemini", "/tmp/x.html")
    e_chatgpt = tools._get_or_create_import_engine(state, "chatgpt", "/tmp/x.html")

    assert e_gemini is not e_chatgpt


def test_content_only_imports_are_not_cached(monkeypatch):
    """No stable cache key without ``file_path`` — each call gets a fresh engine."""
    from totalreclaw.hermes import tools

    state = _bare_state()
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    e1 = tools._get_or_create_import_engine(state, "gemini", None)
    e2 = tools._get_or_create_import_engine(state, "gemini", None)

    assert e1 is not e2


@pytest.mark.asyncio
async def test_import_batch_reuses_engine_across_calls(monkeypatch):
    """End-to-end: two ``import_batch`` tool calls on the same file use one engine.

    Bug-shape assertion: pre-patch this test fails because each ``import_batch``
    invocation created a fresh ``ImportEngine``, so the instance counter would
    show 2 instead of 1.
    """
    from totalreclaw.hermes import tools
    import totalreclaw.import_engine as ie

    instance_count = 0
    real_init = ie.ImportEngine.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal instance_count
        instance_count += 1
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ie.ImportEngine, "__init__", counting_init)
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    # Stub process_batch so we don't trip on missing adapters / files / LLMs.
    async def fake_process_batch(self, **kwargs):
        from totalreclaw.import_adapters.types import BatchImportResult
        return BatchImportResult(
            success=True,
            batch_offset=kwargs.get("offset", 0),
            batch_size=kwargs.get("batch_size", 25),
            chunks_processed=25,
            total_chunks=100,
            facts_extracted=10,
            facts_stored=10,
            remaining_chunks=75,
            is_complete=False,
            duration_ms=1000,
        )
    monkeypatch.setattr(ie.ImportEngine, "process_batch", fake_process_batch)

    state = _bare_state()
    args_b0 = {"source": "gemini", "file_path": "/tmp/big.html", "disclosure_confirmed": True,
               "offset": 0, "batch_size": 25}
    args_b1 = {"source": "gemini", "file_path": "/tmp/big.html", "disclosure_confirmed": True,
               "offset": 25, "batch_size": 25}

    r0 = json.loads(await tools.import_batch(args_b0, state))
    r1 = json.loads(await tools.import_batch(args_b1, state))

    assert r0["batch_offset"] == 0
    assert r1["batch_offset"] == 25
    assert instance_count == 1, (
        f"expected one engine across two batch calls, got {instance_count}"
    )


@pytest.mark.asyncio
async def test_import_batch_content_only_creates_fresh_engine(monkeypatch):
    """Content-only imports (no file_path) keep the per-call engine behaviour."""
    from totalreclaw.hermes import tools
    import totalreclaw.import_engine as ie

    instance_count = 0
    real_init = ie.ImportEngine.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal instance_count
        instance_count += 1
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ie.ImportEngine, "__init__", counting_init)
    monkeypatch.setattr(tools, "_make_extractor", lambda s: AsyncMock())
    monkeypatch.setattr(tools, "_make_llm_completion", lambda s: AsyncMock())

    async def fake_process_batch(self, **kwargs):
        from totalreclaw.import_adapters.types import BatchImportResult
        return BatchImportResult(
            success=True, batch_offset=0, batch_size=25,
            chunks_processed=0, total_chunks=0, facts_extracted=0,
            facts_stored=0, remaining_chunks=0, is_complete=True, duration_ms=1,
        )
    monkeypatch.setattr(ie.ImportEngine, "process_batch", fake_process_batch)

    state = _bare_state()
    args = {"source": "gemini", "content": "<html>...</html>", "disclosure_confirmed": True,
            "offset": 0, "batch_size": 25}

    await tools.import_batch(args, state)
    await tools.import_batch(args, state)

    assert instance_count == 2, (
        "content-only imports have no cache key; engines must not be reused"
    )
