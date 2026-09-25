"""Tests for the relay read-denial typed errors + client-wide read pause (#662).

See docs/specs/totalreclaw/read-error-surfacing.md for the design this
implements.
"""
from __future__ import annotations

import email.utils
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

from totalreclaw.relay import (
    RelayClient,
    RelayReadError,
    RelayReadBlocked,
    RelayReadQuotaExceeded,
    RelayRateLimited,
    _classify_subgraph_error,
    _pause_seconds,
    _retry_after_seconds,
)


def _resp(status: int, json_body=None, content=None, headers=None) -> httpx.Response:
    req = httpx.Request("POST", "https://api-staging.totalreclaw.xyz/v1/subgraph")
    if content is not None:
        return httpx.Response(status, content=content, headers=headers or {}, request=req)
    return httpx.Response(status, json=json_body, headers=headers or {}, request=req)


def _relay_with_handler(handler) -> RelayClient:
    transport = httpx.MockTransport(handler)
    rc = RelayClient(relay_url="https://api-staging.totalreclaw.xyz")

    async def _mock_get_http() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    rc._get_http = _mock_get_http  # type: ignore[assignment]
    return rc


class TestClassifySubgraphError:
    def test_legacy_403_quota_exceeded(self):
        resp = _resp(
            403,
            {
                "error": "quota_exceeded",
                "message": "Read limit reached this month",
                "upgrade_url": "https://totalreclaw.xyz/pricing",
            },
        )
        err = _classify_subgraph_error(resp)
        assert isinstance(err, RelayReadQuotaExceeded)
        assert err.legacy is True
        assert err.upgrade_url == "https://totalreclaw.xyz/pricing"
        assert isinstance(err, httpx.HTTPStatusError)
        assert err.response.status_code == 403

    def test_new_429_read_quota_exceeded(self):
        resets_at = "2026-10-01T00:00:00Z"
        resp = _resp(
            429,
            {
                "error": "read_quota_exceeded",
                "error_code": "read_quota_exceeded",
                "message": "monthly read limit reached",
                "tier": "pro",
                "limit": 1500,
                "used": 1500,
                "resets_at": resets_at,
                "upgrade_url": None,
            },
            headers={"Retry-After": "60"},
        )
        err = _classify_subgraph_error(resp)
        assert isinstance(err, RelayReadQuotaExceeded)
        assert err.legacy is False
        assert err.tier == "pro"
        assert err.limit == 1500
        assert err.used == 1500
        assert err.resets_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert err.resets_at.tzinfo is not None
        # Pro body -> no upgrade link.
        assert err.upgrade_url is None

    def test_legacy_rate_limit_body(self):
        resp = _resp(429, {"success": False, "error": "Rate limit exceeded. Try again later.", "retry_after": 120})
        err = _classify_subgraph_error(resp)
        assert isinstance(err, RelayRateLimited)
        assert err.retry_after_s == 120

    def test_rate_limit_header_only_delta_seconds(self):
        resp = _resp(429, {"error_code": "rate_limited"}, headers={"Retry-After": "90"})
        err = _classify_subgraph_error(resp)
        assert isinstance(err, RelayRateLimited)
        assert err.retry_after_s == 90.0

    def test_rate_limit_header_http_date(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=45)
        http_date = email.utils.format_datetime(target, usegmt=True)
        resp = _resp(429, {"error_code": "rate_limited"}, headers={"Retry-After": http_date})
        err = _classify_subgraph_error(resp)
        assert isinstance(err, RelayRateLimited)
        assert err.retry_after_s is not None
        assert 40 <= err.retry_after_s <= 50

    def test_equality_not_substring_quota_exceeded_x(self):
        resp = _resp(403, {"error": "quota_exceeded_x"})
        err = _classify_subgraph_error(resp)
        assert type(err) is RelayReadError
        assert not isinstance(err, RelayReadBlocked)

    def test_403_html_body_is_not_blocked(self):
        resp = _resp(403, content=b"<html><body>blocked</body></html>")
        err = _classify_subgraph_error(resp)
        assert type(err) is RelayReadError
        assert not isinstance(err, RelayReadBlocked)

    def test_500_is_plain_read_error(self):
        resp = _resp(500, {"error": "internal_error"})
        err = _classify_subgraph_error(resp)
        assert type(err) is RelayReadError
        assert not isinstance(err, RelayReadBlocked)


class TestRetryAfterSeconds:
    def test_negative_body_value_is_ignored(self):
        resp = _resp(429, {"retry_after": -5}, headers={"Retry-After": "30"})
        assert _retry_after_seconds(resp, {"retry_after": -5}) == 30.0

    def test_no_hint_returns_none(self):
        resp = _resp(429, {})
        assert _retry_after_seconds(resp, {}) is None

    def test_unparseable_header_returns_none(self):
        resp = _resp(429, {}, headers={"Retry-After": "not-a-date"})
        assert _retry_after_seconds(resp, {}) is None


class TestReadPauseLength:
    def test_quota_without_resets_at_uses_default_reprobe(self):
        err = RelayReadQuotaExceeded(_resp(403, {"error": "quota_exceeded"}), legacy=True)
        assert _pause_seconds(err) == 900.0

    def test_quota_reprobe_env_override(self):
        err = RelayReadQuotaExceeded(_resp(403, {"error": "quota_exceeded"}), legacy=True)
        with patch.dict("os.environ", {"TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS": "10"}):
            assert _pause_seconds(err) == 10.0

    def test_quota_reprobe_env_floor(self):
        err = RelayReadQuotaExceeded(_resp(403, {"error": "quota_exceeded"}), legacy=True)
        with patch.dict("os.environ", {"TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS": "1"}):
            assert _pause_seconds(err) == 5.0

    def test_quota_resets_at_two_minutes_out(self):
        resets_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        err = RelayReadQuotaExceeded(_resp(429, {}), legacy=False, resets_at=resets_at)
        pause = _pause_seconds(err)
        assert 115 <= pause <= 121

    def test_rate_limit_floor_30(self):
        err = RelayRateLimited(_resp(429, {}), retry_after_s=5)
        assert _pause_seconds(err) == 30.0

    def test_rate_limit_no_hint_defaults_300(self):
        err = RelayRateLimited(_resp(429, {}), retry_after_s=None)
        assert _pause_seconds(err) == 300.0

    def test_rate_limit_ceiling_3600(self):
        err = RelayRateLimited(_resp(429, {}), retry_after_s=99999)
        assert _pause_seconds(err) == 3600.0


class TestClientWidePause:
    @pytest.mark.asyncio
    async def test_second_call_short_circuits_no_new_http(self):
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            return httpx.Response(403, json={"error": "quota_exceeded"})

        rc = _relay_with_handler(handler)
        with pytest.raises(RelayReadQuotaExceeded):
            await rc.query_subgraph("{}", {})
        assert count["n"] == 1

        with pytest.raises(RelayReadQuotaExceeded):
            await rc.query_subgraph("{}", {})
        assert count["n"] == 1  # short-circuited — no new HTTP call
        assert rc.read_block() is not None
        assert rc.read_block().episode == 1

    @pytest.mark.asyncio
    async def test_advance_past_deadline_probes_once(self):
        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            return httpx.Response(403, json={"error": "quota_exceeded"})

        rc = _relay_with_handler(handler)
        with pytest.raises(RelayReadQuotaExceeded):
            await rc.query_subgraph("{}", {})
        assert count["n"] == 1

        import totalreclaw.relay as relay_mod

        future = time.monotonic() + 1000
        with patch.object(relay_mod.time, "monotonic", return_value=future):
            with pytest.raises(RelayReadQuotaExceeded):
                await rc.query_subgraph("{}", {})
            assert count["n"] == 2  # exactly one probe
            assert rc.read_block().episode == 1  # re-block -> same episode

    @pytest.mark.asyncio
    async def test_probe_success_clears_and_next_block_is_new_episode(self):
        mode = {"m": "block"}

        def handler(request):
            if mode["m"] == "block":
                return httpx.Response(403, json={"error": "quota_exceeded"})
            return httpx.Response(200, json={"data": {"facts": []}})

        rc = _relay_with_handler(handler)
        with pytest.raises(RelayReadQuotaExceeded):
            await rc.query_subgraph("{}", {})
        assert rc.read_block().episode == 1

        import totalreclaw.relay as relay_mod

        future = time.monotonic() + 1000
        with patch.object(relay_mod.time, "monotonic", return_value=future):
            mode["m"] = "ok"
            data = await rc.query_subgraph("{}", {})
            assert data == {"data": {"facts": []}}
            assert rc.read_block() is None

            mode["m"] = "block"
            with pytest.raises(RelayReadQuotaExceeded):
                await rc.query_subgraph("{}", {})
            assert rc.read_block().episode == 2

    @pytest.mark.asyncio
    async def test_writes_unaffected_submit_userop(self):
        """A write-path 403 quota_exceeded on /v1/bundler must NOT become a
        RelayReadError and must NOT set the read pause."""

        def handler(request):
            return httpx.Response(403, json={"error": "quota_exceeded"})

        rc = _relay_with_handler(handler)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await rc.submit_userop({"jsonrpc": "2.0", "method": "eth_sendUserOperation"})
        assert not isinstance(exc_info.value, RelayReadError)
        assert rc.read_block() is None
