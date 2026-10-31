"""Session Crystals on current Hermes (internal #630).

Hermes 0.22 runs the CLI and TUI as viewers of the gateway: every time the last
viewer closes, the owner fires ``on_session_finalize``. A one-shot ``hermes chat
-Q`` turn (or a resumed TUI session) therefore finalizes after every turn, so
the plugin's buffer held one turn at each finalize, the Crystal gate (< 4
messages) skipped it, and the buffer was dropped: no Crystal, ever.

Gateway chats (Telegram) have the opposite problem: Hermes stopped rotating
gateway conversations on timers, so finalize almost never fires and the active
conversation never crystallizes (the idle sweep only covers *other* slots).

Two fixes, both covered here:

1. A finalize whose segment is too short to crystallize carries the segment
   over, keyed by the host conversation id, instead of dropping it. A resumed
   conversation keeps accumulating, and the first finalize with enough content
   crystallizes the whole segment.
2. A turn that arrives after the idle timeout first crystallizes the earlier
   part of the same conversation (keeping its session id), so a never-ending
   gateway chat gets a Crystal per burst.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from totalreclaw.agent import state as state_mod
from totalreclaw.agent.lifecycle import crystal_gate_passes
from totalreclaw.hermes import hooks
from totalreclaw.hermes.state import PluginState


class _Clock:
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


class _DebriefRecorder:
    """Stands in for ``_session_debrief``: applies the real gate and records
    what a Crystal would have covered."""

    def __init__(self) -> None:
        self.crystals: list[tuple] = []

    def __call__(self, st, stored_fact_texts=None):
        msgs = [m["content"] for m in st.get_all_messages()]
        if crystal_gate_passes(len(msgs), len(stored_fact_texts or [])):
            self.crystals.append((st.session_id, tuple(msgs)))
            return ["crystal-id"]
        return []


def _one_shot_turn(state, sid: str, n: int) -> None:
    """One ``hermes chat -Q --resume`` turn: the turn, then a viewer-close finalize."""
    hooks.on_session_start(state, session_id=sid)
    hooks.ingest_turn(state, f"u{n}", f"a{n}", session_id=sid)
    hooks.on_session_finalize(state, session_id=sid, reason="cli_close")


class TestGate:
    def test_gate_matches_the_documented_floor(self) -> None:
        assert not crystal_gate_passes(2, 5)
        assert not crystal_gate_passes(6, 1)
        assert crystal_gate_passes(6, 2)
        assert crystal_gate_passes(8, 0)


class TestShortSegmentsCarryOver:
    def test_one_shot_turns_accumulate_into_one_crystal(self) -> None:
        state = _configured_state()
        rec = _DebriefRecorder()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=rec), \
             patch.object(hooks, "_eager_account_register"):
            for n in range(1, 4):
                _one_shot_turn(state, "local-abc", n)
            assert rec.crystals == []          # 6 messages, no facts: below the gate
            _one_shot_turn(state, "local-abc", 4)

        assert len(rec.crystals) == 1
        assert rec.crystals[0][1] == ("u1", "a1", "u2", "a2", "u3", "a3", "u4", "a4")
        # The crystallized segment is gone; nothing is carried after it.
        assert "local-abc" not in state._session_slots

    def test_carry_without_per_turn_ids_resumes_on_session_start(self) -> None:
        """A host that passes the id only to the lifecycle hooks, not per turn."""
        state = _configured_state()
        rec = _DebriefRecorder()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=rec), \
             patch.object(hooks, "_eager_account_register"):
            for n in range(1, 5):
                hooks.on_session_start(state, session_id="local-xyz")
                hooks.ingest_turn(state, f"u{n}", f"a{n}")
                hooks.on_session_finalize(state, session_id="local-xyz", reason="cli_close")

        assert len(rec.crystals) == 1
        assert len(rec.crystals[0][1]) == 8

    def test_finalize_clears_live_session_even_when_carrying(self) -> None:
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=_DebriefRecorder()), \
             patch.object(hooks, "_eager_account_register"):
            _one_shot_turn(state, "local-abc", 1)
        assert state.session_id is None
        assert state.get_all_messages() == []
        assert "local-abc" in state._session_slots

    def test_reset_drops_carried_segments(self) -> None:
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=_DebriefRecorder()), \
             patch.object(hooks, "_eager_account_register"):
            _one_shot_turn(state, "local-abc", 1)
            hooks.on_session_reset(state)
        assert state._session_slots == {}

    def test_carried_segment_retires_through_the_idle_sweep(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")
        state = _configured_state()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=_DebriefRecorder()), \
             patch.object(hooks, "_eager_account_register"):
            _one_shot_turn(state, "local-old", 1)
            clock.advance(2000)
            hooks.ingest_turn(state, "new", "chat", session_id="local-new")
        assert "local-old" not in state._session_slots


class TestIdleActiveConversation:
    def test_turn_after_idle_crystallizes_the_earlier_burst(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")
        state = _configured_state()
        rec = _DebriefRecorder()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=rec):
            for n in range(1, 5):
                hooks.ingest_turn(state, f"u{n}", f"a{n}", session_id="telegram:42")
                clock.advance(60)
            sid_before = state.session_id
            clock.advance(2000)
            hooks.ingest_turn(state, "next morning", "hi again", session_id="telegram:42")

        assert len(rec.crystals) == 1
        assert rec.crystals[0] == (sid_before, ("u1", "a1", "u2", "a2", "u3", "a3", "u4", "a4"))
        assert state.session_id == sid_before
        assert [m["content"] for m in state.get_all_messages()] == ["next morning", "hi again"]

    def test_no_crystal_without_an_idle_gap(self, monkeypatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")
        state = _configured_state()
        rec = _DebriefRecorder()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=rec):
            for n in range(1, 7):
                hooks.ingest_turn(state, f"u{n}", f"a{n}", session_id="telegram:42")
                clock.advance(120)
        assert rec.crystals == []
        assert len(state.get_all_messages()) == 12

    def test_provider_mode_post_llm_call_keeps_the_idle_gap(self, monkeypatch) -> None:
        """With TotalReclaw as the active provider, ``post_llm_call`` returns early
        and ``sync_turn`` records the turn. The hook must not refresh the clock
        first, or the gap is never seen."""
        clock = _Clock()
        monkeypatch.setattr(state_mod.time, "monotonic", clock)
        monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "15")
        state = _configured_state()
        state._provider_active = True
        rec = _DebriefRecorder()
        with patch.object(hooks, "_auto_extract", return_value=[]), \
             patch.object(hooks, "_session_debrief", side_effect=rec), \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False):
            for n in range(1, 5):
                hooks.ingest_turn(state, f"u{n}", f"a{n}", session_id="telegram:42")
            clock.advance(2000)
            hooks.post_llm_call(state, user_message="later", assistant_response="ok", session_id="telegram:42")
            hooks.ingest_turn(state, "later", "ok", session_id="telegram:42")
        assert len(rec.crystals) == 1
