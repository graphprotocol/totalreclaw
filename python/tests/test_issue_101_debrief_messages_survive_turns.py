"""Regression test for issue #101.

hermes_cli dispatches ``on_session_end`` at the end of every
``run_conversation()`` (i.e. once per user turn), NOT at true session end.
The plugin's ``on_session_end`` handler used to do a comprehensive flush +
``state.clear_messages()`` in its ``finally`` block, which wiped the
plugin message buffer after every turn. Result: ``totalreclaw_debrief``
(and the on-finalize session debrief) always saw <8 messages even in
long sessions and returned ``{"skipped": true, "session too short"}``.

This test pins the fix: after 10 turns of simulated hermes_cli dispatch
(post_llm_call + on_session_end per turn), the message buffer must
contain >= 8 messages so the debrief guard does not trip. Clearing only
happens at true session boundaries (``on_session_finalize`` /
``on_session_reset``).
"""
from __future__ import annotations

import pytest

from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes import hooks as hhooks


class _FakeState(PluginState):
    """PluginState that skips real client auto-configure (no creds on disk)."""

    def __init__(self):
        self._client = object()
        self._turn_count = 0
        self._messages = []
        self._last_processed_idx = 0
        self._billing_cache = None
        self._billing_cache_time = 0.0
        self._extraction_interval = 3
        self._max_facts = 15
        self._min_importance = 6
        self._quota_warning = None
        self._server_url = None
        self._env_interval_override = False
        self._env_importance_override = False

    def is_configured(self):
        return True


def _simulate_turn(state, i):
    """Mirror hermes_cli run_agent.py dispatch: post_llm_call then
    on_session_end fire once per ``run_conversation()`` call."""
    hhooks.post_llm_call(
        state,
        session_id="s",
        model="m",
        platform="cli",
        user_message=f"user msg turn {i}",
        assistant_response=f"assistant msg turn {i}",
        conversation_history=[],
    )
    hhooks.on_session_end(
        state,
        session_id="s",
        completed=True,
        interrupted=False,
        model="m",
        platform="cli",
    )


def test_issue_101_messages_survive_per_turn_dispatch():
    state = _FakeState()
    for i in range(1, 11):
        _simulate_turn(state, i)

    all_messages = state.get_all_messages()
    assert len(all_messages) >= 8, (
        "Per-turn on_session_end must not wipe the plugin message buffer — "
        "totalreclaw_debrief's <8 guard depends on the 10-turn history "
        "surviving across run_conversation() dispatches. "
        f"Got len={len(all_messages)}."
    )


def test_issue_101_finalize_clears_buffer():
    """Confirm the new on_session_finalize hook still performs the
    session-end cleanup that was wrongly in on_session_end."""
    state = _FakeState()
    for i in range(1, 11):
        _simulate_turn(state, i)

    assert len(state.get_all_messages()) >= 8
    hhooks.on_session_finalize(
        state,
        session_id="s",
        completed=True,
        interrupted=False,
    )
    assert state.get_all_messages() == []


def test_issue_101_reset_clears_buffer_and_turn_counter():
    state = _FakeState()
    for i in range(1, 4):
        _simulate_turn(state, i)

    assert state.turn_count > 0
    assert len(state.get_all_messages()) > 0

    hhooks.on_session_reset(state, session_id="s")
    assert state.get_all_messages() == []
    assert state.turn_count == 0
