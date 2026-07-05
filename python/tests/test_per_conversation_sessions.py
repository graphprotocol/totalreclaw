"""Per-conversation session isolation (parallel-chat fix).

Before this fix the Hermes plugin kept ONE process-global message buffer + turn
counter + session id. When a user ran several conversations in parallel through
one Hermes process (e.g. interleaved Telegram threads in the same chat), Hermes
handed the plugin a distinct per-conversation ``session_id`` every turn (via
``MemoryProvider.sync_turn`` / the per-turn hooks) — but the plugin ignored it,
so every conversation's turns piled into one buffer and collapsed into one mixed
session Crystal.

The fix routes each turn to a per-conversation *slot* keyed by that host id, so
parallel conversations keep separate buffers/turn-counters/session-ids and each
finalizes to its own clean Crystal. When no per-conversation id is supplied the
behavior is byte-identical to the prior single buffer (legacy hosts).
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.state import AgentState, _DEFAULT_SLOT_KEY
from totalreclaw.hermes import hooks
from totalreclaw.hermes.state import PluginState


def _texts(state) -> list[str]:
    return [m["content"] for m in state.get_all_messages()]


# ── AgentState slot mechanics ────────────────────────────────────────────────


class TestSessionSlots:
    def test_activate_creates_distinct_host_derived_ids(self) -> None:
        s = AgentState()
        s.activate_conversation("convA")
        sid_a = s.session_id
        s.activate_conversation("convB")
        sid_b = s.session_id
        assert sid_a and sid_b and sid_a != sid_b
        assert s._session_id_from_host is True

    def test_activate_is_deterministic_per_id(self) -> None:
        """The same host id always maps to the same session id (stable key)."""
        s1 = AgentState()
        s1.activate_conversation("conv-42")
        s2 = AgentState()
        s2.activate_conversation("conv-42")
        assert s1.session_id == s2.session_id

    def test_activate_same_id_is_noop(self) -> None:
        s = AgentState()
        s.activate_conversation("convA")
        s.increment_turn()
        s.add_message("user", "hi")
        sid_before = s.session_id
        s.activate_conversation("convA")  # re-activate active conv
        assert s.session_id == sid_before
        assert s.turn_count == 1  # not reset
        assert _texts(s) == ["hi"]

    def test_interleaved_turns_do_not_cross_contaminate(self) -> None:
        s = AgentState()
        # A, B, A interleave — the exact shape of Pedro's repro.
        s.activate_conversation("A"); s.increment_turn(); s.add_message("user", "A1")
        s.activate_conversation("B"); s.increment_turn(); s.add_message("user", "B1")
        s.activate_conversation("A"); s.increment_turn(); s.add_message("user", "A2")

        # Active is A: holds only A's messages + A's turn count.
        assert _texts(s) == ["A1", "A2"]
        assert s.turn_count == 2

        # Sweep the rest: B holds only B's message.
        s.stash_active_conversation()
        found = {}
        while s.pop_next_conversation():
            found[s._active_conversation_key] = _texts(s)
        assert found["A"] == ["A1", "A2"]
        assert found["B"] == ["B1"]

    def test_no_key_is_legacy_single_session(self) -> None:
        """Never calling activate_conversation ⇒ one buffer, one finalize slot."""
        s = AgentState()
        s.start_session(external_id="coarse-chat")
        legacy_sid = s.session_id
        s.increment_turn(); s.add_message("user", "one")
        s.increment_turn(); s.add_message("user", "two")
        # Finalize sweep yields exactly one slot (the default), unchanged content.
        s.stash_active_conversation()
        slots = []
        while s.pop_next_conversation():
            slots.append((s._active_conversation_key, s.session_id, _texts(s)))
        assert len(slots) == 1
        key, sid, msgs = slots[0]
        assert key is None  # default slot restores to the legacy (None) key
        assert sid == legacy_sid
        assert msgs == ["one", "two"]

    def test_empty_default_slot_discarded_on_first_activate(self) -> None:
        """The message-less coarse slot on_session_start sets up is dropped when
        the first real conversation activates — no empty _DEFAULT slot lingers."""
        s = AgentState()
        s.start_session(external_id="coarse-chat")  # empty live slot
        s.activate_conversation("realconv")
        assert _DEFAULT_SLOT_KEY not in s._session_slots

    def test_reset_conversations_clears_slots(self) -> None:
        s = AgentState()
        s.activate_conversation("A"); s.add_message("user", "A1")
        s.activate_conversation("B"); s.add_message("user", "B1")
        assert s._session_slots  # A stashed while B active
        s.reset_conversations()
        assert s._session_slots == {}
        assert s._active_conversation_key is None


# ── ingest_turn routing ──────────────────────────────────────────────────────


def _configured_state() -> PluginState:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-id")
    client.recall = AsyncMock(return_value=[])
    state._client = client
    return state


class TestIngestTurnRouting:
    def test_ingest_routes_by_session_id(self) -> None:
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]):
            hooks.ingest_turn(state, "A: q", "A: a", session_id="convA")
            hooks.ingest_turn(state, "B: q", "B: a", session_id="convB")
            hooks.ingest_turn(state, "A: q2", "A: a2", session_id="convA")
        # Active is convA with its two turns; convB is stashed with one.
        assert state._active_conversation_key == "convA"
        assert state.turn_count == 2
        assert _texts(state) == ["A: q", "A: a", "A: q2", "A: a2"]
        assert "convB" in state._session_slots

    def test_ingest_without_session_id_is_single_buffer(self) -> None:
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]):
            hooks.ingest_turn(state, "one", "r1")
            hooks.ingest_turn(state, "two", "r2")
        assert state._active_conversation_key is None
        assert state._session_slots == {}
        assert state.turn_count == 2
        assert _texts(state) == ["one", "r1", "two", "r2"]


# ── End-to-end: interleave → separate Crystals ───────────────────────────────


class TestFinalizeSeparatesCrystals:
    def test_two_interleaved_conversations_finalize_to_two_crystals(self) -> None:
        """The headline guarantee: two parallel conversations → two DISTINCT
        Crystals, not one mixed summary."""
        state = _configured_state()
        # on_session_start hands the plugin one coarse chat id (the bug source).
        state.start_session(external_id="telegram:chat:123")

        seen_debriefs: list[tuple] = []

        def fake_debrief(st, stored_fact_texts=None):
            seen_debriefs.append((st.session_id, tuple(m["content"] for m in st.get_all_messages())))
            return []

        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=fake_debrief):
            # Interleaved turns, each carrying its own per-conversation id.
            hooks.ingest_turn(state, "book the lisbon flight", "aisle seat, done", session_id="conv-flight")
            hooks.ingest_turn(state, "best local LLM for a mac", "try gemma", session_id="conv-llm")
            hooks.ingest_turn(state, "great, thanks", "confirmation sent", session_id="conv-flight")
            hooks.ingest_turn(state, "does ollama run it", "yes", session_id="conv-llm")
            hooks.on_session_finalize(state)

        # Two conversations → two debriefs → two crystals.
        assert len(seen_debriefs) == 2
        sids = {sid for sid, _ in seen_debriefs}
        assert len(sids) == 2, "each conversation must get its own session id"

        # And crucially: NO Crystal mixes the two topics.
        by_topic = {frozenset(msgs): sid for sid, msgs in seen_debriefs}
        for msgs in by_topic:
            joined = " ".join(msgs).lower()
            flight = "lisbon" in joined or "aisle" in joined
            llm = "gemma" in joined or "ollama" in joined or "local llm" in joined
            assert not (flight and llm), f"a Crystal mixed both conversations: {msgs}"

    def test_legacy_single_session_still_one_crystal(self) -> None:
        """No per-conversation ids ⇒ exactly one Crystal (unchanged behavior)."""
        state = _configured_state()
        state.start_session(external_id="coarse")
        seen = []
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=lambda st, stored_fact_texts=None: seen.append(st.session_id) or []):
            hooks.ingest_turn(state, "u1", "a1")
            hooks.ingest_turn(state, "u2", "a2")
            hooks.on_session_finalize(state)
        assert len(seen) == 1
