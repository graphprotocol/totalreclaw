"""2.4.4rc2 — structural enforcement of SKILL.md rules.

Tests cover the F6 (debrief intent + nudge) + F7 (manual remember
suppression vs auto-extract) findings from the 2.4.4rc1 auto-QA NO-GO
verdict. Full spec: ``plans/2026-05-29-skill-md-enforcement-hooks.md``
in the internal repo.

F5 already shipped via [PR #285](https://github.com/p-diogo/totalreclaw/pull/285)
(test file: ``test_remember_self_directive_filter.py``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# F7 — AgentState pending-extract buffer + suppression
# ---------------------------------------------------------------------------


class TestF7PendingExtractBuffer:
    """The post_llm_call hook tracks every turn's user message in a
    bounded buffer. The manual_remember_is_dup_of_pending() helper
    consults the buffer; totalreclaw_remember calls it before storage.
    """

    def _make_state(self):
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                return AgentState()

    def test_track_pending_extract_appends_entry(self):
        state = self._make_state()
        state.increment_turn()
        state.track_pending_extract("I prefer Postgres over MySQL")
        assert len(state._pending_extract_buffer) == 1
        assert state._pending_extract_buffer[0]["turn"] == 1
        assert "postgres" in state._pending_extract_buffer[0]["text_normalized"]

    def test_track_pending_extract_skips_empty(self):
        state = self._make_state()
        state.track_pending_extract("")
        state.track_pending_extract("   ")
        state.track_pending_extract(None)  # type: ignore[arg-type]
        assert len(state._pending_extract_buffer) == 0

    def test_buffer_bounded_to_10_entries(self):
        state = self._make_state()
        for i in range(15):
            state.increment_turn()
            state.track_pending_extract(f"message number {i}")
        assert len(state._pending_extract_buffer) == 10
        # The most recent 10 (i=5..14) should be kept.
        last_text = state._pending_extract_buffer[-1]["text"]
        assert "14" in last_text

    def test_manual_remember_is_dup_substring_containment(self):
        """Manual remember text contained in a recent user message
        triggers suppression. v1 heuristic catches the common case
        (agent extracts a literal sub-statement from the user's
        message). Paraphrase + tense-change isn't caught by the
        substring check — that's handled by embedding dedup at
        storage time."""
        state = self._make_state()
        state.increment_turn()  # turn 1
        state.track_pending_extract(
            "Hi! My name is Pedro and I work as a software engineer in Porto, Portugal."
        )
        # Agent extracts a literal sub-statement from the user's words:
        assert state.manual_remember_is_dup_of_pending(
            "I work as a software engineer in Porto"
        ) is True

    def test_manual_remember_is_dup_full_message_echo(self):
        """Agent echoing the user's message verbatim → suppressed."""
        state = self._make_state()
        state.increment_turn()
        state.track_pending_extract("I prefer Postgres over MySQL")
        # Verbatim echo:
        assert state.manual_remember_is_dup_of_pending(
            "I prefer Postgres over MySQL"
        ) is True

    def test_manual_remember_is_NOT_dup_when_unrelated(self):
        state = self._make_state()
        state.increment_turn()
        state.track_pending_extract("I love espresso")
        # Unrelated:
        assert state.manual_remember_is_dup_of_pending(
            "Pedro's birthday is March 14"
        ) is False

    def test_manual_remember_is_NOT_dup_when_outside_lookback(self):
        """Entries older than `lookback_turns` are ignored."""
        state = self._make_state()
        state.increment_turn()
        state.track_pending_extract("I love espresso")  # turn 1
        # Advance 5 turns without new entries.
        for _ in range(5):
            state.increment_turn()
        # Default lookback is 3 turns — turn 1 is now 5 turns ago.
        assert state.manual_remember_is_dup_of_pending(
            "espresso", lookback_turns=3
        ) is False
        # But a wider lookback catches it:
        assert state.manual_remember_is_dup_of_pending(
            "espresso", lookback_turns=10
        ) is True

    def test_increment_suppressed_writes_counter(self):
        state = self._make_state()
        assert state.get_suppressed_writes_count() == 0
        state.increment_suppressed_writes()
        state.increment_suppressed_writes()
        assert state.get_suppressed_writes_count() == 2

    def test_clear_pending_extract_buffer_resets_both(self):
        state = self._make_state()
        state.increment_turn()
        state.track_pending_extract("entry")
        state.increment_suppressed_writes()
        state.clear_pending_extract_buffer()
        assert len(state._pending_extract_buffer) == 0
        assert state.get_suppressed_writes_count() == 0


class TestF7NormalizationHelper:
    """The `_normalize_for_dedup` helper lowercases + strips punctuation
    + collapses whitespace. Output must be deterministic + handle the
    common QA-failure patterns."""

    def test_normalize_lowercases(self):
        from totalreclaw.agent.state import _normalize_for_dedup
        assert _normalize_for_dedup("HELLO World") == "hello world"

    def test_normalize_strips_punctuation(self):
        from totalreclaw.agent.state import _normalize_for_dedup
        assert _normalize_for_dedup("Hi! My name is Pedro.") == "hi my name is pedro"

    def test_normalize_collapses_whitespace(self):
        from totalreclaw.agent.state import _normalize_for_dedup
        assert _normalize_for_dedup("   spaced   out   ") == "spaced out"

    def test_normalize_empty(self):
        from totalreclaw.agent.state import _normalize_for_dedup
        assert _normalize_for_dedup("") == ""
        assert _normalize_for_dedup(None) == ""  # type: ignore[arg-type]


class TestF7PostLlmCallTracksPending:
    """Verify the `post_llm_call` hook actually calls
    `track_pending_extract` with the turn's user message."""

    def test_post_llm_call_tracks_user_message(self):
        from totalreclaw.hermes.hooks import post_llm_call
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = AgentState()
        post_llm_call(
            state,
            user_message="I love Lisbon",
            assistant_response="Got it.",
        )
        assert len(state._pending_extract_buffer) == 1
        assert "lisbon" in state._pending_extract_buffer[0]["text_normalized"]

    def test_post_llm_call_skips_empty_user_message(self):
        from totalreclaw.hermes.hooks import post_llm_call
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = AgentState()
        post_llm_call(state, user_message="", assistant_response="hi")
        assert len(state._pending_extract_buffer) == 0


# ---------------------------------------------------------------------------
# F6 — debrief intent detection + pre_llm_call nudge
# ---------------------------------------------------------------------------


class TestF6DebriefIntentDetection:
    """Three trigger phrases the QA reproduction used must all match.
    Negative gates ("summary of the code") must NOT match.
    """

    def test_intent_matches_summarize_what_we_discussed(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent(
            "Give me a summary of what we discussed this session."
        ) is True

    def test_intent_matches_debrief_phrase(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent(
            "give me a debrief on this session"
        ) is True

    def test_intent_matches_rolling_memory_phrase(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent(
            "what's the rolling memory of this chat?"
        ) is True

    def test_intent_negative_gate_summary_of_code(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent(
            "Give me a summary of the code I just pasted."
        ) is False

    def test_intent_negative_gate_summary_of_doc(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent(
            "Summary of the doc please."
        ) is False

    def test_intent_skips_unrelated_message(self):
        from totalreclaw.hermes.hooks import _detect_debrief_intent
        assert _detect_debrief_intent("Hi how are you") is False
        assert _detect_debrief_intent("") is False
        assert _detect_debrief_intent(None) is False  # type: ignore[arg-type]


class TestF6DebriefNudgeInjection:
    """When intent fires, pre_llm_call must include the debrief nudge in
    its context return value. Latch prevents same-turn re-injection."""

    def _make_configured_state(self):
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = AgentState()
        state._client = MagicMock()  # forge configured
        return state

    def test_pre_llm_call_injects_debrief_nudge_on_match(self):
        from totalreclaw.hermes.hooks import pre_llm_call
        state = self._make_configured_state()
        state._turn_count = 5
        with patch("totalreclaw.hermes.hooks.auto_recall", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = pre_llm_call(
                    state,
                    is_first_turn=False,
                    user_message="Give me a summary of what we discussed this session.",
                )
        assert result is not None
        ctx = result.get("context", "")
        # Canonical nudge markers.
        assert "user requested session debrief" in ctx.lower()
        assert "totalreclaw_debrief" in ctx
        assert "MUST call" in ctx

    def test_pre_llm_call_no_debrief_nudge_when_intent_absent(self):
        from totalreclaw.hermes.hooks import pre_llm_call
        state = self._make_configured_state()
        with patch("totalreclaw.hermes.hooks.auto_recall", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                result = pre_llm_call(
                    state,
                    is_first_turn=False,
                    user_message="What's the weather like?",
                )
        if result is not None:
            assert "user requested session debrief" not in result.get("context", "").lower()

    def test_pre_llm_call_debrief_nudge_latched_per_turn(self):
        """Same turn → no re-injection. Different turn → re-injects."""
        from totalreclaw.hermes.hooks import pre_llm_call
        state = self._make_configured_state()
        state._turn_count = 5
        with patch("totalreclaw.hermes.hooks.auto_recall", return_value=None):
            with patch.object(Path, "exists", return_value=False):
                first = pre_llm_call(
                    state, is_first_turn=False,
                    user_message="summarize what we discussed",
                )
                # Same turn — re-call should not re-inject.
                second = pre_llm_call(
                    state, is_first_turn=False,
                    user_message="summarize what we discussed",
                )
        assert first is not None
        assert "user requested session debrief" in first.get("context", "").lower()
        # Second call may return None OR a context without the debrief block.
        if second is not None:
            assert "user requested session debrief" not in second.get("context", "").lower()


class TestF6SessionStartResetsDebriefLatch:
    """on_session_start clears the per-turn latch + skip counter so the
    next session can independently nudge."""

    def test_on_session_start_resets_debrief_state(self):
        from totalreclaw.hermes.hooks import on_session_start
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = AgentState()
        state._totalreclaw_debrief_nudge_turn = 7
        state._totalreclaw_debrief_skip_count = 2
        on_session_start(state, session_id="new-session-id")
        assert state._totalreclaw_debrief_nudge_turn == -1
        assert state._totalreclaw_debrief_skip_count == 0


class TestF7SessionStartResetsBuffer:
    """on_session_start clears the pending-extract buffer."""

    def test_on_session_start_clears_pending_buffer(self):
        from totalreclaw.hermes.hooks import on_session_start
        from totalreclaw.agent.state import AgentState
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = AgentState()
        state.increment_turn()
        state.track_pending_extract("stale entry from prior session")
        state.increment_suppressed_writes()
        on_session_start(state, session_id="new-id")
        assert len(state._pending_extract_buffer) == 0
        assert state.get_suppressed_writes_count() == 0
