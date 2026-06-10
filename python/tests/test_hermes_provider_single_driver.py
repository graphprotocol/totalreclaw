"""§5.3 (#351) — single-driver: shared state + hook gating.

When TotalReclaw is the active Hermes MemoryProvider, the provider owns
auto-recall (``prefetch``) + auto-extract (``sync_turn``); the lifecycle hooks
must defer (no double-fire). The provider and the entry-point plugin must also
share ONE ``PluginState``. These tests lock both.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import totalreclaw.hermes.state as state_mod
from totalreclaw.hermes import hooks
from totalreclaw.hermes.state import get_shared_state
from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider


def _reset_shared():
    state_mod._SHARED_STATE = None


class TestSharedState:
    def test_get_shared_state_is_singleton(self):
        _reset_shared()
        assert get_shared_state() is get_shared_state()

    def test_provider_default_uses_shared_state(self):
        _reset_shared()
        provider = TotalReclawMemoryProvider()
        assert provider._state is get_shared_state()

    def test_explicit_state_overrides_shared(self):
        _reset_shared()
        custom = MagicMock()
        provider = TotalReclawMemoryProvider(custom)
        assert provider._state is custom

    def test_initialize_sets_provider_active_on_shared_state(self):
        _reset_shared()
        provider = TotalReclawMemoryProvider()
        provider.initialize(session_id="s1")
        assert get_shared_state()._provider_active is True


def _gate_state(provider_active) -> MagicMock:
    s = MagicMock()
    s.is_configured.return_value = True
    s._provider_active = provider_active  # real bool, not an auto-Mock
    s.turn_count = 1
    s.get_extraction_interval.return_value = 1
    s.get_quota_warning.return_value = None
    return s


class TestPreLlmCallGate:
    def test_skips_recall_when_provider_active(self):
        s = _gate_state(True)
        with patch.object(hooks, "recall_for_query") as rq, \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False):
            hooks.pre_llm_call(s, user_message="who am I?", is_first_turn=True)
        rq.assert_not_called()

    def test_recalls_when_not_provider_active(self):
        s = _gate_state(False)
        with patch.object(hooks, "recall_for_query", return_value="") as rq, \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False):
            hooks.pre_llm_call(s, user_message="who am I?", is_first_turn=True)
        rq.assert_called_once()

    def test_recalls_when_flag_is_a_mock_not_true(self):
        # Defensive: an auto-created MagicMock attr must NOT trip the gate
        # (gate is ``is True``), so legacy/mocked states still recall.
        s = MagicMock()
        s.is_configured.return_value = True
        s.turn_count = 1
        s.get_extraction_interval.return_value = 1
        s.get_quota_warning.return_value = None
        # note: s._provider_active is an auto-Mock (truthy, but not ``is True``)
        with patch.object(hooks, "recall_for_query", return_value="") as rq, \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False):
            hooks.pre_llm_call(s, user_message="who am I?", is_first_turn=True)
        rq.assert_called_once()


class TestPostLlmCallGate:
    def test_skips_ingest_when_provider_active_but_keeps_reconfigure(self):
        s = _gate_state(True)
        with patch.object(hooks, "ingest_turn") as it, \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False) as rc:
            hooks.post_llm_call(s, user_message="u", assistant_response="a")
        it.assert_not_called()
        rc.assert_called_once_with(s)  # reconfigure still runs (provider doesn't)

    def test_ingests_when_not_provider_active(self):
        s = _gate_state(False)
        with patch.object(hooks, "ingest_turn") as it, \
             patch.object(hooks, "_maybe_reconfigure_from_disk", return_value=False):
            hooks.post_llm_call(s, user_message="u", assistant_response="a")
        it.assert_called_once_with(s, "u", "a")
