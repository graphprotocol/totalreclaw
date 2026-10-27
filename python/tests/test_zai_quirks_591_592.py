"""z.ai quirks that silently dropped extractions.

- #592: GLM 5.3 Flash always thinks; ``thinking: {"type": "disabled"}`` gets
  400/1210 and extraction returned nothing. The client now retries with the
  server default and remembers the model.
- #591: on the Coding plan, the rate limit arrives as 429/1113 "Insufficient
  balance". The client flipped to the Standard (pay-as-you-go) endpoint, which
  has no balance either, and spent its retries there. It now returns to the
  user's endpoint and backs off.
- Transport errors other than timeouts (dropped connection, DNS) fell to a
  catch-all and came back as an empty answer with no retry. They now retry
  like timeouts.

All tests mock httpx; no network, no real keys.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from totalreclaw.agent import llm_client
from totalreclaw.agent.llm_client import (
    LLMConfig,
    LLMUpstreamOutageError,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    chat_completion,
)

_URL = f"{ZAI_CODING_BASE_URL}/chat/completions"
_OK = {"choices": [{"message": {"content": '{"topics":[],"facts":[]}'}}]}
_REJECT_1210 = (
    '{"error":{"code":"1210","message":"This model always engages in thinking and '
    'cannot be disabled; please use low, high, or max"}}'
)
_BALANCE_1113 = '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'


def _resp(status: int, body: str | dict) -> httpx.Response:
    request = httpx.Request("POST", _URL)
    if isinstance(body, dict):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, content=body.encode(), request=request)


def _cfg(model: str, base_url: str = ZAI_CODING_BASE_URL) -> LLMConfig:
    return LLMConfig(api_key="k", base_url=base_url, model=model, api_format="openai")


@pytest.fixture(autouse=True)
def _fresh_model_memory():
    llm_client._ZAI_ALWAYS_THINKING_MODELS.clear()
    yield
    llm_client._ZAI_ALWAYS_THINKING_MODELS.clear()


@pytest.fixture
def fast_backoff():
    with patch.object(llm_client, "_BACKOFF_DELAYS", [0.0] * 5), patch.dict(
        os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}
    ):
        yield


# --- #592 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_always_thinking_model_is_retried_without_the_thinking_field():
    bodies: list[dict] = []
    replies = iter([_resp(400, _REJECT_1210), _resp(200, _OK)])

    async def post(self, url, **kwargs):
        bodies.append(dict(kwargs["json"]))  # copy: the client edits the body between calls
        return next(replies)

    with patch("httpx.AsyncClient.post", new=post):
        out = await chat_completion(_cfg("glm-5.3-flash"), "system", "user", max_tokens=2048)

    assert out == _OK["choices"][0]["message"]["content"]
    first, second = bodies
    assert first["thinking"] == {"type": "disabled"}
    assert "thinking" not in second
    # reasoning tokens count against the budget, so it is raised for the retry
    assert second["max_completion_tokens"] >= llm_client._ALWAYS_THINKING_MIN_TOKENS


@pytest.mark.asyncio
async def test_the_model_is_remembered_so_later_calls_skip_the_rejected_request():
    post = AsyncMock(side_effect=[_resp(400, _REJECT_1210), _resp(200, _OK), _resp(200, _OK)])
    with patch("httpx.AsyncClient.post", new=post):
        await chat_completion(_cfg("glm-5.3-flash"), "system", "user")
        await chat_completion(_cfg("glm-5.3-flash"), "system", "user")

    assert post.await_count == 3
    assert "thinking" not in post.await_args_list[2].kwargs["json"]


@pytest.mark.asyncio
async def test_models_that_accept_disabled_thinking_keep_it():
    post = AsyncMock(return_value=_resp(200, _OK))
    with patch("httpx.AsyncClient.post", new=post):
        await chat_completion(_cfg("glm-4.5-flash"), "system", "user", max_tokens=2048)

    body = post.await_args.kwargs["json"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_completion_tokens"] == 2048
    assert not llm_client._ZAI_ALWAYS_THINKING_MODELS


@pytest.mark.asyncio
async def test_other_400s_are_not_retried_as_thinking_rejections():
    post = AsyncMock(return_value=_resp(400, '{"error":{"code":"1214","message":"bad messages"}}'))
    with patch("httpx.AsyncClient.post", new=post):
        with pytest.raises(httpx.HTTPStatusError):
            await chat_completion(_cfg("glm-4.5-flash"), "system", "user")
    assert post.await_count == 1


# --- #591 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_plan_rate_limit_returns_to_coding_endpoint_and_recovers(fast_backoff):
    """Coding 1113 -> flip to Standard -> Standard 1113 too -> back to Coding -> OK."""
    urls: list[str] = []

    async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
        urls.append(cfg.base_url)
        if len(urls) <= 2:
            raise httpx.HTTPStatusError("429", request=httpx.Request("POST", _URL), response=_resp(429, _BALANCE_1113))
        return "ok"

    with patch.object(llm_client, "_call_openai", new=fake_openai):
        out = await chat_completion(_cfg("glm-4.5-flash"), "system", "user")

    assert out == "ok"
    assert urls == [ZAI_CODING_BASE_URL, ZAI_STANDARD_BASE_URL, ZAI_CODING_BASE_URL]


@pytest.mark.asyncio
async def test_persistent_rate_limit_retries_on_the_users_endpoint(fast_backoff):
    urls: list[str] = []

    async def fake_openai(cfg, system_prompt, user_prompt, max_tokens, temperature):
        urls.append(cfg.base_url)
        raise httpx.HTTPStatusError("429", request=httpx.Request("POST", _URL), response=_resp(429, _BALANCE_1113))

    with patch.object(llm_client, "_call_openai", new=fake_openai):
        with pytest.raises(LLMUpstreamOutageError):
            await chat_completion(_cfg("glm-4.5-flash"), "system", "user")

    # one free probe of the other endpoint, then every retry on the user's own
    assert urls.count(ZAI_STANDARD_BASE_URL) == 1
    assert urls[2:] and set(urls[2:]) == {ZAI_CODING_BASE_URL}


# --- transport errors -----------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_connection_is_retried_not_returned_as_empty(fast_backoff):
    post = AsyncMock(side_effect=[httpx.ConnectError("connection reset"), _resp(200, _OK)])
    with patch("httpx.AsyncClient.post", new=post):
        out = await chat_completion(_cfg("glm-4.5-flash"), "system", "user")
    assert out == _OK["choices"][0]["message"]["content"]
    assert post.await_count == 2


@pytest.mark.asyncio
async def test_persistent_transport_failure_surfaces_as_outage(fast_backoff):
    post = AsyncMock(side_effect=httpx.ConnectError("dns down"))
    with patch("httpx.AsyncClient.post", new=post):
        with pytest.raises(LLMUpstreamOutageError, match="ConnectError on 5 attempts"):
            await chat_completion(_cfg("glm-4.5-flash"), "system", "user")
