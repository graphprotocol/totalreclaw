"""Tests for TotalReclaw Hermes plugin."""
import json
import os
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from totalreclaw.hermes.state import (
    PluginState, BILLING_CACHE_TTL,
    DEFAULT_EXTRACTION_INTERVAL, DEFAULT_MAX_FACTS, DEFAULT_MIN_IMPORTANCE,
)
from totalreclaw.hermes import schemas


def _make_state(**env_overrides):
    """Helper to create a PluginState with clean env and no credentials file."""
    with patch.dict(os.environ, env_overrides, clear=True):
        with patch.object(Path, "exists", return_value=False):
            return PluginState()


class TestPluginState:
    def test_initial_state(self):
        state = _make_state()
        assert not state.is_configured()
        assert state.turn_count == 0

    def test_configure(self):
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        state = _make_state()
        with patch.object(Path, "write_text"), patch.object(Path, "chmod"), \
             patch.object(Path, "mkdir", return_value=None):
            state.configure(mnemonic)
        assert state.is_configured()
        assert state.get_client() is not None

    def test_turn_tracking(self):
        state = _make_state()
        state.increment_turn()
        state.increment_turn()
        assert state.turn_count == 2
        state.reset_turn_counter()
        assert state.turn_count == 0

    def test_message_buffer(self):
        state = _make_state()
        state.add_message("user", "hello")
        state.add_message("assistant", "hi")
        assert len(state.get_unprocessed_messages()) == 2
        assert state.has_unprocessed_messages()
        state.mark_messages_processed()
        assert not state.has_unprocessed_messages()
        assert len(state.get_unprocessed_messages()) == 0

    def test_clear_messages(self):
        state = _make_state()
        state.add_message("user", "test")
        state.clear_messages()
        assert len(state.get_unprocessed_messages()) == 0

    def test_billing_cache(self):
        state = _make_state()
        assert state.get_cached_billing() is None
        state.set_billing_cache({"tier": "free", "free_writes_used": 10, "free_writes_limit": 500})
        cached = state.get_cached_billing()
        assert cached["tier"] == "free"

    def test_billing_cache_ttl_is_2_hours(self):
        """Billing cache TTL should be 7200 seconds (2 hours)."""
        assert BILLING_CACHE_TTL == 7200

    def test_extraction_config_defaults(self):
        state = _make_state()
        assert state.get_extraction_interval() == DEFAULT_EXTRACTION_INTERVAL
        assert state.get_max_facts_per_extraction() == DEFAULT_MAX_FACTS
        assert state.get_min_importance() == DEFAULT_MIN_IMPORTANCE

    def test_update_from_billing_sets_server_config(self):
        """Server-driven extraction config from billing features dict."""
        state = _make_state()
        billing = {
            "features": {
                "extraction_interval": 5,
                "max_facts_per_extraction": 20,
            }
        }
        state.update_from_billing(billing)
        assert state.get_extraction_interval() == 5
        assert state.get_max_facts_per_extraction() == 20

    def test_update_from_billing_no_features(self):
        """update_from_billing with no features dict keeps defaults."""
        state = _make_state()
        state.update_from_billing({})
        assert state.get_extraction_interval() == DEFAULT_EXTRACTION_INTERVAL
        assert state.get_max_facts_per_extraction() == DEFAULT_MAX_FACTS

    def test_update_from_billing_partial_features(self):
        """Partial features only updates present keys."""
        state = _make_state()
        state.update_from_billing({"features": {"extraction_interval": 7}})
        assert state.get_extraction_interval() == 7
        assert state.get_max_facts_per_extraction() == DEFAULT_MAX_FACTS

    def test_update_from_billing_invalid_types(self):
        """Invalid types in features are ignored gracefully."""
        state = _make_state()
        state.update_from_billing({"features": {"extraction_interval": "not_a_number"}})
        assert state.get_extraction_interval() == DEFAULT_EXTRACTION_INTERVAL

    def test_env_var_overrides_extraction_interval(self):
        """TOTALRECLAW_EXTRACT_INTERVAL env var overrides default and server config."""
        state = _make_state(TOTALRECLAW_EXTRACT_INTERVAL="10")
        assert state.get_extraction_interval() == 10

        # Server config should NOT override env var
        state.update_from_billing({"features": {"extraction_interval": 5}})
        assert state.get_extraction_interval() == 10

    def test_env_var_overrides_min_importance(self):
        """TOTALRECLAW_MIN_IMPORTANCE env var overrides default."""
        state = _make_state(TOTALRECLAW_MIN_IMPORTANCE="8")
        assert state.get_min_importance() == 8

    def test_env_var_invalid_value_keeps_default(self):
        """Invalid env var values are ignored, defaults kept."""
        state = _make_state(TOTALRECLAW_EXTRACT_INTERVAL="abc", TOTALRECLAW_MIN_IMPORTANCE="xyz")
        assert state.get_extraction_interval() == DEFAULT_EXTRACTION_INTERVAL
        assert state.get_min_importance() == DEFAULT_MIN_IMPORTANCE

    def test_env_var_does_not_block_max_facts_from_server(self):
        """TOTALRECLAW_EXTRACT_INTERVAL env var only blocks interval, not max_facts."""
        state = _make_state(TOTALRECLAW_EXTRACT_INTERVAL="10")
        state.update_from_billing({"features": {"max_facts_per_extraction": 25}})
        assert state.get_extraction_interval() == 10  # env wins
        assert state.get_max_facts_per_extraction() == 25  # server applied

    def test_quota_warning_lifecycle(self):
        """Quota warning set/get/clear lifecycle."""
        state = _make_state()
        assert state.get_quota_warning() is None

        state.set_quota_warning("80% used")
        assert state.get_quota_warning() == "80% used"

        state.clear_quota_warning()
        assert state.get_quota_warning() is None


class TestSchemas:
    def test_remember_schema(self):
        assert schemas.REMEMBER["name"] == "totalreclaw_remember"
        assert "text" in schemas.REMEMBER["parameters"]["properties"]
        assert "text" in schemas.REMEMBER["parameters"]["required"]

    def test_recall_schema(self):
        assert schemas.RECALL["name"] == "totalreclaw_recall"
        assert "query" in schemas.RECALL["parameters"]["properties"]

    def test_forget_schema(self):
        assert schemas.FORGET["name"] == "totalreclaw_forget"
        assert "fact_id" in schemas.FORGET["parameters"]["required"]

    def test_export_schema(self):
        assert schemas.EXPORT["name"] == "totalreclaw_export"

    def test_status_schema(self):
        assert schemas.STATUS["name"] == "totalreclaw_status"

    def test_setup_schema(self):
        assert schemas.SETUP["name"] == "totalreclaw_setup"
        assert "recovery_phrase" in schemas.SETUP["parameters"]["properties"]
        # recovery_phrase is optional (generates one if omitted)
        assert "required" not in schemas.SETUP["parameters"]


class TestTools:
    @pytest.mark.asyncio
    async def test_remember_not_configured(self):
        from totalreclaw.hermes.tools import remember
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        result = json.loads(await remember({"text": "test"}, state))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_remember_no_text(self):
        from totalreclaw.hermes.tools import remember
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        result = json.loads(await remember({"text": ""}, state))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_recall_not_configured(self):
        from totalreclaw.hermes.tools import recall
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        result = json.loads(await recall({"query": "test"}, state))
        assert "error" in result

    def test_setup_no_phrase_generates_one(self):
        from totalreclaw.hermes.tools import setup
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        with patch.object(Path, "write_text"), patch.object(Path, "chmod"), \
             patch.object(Path, "mkdir", return_value=None):
            result = json.loads(setup({"recovery_phrase": ""}, state))
        assert result["configured"] is True
        assert result["generated"] is True
        assert len(result["recovery_phrase"].split()) == 12
        assert result["wallet_address"].startswith("0x")

    def test_setup_success(self):
        from totalreclaw.hermes.tools import setup
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        with patch.object(Path, "write_text"), patch.object(Path, "chmod"), \
             patch.object(Path, "mkdir", return_value=None):
            result = json.loads(setup({"recovery_phrase": mnemonic}, state))
        assert result["configured"] is True
        assert result["wallet_address"].startswith("0x")


class TestHooks:
    def test_on_session_start_not_configured(self):
        from totalreclaw.hermes.hooks import on_session_start
        state = _make_state()
        # Should not raise
        on_session_start(state, session_id="test")

    def test_on_session_start_updates_billing_config(self):
        """on_session_start should call update_from_billing with cached billing."""
        from totalreclaw.hermes.hooks import on_session_start
        state = _make_state()
        state._client = MagicMock()  # Pretend configured
        billing = {
            "free_writes_used": 10,
            "free_writes_limit": 500,
            "features": {"extraction_interval": 5, "max_facts_per_extraction": 20},
        }
        state.set_billing_cache(billing)
        on_session_start(state, session_id="test")
        assert state.get_extraction_interval() == 5
        assert state.get_max_facts_per_extraction() == 20

    def test_on_session_start_sets_quota_warning_above_80_pct(self):
        """on_session_start sets quota warning when usage > 80%."""
        from totalreclaw.hermes.hooks import on_session_start
        state = _make_state()
        state._client = MagicMock()
        billing = {"free_writes_used": 450, "free_writes_limit": 500, "features": {}}
        state.set_billing_cache(billing)
        on_session_start(state, session_id="test")
        warning = state.get_quota_warning()
        assert warning is not None
        assert "450/500" in warning
        assert "90%" in warning

    def test_on_session_start_no_quota_warning_below_80_pct(self):
        """on_session_start does NOT set quota warning when usage <= 80%."""
        from totalreclaw.hermes.hooks import on_session_start
        state = _make_state()
        state._client = MagicMock()
        billing = {"free_writes_used": 200, "free_writes_limit": 500, "features": {}}
        state.set_billing_cache(billing)
        on_session_start(state, session_id="test")
        assert state.get_quota_warning() is None

    def test_pre_llm_call_not_configured(self):
        from totalreclaw.hermes.hooks import pre_llm_call
        state = _make_state()
        result = pre_llm_call(state, is_first_turn=True, user_message="hello")
        assert result is None

    def test_pre_llm_call_injects_quota_warning(self):
        """pre_llm_call should inject and clear quota warning."""
        from totalreclaw.hermes.hooks import pre_llm_call
        state = _make_state()
        state._client = MagicMock()
        state.set_quota_warning("TotalReclaw: 450/500 memories used")

        result = pre_llm_call(state, is_first_turn=False, user_message="")
        assert result is not None
        assert "450/500" in result["context"]

        # Warning should be cleared after injection
        assert state.get_quota_warning() is None

        # Second call should not inject warning
        result2 = pre_llm_call(state, is_first_turn=False, user_message="")
        assert result2 is None

    def test_pre_llm_call_auto_recall_top_k_8(self):
        """Auto-recall should use top_k=8."""
        from totalreclaw.hermes.hooks import pre_llm_call

        state = _make_state()
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "User prefers dark mode"

        async def mock_recall(query, top_k=8):
            assert top_k == 8, f"Expected top_k=8, got top_k={top_k}"
            return [mock_result]

        mock_client.recall = mock_recall
        state._client = mock_client

        result = pre_llm_call(state, is_first_turn=True, user_message="hello")
        assert result is not None
        assert "dark mode" in result["context"]

    def test_pre_llm_call_quota_warning_and_memories_combined(self):
        """pre_llm_call combines quota warning and memories when both present."""
        from totalreclaw.hermes.hooks import pre_llm_call

        state = _make_state()
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "User likes Python"

        async def mock_recall(query, top_k=8):
            return [mock_result]

        mock_client.recall = mock_recall
        state._client = mock_client
        state.set_quota_warning("Quota warning text")

        result = pre_llm_call(state, is_first_turn=True, user_message="hello")
        assert result is not None
        assert "Quota warning text" in result["context"]
        assert "Python" in result["context"]

    def test_post_llm_call_increments_turn(self):
        from totalreclaw.hermes.hooks import post_llm_call
        state = _make_state()
        post_llm_call(state, user_message="hi", assistant_response="hello")
        assert state.turn_count == 1

    def test_on_session_end_not_configured(self):
        from totalreclaw.hermes.hooks import on_session_end
        state = _make_state()
        # Should not raise
        on_session_end(state, session_id="test", completed=True, interrupted=False)

    def test_heuristic_extract(self):
        from totalreclaw.hermes.extractor import extract_facts_heuristic
        messages = [
            {"role": "user", "content": "I prefer dark mode for all my editors"},
            {"role": "assistant", "content": "Got it, dark mode preference noted"},
            {"role": "user", "content": "My name is Pedro and I work at TotalReclaw"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert len(facts) >= 2
        assert any("dark mode" in f.text.lower() for f in facts)


_has_cosine = hasattr(__import__("totalreclaw_core"), "cosine_similarity")


@pytest.mark.skipif(not _has_cosine, reason="totalreclaw_core missing cosine_similarity")
class TestStoreTimeDedup:
    """Tests for cosine-based near-duplicate detection at store time."""

    def test_is_near_duplicate_above_threshold(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        # Identical vectors -> cosine sim = 1.0, well above 0.85
        embedding = [1.0, 0.0, 0.0]
        existing = [{"id": "1", "text": "test", "embedding": [1.0, 0.0, 0.0]}]
        assert _is_near_duplicate(embedding, existing) is True

    def test_is_near_duplicate_below_threshold(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        # Orthogonal vectors -> cosine sim = 0.0
        embedding = [1.0, 0.0, 0.0]
        existing = [{"id": "1", "text": "test", "embedding": [0.0, 1.0, 0.0]}]
        assert _is_near_duplicate(embedding, existing) is False

    def test_is_near_duplicate_empty_embedding(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        existing = [{"id": "1", "text": "test", "embedding": [1.0, 0.0]}]
        assert _is_near_duplicate([], existing) is False
        assert _is_near_duplicate(None, existing) is False

    def test_is_near_duplicate_no_existing_embeddings(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        embedding = [1.0, 0.0, 0.0]
        # Existing memory has no embedding
        existing = [{"id": "1", "text": "test", "embedding": None}]
        assert _is_near_duplicate(embedding, existing) is False

    def test_is_near_duplicate_empty_existing_list(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        embedding = [1.0, 0.0, 0.0]
        assert _is_near_duplicate(embedding, []) is False

    def test_is_near_duplicate_custom_threshold(self):
        from totalreclaw.hermes.hooks import _is_near_duplicate
        # Vectors at ~45 degree angle -> cosine sim ~ 0.707
        import math
        embedding = [1.0, 0.0]
        existing = [{"id": "1", "text": "test", "embedding": [1.0, 1.0]}]
        # Should be duplicate at 0.7 threshold
        assert _is_near_duplicate(embedding, existing, threshold=0.7) is True
        # Should NOT be duplicate at 0.8 threshold
        assert _is_near_duplicate(embedding, existing, threshold=0.8) is False

    def test_dedup_skips_add_but_allows_update(self):
        """Near-duplicate detection skips ADD but allows UPDATE actions."""
        from totalreclaw.hermes.hooks import _extract_and_store, _is_near_duplicate
        from totalreclaw.hermes.extractor import ExtractedFact

        state = _make_state()
        mock_client = MagicMock()
        state._client = mock_client

        # Setup: existing memories with embeddings
        mock_recall_result = MagicMock()
        mock_recall_result.id = "existing-1"
        mock_recall_result.text = "User prefers dark mode"
        mock_recall_result.embedding = [1.0, 0.0, 0.0]

        async def mock_recall(query, top_k=50):
            return [mock_recall_result]

        async def mock_remember(*args, **kwargs):
            return "new-id"

        async def mock_forget(fact_id):
            return True

        mock_client.recall = mock_recall
        mock_client.remember = mock_remember
        mock_client.forget = mock_forget

        state.add_message("user", "I like dark mode")
        state.add_message("assistant", "OK")

        add_fact = ExtractedFact(
            text="User prefers dark mode", type="preference",
            importance=8, action="ADD"
        )
        update_fact = ExtractedFact(
            text="User prefers dark mode for all apps", type="preference",
            importance=9, action="UPDATE", existing_fact_id="existing-1"
        )

        remember_calls = []
        original_remember = mock_client.remember

        async def tracking_remember(*args, **kwargs):
            remember_calls.append((args, kwargs))
            return "new-id"

        mock_client.remember = tracking_remember

        with patch("totalreclaw.agent.lifecycle.extract_facts_llm") as mock_llm, \
             patch("totalreclaw.agent.lifecycle.extract_facts_heuristic"), \
             patch("totalreclaw.embedding.get_embedding", return_value=[1.0, 0.0, 0.0]):

            async def llm_extract(messages, mode, existing_memories):
                return [add_fact, update_fact]

            mock_llm.side_effect = llm_extract

            _extract_and_store(state, mode="turn")

            # ADD should be skipped (near-duplicate of existing [1,0,0])
            # UPDATE should go through (bypasses dedup)
            # So we expect exactly 1 remember call (for the UPDATE)
            assert len(remember_calls) == 1
            assert remember_calls[0][1].get("source") == "hermes-auto"


class TestRegister:
    def test_register(self):
        from totalreclaw.hermes import register
        ctx = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                register(ctx)
        # Should register 6 tools and 4 hooks
        assert ctx.register_tool.call_count == 6
        assert ctx.register_hook.call_count == 4

        # Check tool names
        tool_names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        assert "totalreclaw_remember" in tool_names
        assert "totalreclaw_recall" in tool_names
        assert "totalreclaw_forget" in tool_names
        assert "totalreclaw_export" in tool_names
        assert "totalreclaw_status" in tool_names
        assert "totalreclaw_setup" in tool_names

        # Check hook names
        hook_names = [call.args[0] for call in ctx.register_hook.call_args_list]
        assert "on_session_start" in hook_names
        assert "pre_llm_call" in hook_names
        assert "post_llm_call" in hook_names
        assert "on_session_end" in hook_names
