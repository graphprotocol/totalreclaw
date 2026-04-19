"""Tests for the ``totalreclaw_debrief`` Hermes tool (Phase A).

The explicit-invocation form of the debrief flow. The tool reuses the
same ``session_debrief`` pipeline as the auto ``on_session_end`` hook,
so the resulting summary facts are indistinguishable from the auto-flow
output (same ``type=summary``, ``provenance=derived``, ``scope=unspecified``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.debrief import DebriefItem
from totalreclaw.hermes import schemas
from totalreclaw.hermes.state import PluginState


def _make_state_with_client():
    """Return a configured PluginState + fake remember-capturing client."""
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    # The agent pipeline calls ``client.remember`` via ``run_sync``; return
    # a synthetic fact id so we can assert the tool threads it back.
    remember_return = iter(["fact-id-1", "fact-id-2", "fact-id-3"])

    async def _remember(*args, **kwargs):
        return next(remember_return)

    fake_client.remember = AsyncMock(side_effect=_remember)
    fake_client.recall = AsyncMock(return_value=[])  # empty vault, no dedup noise
    state._client = fake_client
    return state, fake_client


class TestDebriefSchema:
    def test_schema_exists(self) -> None:
        assert hasattr(schemas, "DEBRIEF")
        assert schemas.DEBRIEF["name"] == "totalreclaw_debrief"

    def test_schema_description_has_utterance_mapping(self) -> None:
        """Follow the Phase 2 style — spell out INVOKE WHEN / WHEN NOT TO USE."""
        desc = schemas.DEBRIEF["description"]
        lowered = desc.lower()
        # Must advertise the conclusion-capturing intent.
        assert "summary" in lowered or "debrief" in lowered
        # Explicit utterance hints (parity with mcp/src/tools/debrief.ts).
        assert "goodbye" in lowered or "wrap" in lowered or "done" in lowered

    def test_schema_parameters_empty(self) -> None:
        """No args required — the tool reuses the buffered session."""
        params = schemas.DEBRIEF["parameters"]
        assert params["type"] == "object"
        assert params.get("required", []) == []


class TestDebriefTool:
    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self) -> None:
        from totalreclaw.hermes.tools import debrief

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        result = json.loads(await debrief({}, state))
        assert "error" in result
        assert "totalreclaw_setup" in result["error"]

    @pytest.mark.asyncio
    async def test_too_short_session_returns_skipped(self) -> None:
        """Fewer than 4 turns (8 messages) → skip, no debrief generated."""
        from totalreclaw.hermes.tools import debrief

        state, _client = _make_state_with_client()
        # Only 3 messages — below the 8-message threshold.
        for i in range(3):
            state.add_message("user", f"hi {i}")
        result = json.loads(await debrief({}, state))
        assert result.get("stored") == 0
        # Skipped field signals the shortcut.
        assert result.get("skipped") is True

    @pytest.mark.asyncio
    async def test_explicit_invocation_produces_summary_facts(self) -> None:
        """The tool must call the SAME code path as the auto-flow.

        We fake ``generate_debrief`` so the test is LLM-free and assert:
        1. The stored facts have ``type=summary``, ``provenance=derived``
           (the v1 contract the auto-flow commits).
        2. The tool's response surfaces the stored count + fact ids.
        """
        from totalreclaw.hermes.tools import debrief

        state, fake_client = _make_state_with_client()

        # Seed a long-enough session (>= 8 messages).
        for i in range(8):
            state.add_message("user", f"A meaningful user message about project X turn {i}")
            state.add_message("assistant", f"A long assistant response covering topic Y turn {i}")

        fake_items = [
            DebriefItem(text="Session concluded refactor of the auth module.", type="summary", importance=8),
            DebriefItem(text="API migration still pending for billing module.", type="context", importance=7),
        ]

        async def fake_generate(*args, **kwargs):
            return fake_items

        with patch("totalreclaw.agent.lifecycle.generate_debrief", new=fake_generate):
            result = json.loads(await debrief({}, state))

        # Verify the v1 contract — parity with ``on_session_end`` hook.
        calls = fake_client.remember.call_args_list
        assert len(calls) == 2
        for call in calls:
            kwargs = call.kwargs
            assert kwargs["fact_type"] == "summary"
            assert kwargs["provenance"] == "derived"
            assert kwargs["scope"] == "unspecified"

        # Tool response surfaces the count + fact ids for the user.
        assert result.get("stored") == 2
        assert result.get("count") == 2
        assert isinstance(result.get("fact_ids"), list)
        assert len(result["fact_ids"]) == 2

    @pytest.mark.asyncio
    async def test_debrief_returns_zero_when_llm_yields_empty(self) -> None:
        """LLM returns no items → tool reports stored=0 without erroring."""
        from totalreclaw.hermes.tools import debrief

        state, _client = _make_state_with_client()
        for i in range(8):
            state.add_message("user", f"turn {i}")
            state.add_message("assistant", f"reply {i}")

        async def fake_generate(*args, **kwargs):
            return []

        with patch("totalreclaw.agent.lifecycle.generate_debrief", new=fake_generate):
            result = json.loads(await debrief({}, state))

        assert result.get("stored") == 0
        assert "error" not in result


class TestDebriefRegistration:
    def test_register_wires_debrief_tool(self) -> None:
        from totalreclaw.hermes import register

        ctx = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                register(ctx)

        tool_names = [call.kwargs["name"] for call in ctx.register_tool.call_args_list]
        assert "totalreclaw_debrief" in tool_names
