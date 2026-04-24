"""Tests for v2.0.2 Hermes plugin fixes (Phase 2 — bugs #5, #6, #8, #9).

Covers:
- Bug #5: LLM auto-detect surfaces a visible error when no config resolves.
- Bug #6: Tool descriptions are sharpened + setup nudge fires on first-run.
- Bug #8: In-batch dedup (3 near-identical facts → only 1 survives).
- Bug #9: Setup meta-content ("I want encrypted memory across my AI tools")
         is filtered, but genuine preferences ("I like encrypted tools") are kept.
"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.extraction import (
    ExtractedFact,
    deduplicate_facts_by_embedding,
    is_product_meta_request,
    extract_facts_llm,
)


# ----------------------------------------------------------------------------
# Bug #8 — In-batch + cross-batch cosine dedup
# ----------------------------------------------------------------------------


class TestBug8InBatchDedup:
    """Store-time cosine dedup must collapse near-duplicates within a single
    extraction batch. Prior code fetched ``existing_memories`` ONCE before the
    store loop, so 3 near-identical facts in the same batch all persisted.
    """

    def test_deduplicate_collapses_near_identical_batch(self):
        """3 near-identical facts in a batch → only 1 survives.

        Uses synthetic normalized embeddings with cosine sim > 0.85.
        """
        # All three point in nearly the same direction.
        e1 = [1.0, 0.0, 0.0]
        e2 = [0.98, 0.02, 0.0]  # cosine ~ 0.9998
        e3 = [0.99, 0.01, 0.0]  # cosine ~ 0.9999

        fact1 = ExtractedFact(
            text="User uses PyCharm with the Darcula theme.",
            type="preference", importance=7, action="ADD",
            _embedding=e1,
        )
        fact2 = ExtractedFact(
            text="User uses PyCharm with Darcula theme for Python work.",
            type="preference", importance=7, action="ADD",
            _embedding=e2,
        )
        fact3 = ExtractedFact(
            text="PyCharm + Darcula is the user's Python IDE setup.",
            type="preference", importance=7, action="ADD",
            _embedding=e3,
        )

        # No existing memories — pure in-batch dedup.
        result = deduplicate_facts_by_embedding(
            [fact1, fact2, fact3], existing_memories=[], threshold=0.85,
        )
        assert len(result) == 1, f"expected 1 survivor, got {len(result)}"
        assert result[0] is fact1  # First one wins.

    def test_deduplicate_keeps_dissimilar_facts(self):
        """Facts with orthogonal embeddings are not deduped."""
        f1 = ExtractedFact(
            text="User prefers PyCharm IDE.",
            type="preference", importance=7, action="ADD",
            _embedding=[1.0, 0.0, 0.0],
        )
        f2 = ExtractedFact(
            text="User runs on Ubuntu Linux.",
            type="claim", importance=7, action="ADD",
            _embedding=[0.0, 1.0, 0.0],
        )
        f3 = ExtractedFact(
            text="User drinks espresso in the morning.",
            type="preference", importance=6, action="ADD",
            _embedding=[0.0, 0.0, 1.0],
        )
        result = deduplicate_facts_by_embedding([f1, f2, f3], [], 0.85)
        assert len(result) == 3

    def test_deduplicate_drops_fact_matching_existing_memory(self):
        """A fact with embedding matching an existing vault entry is dropped."""
        fact = ExtractedFact(
            text="User uses dark mode.",
            type="preference", importance=7, action="ADD",
            _embedding=[1.0, 0.0, 0.0],
        )
        existing = [
            {"id": "existing-1", "text": "dark mode", "embedding": [0.99, 0.01, 0.0]},
        ]
        result = deduplicate_facts_by_embedding([fact], existing, 0.85)
        assert len(result) == 0

    def test_deduplicate_bypasses_update_actions(self):
        """UPDATE facts are not near-dup checked — they intentionally supersede."""
        update_fact = ExtractedFact(
            text="User uses dark mode (always).",
            type="preference", importance=8, action="UPDATE",
            existing_fact_id="old-1",
            _embedding=[1.0, 0.0, 0.0],
        )
        existing = [
            {"id": "old-1", "text": "dark mode", "embedding": [1.0, 0.0, 0.0]},
        ]
        result = deduplicate_facts_by_embedding([update_fact], existing, 0.85)
        assert len(result) == 1

    def test_deduplicate_handles_missing_embeddings(self):
        """Facts without embeddings are kept (can't dedup without signal)."""
        fact = ExtractedFact(
            text="Plain fact",
            type="claim", importance=6, action="ADD",
            _embedding=None,
        )
        result = deduplicate_facts_by_embedding([fact], [], 0.85)
        assert len(result) == 1

    def test_deduplicate_delete_actions_pass_through(self):
        """DELETE actions are tombstones — they bypass dedup."""
        delete_fact = ExtractedFact(
            text="obsolete fact",
            type="claim", importance=5, action="DELETE",
            existing_fact_id="old-1",
            _embedding=[1.0, 0.0, 0.0],
        )
        existing = [
            {"id": "old-1", "text": "old", "embedding": [1.0, 0.0, 0.0]},
        ]
        result = deduplicate_facts_by_embedding([delete_fact], existing, 0.85)
        assert len(result) == 1


# ----------------------------------------------------------------------------
# Bug #9 — Spurious extraction of setup/meta content
# ----------------------------------------------------------------------------


class TestBug9MetaFilter:
    """Product-meta-request filter drops setup utterances but keeps genuine
    preferences that happen to mention encryption.
    """

    def test_meta_filter_catches_setup_utterance(self):
        """QA-reported phrase is flagged as meta."""
        assert is_product_meta_request(
            "I want encrypted memory across my AI tools"
        ) is True

    def test_meta_filter_catches_totalreclaw_mention(self):
        assert is_product_meta_request("Please set up TotalReclaw for me") is True
        assert is_product_meta_request("can you configure totalreclaw") is True
        assert is_product_meta_request(
            "I need to install the memory plugin"
        ) is True

    def test_meta_filter_allows_genuine_preferences(self):
        """Preferences like 'I like encrypted tools' SHOULD be extracted."""
        assert is_product_meta_request("I like encrypted tools") is False
        assert is_product_meta_request(
            "I prefer PostgreSQL over MySQL for OLTP"
        ) is False
        assert is_product_meta_request("My favorite editor is Vim") is False

    def test_meta_filter_allows_legitimate_encryption_preferences(self):
        """'Encrypted' as an adjective in a real preference is not meta."""
        # Borderline — but these are genuine preferences, not setup requests.
        assert is_product_meta_request(
            "I prefer using end-to-end encrypted messengers"
        ) is False
        assert is_product_meta_request(
            "I like using Signal because it's encrypted"
        ) is False


# ----------------------------------------------------------------------------
# Bug #5 — LLM auto-detect surfaces a visible error (no more silent disable)
# ----------------------------------------------------------------------------


class TestBug5VisibleLLMError:
    """When no LLM config resolves, extraction must surface a loud warning
    with actionable guidance — not a quiet INFO log line.
    """

    @pytest.mark.asyncio
    async def test_extract_facts_llm_warns_when_no_config(self, caplog, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
            with caplog.at_level(logging.WARNING, logger="totalreclaw.agent.extraction"):
                result = await extract_facts_llm(
                    [{"role": "user", "content": "I like coffee"}],
                )
        assert result == []
        # Must log at WARNING (not INFO) with actionable guidance.
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1, (
            "no WARNING logged — bug #5 says silent failure must be loud"
        )
        msg = warnings[0].getMessage().lower()
        assert "llm" in msg
        assert "extraction" in msg
        # Actionable guidance: mention setup path or config file
        assert any(term in msg for term in [
            "openai_model", "config", "extraction disabled", "hermes",
            "set up", "configure",
        ]), f"no actionable guidance in warning: {msg!r}"

    def test_post_llm_call_surfaces_quota_warning_when_no_llm_config(self):
        """User-visible: when LLM can't be resolved, a one-time quota-channel
        warning appears so the user sees something in their next context.
        """
        from totalreclaw.hermes.hooks import post_llm_call
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        state._client = MagicMock()  # Configured

        # Force LLM detect to return None (no env) + Hermes config absent.
        with patch(
            "totalreclaw.hermes.hooks._get_hermes_llm_config",
            return_value=None,
        ):
            with patch(
                "totalreclaw.agent.llm_client.detect_llm_config",
                return_value=None,
            ):
                # Call enough times to hit the extraction interval.
                from totalreclaw.hermes.state import DEFAULT_EXTRACTION_INTERVAL
                for _ in range(DEFAULT_EXTRACTION_INTERVAL):
                    post_llm_call(state, user_message="u", assistant_response="a")

        # A quota_warning should now be queued and mention LLM.
        warning = state.get_quota_warning()
        assert warning is not None, "post_llm_call should surface a visible warning"
        assert "llm" in warning.lower() or "extraction" in warning.lower()

    def test_post_llm_call_quota_warning_fires_only_once(self):
        """Multiple failed extraction attempts → still only ONE warning."""
        from totalreclaw.hermes.hooks import post_llm_call
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        state._client = MagicMock()

        with patch(
            "totalreclaw.hermes.hooks._get_hermes_llm_config",
            return_value=None,
        ):
            with patch(
                "totalreclaw.agent.llm_client.detect_llm_config",
                return_value=None,
            ):
                from totalreclaw.hermes.state import DEFAULT_EXTRACTION_INTERVAL
                # Trigger twice.
                for _ in range(DEFAULT_EXTRACTION_INTERVAL * 2):
                    post_llm_call(state, user_message="u", assistant_response="a")

        # First consume clears the warning; second cycle shouldn't re-set it.
        w1 = state.get_quota_warning()
        state.clear_quota_warning()
        with patch(
            "totalreclaw.hermes.hooks._get_hermes_llm_config",
            return_value=None,
        ):
            with patch(
                "totalreclaw.agent.llm_client.detect_llm_config",
                return_value=None,
            ):
                post_llm_call(state, user_message="u2", assistant_response="a2")
                post_llm_call(state, user_message="u3", assistant_response="a3")
                post_llm_call(state, user_message="u4", assistant_response="a4")
        w2 = state.get_quota_warning()

        assert w1 is not None
        assert w2 is None, "warning should fire only once per session"


# ----------------------------------------------------------------------------
# Bug #6 — Tool descriptions sharpened to outrank Hermes built-in memory
# ----------------------------------------------------------------------------


class TestBug6ToolDescriptions:
    """TotalReclaw tool descriptions must be distinctive/specific enough
    that the LLM picks them over Hermes's built-in ``memory`` tool.

    Heuristic: descriptions should mention encryption + cross-agent
    portability + persistence qualities that the built-in tool lacks.
    """

    def test_remember_description_is_distinctive(self):
        from totalreclaw.hermes.schemas import REMEMBER
        desc = REMEMBER["description"].lower()
        # Key differentiators vs built-in memory:
        # Must contain at least 3 of: encrypted, persistent, cross-session,
        # portable, onchain, vault, e2e, permanent, durable.
        distinctive_terms = [
            "encrypted", "persistent", "cross-session", "portable",
            "onchain", "on-chain", "vault", "e2e", "permanent", "durable",
            "long-term",
        ]
        hits = [t for t in distinctive_terms if t in desc]
        assert len(hits) >= 3, (
            f"remember description too generic; only {len(hits)} distinctive "
            f"terms ({hits}). Add more to outrank built-in memory."
        )

    def test_recall_description_mentions_persistence(self):
        from totalreclaw.hermes.schemas import RECALL
        desc = RECALL["description"].lower()
        # Must be clearly about cross-session/persistent memory.
        assert any(t in desc for t in [
            "cross-session", "persistent", "onchain", "on-chain", "vault",
            "encrypted", "long-term",
        ]), (
            f"recall description should signal persistence: {desc!r}"
        )

    def test_recall_description_signals_preference_over_builtin(self):
        """Description should include an explicit usage hint."""
        from totalreclaw.hermes.schemas import RECALL
        desc = RECALL["description"].lower()
        # Either mention "use this for X" or "prefer this" or similar.
        assert any(term in desc for term in [
            "use this", "for any", "always", "whenever", "preferred",
            "primary",
        ]), f"no usage-preference hint in recall description: {desc!r}"


# ----------------------------------------------------------------------------
# Bug #6 continued — on_session_start nudges setup when not configured
# ----------------------------------------------------------------------------


class TestBug6FirstRunNudge:
    """When the user has installed the plugin but not run totalreclaw_setup,
    the first-turn ``pre_llm_call`` must surface a one-time nudge telling
    the user (via the system) to run setup.
    """

    def test_pre_llm_call_nudges_unconfigured_user_on_first_turn(self):
        from totalreclaw.hermes.hooks import pre_llm_call
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()

        assert not state.is_configured()

        # First turn — user asks a natural memory-related question.
        result = pre_llm_call(
            state,
            is_first_turn=True,
            user_message="Can you remember that I prefer dark mode?",
        )

        # Should return a context nudge mentioning setup.
        assert result is not None, "expected setup nudge on first turn"
        ctx = result.get("context", "")
        assert "totalreclaw_setup" in ctx.lower() or "set up" in ctx.lower()

    def test_pre_llm_call_nudge_only_fires_once(self):
        """Second invocation should NOT re-inject the nudge."""
        from totalreclaw.hermes.hooks import pre_llm_call
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()

        first = pre_llm_call(
            state, is_first_turn=True,
            user_message="Remember I like dark mode",
        )
        second = pre_llm_call(
            state, is_first_turn=True,
            user_message="Can you remember another thing for me?",
        )
        assert first is not None
        # Second call: nudge should not re-fire (no "totalreclaw_setup" in ctx).
        if second is not None:
            ctx = second.get("context", "").lower()
            assert "totalreclaw_setup" not in ctx

    def test_pre_llm_call_configured_user_no_nudge(self):
        """Configured users don't see the setup nudge."""
        from totalreclaw.hermes.hooks import pre_llm_call
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        state._client = MagicMock()  # Forge configured state

        with patch("totalreclaw.hermes.hooks.auto_recall", return_value=None):
            result = pre_llm_call(
                state,
                is_first_turn=True,
                user_message="Any natural question",
            )
        # No nudge when configured.
        if result is not None:
            ctx = result.get("context", "").lower()
            assert "totalreclaw_setup" not in ctx
