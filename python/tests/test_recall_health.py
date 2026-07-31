"""Tests for the recall-health fail-loud path (internal#486 follow-up).

Covers:
- ``totalreclaw.recall_health`` gating (kill-switch, actionable statuses,
  empty-vault suppression, 24h rate-limit).
- ``agent.recall.auto_recall_with_status`` status classification
  (unconfigured / ok / empty / error) and ``auto_recall`` delegation.
- ``hermes.hooks.recall_for_query`` wiring: a broken read path injects the
  diagnostic notice exactly once per 24h; a healthy path is untouched.
- ``totalreclaw_status`` tool surfacing ``update_available`` from the relay
  billing features (with PyPI fallback resolution mocked out).
"""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from totalreclaw import recall_health as rh
from totalreclaw import update_notice as un


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    # recall_health shares update_notice's sentinel dir; redirect both.
    monkeypatch.setattr(un, "_STATE_DIR", tmp_path / ".totalreclaw")
    monkeypatch.delenv("TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE", raising=False)
    monkeypatch.delenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", raising=False)


# ── recall_health gating ─────────────────────────────────────────────────
class TestMaybeBuildRecallFailureNotice:
    def test_error_fires_even_without_billing(self):
        notice = rh.maybe_build_recall_failure_notice("error", None)
        assert notice is not None
        assert "raised an error" in notice
        assert "doctor --recall-smoke" in notice

    def test_empty_with_writes_fires_and_mentions_count(self):
        notice = rh.maybe_build_recall_failure_notice("empty", 157)
        assert notice is not None
        assert "157" in notice

    def test_empty_without_writes_is_silent(self):
        assert rh.maybe_build_recall_failure_notice("empty", 0) is None
        assert rh.maybe_build_recall_failure_notice("empty", None) is None

    def test_ok_and_unconfigured_never_fire(self):
        assert rh.maybe_build_recall_failure_notice("ok", 157) is None
        assert rh.maybe_build_recall_failure_notice("unconfigured", 157) is None

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("TOTALRECLAW_DISABLE_RECALL_HEALTH_NOTICE", "1")
        assert rh.maybe_build_recall_failure_notice("error", 157) is None

    def test_rate_limited_after_mark(self):
        assert rh.maybe_build_recall_failure_notice("error", None) is not None
        rh.mark_notified()
        assert rh.maybe_build_recall_failure_notice("error", None) is None

    def test_window_reopens_after_24h(self):
        now = time.time()
        rh.mark_notified(now=now - (un.NOTICE_INTERVAL_SECONDS + 10))
        assert rh.maybe_build_recall_failure_notice("error", None, now=now) is not None

    def test_does_not_share_window_with_update_notice(self):
        # Marking the UPDATE notice must not suppress the recall-health one.
        un.mark_notified()
        assert rh.maybe_build_recall_failure_notice("error", None) is not None


# ── auto_recall_with_status classification ───────────────────────────────
class TestAutoRecallWithStatus:
    def _state(self, client):
        state = MagicMock()
        state.is_configured.return_value = True
        state.get_client.return_value = client
        state.get_max_candidate_pool.return_value = 250
        return state

    def test_unconfigured(self):
        from totalreclaw.agent.recall import auto_recall_with_status

        state = MagicMock()
        state.is_configured.return_value = False
        assert auto_recall_with_status("q", state) == (None, "unconfigured")

    def test_no_client(self):
        from totalreclaw.agent.recall import auto_recall_with_status

        state = MagicMock()
        state.is_configured.return_value = True
        state.get_client.return_value = None
        assert auto_recall_with_status("q", state) == (None, "unconfigured")

    def test_empty_query(self):
        from totalreclaw.agent.recall import auto_recall_with_status

        state = MagicMock()
        assert auto_recall_with_status("", state) == (None, "unconfigured")

    def test_error_status_on_raise(self):
        from totalreclaw.agent import recall as recall_mod

        state = self._state(MagicMock())

        def boom(coro):
            coro.close()
            raise RuntimeError("relay down")

        with patch.object(recall_mod, "run_sync", side_effect=boom):
            assert recall_mod.auto_recall_with_status("q", state) == (None, "error")

    def test_empty_status_on_zero_results(self):
        from totalreclaw.agent import recall as recall_mod

        state = self._state(MagicMock())

        def empty(coro):
            coro.close()
            return []

        with patch.object(recall_mod, "run_sync", side_effect=empty):
            assert recall_mod.auto_recall_with_status("q", state) == (None, "empty")

    def test_ok_status_formats_context(self):
        from totalreclaw.agent import recall as recall_mod

        state = self._state(MagicMock())
        result = MagicMock()
        result.category = "claim"
        result.text = "user prefers dark mode"
        result.created_at = None

        def one(coro):
            coro.close()
            return [result]

        with patch.object(recall_mod, "run_sync", side_effect=one):
            ctx, status = recall_mod.auto_recall_with_status("q", state)
        assert status == "ok"
        assert ctx is not None and "dark mode" in ctx

    def test_auto_recall_delegates(self):
        from totalreclaw.agent import recall as recall_mod

        with patch.object(
            recall_mod, "auto_recall_with_status", return_value=("CTX", "ok")
        ):
            assert recall_mod.auto_recall("q", MagicMock()) == "CTX"


# ── recall_for_query fail-loud wiring ────────────────────────────────────
class TestRecallForQueryFailLoud:
    def _state(self, billing=None):
        state = MagicMock()
        state.get_cached_billing.return_value = billing
        return state

    def test_healthy_path_passes_context_through(self):
        from totalreclaw.hermes import hooks

        with patch.object(
            hooks, "auto_recall_with_status", return_value=("## Memories", "ok")
        ):
            assert hooks.recall_for_query(self._state(), "q") == "## Memories"

    def test_empty_with_writes_injects_notice_once(self):
        from totalreclaw.hermes import hooks

        state = self._state({"free_writes_used": 157})
        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "empty")
        ):
            first = hooks.recall_for_query(state, "q")
            second = hooks.recall_for_query(state, "q")
        assert first is not None and "[totalreclaw]" in first and "157" in first
        assert second is None  # 24h rate-limit

    def test_error_injects_notice_without_billing(self):
        from totalreclaw.hermes import hooks

        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "error")
        ):
            result = hooks.recall_for_query(self._state(None), "q")
        assert result is not None and "[totalreclaw]" in result

    def test_empty_vault_stays_silent(self):
        from totalreclaw.hermes import hooks

        state = self._state({"free_writes_used": 0})
        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "empty")
        ):
            assert hooks.recall_for_query(state, "q") is None

    def test_unconfigured_stays_silent(self):
        from totalreclaw.hermes import hooks

        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "unconfigured")
        ):
            assert hooks.recall_for_query(self._state(), "q") is None

    def test_provider_prefetch_surfaces_notice(self):
        # MemoryProvider.prefetch delegates to recall_for_query, so a broken
        # read path must surface through the native-provider driver too.
        from totalreclaw.hermes import hooks
        from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider

        provider = TotalReclawMemoryProvider.__new__(TotalReclawMemoryProvider)
        state = self._state({"free_writes_used": 42})
        state.is_configured.return_value = True
        state.get_recall_top_k.return_value = 16
        provider._state = state

        with patch.object(
            hooks, "auto_recall_with_status", return_value=(None, "empty")
        ):
            out = provider.prefetch("q")
        assert "[totalreclaw]" in out and "42" in out


# ── totalreclaw_status update surfacing ──────────────────────────────────
class TestStatusUpdateSurfacing:
    def _run_status(self, latest_stable_python, installed="2.4.6"):
        from totalreclaw.hermes import tools

        features = MagicMock()
        features.latest_stable_python = latest_stable_python
        billing = MagicMock()
        billing.tier = "pro"
        billing.free_writes_used = 10
        billing.free_writes_limit = 1500
        billing.expires_at = None
        billing.features = features

        client = MagicMock()

        async def fake_status():
            return billing

        client.status = fake_status
        client.resolved_wallet_address = "0xabc"
        client.eoa_address = "0xeoa"

        state = MagicMock()
        state.get_client.return_value = client

        with patch("totalreclaw.__version__", installed):
            # Keep the test hermetic: no PyPI fallback network call.
            with patch(
                "totalreclaw.update_notice.fetch_latest_stable_from_pypi",
                return_value=None,
            ):
                raw = asyncio.run(tools.status({}, state))
        return json.loads(raw)

    def test_update_available_true(self):
        payload = self._run_status("2.4.7", installed="2.4.6")
        assert payload["latest_stable"] == "2.4.7"
        assert payload["latest_stable_source"] == "relay"
        assert payload["update_available"] is True
        assert "pip install --upgrade totalreclaw" in payload["update_hint"]

    def test_update_available_false_when_current(self):
        payload = self._run_status("2.4.6", installed="2.4.6")
        assert payload["update_available"] is False
        assert "update_hint" not in payload

    def test_rc_older_than_stable_flags_update(self):
        payload = self._run_status("2.4.7", installed="2.4.6rc7")
        assert payload["update_available"] is True

    def test_relay_dark_and_no_fallback_omits_fields(self):
        payload = self._run_status(None)
        assert "latest_stable" not in payload
        assert "update_available" not in payload
        assert payload["installed_version"]

    def test_status_core_fields_untouched(self):
        payload = self._run_status("2.4.7")
        assert payload["tier"] == "pro"
        assert payload["free_writes_used"] == 10
        assert payload["wallet_address"] == "0xabc"
