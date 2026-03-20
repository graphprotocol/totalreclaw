"""
Per-user search telemetry for server-blind observability.

Tracks rolling search metrics in-memory (no DB table needed):
- total_candidates_matched per search
- whether max_candidates was hit
- GIN query latency

All data is per-user and kept in bounded deques (maxlen=100).
This is operational telemetry, not persistent state.
"""
import statistics
import threading
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional


# Maximum number of recent searches to track per user
_WINDOW_SIZE = 100


@dataclass
class SearchSample:
    """A single search observation."""
    total_candidates_matched: int
    max_candidates_requested: int
    gin_query_ms: float


class UserSearchTelemetry:
    """Rolling search metrics for a single user."""

    def __init__(self, maxlen: int = _WINDOW_SIZE):
        self._samples: deque[SearchSample] = deque(maxlen=maxlen)

    def record(self, sample: SearchSample) -> None:
        """Record a search observation."""
        self._samples.append(sample)

    @property
    def count(self) -> int:
        return len(self._samples)

    def avg_candidates(self) -> float:
        """Rolling average of total_candidates_matched."""
        if not self._samples:
            return 0.0
        return statistics.mean(s.total_candidates_matched for s in self._samples)

    def max_candidates_hit_rate(self) -> float:
        """Fraction of searches where total_candidates >= max_candidates requested."""
        if not self._samples:
            return 0.0
        hits = sum(
            1 for s in self._samples
            if s.total_candidates_matched >= s.max_candidates_requested
        )
        return hits / len(self._samples)

    def p95_latency_ms(self) -> float:
        """P95 of GIN query latency in milliseconds."""
        if not self._samples:
            return 0.0
        latencies = sorted(s.gin_query_ms for s in self._samples)
        idx = int(len(latencies) * 0.95)
        # Clamp to last element if idx equals length
        idx = min(idx, len(latencies) - 1)
        return latencies[idx]


class SearchTelemetryStore:
    """
    Thread-safe store of per-user search telemetry.

    Access via the module-level singleton `telemetry_store`.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._users: Dict[str, UserSearchTelemetry] = defaultdict(UserSearchTelemetry)

    def record(self, user_id: str, sample: SearchSample) -> None:
        """Record a search sample for a user."""
        with self._lock:
            self._users[user_id].record(sample)

    def get(self, user_id: str) -> Optional[UserSearchTelemetry]:
        """Get telemetry for a user. Returns None if no data."""
        with self._lock:
            t = self._users.get(user_id)
            return t if t and t.count > 0 else None

    def clear(self) -> None:
        """Clear all telemetry data (for testing)."""
        with self._lock:
            self._users.clear()


# Module-level singleton
telemetry_store = SearchTelemetryStore()
