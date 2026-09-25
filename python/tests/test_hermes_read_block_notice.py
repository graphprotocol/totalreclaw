"""Hermes-side surfacing of the relay read-pause notice (#662).

Covers auto-recall, the ``pre_llm_call`` hook's off-turn notice, the
MemoryProvider ``prefetch`` path, the Hermes tool JSON payload shape, and
the ``status`` tool's ``reads`` block. See
docs/specs/totalreclaw/read-error-surfacing.md §4.5-4.6.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from totalreclaw.agent.recall import auto_recall, auto_recall_async
from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes import hooks, tools
from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider
from totalreclaw.relay import (
    ReadBlockState,
    RelayRateLimited,
    RelayReadQuotaExceeded,
)


def _resp(status: int, json_body) -> httpx.Response:
    req = httpx.Request("POST", "https://api-staging.totalreclaw.xyz/v1/subgraph")
    return httpx.Response(status, json=json_body, request=req)


def _mk_block(err, episode: int, pause_s: float = 900.0) -> ReadBlockState:
    now = datetime.now(timezone.utc)
    return ReadBlockState(
        error=err,
        paused_until=__import__("time").monotonic() + pause_s,
        paused_until_utc=now + timedelta(seconds=pause_s),
        since_utc=now,
        episode=episode,
    )


def _quota_err(upgrade_url="https://totalreclaw.xyz/pricing") -> RelayReadQuotaExceeded:
    return RelayReadQuotaExceeded(
        _resp(403, {"error": "quota_exceeded"}), legacy=True, upgrade_url=upgrade_url
    )


def _rate_err(retry_after_s=120) -> RelayRateLimited:
    return RelayRateLimited(_resp(429, {}), retry_after_s=retry_after_s)


def _mk_state(configured: bool = True) -> PluginState:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    if configured:
        state._client = MagicMock()
    return state


class TestAutoRecallNotice:
    def test_first_call_full_quota_notice(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        notice = auto_recall("what do I like?", state)
        assert notice is not None
        assert "paused" in notice
        assert "https://totalreclaw.xyz/pricing" in notice
        assert "Relevant memories" not in notice

    def test_second_call_same_episode_is_compact(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        first = auto_recall("q1", state)
        second = auto_recall("q2", state)
        assert "paused" in first
        assert "still paused" in second
        assert second != first

    def test_new_episode_after_clear_is_full_again(self):
        state = _mk_state()
        client = state.get_client()
        blk1 = _mk_block(_quota_err(), episode=1)
        client.read_block = blk1
        client.recall = AsyncMock(side_effect=_quota_err())
        first = auto_recall("q1", state)
        assert "used its monthly memory-read allowance" in first

        # Episode cleared + a NEW episode (2) begins.
        blk2 = _mk_block(_quota_err(), episode=2)
        client.read_block = blk2
        client.recall = AsyncMock(side_effect=_quota_err())
        third = auto_recall("q3", state)
        assert "used its monthly memory-read allowance" in third
        assert "still paused" not in third

    def test_rate_limited_variant_shows_minutes(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_rate_err(retry_after_s=180), episode=1, pause_s=180)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_rate_err(retry_after_s=180))

        notice = auto_recall("q", state)
        assert "too many requests" in notice
        assert "minutes" in notice

    @pytest.mark.asyncio
    async def test_async_variant_also_surfaces_notice(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        notice = await auto_recall_async("q", state)
        assert notice is not None
        assert "paused" in notice


class TestPreLlmCallOffTurnNotice:
    def test_first_turn_auto_recall_raises_notice_appears_once(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        result = hooks.pre_llm_call(state, user_message="hello there", is_first_turn=True)
        assert result is not None
        context = result["context"]
        assert context.count("Memory lookups are paused") == 1

    def test_later_turn_unannounced_block_full_once_then_nothing_then_cleared(self):
        state = _mk_state()
        client = state.get_client()
        # A block appeared off-turn (e.g. background dedup hit it) — no
        # auto-recall call happens this turn (not first turn).
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(return_value=[])

        result1 = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
        assert result1 is not None
        assert "Memory lookups are paused" in result1["context"]

        # Same turn/episode again — already announced, nothing new.
        result2 = hooks.pre_llm_call(state, user_message="hello again", is_first_turn=False)
        assert result2 is None or "Memory lookups are paused" not in (result2 or {}).get("context", "")

        # Cleared — one "working again" line, then nothing.
        client.read_block = None
        result3 = hooks.pre_llm_call(state, user_message="still there?", is_first_turn=False)
        assert result3 is not None
        assert "working again" in result3["context"]

        result4 = hooks.pre_llm_call(state, user_message="ok", is_first_turn=False)
        assert result4 is None or "working again" not in (result4 or {}).get("context", "")


class TestMemoryProviderPrefetch:
    def test_prefetch_returns_notice_string(self):
        state = _mk_state()
        provider = TotalReclawMemoryProvider(state)
        with patch(
            "totalreclaw.hermes.hooks.recall_for_query",
            return_value="[totalreclaw] Memory lookups are paused: test.",
        ):
            out = provider.prefetch("who am I?", session_id="s1")
        assert out == "[totalreclaw] Memory lookups are paused: test."


class TestHermesToolPayloadShape:
    @pytest.mark.asyncio
    async def test_recall_tool_payload_shape(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        raw = await tools.recall({"query": "hi"}, state)
        payload = json.loads(raw)
        assert payload["error_code"] == "read_quota_exceeded"
        assert "instruction" in payload
        assert "count" not in payload
        assert "memories" not in payload

    @pytest.mark.asyncio
    async def test_export_tool_payload_shape(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.export_all = AsyncMock(side_effect=_quota_err())

        raw = await tools.export_all({}, state)
        payload = json.loads(raw)
        assert payload["error_code"] == "read_quota_exceeded"
        assert "instruction" in payload
        assert "count" not in payload

    @pytest.mark.asyncio
    async def test_pin_tool_payload_shape(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.pin_fact = AsyncMock(side_effect=_quota_err())

        raw = await tools.pin({"fact_id": "abc"}, state)
        payload = json.loads(raw)
        assert payload["error_code"] == "read_quota_exceeded"
        assert "instruction" in payload

    @pytest.mark.asyncio
    async def test_retype_tool_payload_shape(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.retype = AsyncMock(side_effect=_quota_err())

        raw = await tools.retype({"fact_id": "abc", "new_type": "preference"}, state)
        payload = json.loads(raw)
        assert payload["error_code"] == "read_quota_exceeded"
        assert "instruction" in payload


class TestStatusToolReadsBlock:
    @pytest.mark.asyncio
    async def test_status_reports_paused_reads(self):
        from totalreclaw.relay import BillingStatus

        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.status = AsyncMock(
            return_value=BillingStatus(
                tier="free", free_writes_used=1, free_writes_limit=250
            )
        )
        client.resolved_wallet_address = "0xabc"
        client.eoa_address = "0xdef"

        raw = await tools.status({}, state)
        payload = json.loads(raw)
        assert payload["reads"]["paused"] is True
        assert payload["reads"]["reason"] == "read_quota"

    @pytest.mark.asyncio
    async def test_status_reports_unpaused_reads(self):
        from totalreclaw.relay import BillingStatus

        state = _mk_state()
        client = state.get_client()
        client.read_block = None
        client.status = AsyncMock(
            return_value=BillingStatus(
                tier="free", free_writes_used=1, free_writes_limit=250
            )
        )
        client.resolved_wallet_address = "0xabc"
        client.eoa_address = "0xdef"

        raw = await tools.status({}, state)
        payload = json.loads(raw)
        assert payload["reads"]["paused"] is False


class TestRememberUnaffected:
    @pytest.mark.asyncio
    async def test_remember_succeeds_while_reads_paused(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.remember = AsyncMock(return_value="new-fact-id")

        raw = await tools.remember({"text": "Pedro likes espresso"}, state)
        payload = json.loads(raw)
        assert "error" not in payload or payload.get("stored") is not False


class TestBillingCacheUnaffectedByReadDenial:
    """Decision #3 (2026-09-25, binding): the read-denial path must NOT
    invalidate the billing cache in this PR — that's a separate,
    already-filed issue (the write-quota billing-cache writer is dead in
    production; wiring a new caller here would be an untested, out-of-scope
    change). This deviates from the original §4.5 draft, which proposed
    calling ``invalidate_billing_cache()`` on the first announcement of a
    ``read_quota`` episode."""

    def test_quota_notice_leaves_billing_cache_untouched(self):
        state = _mk_state()
        state.set_billing_cache({"tier": "free"})
        client = state.get_client()
        blk = _mk_block(_quota_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_quota_err())

        auto_recall("q", state)
        assert state.get_cached_billing() == {"tier": "free"}

    def test_rate_limited_notice_leaves_billing_cache_untouched(self):
        state = _mk_state()
        state.set_billing_cache({"tier": "free"})
        client = state.get_client()
        blk = _mk_block(_rate_err(), episode=1)
        client.read_block = blk
        client.recall = AsyncMock(side_effect=_rate_err())

        auto_recall("q", state)
        assert state.get_cached_billing() == {"tier": "free"}
