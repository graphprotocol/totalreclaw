"""Tests for 3.3.1-rc.3 additions to totalreclaw.agent.llm_client.

Covers:
  - ``ZAI_BASE_URL`` env override (``get_zai_base_url``).
  - zai "Insufficient balance" detector + fallback URL picker.
  - ``chat_completion`` zai auto-fallback on 429+balance-error body.
  - ``LLMUpstreamOutageError`` surfacing on exhausted retries.
  - Retry budget ``TOTALRECLAW_LLM_RETRY_BUDGET_MS`` env override.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from totalreclaw.agent.llm_client import (
    LLMConfig,
    LLMUpstreamOutageError,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    chat_completion,
    get_zai_base_url,
    is_zai_balance_error,
    zai_fallback_base_url,
)


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestZaiBalanceHelpers:
    def test_detector_full_message(self):
        assert is_zai_balance_error(
            "Insufficient balance or no resource package. Please recharge."
        )

    def test_detector_short_insufficient(self):
        assert is_zai_balance_error("429: insufficient balance")

    def test_detector_no_resource_package(self):
        assert is_zai_balance_error("no resource package available")

    def test_detector_rate_limit_not_balance(self):
        assert not is_zai_balance_error("Rate limit exceeded")

    def test_detector_server_error_not_balance(self):
        assert not is_zai_balance_error("502 bad gateway")

    def test_detector_empty(self):
        assert not is_zai_balance_error("")
        assert not is_zai_balance_error(None)  # type: ignore[arg-type]

    def test_fallback_coding_to_standard(self):
        assert zai_fallback_base_url(ZAI_CODING_BASE_URL) == ZAI_STANDARD_BASE_URL

    def test_fallback_standard_to_coding(self):
        assert zai_fallback_base_url(ZAI_STANDARD_BASE_URL) == ZAI_CODING_BASE_URL

    def test_fallback_trailing_slash_normalized(self):
        assert zai_fallback_base_url(ZAI_CODING_BASE_URL + "/") == ZAI_STANDARD_BASE_URL

    def test_fallback_unknown_url(self):
        assert zai_fallback_base_url("https://custom.proxy/v1") is None

    def test_fallback_empty(self):
        assert zai_fallback_base_url("") is None


class TestGetZaiBaseUrl:
    def test_default_is_coding(self):
        with patch.dict(os.environ, {}, clear=True):
            assert get_zai_base_url() == ZAI_CODING_BASE_URL

    def test_env_override_standard(self):
        with patch.dict(os.environ, {"ZAI_BASE_URL": ZAI_STANDARD_BASE_URL}):
            assert get_zai_base_url() == ZAI_STANDARD_BASE_URL

    def test_env_override_custom_strips_trailing_slash(self):
        with patch.dict(os.environ, {"ZAI_BASE_URL": "https://custom.proxy/v1/"}):
            assert get_zai_base_url() == "https://custom.proxy/v1"

    def test_env_override_whitespace_falls_back_to_default(self):
        with patch.dict(os.environ, {"ZAI_BASE_URL": "   "}):
            assert get_zai_base_url() == ZAI_CODING_BASE_URL


# ---------------------------------------------------------------------------
# chat_completion — zai auto-fallback
# ---------------------------------------------------------------------------


def _make_http_error(status_code: int, body: str) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError mirroring what raise_for_status throws."""
    request = httpx.Request("POST", "https://api.z.ai/api/coding/paas/v4/chat/completions")
    response = httpx.Response(status_code, content=body.encode("utf-8"), request=request)
    return httpx.HTTPStatusError("http error", request=request, response=response)


class TestZaiAutoFallback:
    @pytest.mark.asyncio
    async def test_coding_balance_error_flips_to_standard(self):
        """First call CODING → 429 balance; flipped to STANDARD → 200."""
        config = LLMConfig(
            api_key="zai-test",
            base_url=ZAI_CODING_BASE_URL,
            model="glm-4.5-flash",
            api_format="openai",
        )

        # Track which base URL each _call_openai used.
        call_log: list[str] = []

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_log.append(cfg.base_url)
            if len(call_log) == 1:
                raise _make_http_error(
                    429,
                    '{"error":{"message":"Insufficient balance or no resource package. Please recharge."}}',
                )
            return "recovered"

        with patch(
            "totalreclaw.agent.llm_client._call_openai",
            new=fake_openai,
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            result = await chat_completion(config, "system", "user")

        assert result == "recovered"
        assert call_log == [ZAI_CODING_BASE_URL, ZAI_STANDARD_BASE_URL]
        # Caller's config must NOT be mutated — we clone internally.
        assert config.base_url == ZAI_CODING_BASE_URL

    @pytest.mark.asyncio
    async def test_standard_balance_error_flips_to_coding(self):
        config = LLMConfig(
            api_key="zai-test",
            base_url=ZAI_STANDARD_BASE_URL,
            model="glm-4.5-flash",
            api_format="openai",
        )

        call_log: list[str] = []

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_log.append(cfg.base_url)
            if len(call_log) == 1:
                raise _make_http_error(
                    429,
                    '{"error":{"message":"Insufficient balance"}}',
                )
            return "ok"

        with patch("totalreclaw.agent.llm_client._call_openai", new=fake_openai):
            result = await chat_completion(config, "system", "user")
        assert result == "ok"
        assert call_log == [ZAI_STANDARD_BASE_URL, ZAI_CODING_BASE_URL]

    @pytest.mark.asyncio
    async def test_fallback_fires_only_once(self):
        """If BOTH endpoints reject with balance-error, we surface outage
        rather than ping-pong forever."""
        config = LLMConfig(
            api_key="zai-test",
            base_url=ZAI_CODING_BASE_URL,
            model="glm-4.5-flash",
            api_format="openai",
        )

        call_log: list[str] = []

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_log.append(cfg.base_url)
            raise _make_http_error(
                429,
                '{"error":{"message":"Insufficient balance"}}',
            )

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "50"}
        ), patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01, 0.01, 0.01, 0.01, 0.01]):
            with pytest.raises(LLMUpstreamOutageError):
                await chat_completion(config, "system", "user")

        # At least 2 calls (CODING + STANDARD). The normal retry loop after
        # the fallback is bounded by the tiny budget.
        assert len(call_log) >= 2
        # The fallback flip happened: STANDARD was hit at some point.
        assert ZAI_STANDARD_BASE_URL in call_log

    @pytest.mark.asyncio
    async def test_non_zai_url_no_fallback(self):
        """A balance-error from a non-zai baseUrl does NOT trigger the flip.
        Instead it follows the normal retry path until exhausted."""
        config = LLMConfig(
            api_key="proxy-test",
            base_url="https://custom.proxy/v1",
            model="glm-4.5-flash",
            api_format="openai",
        )

        call_log: list[str] = []

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            call_log.append(cfg.base_url)
            raise _make_http_error(
                429,
                '{"error":{"message":"Insufficient balance"}}',
            )

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01, 0.01, 0.01, 0.01, 0.01]), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            with pytest.raises(LLMUpstreamOutageError):
                await chat_completion(config, "system", "user")

        # All calls stayed on the custom.proxy URL — no flip attempted.
        assert all(url == "https://custom.proxy/v1" for url in call_log)


# ---------------------------------------------------------------------------
# chat_completion — LLMUpstreamOutageError on exhausted retries
# ---------------------------------------------------------------------------


class TestLLMUpstreamOutageError:
    @pytest.mark.asyncio
    async def test_503_exhaustion_raises_outage(self):
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            raise _make_http_error(503, '{"error":{"message":"down"}}')

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01, 0.01, 0.01, 0.01, 0.01]), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            with pytest.raises(LLMUpstreamOutageError) as exc_info:
                await chat_completion(config, "system", "user")

        assert exc_info.value.last_status == 503
        assert exc_info.value.attempts >= 1

    @pytest.mark.asyncio
    async def test_401_non_retryable_raises_http_error_not_outage(self):
        """Non-retryable 4xx errors propagate as the underlying
        HTTPStatusError, NOT as LLMUpstreamOutageError — callers must
        distinguish config errors from transient outages."""
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            raise _make_http_error(401, '{"error":{"message":"unauthorized"}}')

        with patch("totalreclaw.agent.llm_client._call_openai", new=fake_openai):
            with pytest.raises(httpx.HTTPStatusError):
                await chat_completion(config, "system", "user")

    @pytest.mark.asyncio
    async def test_timeout_exhaustion_raises_outage(self):
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            raise httpx.ReadTimeout("timed out")

        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01, 0.01, 0.01, 0.01, 0.01]), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
        ):
            with pytest.raises(LLMUpstreamOutageError) as exc_info:
                await chat_completion(config, "system", "user")

        assert exc_info.value.last_status is None  # timeouts have no HTTP status
        assert exc_info.value.attempts >= 1

    @pytest.mark.asyncio
    async def test_retry_budget_short_circuit(self):
        """A small budgetMs stops the retry loop before exhausting
        attempts."""
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )

        call_count = 0

        async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
            nonlocal call_count
            call_count += 1
            raise _make_http_error(503, '{"error":{"message":"down"}}')

        # Budget 30ms, backoff 10/20/... → first retry 10ms (cum=10, ok),
        # second would add 20ms → cum=30 (exactly equal; rule is strict >).
        # third would add 30ms → cum=40 > 30 → budget trips.
        with patch(
            "totalreclaw.agent.llm_client._call_openai", new=fake_openai
        ), patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01, 0.02, 0.03, 0.04, 0.05]), patch.dict(
            os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "30"}
        ):
            with pytest.raises(LLMUpstreamOutageError):
                await chat_completion(config, "system", "user")

        # Budget stops retries before the full 5-attempt cycle.
        assert call_count <= 4
