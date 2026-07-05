"""Per-conversation idle Crystal sweep.

#441 gave each Telegram topic its own slot (separate buffer + session id), and
`on_session_finalize` crystallizes each separately. But in Hermes GATEWAY mode
`on_session_finalize` almost never fires (the box's `session_reset: mode: none`
disarms the idle-finalize watcher), so a topic the user simply stops replying to
never crystallizes until a restart.

This sweep closes that: on each turn, the plugin crystallizes + retires any
*other* conversation slot that has gone quiet past
`TOTALRECLAW_SESSION_IDLE_MINUTES`, piggybacking on the active conversation's
turn cadence. The live conversation is never swept and is left untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from totalreclaw.agent import state as state_mod
from totalreclaw.agent.state import AgentState, _DEFAULT_SLOT_KEY
from totalreclaw.hermes import hooks
from totalreclaw.hermes.state import PluginState


class _Clock:
    """Monkeypatchable monotonic clock (state.py reads ``time.monotonic``)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _configured_state() -> PluginState:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-id")
    client.recall = AsyncMock(return_value=[])
    state._client = client
    return state


# ── find_idle_slots ──────────────────────────────────────────────────────────


class TestFindIdleSlots:
    def test_returns_only_stashed_idle_slots_not_active(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        s = AgentState()
        s.activate_conversation("A")
        s.note_activity()          # A active at t=1000
        clock.advance(10)
        s.activate_conversation("B")  # stashes A (last_activity=1000), B now live
        s.note_activity()          # B active at t=1010
        clock.advance(1000)        # now t=2010
        # A idle for 1010s, B is the LIVE slot (never returned).
        assert s.find_idle_slots(900) == ["A"]
        assert s.find_idle_slots(2000) == []     # threshold not met
        assert s.find_idle_slots(0) == []        # disabled

    def test_never_set_activity_is_not_idle(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        s = AgentState()
        s.activate_conversation("A")
        s.activate_conversation("B")  # A stashed; A got _last_activity on create
        # A's _last_activity was set on activation; force it to 0 to simulate "never set"
        s._session_slots["A"]["_last_activity"] = 0.0
        clock.advance(10_000)
        assert "A" not in s.find_idle_slots(1)


# ── sweep_idle_slots ─────────────────────────────────────────────────────────


class TestSweepIdleSlots:
    def test_crystallizes_idle_and_preserves_live(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        s = AgentState()
        s.activate_conversation("A"); s.add_message("user", "A1"); s.note_activity()
        clock.advance(10)
        s.activate_conversation("B"); s.add_message("user", "B1"); s.note_activity()
        clock.advance(2000)  # A idle, B live

        finalized: list = []
        # record the session_id + messages of whatever is live when finalize runs
        s.sweep_idle_slots(900, lambda: finalized.append((s.session_id, [m["content"] for m in s.get_all_messages()])))

        # A was crystallized (its content was live during finalize), then dropped.
        assert len(finalized) == 1
        assert finalized[0][1] == ["A1"]
        assert "A" not in s._session_slots
        # B is restored as the live conversation, untouched.
        assert s._active_conversation_key == "B"
        assert [m["content"] for m in s.get_all_messages()] == ["B1"]

    def test_sweep_restores_live_even_if_finalize_raises(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        s = AgentState()
        s.activate_conversation("A"); s.add_message("user", "A1"); s.note_activity()
        clock.advance(10)
        s.activate_conversation("B"); s.add_message("user", "B1"); s.note_activity()
        clock.advance(2000)

        def boom():
            raise RuntimeError("crystal failed")

        try:
            s.sweep_idle_slots(900, boom)
        except RuntimeError:
            pass
        # Live conversation B is still intact despite the failure.
        assert s._active_conversation_key == "B"
        assert [m["content"] for m in s.get_all_messages()] == ["B1"]


# ── End-to-end via ingest_turn ───────────────────────────────────────────────


class TestIngestTurnIdleCrystals:
    def test_quiet_topic_crystallizes_on_another_topics_turn(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")  # 900s
        state = _configured_state()

        finalized_sids: list = []

        def fake_finalize(st):
            finalized_sids.append(st.session_id)

        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_finalize_one_conversation", side_effect=fake_finalize):
            # Topic A speaks, then goes quiet.
            hooks.ingest_turn(state, "sell the monitor", "specs here", session_id="convA")
            sid_a = state.session_id
            clock.advance(1000)  # A now idle past 900s
            # Topic B speaks — this turn should sweep + crystallize the idle A.
            hooks.ingest_turn(state, "find AI jobs", "searching", session_id="convB")

        assert finalized_sids == [sid_a], "the idle topic A should crystallize on B's turn"
        # B is live and was NOT crystallized; A's slot is gone.
        assert state._active_conversation_key == "convB"
        assert "convA" not in state._session_slots

    def test_no_sweep_when_disabled(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "0")  # disabled
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_finalize_one_conversation") as fin:
            hooks.ingest_turn(state, "a", "a", session_id="convA")
            clock.advance(100_000)
            hooks.ingest_turn(state, "b", "b", session_id="convB")
        fin.assert_not_called()
        assert "convA" in state._session_slots  # still stashed, never crystallized

    def test_active_topic_never_crystallized_midstream(self, monkeypatch) -> None:
        """The conversation currently being talked to is never swept."""
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_finalize_one_conversation") as fin:
            # Same topic, many turns spread over a long time — never swept.
            for i in range(5):
                hooks.ingest_turn(state, f"m{i}", f"r{i}", session_id="convA")
                clock.advance(10_000)  # long gaps, but it's the ACTIVE topic each turn
        fin.assert_not_called()
