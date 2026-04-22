"""Tests for the 3.3.1-rc.3 per-account submission mutex that serializes
concurrent UserOp submissions to the same Smart Account address,
eliminating the AA25 nonce race.

Unit-tests the lock primitive directly; full-stack bundler behaviour is
covered by the existing integration suite.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from totalreclaw.userop import (
    _get_sender_lock,
    _reset_sender_locks_for_tests,
)


@pytest.fixture(autouse=True)
def _clear_locks():
    _reset_sender_locks_for_tests()
    yield
    _reset_sender_locks_for_tests()


class TestPerAccountLock:
    @pytest.mark.asyncio
    async def test_same_sender_serialized(self):
        """Concurrent calls to the same sender run one at a time."""
        timeline: list[str] = []

        async def op(label: str, dur_s: float, sender: str) -> str:
            lock = await _get_sender_lock(sender)
            async with lock:
                timeline.append(f"{label}-start")
                await asyncio.sleep(dur_s)
                timeline.append(f"{label}-end")
                return label

        results = await asyncio.gather(
            op("A", 0.03, "0xabc"),
            op("B", 0.01, "0xabc"),
        )
        assert results == ["A", "B"]
        # Must be A-start, A-end, B-start, B-end — never interleaved.
        assert timeline == ["A-start", "A-end", "B-start", "B-end"]

    @pytest.mark.asyncio
    async def test_different_sender_parallel(self):
        """Calls on different senders DO run in parallel — no
        over-serialization across accounts."""
        async def op(dur_s: float, sender: str) -> float:
            lock = await _get_sender_lock(sender)
            t0 = time.monotonic()
            async with lock:
                await asyncio.sleep(dur_s)
            return time.monotonic() - t0

        t_start = time.monotonic()
        await asyncio.gather(
            op(0.05, "0xAAA"),
            op(0.05, "0xBBB"),
        )
        elapsed = time.monotonic() - t_start
        # Each takes 50ms; running in parallel should finish in ~50ms,
        # NOT ~100ms. Leave slack for test runner jitter.
        assert elapsed < 0.09, f"ran sequentially (elapsed={elapsed:.3f}s)"

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        """'0xABC' and '0xabc' must share a lock."""
        lock1 = await _get_sender_lock("0xABC")
        lock2 = await _get_sender_lock("0xabc")
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_lock_is_idempotent(self):
        """Calling _get_sender_lock twice for the same sender returns the
        SAME lock instance (so the map doesn't blow up over time)."""
        lock1 = await _get_sender_lock("0xdef456")
        lock2 = await _get_sender_lock("0xdef456")
        assert lock1 is lock2
        assert lock1.locked() is False

    @pytest.mark.asyncio
    async def test_failure_releases_lock(self):
        """When one locked call raises, the next still runs."""
        async def first(sender: str) -> None:
            lock = await _get_sender_lock(sender)
            async with lock:
                raise RuntimeError("boom")

        async def second(sender: str) -> str:
            lock = await _get_sender_lock(sender)
            async with lock:
                return "ok"

        with pytest.raises(RuntimeError):
            await first("0xccc")
        result = await second("0xccc")
        assert result == "ok"
