"""Integration tests — all 3 Hermes hooks emit v1 by default.

Stubs ``client.remember`` and ``client.recall`` and asserts each hook:

1. ``pre_llm_call``  — passes through recall with source-weighted reranker
   flag wired via ``search_facts``. Because the hook just calls into
   ``client.recall()``, we only verify the call path is invoked (the
   Tier 1 flag is asserted directly in ``test_v1_taxonomy.py``).
2. ``post_llm_call`` — auto-extracts every N turns, forwards v1
   ``type/source/scope`` to ``client.remember``.
3. ``on_session_end`` — emits debrief items with ``type='summary'`` and
   ``provenance='derived'``, matching the plugin's debrief behavior.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.extraction import ExtractedFact


def _make_state(**env_overrides):
    """Build a PluginState with a stubbed client — no real wallet derivation."""
    from totalreclaw.hermes.state import PluginState
    with patch.dict(os.environ, env_overrides, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    # Forge a fake client so is_configured() returns True.
    fake_client = MagicMock()
    fake_client.remember = AsyncMock(return_value="fact-uuid-abc")
    fake_client.recall = AsyncMock(return_value=[])
    fake_client.forget = AsyncMock(return_value=True)
    state._client = fake_client
    return state, fake_client


# ---------------------------------------------------------------------------
# post_llm_call — auto-extraction forwards v1 fields
# ---------------------------------------------------------------------------


def test_post_llm_call_forwards_v1_fields_to_remember() -> None:
    """When auto_extract fires, client.remember receives v1 type/source/scope."""
    from totalreclaw.hermes.hooks import post_llm_call

    state, fake_client = _make_state()

    # Seed enough turns so the extraction interval triggers.
    # Default extraction_interval is typically 3; call post_llm_call until it hits.
    from totalreclaw.hermes.state import DEFAULT_EXTRACTION_INTERVAL

    # Mock extract_facts_llm to return a v1 fact directly. This bypasses
    # the LLM call entirely — we're testing the wiring, not the extractor.
    fake_fact = ExtractedFact(
        text="User prefers PostgreSQL for OLTP",
        type="preference",
        importance=8,
        action="ADD",
        confidence=0.95,
        source="user",
        scope="work",
        reasoning=None,
        volatility="stable",
    )

    async def fake_extract(*args, **kwargs):
        return [fake_fact]

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract):
        # Inline contradiction detection (skip network).
        async def passthrough(facts, *_args, **_kwargs):
            return facts
        with patch(
            "totalreclaw.agent.lifecycle.detect_and_resolve_contradictions",
            new=passthrough,
        ):
            # Mock _fetch_recent_memories to avoid calling the (stubbed) client.
            with patch(
                "totalreclaw.agent.lifecycle._fetch_recent_memories",
                return_value=[],
            ):
                # Mock embedding so near-dup detection is a no-op.
                with patch(
                    "totalreclaw.agent.lifecycle._is_near_duplicate",
                    return_value=False,
                ):
                    # Trigger extraction by calling post_llm_call enough times.
                    for _ in range(DEFAULT_EXTRACTION_INTERVAL):
                        post_llm_call(state, user_message="u", assistant_response="a")

    # Assert client.remember was called with v1 kwargs.
    assert fake_client.remember.called, "client.remember should have been called"
    call = fake_client.remember.call_args
    kwargs = call.kwargs

    # The first positional is the text; then we check kwargs.
    assert call.args[0] == fake_fact.text
    assert kwargs["fact_type"] == "preference"
    assert kwargs["provenance"] == "user"
    assert kwargs["scope"] == "work"
    assert kwargs["volatility"] == "stable"
    assert kwargs["reasoning"] is None


# ---------------------------------------------------------------------------
# on_session_end — debrief emits v1 summaries
# ---------------------------------------------------------------------------


def test_on_session_end_debrief_emits_v1_summaries() -> None:
    """Debrief path forwards type='summary' + provenance='derived' to client.remember.

    We call ``session_debrief`` directly (the function the hook invokes) so
    the test is independent of the hook's try/except wrapping.
    """
    from totalreclaw.agent.debrief import DebriefItem
    from totalreclaw.agent.lifecycle import session_debrief

    state, fake_client = _make_state()

    # Seed enough conversation so the debrief triggers (>= 8 messages).
    for i in range(10):
        state.add_message("user", f"User message {i} with content words here")
        state.add_message("assistant", f"Assistant reply {i} with different content here")

    debrief_items = [
        DebriefItem(text="Session conclusion summary here", type="summary", importance=8),
        DebriefItem(text="Project context notes here for future", type="context", importance=7),
    ]

    async def fake_generate(*args, **kwargs):
        return debrief_items

    # Patch at the call site — lifecycle.py imports generate_debrief from
    # totalreclaw.agent.debrief and then calls it locally.
    with patch("totalreclaw.agent.lifecycle.generate_debrief", new=fake_generate):
        session_debrief(state)

    # Both debrief items should have been stored with v1 provenance.
    calls = fake_client.remember.call_args_list
    assert len(calls) == 2, f"expected 2 remember calls, got {len(calls)}"

    for call in calls:
        kwargs = call.kwargs
        assert kwargs["fact_type"] == "summary"
        assert kwargs["provenance"] == "derived"
        assert kwargs["scope"] == "unspecified"


# ---------------------------------------------------------------------------
# pre_llm_call — recall path uses search_facts which applies Tier 1 weights
# ---------------------------------------------------------------------------


def test_pre_llm_call_invokes_recall_with_top_k_8() -> None:
    """pre_llm_call runs auto-recall with top_k=8 on the first turn."""
    from totalreclaw.hermes.hooks import pre_llm_call

    state, fake_client = _make_state()

    # Fake auto_recall — we just assert the hook invokes it.
    with patch("totalreclaw.hermes.hooks.auto_recall") as fake_auto_recall:
        fake_auto_recall.return_value = "## Memories\n- user prefers dark mode"
        result = pre_llm_call(state, is_first_turn=True, user_message="what do i prefer?")

    assert fake_auto_recall.called
    # top_k=8 should be passed through (Hermes hook default).
    call = fake_auto_recall.call_args
    assert call.kwargs.get("top_k") == 8 or (len(call.args) >= 3 and call.args[2] == 8)


def test_pre_llm_call_silently_no_op_when_not_first_turn() -> None:
    """pre_llm_call does not auto-recall on non-first turns."""
    from totalreclaw.hermes.hooks import pre_llm_call

    state, fake_client = _make_state()

    with patch("totalreclaw.hermes.hooks.auto_recall") as fake_auto_recall:
        result = pre_llm_call(state, is_first_turn=False, user_message="follow-up?")

    assert not fake_auto_recall.called
