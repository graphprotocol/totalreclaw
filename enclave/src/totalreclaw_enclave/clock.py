"""Time source. Every timestamp the enclave stores is integer Unix seconds (UTC)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> int:
        """Current Unix time in whole seconds (UTC)."""
        ...


class SystemClock:
    def now(self) -> int:
        return int(time.time())


class FixedClock:
    """Deterministic clock for tests (importable by every later leaf's tests)."""

    def __init__(self, now: int) -> None:
        self._now = now

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += seconds


def utc_month(ts: int) -> str:
    """``YYYY-MM`` of a Unix timestamp in UTC.

    The format of ``usage_local.month`` and of the relay's
    ``POST /v1/usage/ingest`` body ``month`` (DEP-2 D3).
    """
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")
