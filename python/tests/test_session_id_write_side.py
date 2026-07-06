"""Write-side wiring: atomic facts + Crystals carry the active conversation's
session_id in their encrypted-blob ``metadata``.

memq-2/-3 built the plumbing (``Crystal.session_id`` -> ``to_metadata()``;
``AgentState.session_id``) but nothing stamped the live session id onto what
gets written. Without this, a vault reader (SPA) has no per-conversation key to
group a conversation's facts + its Crystal — only a time-gap approximation.

These tests pin the wiring:
  * every atomic fact_dict handed to ``remember_batch`` carries
    ``extra_metadata={"session_id": <active>}`` (and omits it when there is no
    active session);
  * the session-end Crystal is stamped with ``state.session_id`` so it shares
    the exact id its atomic facts carry.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.agent.debrief import Crystal


def _make_state_and_client():
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    fake_client.recall = AsyncMock(return_value=[])
    fake_client.forget = AsyncMock(return_value=True)
    fake_client.remember = AsyncMock(return_value="crystal-id")

    async def _batch_side_effect(facts, source="python-client"):
        return [f"fact-id-{i}" for i in range(len(facts))]

    fake_client.remember_batch = AsyncMock(side_effect=_batch_side_effect)
    state._client = fake_client
    return state, fake_client


def _make_facts(n: int) -> list[ExtractedFact]:
    return [
        ExtractedFact(
            text=f"Fact number {i}: user detail here",
            type="claim",
            importance=7,
            action="ADD",
            confidence=0.9,
            source="user",
            scope="unspecified",
        )
        for i in range(n)
    ]


def _run_auto_extract(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*args, **kwargs):
        return facts

    async def passthrough(fs, *args, **kwargs):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        state.add_message("user", "I like fact number 0")
        state.add_message("assistant", "Got it")
        return auto_extract(state)


# ---------------------------------------------------------------------------
# Atomic facts
# ---------------------------------------------------------------------------


def test_atomic_facts_carry_active_session_id() -> None:
    state, fake_client = _make_state_and_client()
    # Simulate a per-conversation activation (the id the SPA groups by).
    state.activate_conversation("dm:chat-123:topic-A")
    expected_sid = state.session_id
    assert expected_sid  # sanity — activation minted a host-derived id

    stored = _run_auto_extract(state, _make_facts(3))
    assert len(stored) == 3

    fact_dicts = fake_client.remember_batch.call_args.args[0]
    assert len(fact_dicts) == 3
    for fd in fact_dicts:
        assert fd.get("extra_metadata") == {"session_id": expected_sid}


def test_atomic_facts_omit_session_id_when_no_active_session() -> None:
    state, fake_client = _make_state_and_client()
    # No activate_conversation / start_session → session_id is None.
    assert state.session_id is None

    _run_auto_extract(state, _make_facts(2))

    fact_dicts = fake_client.remember_batch.call_args.args[0]
    for fd in fact_dicts:
        assert "extra_metadata" not in fd


# ---------------------------------------------------------------------------
# Crystal
# ---------------------------------------------------------------------------


def test_crystal_stamped_with_active_session_id() -> None:
    state, fake_client = _make_state_and_client()
    state.activate_conversation("dm:chat-123:topic-A")
    expected_sid = state.session_id

    # Enough messages to clear the content-aware gate (>= 4).
    for i in range(4):
        state.add_message("user" if i % 2 == 0 else "assistant", f"msg {i}")

    crystal = Crystal(narrative="We booked the Lisbon flight, aisle seat, under 400.")
    assert crystal.session_id == ""  # generate_crystal doesn't set it

    async def fake_generate(*args, **kwargs):
        return crystal

    from totalreclaw.agent.lifecycle import session_debrief

    with patch("totalreclaw.agent.lifecycle.generate_crystal", new=fake_generate), \
         patch.object(state, "is_configured", return_value=True):
        ids = session_debrief(state, stored_fact_texts=["a", "b"])

    assert ids  # a crystal was stored
    meta = fake_client.remember.call_args.kwargs["extra_metadata"]
    assert meta["session_id"] == expected_sid
    assert meta["subtype"] == "session_crystal"
    # And the dataclass was mutated so downstream consumers see it too.
    assert crystal.session_id == expected_sid


def test_crystal_keeps_its_own_session_id_if_already_set() -> None:
    """If a Crystal already carries a session_id (e.g. a future generator sets
    it), the write path must not overwrite it with the live session."""
    state, fake_client = _make_state_and_client()
    state.activate_conversation("dm:chat-123:topic-A")

    preset = "01902d40-7a2b-7f12-9c44-1c5e7d2af6a1"
    crystal = Crystal(narrative="Preset session.", session_id=preset)

    for i in range(4):
        state.add_message("user" if i % 2 == 0 else "assistant", f"msg {i}")

    async def fake_generate(*args, **kwargs):
        return crystal

    from totalreclaw.agent.lifecycle import session_debrief

    with patch("totalreclaw.agent.lifecycle.generate_crystal", new=fake_generate), \
         patch.object(state, "is_configured", return_value=True):
        session_debrief(state, stored_fact_texts=["a", "b"])

    meta = fake_client.remember.call_args.kwargs["extra_metadata"]
    assert meta["session_id"] == preset
