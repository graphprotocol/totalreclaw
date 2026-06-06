"""#376 — auto-extraction falls back to the chat model when the cheap
extraction model returns empty content.

QA-hermes-prestable-2.4.4rc6 F8: the derived cheap extraction model
(``glm-4.5-flash`` on z.ai) returned empty content, so that turn's facts
were silently dropped. rc.24 F2 swaps the flagship chat model down to a
cheap workhorse for extraction; when even the cheap model empties, retry
ONCE with the original chat model on the SAME endpoint (Pedro's directive:
never flip the endpoint — the API key is endpoint-scoped).
"""
from __future__ import annotations

import pytest

from totalreclaw.agent import extraction
from totalreclaw.agent.llm_client import LLMConfig


_LONG_MSG = (
    "Hi, I'm Pedro. I live in Porto and I use PostgreSQL for all my side "
    "projects — never MySQL. I also prefer Vim over VS Code."
)


@pytest.mark.asyncio
async def test_empty_cheap_extraction_falls_back_to_chat_model(monkeypatch):
    chat = LLMConfig(
        api_key="k",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5.1",
        api_format="openai",
    )
    seen: list[tuple[str, str]] = []

    async def fake_cc(cfg, system, user, *a, **k):
        seen.append((cfg.model, cfg.base_url))
        return ""  # both attempts empty -> graceful []

    monkeypatch.setattr(extraction, "chat_completion", fake_cc)

    out = await extraction.extract_facts_llm(
        [{"role": "user", "content": _LONG_MSG}], llm_config=chat
    )

    assert out == []
    models = [m for m, _ in seen]
    assert models[0] == "glm-4.5-flash", "extraction first tries the cheap model"
    assert "glm-5.1" in models, "on empty, must fall back to the chat model (#376)"
    assert len(seen) == 2, "exactly one fallback retry"
    # Pedro's no-endpoint-flip directive: the fallback stays on the same endpoint.
    assert seen[1][1] == chat.base_url


@pytest.mark.asyncio
async def test_fallback_succeeds_yields_facts(monkeypatch):
    """If the chat-model retry returns content, extraction is no longer a
    no-op for the turn."""
    chat = LLMConfig(
        api_key="k",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5.1",
        api_format="openai",
    )
    valid = (
        '{"topics": ["identity"], "facts": [{"text": "Lives in Porto", '
        '"type": "claim", "scope": "personal", "importance": 0.6, '
        '"confidence": 0.9, "provenance": "user"}]}'
    )
    calls = {"n": 0}

    async def fake_cc(cfg, system, user, *a, **k):
        calls["n"] += 1
        return "" if calls["n"] == 1 else valid  # cheap empty, chat-model OK

    monkeypatch.setattr(extraction, "chat_completion", fake_cc)

    out = await extraction.extract_facts_llm(
        [{"role": "user", "content": _LONG_MSG}], llm_config=chat
    )
    # The fallback fired (2 calls) and the non-empty chat-model response
    # proceeded PAST the empty-return branch into the parse pipeline (what
    # the parser ultimately yields is exercised by the parser's own tests).
    assert calls["n"] == 2
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_no_fallback_when_extraction_model_equals_chat_model(monkeypatch):
    """User already on the cheap model -> no swap -> a single attempt, no
    pointless duplicate call."""
    chat = LLMConfig(
        api_key="k",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-4.5-flash",
        api_format="openai",
    )
    n = {"c": 0}

    async def fake_cc(cfg, system, user, *a, **k):
        n["c"] += 1
        return ""

    monkeypatch.setattr(extraction, "chat_completion", fake_cc)

    await extraction.extract_facts_llm(
        [{"role": "user", "content": _LONG_MSG}], llm_config=chat
    )
    assert n["c"] == 1, "no fallback when extraction model == chat model"
