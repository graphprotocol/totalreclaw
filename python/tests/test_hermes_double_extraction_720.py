"""internal#720 — two provider instances must not extract the same turn twice."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import totalreclaw.hermes.state as state_mod
from totalreclaw.hermes import hooks
from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider
from totalreclaw.hermes.state import PluginState


def _fresh():
    hooks._recent_turns.clear()
    state_mod._SHARED_STATE = None
    state = PluginState()
    state_mod._SHARED_STATE = state
    return state


def test_two_provider_instances_extract_a_turn_once():
    state = _fresh()
    p1, p2 = TotalReclawMemoryProvider(), TotalReclawMemoryProvider()
    assert p1._state is p2._state and p1._instance_id != p2._instance_id
    with patch.object(PluginState, "is_configured", return_value=True), \
         patch.object(PluginState, "get_extraction_interval", return_value=1), \
         patch.object(hooks, "_resolve_extraction_llm_config", return_value=None), \
         patch.object(hooks, "_auto_extract") as extract:
        p1.sync_turn("I live in Lisbon", "Noted.", session_id="s1")
        p2.sync_turn("I live in Lisbon", "Noted.", session_id="s1")
    assert extract.call_count == 1
    assert state.turn_count == 1


def test_distinct_turns_and_sessions_still_extract():
    state = _fresh()
    with patch.object(PluginState, "is_configured", return_value=True), \
         patch.object(PluginState, "get_extraction_interval", return_value=1), \
         patch.object(hooks, "_resolve_extraction_llm_config", return_value=None), \
         patch.object(hooks, "_auto_extract") as extract:
        hooks.ingest_turn(state, "a", "b", session_id="s1")
        hooks.ingest_turn(state, "c", "d", session_id="s1")
        hooks.ingest_turn(state, "c", "d", session_id="s2")
    assert extract.call_count == 3


def test_repeat_after_window_is_not_suppressed():
    _fresh()
    assert hooks._is_duplicate_turn("s", "u", "a") is False
    assert hooks._is_duplicate_turn("s", "u", "a") is True
    with patch.object(hooks, "_now", return_value=hooks._now() + 60):
        assert hooks._is_duplicate_turn("s", "u", "a") is False


def test_provider_logs_instance_id(caplog):
    _fresh()
    with caplog.at_level(logging.INFO, logger="totalreclaw.hermes.memory_provider"):
        p = TotalReclawMemoryProvider()
        p.initialize("sess-abc")
    text = caplog.text
    assert p._instance_id in text and "initialize" in text
