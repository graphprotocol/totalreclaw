from __future__ import annotations

import time

from totalreclaw_enclave.clock import FixedClock, SystemClock, utc_month


def test_fixed_clock_advances() -> None:
    clock = FixedClock(1_790_000_000)
    clock.advance(61)
    assert clock.now() == 1_790_000_061


def test_system_clock_is_unix_seconds() -> None:
    assert abs(SystemClock().now() - int(time.time())) <= 1


def test_utc_month_is_zero_padded_utc() -> None:
    assert utc_month(1_790_000_000) == "2026-09"
    assert utc_month(1_767_225_599) == "2025-12"  # 2025-12-31T23:59:59Z
    assert utc_month(1_767_225_600) == "2026-01"
