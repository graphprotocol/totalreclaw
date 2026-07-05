"""§5.1 — hook-free shared memory entry points (provider conformance, #351).

``recall_for_query`` and ``ingest_turn`` are the single code path that both the
Hermes lifecycle hooks (``pre_llm_call`` / ``post_llm_call``) and the future
``MemoryProvider`` (``prefetch`` / ``sync_turn``, §5.2) call. These tests lock
the contract so §5.2 can depend on them, and assert ``post_llm_call`` is now a
thin wrapper over ``ingest_turn``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from totalreclaw.hermes import hooks


def _state(*, configured=True, turn_count=3, interval=3) -> MagicMock:
    s = MagicMock()
    s.is_configured.return_value = configured
    s.turn_count = turn_count
    s.get_extraction_interval.return_value = interval
    return s


class TestRecallForQuery:
    def test_delegates_to_auto_recall(self):
        s = _state()
        with patch.object(hooks, "auto_recall", return_value="CTX") as ar:
            out = hooks.recall_for_query(s, "who am I?", top_k=8)
        assert out == "CTX"
        ar.assert_called_once_with("who am I?", s, top_k=8)


class TestIngestTurn:
    def test_always_records_the_turn(self):
        s = _state(configured=False)  # even unconfigured, bookkeeping runs
        hooks.ingest_turn(s, "u", "a")
        s.increment_turn.assert_called_once()
        assert s.add_message.call_count == 2
        s.add_message.assert_any_call("user", "u")
        s.add_message.assert_any_call("assistant", "a")
        s.track_pending_extract.assert_called_once_with("u")

    def test_unconfigured_skips_extract(self):
        s = _state(configured=False)
        with patch.object(hooks, "_auto_extract") as ax:
            hooks.ingest_turn(s, "u", "a")
        ax.assert_not_called()

    def test_off_interval_skips_extract(self):
        s = _state(configured=True, turn_count=3, interval=5)  # 3 % 5 != 0
        with patch.object(hooks, "_auto_extract") as ax:
            hooks.ingest_turn(s, "u", "a")
        ax.assert_not_called()

    def test_on_interval_runs_extract_with_resolved_config(self):
        s = _state(configured=True, turn_count=6, interval=3)  # 6 % 3 == 0
        sentinel = object()
        with patch.object(hooks, "_resolve_extraction_llm_config", return_value=sentinel), \
             patch.object(hooks, "_auto_extract") as ax:
            hooks.ingest_turn(s, "u", "a")
        ax.assert_called_once_with(s, mode="turn", llm_config=sentinel)

    def test_extract_exception_is_swallowed(self):
        s = _state(configured=True, turn_count=3, interval=3)
        with patch.object(hooks, "_resolve_extraction_llm_config", return_value=None), \
             patch.object(hooks, "_auto_extract", side_effect=RuntimeError("boom")):
            hooks.ingest_turn(s, "u", "a")  # must not raise


class TestPostLlmCallDelegates:
    def test_post_llm_call_reconfigures_then_ingests(self):
        s = _state()
        with patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False) as rc, \
             patch.object(hooks, "ingest_turn") as it:
            hooks.post_llm_call(s, user_message="u", assistant_response="a")
        rc.assert_called_once_with(s)
        # session_id forwarded (empty here — no per-conversation id supplied).
        it.assert_called_once_with(s, "u", "a", session_id="")

    def test_post_llm_call_eager_register_on_midsession_pair(self):
        s = _state()
        with patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=True), \
             patch.object(hooks, "_eager_account_register") as er, \
             patch.object(hooks, "ingest_turn"):
            hooks.post_llm_call(s, user_message="u", assistant_response="a")
        er.assert_called_once_with(s)
