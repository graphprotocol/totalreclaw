"""Issue #208 — chat_completion honors HTTP Retry-After on 429.

Pre-fix behavior: the 429 retry path used a static
[2,4,8,16,32]s backoff regardless of any ``Retry-After`` header the
upstream sent. A 429 with ``Retry-After: 60`` was followed by a 2s retry,
guaranteed to 429 again, burning the whole retry budget in seconds.

Post-fix behavior:
  - When a 429 response carries a parseable ``Retry-After`` and the
    suggested delay is *longer* than the static backoff for that attempt,
    the longer value is used (capped at ``_RETRY_AFTER_CEILING_S``).
  - When ``Retry-After`` exceeds the ceiling (e.g. a daily-quota wall),
    the call surfaces :class:`LLMUpstreamOutageError` immediately rather
    than blocking the session for minutes.
  - When ``Retry-After`` is shorter than the static backoff, the static
    backoff still wins (we don't shorten waits — the static ladder is the
    floor for stability).
  - When the header is missing / malformed, the static backoff is used
    unchanged (regression cover for the existing rc.3 behavior).
"""
from __future__ import annotations

import datetime as dt
import os
from unittest.mock import patch

import httpx
import pytest

from totalreclaw.agent.llm_client import (
    LLMConfig,
    LLMUpstreamOutageError,
    _RETRY_AFTER_CEILING_S,
    _parse_retry_after,
    chat_completion,
)


def _http_error_with_retry_after(
    status_code: int, body: str, retry_after: str | None
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response = httpx.Response(
        status_code,
        content=body.encode("utf-8"),
        request=request,
        headers=headers,
    )
    return httpx.HTTPStatusError("rate limit", request=request, response=response)


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def _resp(self, headers: dict | None = None) -> httpx.Response:
        request = httpx.Request("POST", "https://example.test/")
        return httpx.Response(429, request=request, headers=headers or {})

    def test_delta_seconds_integer(self):
        assert _parse_retry_after(self._resp({"Retry-After": "5"})) == 5.0

    def test_delta_seconds_float_string(self):
        # Some providers send fractional seconds.
        assert _parse_retry_after(self._resp({"Retry-After": "2.5"})) == 2.5

    def test_delta_seconds_zero(self):
        # Spec-legal: server says "retry now".
        assert _parse_retry_after(self._resp({"Retry-After": "0"})) == 0.0

    def test_negative_delta_clamped_to_zero(self):
        # A bizarre but spec-violating "-1" should not produce a negative
        # sleep duration.
        assert _parse_retry_after(self._resp({"Retry-After": "-1"})) == 0.0

    def test_http_date_future(self):
        future = dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=10)
        # RFC 1123 / IMF-fixdate
        formatted = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = _parse_retry_after(self._resp({"Retry-After": formatted}))
        assert parsed is not None
        assert 8.0 < parsed <= 11.0

    def test_http_date_past_clamped_to_zero(self):
        past = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(seconds=10)
        formatted = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = _parse_retry_after(self._resp({"Retry-After": formatted}))
        assert parsed == 0.0

    def test_lowercase_header_name(self):
        # httpx headers are case-insensitive but be explicit about it —
        # provider proxies sometimes normalize the casing.
        assert _parse_retry_after(self._resp({"retry-after": "7"})) == 7.0

    def test_missing_header_returns_none(self):
        assert _parse_retry_after(self._resp()) is None

    def test_empty_header_returns_none(self):
        assert _parse_retry_after(self._resp({"Retry-After": ""})) is None

    def test_garbage_header_returns_none(self):
        assert _parse_retry_after(self._resp({"Retry-After": "soonish"})) is None


# ---------------------------------------------------------------------------
# chat_completion behavior
# ---------------------------------------------------------------------------


class TestChatCompletionRetryAfter:
    """Drives chat_completion with a stub upstream and asserts the wait
    duration between retries matches the post-fix policy."""

    def _config(self) -> LLMConfig:
        return LLMConfig(
            api_key="test",
            base_url="https://api.example.com/v1",
            model="test-model",
            api_format="openai",
        )

    @pytest.mark.asyncio
    async def test_retry_after_longer_than_static_is_honored(self):
        """Server says 'wait 8s'; static backoff for attempt 1 is 2s.
        chat_completion should sleep 8s, not 2s."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        call_count = {"n": 0}

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _http_error_with_retry_after(429, "rate limited", "8")
            return "ok"

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch(
            "totalreclaw.agent.llm_client._asyncio.sleep", new=fake_sleep
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            result = await chat_completion(self._config(), "system", "user")

        assert result == "ok"
        assert sleep_calls == [8.0], (
            f"Retry-After=8 should have produced an 8s sleep; got {sleep_calls}"
        )

    @pytest.mark.asyncio
    async def test_retry_after_shorter_than_static_uses_static_floor(self):
        """Server says 'wait 1s' but static attempt-1 backoff is 2s.
        chat_completion keeps the longer static delay (the static ladder
        is the floor for stability — we don't shorten waits on a 429)."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        call_count = {"n": 0}

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _http_error_with_retry_after(429, "rate limited", "1")
            return "ok"

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch(
            "totalreclaw.agent.llm_client._asyncio.sleep", new=fake_sleep
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            result = await chat_completion(self._config(), "system", "user")

        assert result == "ok"
        assert sleep_calls == [2.0]

    @pytest.mark.asyncio
    async def test_retry_after_above_ceiling_surfaces_outage(self):
        """A 'come back tomorrow' Retry-After (e.g. 300s) should not
        block the session — surface LLMUpstreamOutageError immediately
        so the caller can fall back to heuristic extraction."""

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            raise _http_error_with_retry_after(
                429,
                "daily quota exceeded",
                str(int(_RETRY_AFTER_CEILING_S) + 1),
            )

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            with pytest.raises(LLMUpstreamOutageError) as excinfo:
                await chat_completion(self._config(), "system", "user")

        assert excinfo.value.attempts == 1
        assert excinfo.value.last_status == 429
        assert "Retry-After" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_no_retry_after_uses_static_backoff(self):
        """Regression: when Retry-After is missing, behavior matches rc.3
        (static [2, 4, ...] backoff)."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        call_count = {"n": 0}

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise _http_error_with_retry_after(429, "rate limited", None)
            return "ok"

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch(
            "totalreclaw.agent.llm_client._asyncio.sleep", new=fake_sleep
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            result = await chat_completion(self._config(), "system", "user")

        assert result == "ok"
        assert sleep_calls == [2.0, 4.0]

    @pytest.mark.asyncio
    async def test_502_ignores_retry_after_header(self):
        """Retry-After is honored only on 429. 5xx retries keep using the
        static ladder so a flaky upstream's stray ``Retry-After`` on a
        gateway error doesn't shift our backoff timing."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)

        call_count = {"n": 0}

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _http_error_with_retry_after(502, "bad gateway", "30")
            return "ok"

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch(
            "totalreclaw.agent.llm_client._asyncio.sleep", new=fake_sleep
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            result = await chat_completion(self._config(), "system", "user")

        assert result == "ok"
        assert sleep_calls == [2.0]
