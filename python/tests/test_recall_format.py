"""Tests for recall context formatting with dates (2026-06-08 benchmark feature).

Validates:
  - _fmt_date: Unix-seconds → 'YYYY-MM-DD', '' for None/invalid.
  - auto_recall / auto_recall_async: date tags + current-date header rendered
    when created_at is present; omitted (no parentheses) when absent.
  - hermes tools.recall JSON: 'date' field present per memory.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.recall import _fmt_date, _format_recall_context


# ---------------------------------------------------------------------------
# _fmt_date
# ---------------------------------------------------------------------------


class TestFmtDate:
    def test_valid_unix_seconds_float(self):
        # 2024-01-15 00:00:00 UTC
        ts = datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp()
        assert _fmt_date(ts) == "2024-01-15"

    def test_valid_unix_seconds_int(self):
        ts = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp())
        assert _fmt_date(ts) == "2023-06-01"

    def test_valid_unix_seconds_string(self):
        ts = str(int(datetime(2025, 12, 31, tzinfo=timezone.utc).timestamp()))
        assert _fmt_date(ts) == "2025-12-31"

    def test_none_returns_empty(self):
        assert _fmt_date(None) == ""

    def test_zero_returns_empty(self):
        # 0 is falsy — treated as absent
        assert _fmt_date(0) == ""

    def test_invalid_string_returns_empty(self):
        assert _fmt_date("not-a-number") == ""

    def test_negative_timestamp(self):
        # Negative Unix timestamps are valid (pre-1970) but unusual; just
        # verify no exception is raised and a date string is returned.
        result = _fmt_date(-1)
        # Either a formatted date or "" — no exception.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _format_recall_context
# ---------------------------------------------------------------------------


def _make_result(text: str, category: str = "claim", created_at=None):
    """Build a minimal RerankerResult-like object."""
    r = SimpleNamespace()
    r.text = text
    r.category = category
    r.created_at = created_at
    return r


class TestFormatRecallContext:
    def test_header_includes_current_date(self):
        results = [_make_result("likes tea", created_at=None)]
        output = _format_recall_context(results)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert f"The current date is {today}." in output

    def test_header_includes_reasoning_nudge(self):
        results = [_make_result("likes tea")]
        output = _format_recall_context(results)
        assert "reason carefully" in output
        assert "compute differences precisely" in output

    def test_result_with_created_at_includes_date_tag(self):
        ts = datetime(2024, 3, 5, tzinfo=timezone.utc).timestamp()
        results = [_make_result("prefers dark mode", category="preference", created_at=ts)]
        output = _format_recall_context(results)
        assert "(2024-03-05)" in output
        assert "- [preference] (2024-03-05) prefers dark mode" in output

    def test_result_without_created_at_omits_parentheses(self):
        results = [_make_result("prefers dark mode", category="preference", created_at=None)]
        output = _format_recall_context(results)
        # No empty parens "()" and no date field
        assert "()" not in output
        assert "- [preference] prefers dark mode" in output

    def test_mixed_results_date_and_no_date(self):
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp()
        results = [
            _make_result("fact with date", category="claim", created_at=ts),
            _make_result("fact without date", category="episode", created_at=None),
        ]
        output = _format_recall_context(results)
        assert "(2024-06-01)" in output
        assert "- [episode] fact without date" in output
        assert "()" not in output

    def test_section_header_present(self):
        results = [_make_result("some memory")]
        output = _format_recall_context(results)
        assert output.startswith("## Relevant memories from TotalReclaw\n")

    def test_multiple_results_all_rendered(self):
        results = [_make_result(f"memory {i}") for i in range(3)]
        output = _format_recall_context(results)
        for i in range(3):
            assert f"memory {i}" in output


# ---------------------------------------------------------------------------
# auto_recall (sync)
# ---------------------------------------------------------------------------


class TestAutoRecallFormat:
    def _make_state(self, results):
        """Build a minimal AgentState mock that returns `results` from recall."""
        mock_client = MagicMock()

        async def _recall(query, top_k=8, max_candidates=250):
            return results

        mock_client.recall = _recall

        state = MagicMock()
        state.is_configured.return_value = True
        state.get_client.return_value = mock_client
        return state

    def test_returns_none_when_no_results(self):
        from totalreclaw.agent.recall import auto_recall
        state = self._make_state([])
        assert auto_recall("query", state) is None

    def test_date_in_output_when_created_at_set(self):
        from totalreclaw.agent.recall import auto_recall
        ts = datetime(2024, 9, 20, tzinfo=timezone.utc).timestamp()
        results = [_make_result("user likes tea", category="preference", created_at=ts)]
        state = self._make_state(results)
        output = auto_recall("what do I like?", state)
        assert output is not None
        assert "(2024-09-20)" in output

    def test_no_parens_when_created_at_none(self):
        from totalreclaw.agent.recall import auto_recall
        results = [_make_result("user likes tea", category="preference", created_at=None)]
        state = self._make_state(results)
        output = auto_recall("what do I like?", state)
        assert output is not None
        assert "()" not in output
        assert "user likes tea" in output

    def test_header_contains_today(self):
        from totalreclaw.agent.recall import auto_recall
        results = [_make_result("some fact", created_at=None)]
        state = self._make_state(results)
        output = auto_recall("test", state)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in output


# ---------------------------------------------------------------------------
# auto_recall_async
# ---------------------------------------------------------------------------


class TestAutoRecallAsyncFormat:
    def _make_state(self, results):
        mock_client = MagicMock()

        async def _recall(query, top_k=8, max_candidates=250):
            return results

        mock_client.recall = _recall

        state = MagicMock()
        state.is_configured.return_value = True
        state.get_client.return_value = mock_client
        return state

    @pytest.mark.asyncio
    async def test_date_in_output_when_created_at_set(self):
        from totalreclaw.agent.recall import auto_recall_async
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        results = [_make_result("works at Nexus", category="claim", created_at=ts)]
        state = self._make_state(results)
        output = await auto_recall_async("where does Pedro work?", state)
        assert output is not None
        assert "(2025-01-01)" in output

    @pytest.mark.asyncio
    async def test_no_parens_when_created_at_none(self):
        from totalreclaw.agent.recall import auto_recall_async
        results = [_make_result("works at Nexus", category="claim", created_at=None)]
        state = self._make_state(results)
        output = await auto_recall_async("where?", state)
        assert output is not None
        assert "()" not in output

    @pytest.mark.asyncio
    async def test_returns_none_when_no_results(self):
        from totalreclaw.agent.recall import auto_recall_async
        state = self._make_state([])
        assert await auto_recall_async("query", state) is None


# ---------------------------------------------------------------------------
# hermes tools.recall — date field in JSON response
# ---------------------------------------------------------------------------


class TestHermesRecallDateField:
    @pytest.mark.asyncio
    async def test_date_field_present_when_created_at_set(self):
        from totalreclaw.hermes.tools import recall

        ts = datetime(2024, 7, 4, tzinfo=timezone.utc).timestamp()
        mock_result = SimpleNamespace(
            id="fact-1",
            text="user likes fireworks",
            category="episode",
            created_at=ts,
            rrf_score=0.8765,
        )

        mock_client = MagicMock()

        async def _recall(query, query_embedding=None, top_k=8, max_candidates=250):
            return [mock_result]

        mock_client.recall = _recall

        state = MagicMock()
        state.get_client.return_value = mock_client

        with patch("totalreclaw.embedding.get_embedding", side_effect=Exception("no model")):
            response = await recall({"query": "fireworks"}, state)

        data = json.loads(response)
        assert data["count"] == 1
        mem = data["memories"][0]
        assert mem["date"] == "2024-07-04"
        assert mem["type"] == "episode"
        assert mem["id"] == "fact-1"

    @pytest.mark.asyncio
    async def test_date_field_empty_string_when_no_created_at(self):
        from totalreclaw.hermes.tools import recall

        mock_result = SimpleNamespace(
            id="fact-2",
            text="likes dark mode",
            category="preference",
            created_at=None,
            rrf_score=0.5,
        )

        mock_client = MagicMock()

        async def _recall(query, query_embedding=None, top_k=8, max_candidates=250):
            return [mock_result]

        mock_client.recall = _recall

        state = MagicMock()
        state.get_client.return_value = mock_client

        with patch("totalreclaw.embedding.get_embedding", side_effect=Exception("no model")):
            response = await recall({"query": "dark mode"}, state)

        data = json.loads(response)
        mem = data["memories"][0]
        assert mem["date"] == ""

    @pytest.mark.asyncio
    async def test_result_missing_created_at_attr_gracefully(self):
        """Results that lack the created_at attribute entirely should not crash."""
        from totalreclaw.hermes.tools import recall

        # Deliberately omit created_at to simulate legacy results
        mock_result = SimpleNamespace(
            id="fact-3",
            text="some legacy memory",
            category="claim",
            rrf_score=0.3,
        )

        mock_client = MagicMock()

        async def _recall(query, query_embedding=None, top_k=8, max_candidates=250):
            return [mock_result]

        mock_client.recall = _recall

        state = MagicMock()
        state.get_client.return_value = mock_client

        with patch("totalreclaw.embedding.get_embedding", side_effect=Exception("no model")):
            response = await recall({"query": "legacy"}, state)

        data = json.loads(response)
        mem = data["memories"][0]
        # Should degrade to empty string, not raise
        assert mem["date"] == ""
