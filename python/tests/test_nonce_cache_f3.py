"""F3 (internal #423), restated for #646 / #650: the nonce FLOOR.

#423 added a pipelined next-nonce cache recorded at bundler-ACCEPT time.
Pimlico's paymaster simulation rejects the pipelined nonce once the
predecessor is bundled but not mined (#646: AA25, then a wait for nonce + 2
that never comes), so the cache is gone. What remains is a floor that only
ever records EXECUTED ops (``nonce + 1``): a stale replica read can never lower
the nonce below it. Writes are ordered by the predecessor gate instead of by
pipelining.
"""
from __future__ import annotations

import pytest

import totalreclaw.userop as userop
from totalreclaw.userop import (
    _await_nonce_advance,
    _floor_of,
    _raise_floor,
    _reset_floor_to,
    _reset_nonce_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _reset_nonce_cache_for_tests()
    yield
    _reset_nonce_cache_for_tests()


class TestNonceFloor:
    def test_no_floor_means_zero(self):
        assert _floor_of("0xAbC") == 0

    def test_floor_never_decreases(self):
        _raise_floor("0xabc", 9)
        _raise_floor("0xabc", 3)  # late/out-of-order record
        assert _floor_of("0xabc") == 9

    def test_keys_are_case_insensitive(self):
        _raise_floor("0xAbCd", 7)
        assert _floor_of("0xabcd") == 7

    def test_reorg_reset_is_the_only_way_down(self):
        _raise_floor("0xabc", 42)
        _reset_floor_to("0xABC", 5)
        assert _floor_of("0xabc") == 5

    def test_nonce_is_max_of_chain_and_floor(self):
        """The rule the writers apply: ``max(chain_nonce, floor)``."""
        _raise_floor("0xabc", 6)
        assert max(5, _floor_of("0xabc")) == 6      # replica lag: floor wins
        assert max(9, _floor_of("0xabc")) == 9      # external activity: chain wins

    def test_accept_no_longer_records_a_nonce(self):
        """The accept-time pipelined record (#423) is gone."""
        assert not hasattr(userop, "_record_submitted_nonce")
        assert not hasattr(userop, "_resolve_submission_nonce")


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
