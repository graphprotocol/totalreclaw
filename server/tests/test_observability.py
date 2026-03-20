"""
Tests for server-blind observability features:
1. total_candidates_matched in search response
2. GET /v1/metrics per-user endpoint
3. SearchTelemetryStore in-memory metrics
"""
import os
import sys
import time
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.search_telemetry import (
    SearchSample,
    UserSearchTelemetry,
    SearchTelemetryStore,
    telemetry_store,
)


# ============ Unit Tests: SearchTelemetry ============


class TestSearchSample:
    """Tests for SearchSample dataclass."""

    def test_create_sample(self):
        s = SearchSample(
            total_candidates_matched=500,
            max_candidates_requested=1200,
            gin_query_ms=12.5,
        )
        assert s.total_candidates_matched == 500
        assert s.max_candidates_requested == 1200
        assert s.gin_query_ms == 12.5


class TestUserSearchTelemetry:
    """Tests for per-user rolling telemetry."""

    def test_empty_telemetry(self):
        t = UserSearchTelemetry()
        assert t.count == 0
        assert t.avg_candidates() == 0.0
        assert t.max_candidates_hit_rate() == 0.0
        assert t.p95_latency_ms() == 0.0

    def test_single_sample(self):
        t = UserSearchTelemetry()
        t.record(SearchSample(
            total_candidates_matched=100,
            max_candidates_requested=200,
            gin_query_ms=5.0,
        ))
        assert t.count == 1
        assert t.avg_candidates() == 100.0
        assert t.max_candidates_hit_rate() == 0.0  # 100 < 200
        assert t.p95_latency_ms() == 5.0

    def test_single_sample_hit(self):
        """When total >= max, hit_rate should be 1.0."""
        t = UserSearchTelemetry()
        t.record(SearchSample(
            total_candidates_matched=200,
            max_candidates_requested=200,
            gin_query_ms=3.0,
        ))
        assert t.max_candidates_hit_rate() == 1.0

    def test_multiple_samples_avg(self):
        t = UserSearchTelemetry()
        t.record(SearchSample(100, 500, 10.0))
        t.record(SearchSample(200, 500, 20.0))
        t.record(SearchSample(300, 500, 30.0))
        assert t.count == 3
        assert t.avg_candidates() == 200.0  # (100+200+300)/3

    def test_hit_rate_mixed(self):
        t = UserSearchTelemetry()
        # 2 hits, 2 misses
        t.record(SearchSample(500, 500, 5.0))  # hit (==)
        t.record(SearchSample(600, 500, 5.0))  # hit (>)
        t.record(SearchSample(100, 500, 5.0))  # miss
        t.record(SearchSample(499, 500, 5.0))  # miss
        assert t.max_candidates_hit_rate() == 0.5

    def test_p95_latency(self):
        t = UserSearchTelemetry()
        # 20 samples with latencies 1..20
        for i in range(1, 21):
            t.record(SearchSample(100, 500, float(i)))
        # P95 of [1..20]: index = int(20*0.95) = 19, sorted[19] = 20
        assert t.p95_latency_ms() == 20.0

    def test_p95_latency_100_samples(self):
        t = UserSearchTelemetry()
        for i in range(1, 101):
            t.record(SearchSample(100, 500, float(i)))
        # P95 of [1..100]: index = int(100*0.95) = 95, sorted[95] = 96
        assert t.p95_latency_ms() == 96.0

    def test_maxlen_eviction(self):
        """Deque should evict oldest entries when maxlen is exceeded."""
        t = UserSearchTelemetry(maxlen=5)
        for i in range(10):
            t.record(SearchSample(i * 100, 500, float(i)))
        assert t.count == 5
        # avg of last 5: 500,600,700,800,900 -> 700
        assert t.avg_candidates() == 700.0

    def test_maxlen_default(self):
        """Default maxlen should be 100."""
        t = UserSearchTelemetry()
        for i in range(150):
            t.record(SearchSample(i, 500, 1.0))
        assert t.count == 100


class TestSearchTelemetryStore:
    """Tests for the global telemetry store."""

    def test_empty_store(self):
        store = SearchTelemetryStore()
        assert store.get("nonexistent_user") is None

    def test_record_and_get(self):
        store = SearchTelemetryStore()
        store.record("user1", SearchSample(100, 500, 5.0))
        t = store.get("user1")
        assert t is not None
        assert t.count == 1
        assert t.avg_candidates() == 100.0

    def test_per_user_isolation(self):
        store = SearchTelemetryStore()
        store.record("user1", SearchSample(100, 500, 5.0))
        store.record("user2", SearchSample(200, 500, 10.0))
        t1 = store.get("user1")
        t2 = store.get("user2")
        assert t1.avg_candidates() == 100.0
        assert t2.avg_candidates() == 200.0

    def test_clear(self):
        store = SearchTelemetryStore()
        store.record("user1", SearchSample(100, 500, 5.0))
        store.clear()
        assert store.get("user1") is None


# ============ Integration Tests: Search Response ============


class TestSearchTotalCandidatesMatched:
    """Tests that search response includes total_candidates_matched."""

    def _make_mock_user(self, user_id="test-user-123"):
        """Create a mock user object."""
        return type("User", (), {
            "user_id": user_id,
            "auth_key_hash": bytes.fromhex("aa" * 32),
            "salt": bytes.fromhex("bb" * 32),
            "created_at": datetime.now(timezone.utc),
            "last_seen_at": None,
        })()

    def _make_mock_fact(self, fact_id=None, decay=1.0):
        """Create a mock fact."""
        from src.db.models import Fact
        f = Fact(
            id=fact_id or str(uuid.uuid4()),
            encrypted_blob=b"\x00" * 32,
            decay_score=decay,
            created_at=datetime.now(timezone.utc),
            version=1,
        )
        f.encrypted_embedding = None
        return f

    def test_search_includes_total_candidates_matched(self, client, mock_db):
        """Search response should include total_candidates_matched field."""
        user_id = "test-user-123"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()

        # Return 2 facts but total_matched = 15
        facts = [self._make_mock_fact(), self._make_mock_fact()]
        mock_db.search_facts_by_blind_indices = AsyncMock(
            return_value=(facts, 15)
        )

        # Clear telemetry before test
        telemetry_store.clear()

        response = client.post(
            "/v1/search",
            json={
                "user_id": user_id,
                "trapdoors": ["aa" * 32],
                "max_candidates": 2,
            },
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_candidates_matched"] == 15
        assert data["total_candidates"] == 2  # len(results)
        assert len(data["results"]) == 2

    def test_search_total_candidates_matched_zero(self, client, mock_db):
        """When no facts match, total_candidates_matched should be 0."""
        user_id = "test-user-123"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.search_facts_by_blind_indices = AsyncMock(
            return_value=([], 0)
        )

        telemetry_store.clear()

        response = client.post(
            "/v1/search",
            json={
                "user_id": user_id,
                "trapdoors": ["aa" * 32],
                "max_candidates": 100,
            },
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_candidates_matched"] == 0
        assert data["total_candidates"] == 0

    def test_search_records_telemetry(self, client, mock_db):
        """Search should record telemetry in the store."""
        user_id = "test-user-telemetry"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.search_facts_by_blind_indices = AsyncMock(
            return_value=([], 42)
        )

        telemetry_store.clear()

        response = client.post(
            "/v1/search",
            json={
                "user_id": user_id,
                "trapdoors": ["aa" * 32],
                "max_candidates": 100,
            },
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200

        # Verify telemetry was recorded
        t = telemetry_store.get(user_id)
        assert t is not None
        assert t.count == 1
        assert t.avg_candidates() == 42.0


# ============ Integration Tests: GET /v1/metrics ============


class TestUserMetricsEndpoint:
    """Tests for GET /v1/metrics per-user endpoint."""

    def _make_mock_user(self, user_id="test-user-123"):
        return type("User", (), {
            "user_id": user_id,
            "auth_key_hash": bytes.fromhex("aa" * 32),
            "salt": bytes.fromhex("bb" * 32),
            "created_at": datetime.now(timezone.utc),
            "last_seen_at": None,
        })()

    def test_metrics_requires_auth(self, client):
        """GET /v1/metrics without auth should return 401."""
        response = client.get("/v1/metrics")
        assert response.status_code == 401

    def test_metrics_invalid_auth(self, client, mock_db):
        """GET /v1/metrics with bad auth should return 401."""
        mock_db.get_user_by_auth_hash = AsyncMock(return_value=None)
        response = client.get(
            "/v1/metrics",
            headers={"Authorization": "Bearer " + "bb" * 32},
        )
        assert response.status_code == 401

    def test_metrics_empty_user(self, client, mock_db):
        """New user with no facts/searches should get all zeros."""
        user_id = "metrics-user-empty"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=0)

        telemetry_store.clear()

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["fact_count"] == 0
        assert data["avg_candidates_per_search"] == 0.0
        assert data["max_candidates_hit_rate"] == 0.0
        assert data["p95_search_latency_ms"] == 0.0

    def test_metrics_with_facts(self, client, mock_db):
        """User with facts should see correct fact_count."""
        user_id = "metrics-user-facts"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=1847)

        telemetry_store.clear()

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["fact_count"] == 1847

    def test_metrics_with_search_telemetry(self, client, mock_db):
        """User with search history should see rolling metrics."""
        user_id = "metrics-user-searches"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=500)

        telemetry_store.clear()

        # Simulate 10 searches: 3 hit max_candidates, varied latencies
        for i in range(10):
            total = 1200 if i < 3 else 500  # 3 hits
            telemetry_store.record(user_id, SearchSample(
                total_candidates_matched=total,
                max_candidates_requested=1200,
                gin_query_ms=float(i + 1),  # 1..10 ms
            ))

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["fact_count"] == 500
        # avg: (1200*3 + 500*7) / 10 = (3600+3500)/10 = 710
        assert data["avg_candidates_per_search"] == 710.0
        # hit_rate: 3/10 = 0.3
        assert data["max_candidates_hit_rate"] == 0.3
        # p95 of [1..10]: index = int(10*0.95) = 9, sorted[9] = 10
        assert data["p95_search_latency_ms"] == 10.0

    def test_metrics_does_not_leak_sensitive_data(self, client, mock_db):
        """Metrics response should not contain any sensitive fields."""
        user_id = "metrics-user-noleak"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=10)

        telemetry_store.clear()

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        text = response.text.lower()
        assert "password" not in text
        assert "auth_key" not in text
        assert "encrypted" not in text
        assert "bearer" not in text
        assert "salt" not in text

    def test_metrics_per_user_isolation(self, client, mock_db):
        """Metrics should only show data for the authenticated user."""
        user_a_id = "user-a"
        user_b_id = "user-b"
        auth_key = "aa" * 32

        telemetry_store.clear()

        # Record telemetry for user_b (different user)
        telemetry_store.record(user_b_id, SearchSample(
            total_candidates_matched=9999,
            max_candidates_requested=100,
            gin_query_ms=999.0,
        ))

        # Authenticate as user_a
        mock_user_a = self._make_mock_user(user_a_id)
        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user_a)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=0)

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        # user_a has no telemetry — should see zeros, not user_b's data
        assert data["avg_candidates_per_search"] == 0.0
        assert data["max_candidates_hit_rate"] == 0.0
        assert data["p95_search_latency_ms"] == 0.0

    def test_metrics_response_format(self, client, mock_db):
        """Response should have exactly the expected fields."""
        user_id = "metrics-user-format"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=0)

        telemetry_store.clear()

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        expected_keys = {
            "fact_count",
            "avg_candidates_per_search",
            "max_candidates_hit_rate",
            "p95_search_latency_ms",
        }
        assert set(data.keys()) == expected_keys

    def test_metrics_fact_count_type(self, client, mock_db):
        """fact_count should be an integer."""
        user_id = "metrics-type-check"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=42)

        telemetry_store.clear()

        response = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        data = response.json()
        assert isinstance(data["fact_count"], int)
        assert isinstance(data["avg_candidates_per_search"], float)
        assert isinstance(data["max_candidates_hit_rate"], float)
        assert isinstance(data["p95_search_latency_ms"], float)


# ============ Integration Tests: Search + Metrics E2E Flow ============


class TestSearchMetricsE2EFlow:
    """End-to-end test: search then check metrics."""

    def _make_mock_user(self, user_id="e2e-user"):
        return type("User", (), {
            "user_id": user_id,
            "auth_key_hash": bytes.fromhex("aa" * 32),
            "salt": bytes.fromhex("bb" * 32),
            "created_at": datetime.now(timezone.utc),
            "last_seen_at": None,
        })()

    def _make_mock_fact(self, fact_id=None):
        from src.db.models import Fact
        f = Fact(
            id=fact_id or str(uuid.uuid4()),
            encrypted_blob=b"\x00" * 32,
            decay_score=1.0,
            created_at=datetime.now(timezone.utc),
            version=1,
        )
        f.encrypted_embedding = None
        return f

    def test_search_then_metrics(self, client, mock_db):
        """Run searches, then verify metrics reflect them."""
        user_id = "e2e-user"
        auth_key = "aa" * 32
        mock_user = self._make_mock_user(user_id)

        mock_db.get_user_by_auth_hash = AsyncMock(return_value=mock_user)
        mock_db.update_last_seen = AsyncMock()
        mock_db.count_active_facts = AsyncMock(return_value=100)

        telemetry_store.clear()

        facts = [self._make_mock_fact() for _ in range(3)]

        # Simulate 5 searches with different total_matched values
        for total in [50, 100, 150, 200, 1200]:
            mock_db.search_facts_by_blind_indices = AsyncMock(
                return_value=(facts, total)
            )
            resp = client.post(
                "/v1/search",
                json={
                    "user_id": user_id,
                    "trapdoors": ["aa" * 32],
                    "max_candidates": 1200,
                },
                headers={"Authorization": f"Bearer {auth_key}"},
            )
            assert resp.status_code == 200
            assert resp.json()["total_candidates_matched"] == total

        # Now check metrics
        resp = client.get(
            "/v1/metrics",
            headers={"Authorization": f"Bearer {auth_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fact_count"] == 100
        # avg: (50+100+150+200+1200)/5 = 340
        assert data["avg_candidates_per_search"] == 340.0
        # hit_rate: 1/5 = 0.2 (only 1200 >= 1200)
        assert data["max_candidates_hit_rate"] == 0.2
        # p95 latency will be > 0 (actual timing from mock calls)
        assert data["p95_search_latency_ms"] >= 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
