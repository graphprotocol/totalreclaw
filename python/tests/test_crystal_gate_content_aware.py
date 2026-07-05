"""Content-aware Crystal length gate.

Historically a session had to reach 4 full turns (8 messages) before it
earned a Crystal (``len(messages) < 8 → skip``). That silently dropped
crisp 2-3 turn topical exchanges ("book the Lisbon flight, aisle seat,
under $400") even when they produced real, storable facts.

The gate is now content-aware:

* Hard floor: ``< 4`` messages (< 2 turns) → never crystallize.
* 4-7 messages (2-3 turns) → crystallize only if the session produced
  substance (``>= 2`` stored facts).
* ``>= 8`` messages (4+ turns) → always qualify, as before.

Two layers enforce it and are covered here:

1. :func:`totalreclaw.agent.lifecycle.session_debrief` — the auto path
   (``on_session_finalize`` → debrief → Crystal). This is the layer that
   holds the ``stored_fact_texts`` context, so it is the real decision point.
2. :func:`totalreclaw.agent.debrief.generate_crystal` — defense in depth,
   so a direct caller can't slip a trivial session past the gate.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.debrief import Crystal, generate_crystal
from totalreclaw.agent.lifecycle import session_debrief
from totalreclaw.hermes.state import PluginState


def _make_state_with_client() -> PluginState:
    """A configured PluginState whose fake client records ``remember`` calls."""
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    remember_return = iter([f"fact-id-{i}" for i in range(1, 10)])

    async def _remember(*args, **kwargs):
        return next(remember_return)

    fake_client.remember = AsyncMock(side_effect=_remember)
    fake_client.recall = AsyncMock(return_value=[])
    state._client = fake_client
    return state


def _seed(state: PluginState, n_messages: int) -> None:
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        state.add_message(role, f"a substantive message about the Lisbon trip, turn {i}")


_CRYSTAL = Crystal(
    narrative="Booked the Lisbon flight — aisle seat, under $400.",
    key_outcomes=["flight booked"],
    open_threads=[],
    lessons=[],
    importance=7,
)


async def _fake_generate(*args, **kwargs):
    return _CRYSTAL


# ── session_debrief (auto path — the real decision point) ─────────────────────


class TestSessionDebriefGate:
    def test_hard_floor_gates_below_two_turns(self) -> None:
        """< 4 messages (< 2 turns) never crystallizes, even with facts."""
        state = _make_state_with_client()
        _seed(state, 2)  # 1 turn
        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=_fake_generate):
            out = session_debrief(state, stored_fact_texts=["a", "b", "c"])
        assert out == []

    def test_short_trivial_session_is_gated(self) -> None:
        """4-7 messages with < 2 stored facts → no Crystal."""
        state = _make_state_with_client()
        _seed(state, 6)  # 3 turns
        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=_fake_generate):
            out = session_debrief(state, stored_fact_texts=["only one fact"])
        assert out == []

    def test_short_substantive_session_crystallizes(self) -> None:
        """4-7 messages WITH >= 2 stored facts → a crisp short topic gets a Crystal."""
        state = _make_state_with_client()
        _seed(state, 6)  # 3 turns
        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=_fake_generate):
            out = session_debrief(state, stored_fact_texts=["flight", "seat"])
        assert out == ["fact-id-1"]

    def test_long_session_crystallizes_without_facts(self) -> None:
        """>= 8 messages (4+ turns) always qualifies — unchanged behaviour."""
        state = _make_state_with_client()
        _seed(state, 8)  # 4 turns
        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=_fake_generate):
            out = session_debrief(state, stored_fact_texts=[])
        assert out == ["fact-id-1"]

    def test_stored_fact_texts_none_is_safe(self) -> None:
        """A short session with no fact context (None) gates cleanly, no crash."""
        state = _make_state_with_client()
        _seed(state, 6)
        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=_fake_generate):
            out = session_debrief(state, stored_fact_texts=None)
        assert out == []


# ── generate_crystal (defense in depth) ───────────────────────────────────────


class TestGenerateCrystalGate:
    @pytest.mark.asyncio
    async def test_hard_floor_returns_none(self) -> None:
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        with patch("totalreclaw.agent.debrief.detect_llm_config", return_value=MagicMock()):
            # A truthy config means we reach the length gate; the gate must
            # short-circuit to None *before* any LLM call.
            with patch("totalreclaw.agent.debrief.chat_completion", new=AsyncMock()) as llm:
                out = await generate_crystal(msgs, ["a", "b", "c"])
        assert out is None
        llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_trivial_returns_none(self) -> None:
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        with patch("totalreclaw.agent.debrief.detect_llm_config", return_value=MagicMock()):
            with patch("totalreclaw.agent.debrief.chat_completion", new=AsyncMock()) as llm:
                out = await generate_crystal(msgs, ["only one"])
        assert out is None
        llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_substantive_reaches_llm(self) -> None:
        """4-7 messages + >= 2 facts passes the gate and calls the LLM."""
        msgs = [
            {"role": "user", "content": "book the Lisbon flight"},
            {"role": "assistant", "content": "aisle seat, under $400 — done"},
            {"role": "user", "content": "great"},
            {"role": "assistant", "content": "confirmation sent"},
        ]
        fake_response = (
            '{"narrative": "Booked the Lisbon flight, aisle seat under $400.", '
            '"key_outcomes": ["flight booked"], "open_threads": [], '
            '"lessons": [], "importance": 7}'
        )
        with patch("totalreclaw.agent.debrief.detect_llm_config", return_value=MagicMock()):
            with patch(
                "totalreclaw.agent.debrief.chat_completion",
                new=AsyncMock(return_value=fake_response),
            ) as llm:
                out = await generate_crystal(msgs, ["flight", "seat"])
        llm.assert_awaited_once()
        assert out is not None
        assert out.importance == 7
