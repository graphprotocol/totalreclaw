"""F3 (internal #423) — local monotonic nonce cache + receipt-aware retry.

rc12 QA: 44 import facts chunked 15+15+14 stored only the FIRST 15. Root
cause: the client never waits for UserOp inclusion, and the EntryPoint
nonce only advances at execution — so batches 2 and 3 re-fetched batch 1's
nonce, collided (AA25), and exhausted the ~7s blind retry window while
Gnosis blocks take ~5s.

The fix: a per-sender local nonce cache lets sequential submissions use
nonce+1 pipelining (bundlers accept queued sequential-nonce ops), and the
AA25 retry waits for the chain nonce to actually advance instead of
sleeping blind.
"""
from __future__ import annotations

import asyncio

import pytest

import totalreclaw.userop as userop
from totalreclaw.userop import (
    _resolve_submission_nonce,
    _record_submitted_nonce,
    _reset_nonce_cache_for_tests,
    _await_nonce_advance,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _reset_nonce_cache_for_tests()
    yield
    _reset_nonce_cache_for_tests()


class TestNonceCache:
    def test_no_cache_uses_chain_nonce(self):
        assert _resolve_submission_nonce("0xAbC", 5) == 5

    def test_cache_wins_when_chain_is_stale(self):
        """The QA scenario: batch 1 sent with nonce 5 but unmined — chain
        still reports 5; batch 2 must use 6."""
        _record_submitted_nonce("0xabc", 5)
        assert _resolve_submission_nonce("0xABC", 5) == 6

    def test_chain_wins_when_ahead_of_cache(self):
        """Cache is self-healing: external activity (another device) can
        advance the chain past our cache."""
        _record_submitted_nonce("0xabc", 5)
        assert _resolve_submission_nonce("0xabc", 9) == 9

    def test_sequential_pipeline(self):
        """Three back-to-back batches with the chain stuck at 5 → 5, 6, 7."""
        chain = 5
        n1 = _resolve_submission_nonce("0xabc", chain)
        _record_submitted_nonce("0xabc", n1)
        n2 = _resolve_submission_nonce("0xabc", chain)
        _record_submitted_nonce("0xabc", n2)
        n3 = _resolve_submission_nonce("0xabc", chain)
        assert (n1, n2, n3) == (5, 6, 7)

    def test_record_never_decreases(self):
        _record_submitted_nonce("0xabc", 9)
        _record_submitted_nonce("0xabc", 3)  # late/out-of-order record
        assert _resolve_submission_nonce("0xabc", 0) == 10

    def test_reset_on_aa25_falls_back_to_chain(self):
        _record_submitted_nonce("0xabc", 42)
        userop._reset_sender_nonce("0xABC")
        assert _resolve_submission_nonce("0xabc", 5) == 5

    def test_keys_are_case_insensitive(self):
        _record_submitted_nonce("0xAbCd", 7)
        assert _resolve_submission_nonce("0xabcd", 0) == 8


class TestAwaitNonceAdvance:
    @pytest.mark.asyncio
    async def test_returns_when_nonce_reaches_target(self, monkeypatch):
        seq = [5, 5, 7]

        async def fake_get_nonce(http, sender, chain_id):
            return seq.pop(0) if seq else 7

        monkeypatch.setattr(userop, "get_nonce", fake_get_nonce)
        result = await _await_nonce_advance(
            None, "0xabc", 100, min_nonce=6, timeout_s=5.0, poll_s=0.01,
        )
        assert result == 7

    @pytest.mark.asyncio
    async def test_times_out_returning_last_seen(self, monkeypatch):
        async def fake_get_nonce(http, sender, chain_id):
            return 5  # never advances

        monkeypatch.setattr(userop, "get_nonce", fake_get_nonce)
        result = await _await_nonce_advance(
            None, "0xabc", 100, min_nonce=6, timeout_s=0.05, poll_s=0.01,
        )
        assert result == 5

    @pytest.mark.asyncio
    async def test_survives_rpc_errors(self, monkeypatch):
        calls = {"n": 0}

        async def flaky_get_nonce(http, sender, chain_id):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("rpc hiccup")
            return 6

        monkeypatch.setattr(userop, "get_nonce", flaky_get_nonce)
        result = await _await_nonce_advance(
            None, "0xabc", 100, min_nonce=6, timeout_s=5.0, poll_s=0.01,
        )
        assert result == 6
