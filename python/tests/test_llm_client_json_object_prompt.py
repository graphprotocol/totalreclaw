"""internal#590: json_object response_format requires "json" in the messages.

OpenAI rejects ``response_format: json_object`` (400) unless the word
"json" appears somewhere in the messages. The shared extraction prompts
never say it, so ``_call_openai`` must add it when it requests json_object.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from totalreclaw.agent import llm_client
from totalreclaw.agent.llm_client import LLMConfig, _call_openai


async def _capture_body(base_url: str, system: str, user: str) -> dict:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        )

    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    config = LLMConfig(api_key="k", base_url=base_url, model="gpt-4o-mini", api_format="openai")
    with patch.object(llm_client.httpx, "AsyncClient", factory):
        await _call_openai(config, system, user, 100, 0.0)
    return captured


def _has_json(body: dict) -> bool:
    return any("json" in m["content"].lower() for m in body["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url", ["https://api.openai.com/v1", "https://openrouter.ai/api/v1"]
)
async def test_json_object_request_mentions_json(base_url):
    body = await _capture_body(base_url, "Extract facts.", "Conversation: hi")
    assert body["response_format"] == {"type": "json_object"}
    assert _has_json(body)


@pytest.mark.asyncio
async def test_existing_json_mention_left_untouched():
    body = await _capture_body("https://api.openai.com/v1", "Return JSON facts.", "hi")
    assert body["messages"][0]["content"] == "Return JSON facts."


@pytest.mark.asyncio
async def test_no_json_object_no_change():
    body = await _capture_body("https://example.invalid/v1", "Extract facts.", "hi")
    assert "response_format" not in body
    assert body["messages"][0]["content"] == "Extract facts."
