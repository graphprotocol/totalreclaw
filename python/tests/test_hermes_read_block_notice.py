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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk1.episode
        client.recall = AsyncMock(side_effect=_quota_err())
        first = auto_recall("q1", state)
        assert "used its monthly memory-read allowance" in first

        # Episode cleared + a NEW episode (2) begins.
        blk2 = _mk_block(_quota_err(), episode=2)
        client.read_block = blk2
        client.read_block_episode = blk2.episode
        client.recall = AsyncMock(side_effect=_quota_err())
        third = auto_recall("q3", state)
        assert "used its monthly memory-read allowance" in third
        assert "still paused" not in third

    def test_rate_limited_variant_shows_minutes(self):
        state = _mk_state()
        client = state.get_client()
        blk = _mk_block(_rate_err(retry_after_s=180), episode=1, pause_s=180)
        client.read_block = blk
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
        client.recall = AsyncMock(return_value=[])

        result1 = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
        assert result1 is not None
        assert "Memory lookups are paused" in result1["context"]

        # Same turn/episode again — already announced, nothing new.
        result2 = hooks.pre_llm_call(state, user_message="hello again", is_first_turn=False)
        assert result2 is None or "Memory lookups are paused" not in (result2 or {}).get("context", "")

        # Cleared — one "working again" line, then nothing.
        client.read_block = None
        client.read_block_episode = None
        result3 = hooks.pre_llm_call(state, user_message="still there?", is_first_turn=False)
        assert result3 is not None
        assert "working again" in result3["context"]

        result4 = hooks.pre_llm_call(state, user_message="ok", is_first_turn=False)
        assert result4 is None or "working again" not in (result4 or {}).get("context", "")


class _RealClientProxy:
    """Exposes exactly the ``.read_block`` / ``.read_block_episode``
    surface ``TotalReclaw`` does (see ``client.py``), backed by a REAL
    ``RelayClient`` instead of a hand-rolled ``ReadBlockState`` — so the
    false-recovery regression below exercises the actual state machine
    (``_set_read_block`` / ``_clear_read_block`` / ``read_block()`` /
    ``read_block_episode()``), not a mock standing in for it.
    """

    def __init__(self, relay):
        self._relay = relay

    @property
    def read_block(self):
        return self._relay.read_block()

    @property
    def read_block_episode(self):
        return self._relay.read_block_episode()


class TestPendingNoticeFalseRecoveryRegression:
    """Blocker fix (Opus review round on PR #664): ``client.read_block``
    goes ``None`` BOTH when a success clears the pause AND when the
    re-probe deadline merely passes with no probe attempted yet.
    ``pending_read_block_notice`` must only ever say "working again" for
    the former — keying off ``read_block_episode`` (kept until a real
    ``_clear_read_block``), not ``read_block`` (the deadline-gated view).
    """

    def test_deadline_expiry_alone_is_not_reported_as_recovery(self):
        import time as _time

        from totalreclaw.relay import RelayClient

        relay = RelayClient(relay_url="https://api-staging.totalreclaw.xyz")
        client = _RealClientProxy(relay)
        state = _mk_state(configured=False)  # no client needed on state itself

        err1 = _quota_err()
        relay._set_read_block(err1)
        episode = relay.read_block_episode()
        assert episode is not None

        # 1) First call: full announcement, latches (episode, kind).
        first = state.pending_read_block_notice(client)
        assert first is not None
        assert "paused" in first

        # 2) Same turn/episode again, deadline not yet passed: nothing new.
        again = state.pending_read_block_notice(client)
        assert again is None

        # 3) Advance monotonic PAST the deadline. NO probe has happened —
        #    read_block() now goes None, but read_block_episode() must
        #    still report the same episode (the record is only cleared by
        #    an actual success). This must NOT be reported as "working
        #    again", and must NOT reset the latch.
        future = _time.monotonic() + 100000
        with patch("totalreclaw.relay.time.monotonic", return_value=future):
            assert relay.read_block() is None  # sanity: the buggy signal
            assert relay.read_block_episode() == episode  # the record persists

            after_deadline = state.pending_read_block_notice(client)
            assert after_deadline is None, (
                f"deadline expiry alone must not be reported as recovery, got: {after_deadline!r}"
            )

            # 4) A probe re-blocks under the SAME episode (still denied).
            #    No new full announcement — already announced.
            err2 = _quota_err()
            relay._set_read_block(err2)
            assert relay.read_block_episode() == episode  # same episode, reused

            reblocked = state.pending_read_block_notice(client)
            assert reblocked is None, (
                f"a re-block under the same (episode, kind) must not re-announce, got: {reblocked!r}"
            )

        # 5) NOW a real success clears it (the actual clear path — not a
        #    hand-set ``client.read_block = None``).
        relay._clear_read_block()
        assert relay.read_block_episode() is None

        recovered = state.pending_read_block_notice(client)
        assert recovered == "[totalreclaw] Memory lookups are working again."

        # 6) Exactly once — calling again after the recovery line reports
        #    nothing further.
        again_after_recovery = state.pending_read_block_notice(client)
        assert again_after_recovery is None

    def test_kind_change_within_episode_reannounces_in_full(self):
        """Nit 3: a rate_limited -> read_quota transition within one
        episode (the relay re-blocks with a different denial kind before
        either clears) must re-announce in full, not stay compact/suppressed
        — the user needs the quota wording + upgrade link, not stale
        rate-limit text."""
        from totalreclaw.relay import RelayClient

        relay = RelayClient(relay_url="https://api-staging.totalreclaw.xyz")
        client = _RealClientProxy(relay)
        state = _mk_state(configured=False)

        relay._set_read_block(_rate_err())
        first = state.pending_read_block_notice(client)
        assert first is not None
        assert "too many requests" in first

        # Same episode (never cleared), but the kind flips to quota.
        relay._set_read_block(_quota_err())
        assert relay.read_block_episode() == 1  # confirms same episode reused

        second = state.pending_read_block_notice(client)
        assert second is not None, "a kind change within the episode must re-announce in full"
        assert "monthly memory-read allowance" in second


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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = None
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
        client.read_block_episode = blk.episode
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
        client.read_block_episode = blk.episode
        client.recall = AsyncMock(side_effect=_quota_err())

        auto_recall("q", state)
        assert state.get_cached_billing() == {"tier": "free"}

    def test_rate_limited_notice_leaves_billing_cache_untouched(self):
        state = _mk_state()
        state.set_billing_cache({"tier": "free"})
        client = state.get_client()
        blk = _mk_block(_rate_err(), episode=1)
        client.read_block = blk
        client.read_block_episode = blk.episode
        client.recall = AsyncMock(side_effect=_rate_err())

        auto_recall("q", state)
        assert state.get_cached_billing() == {"tier": "free"}
