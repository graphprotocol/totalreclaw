"""Tests for issue #337 — block agent-fabricated self-directives at the
``totalreclaw_remember`` call site.

Repro from auto-QA on rc.2.4.4rc1 (umbrella #336 / sub-issue #337 / F5):
the user said
    "Remember this going forward: always structure your code responses with
     explicit type hints in Python. No exceptions."
and the agent (glm-5-turbo) stored TWO directives — the correct user-attributed
one AND a fabricated
    "I'll always use totalreclaw_remember and totalreclaw_recall over the
     built-in memory tool"
that the user never said. The fabricated one received ``provenance="user"``
(hardcoded in ``tools.py``) and importance 9, polluting the vault.

SKILL.md 2.4.4 already covered this in "Don't store transient noise" but
glm-class models ignore the rule. This filter enforces it at the call gate.

The detector requires BOTH first-person agent voice ("I'll always …") AND a
reference to an internal ``totalreclaw_*`` tool by name — either alone is not
sufficient to flag, which keeps false-positive risk near zero on legitimate
user-attributed directives.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes.tools import (
    _is_likely_agent_self_directive,
    remember,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _configured_state():
    """A PluginState wired up with a mocked client whose remember() returns a
    fixed fact id. Lets us assert whether the filter SHORT-CIRCUITED (no
    client.remember call) or let the write through.
    """
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    mock_client = MagicMock()
    mock_client.remember = AsyncMock(return_value="fact-id-stub-1")
    state.get_client = MagicMock(return_value=mock_client)
    return state, mock_client


# ----------------------------------------------------------------------------
# Unit: detector heuristic
# ----------------------------------------------------------------------------


class TestIssue337Detector:
    """``_is_likely_agent_self_directive`` — pure heuristic, no I/O."""

    def test_issue_337_fabricated_self_directive_matches(self):
        # The exact F5 fabrication that polluted the vault on rc.2.4.4rc1.
        text = (
            "I'll always use totalreclaw_remember and totalreclaw_recall "
            "over the built-in memory tool"
        )
        assert _is_likely_agent_self_directive(text) is True

    def test_issue_337_user_directive_without_tool_name_passes(self):
        # Legitimate user-attributed directive — the user-correct half of F5.
        text = (
            "Always structure your code responses with explicit type hints "
            "in Python. No exceptions."
        )
        assert _is_likely_agent_self_directive(text) is False

    def test_issue_337_user_says_totalreclaw_without_first_person_passes(self):
        # User can reference the product by name without triggering the gate.
        text = "Use TotalReclaw instead of Mem0 for memory storage."
        assert _is_likely_agent_self_directive(text) is False

    def test_issue_337_first_person_alone_without_tool_name_passes(self):
        # User saying "I will always X" about themselves is fine. No tool name.
        text = "I will always commit Python work with explicit type hints."
        assert _is_likely_agent_self_directive(text) is False

    def test_issue_337_case_insensitive(self):
        text = "I'LL ALWAYS USE totalreclaw_recall before answering."
        assert _is_likely_agent_self_directive(text) is True

    def test_issue_337_empty_text_passes(self):
        assert _is_likely_agent_self_directive("") is False
        assert _is_likely_agent_self_directive("   ") is False

    @pytest.mark.parametrize(
        "phrase",
        [
            "i'll always",
            "i will always",
            "i should always",
            "i'll use",
            "i will prefer",
            "i'll favor",
            "i'll route",
            "i'll switch",
        ],
    )
    def test_issue_337_agent_voice_variants_with_tool_name_block(self, phrase):
        text = f"{phrase} totalreclaw_remember for important user facts."
        assert _is_likely_agent_self_directive(text) is True

    @pytest.mark.parametrize(
        "tool_name",
        [
            "totalreclaw_remember",
            "totalreclaw_recall",
            "totalreclaw_debrief",
            "totalreclaw_pin",
        ],
    )
    def test_issue_337_tool_name_variants_with_agent_voice_block(self, tool_name):
        text = f"I'll always call {tool_name} after each turn."
        assert _is_likely_agent_self_directive(text) is True


# ----------------------------------------------------------------------------
# Integration: remember() call site short-circuits before client.remember
# ----------------------------------------------------------------------------


class TestIssue337RememberCallSite:
    """``remember()`` must NOT invoke ``client.remember`` when the filter fires."""

    @pytest.mark.asyncio
    async def test_issue_337_fabricated_directive_returns_blocked_not_stored(self):
        state, mock_client = _configured_state()
        text = (
            "I'll always use totalreclaw_remember and totalreclaw_recall "
            "over the built-in memory tool"
        )

        result = json.loads(
            await remember(
                {"text": text, "type": "directive", "importance": 9.0},
                state,
            )
        )

        assert result.get("stored") is False
        assert result.get("blocked") == "agent_self_directive"
        assert "vault" in result.get("reason", "").lower()
        mock_client.remember.assert_not_called()

    @pytest.mark.asyncio
    async def test_issue_337_legitimate_user_directive_still_stores(self):
        state, mock_client = _configured_state()
        text = (
            "Always structure your code responses with explicit type hints "
            "in Python. No exceptions."
        )

        result = json.loads(
            await remember(
                {"text": text, "type": "directive", "importance": 9.0},
                state,
            )
        )

        assert result.get("stored") is True
        assert result.get("fact_id") == "fact-id-stub-1"
        mock_client.remember.assert_called_once()

    @pytest.mark.asyncio
    async def test_issue_337_block_response_is_distinct_from_error_response(self):
        """The blocked response must not be shaped like an error — agents that
        re-try on ``error`` would otherwise loop. ``stored=False`` + an
        explicit ``blocked`` discriminator carries that signal.
        """
        state, _ = _configured_state()
        text = "I'll always use totalreclaw_remember for important facts."

        result = json.loads(await remember({"text": text}, state))

        assert "error" not in result
        assert result.get("stored") is False
        assert result.get("blocked") == "agent_self_directive"
