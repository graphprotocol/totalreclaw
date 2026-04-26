"""F2 (rc.24) — auto-extraction empty-response visibility regression test.

Background
----------
QA report ``QA-hermes-RC-2.3.1-rc.23-20260426.md`` Finding F2: Z.AI /
GLM-5.1 returned EMPTY content for the merged-extraction prompt during
an entire 27-turn conversation. The auto-extraction pipeline silently
no-op'd on every turn, logging only the single line::

    extract_facts_llm: chat_completion returned None/empty

at INFO level. Operators had no way to tell what shape the response
had, what the model actually returned, or why content was empty.

rc.24 fix
---------
1. ``_call_openai`` sends ``response_format: {"type": "json_object"}``
   to known structured-output-aware providers (zai/GLM, OpenAI, Groq,
   DeepSeek, OpenRouter, Mistral, x.ai, Together). For GLM in
   particular this flips the model from empty-content responses to
   deterministic JSON.

2. When ``message.content`` IS empty the call site WARNs with the
   request payload sizes, model name, ``finish_reason``, and ``usage``.
   The downstream extraction pipeline ALSO bumps from INFO to WARN.

These tests guard both invariants. The test does NOT call out to a real
Z.AI endpoint — it mocks ``httpx.AsyncClient.post`` to drive specific
response shapes (empty content, valid JSON, finish_reason=stop, etc.)
and asserts the warn output + the extraction return.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.extraction import extract_facts_llm
from totalreclaw.agent.llm_client import (
    LLMConfig,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    _supports_json_object_response_format,
    chat_completion,
    get_zai_base_url,
    zai_base_url_for_model,
)


# ---------------------------------------------------------------------------
# Helpers — minimal httpx response double, just enough for the call sites.
# ---------------------------------------------------------------------------


class _FakeResp:
    """Stub of ``httpx.Response`` exercising only what _call_openai uses."""

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "fake error",
                request=MagicMock(),
                response=MagicMock(status_code=self.status_code),
            )


def _make_async_post(payload: dict, status_code: int = 200) -> AsyncMock:
    """Return an AsyncMock that emulates ``client.post`` returning ``payload``."""
    fake = _FakeResp(payload, status_code=status_code)
    return AsyncMock(return_value=fake)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_supports_json_object_response_format_for_zai() -> None:
    """The zai/GLM coding + standard endpoints must opt into json_object."""
    assert _supports_json_object_response_format(
        "https://api.z.ai/api/coding/paas/v4"
    )
    assert _supports_json_object_response_format("https://api.z.ai/api/paas/v4")


def test_supports_json_object_response_format_for_openai() -> None:
    assert _supports_json_object_response_format("https://api.openai.com/v1")


def test_supports_json_object_response_format_skips_unknown() -> None:
    """Unrecognized base URLs (custom self-hosted) should NOT receive
    response_format — we don't know if the provider implements it."""
    assert not _supports_json_object_response_format(
        "https://my-self-hosted-llm.example.com/v1"
    )
    assert not _supports_json_object_response_format("")


@pytest.mark.asyncio
async def test_chat_completion_sends_response_format_to_zai() -> None:
    """When base_url matches a json-aware provider, the request body
    must carry ``response_format: {type: "json_object"}``.

    F2 (rc.24): without this hint, GLM-5.1 returned empty content for
    the merged-extraction prompt; the pipeline silently no-op'd."""
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5-turbo",
        api_format="openai",
    )

    fake_post = _make_async_post(
        {
            "choices": [{"message": {"content": '{"topics": [], "facts": []}'}}],
        }
    )

    with patch("httpx.AsyncClient.post", new=fake_post):
        out = await chat_completion(config, "system", "user")

    assert out == '{"topics": [], "facts": []}'
    # Inspect the kwargs passed to client.post.
    call_kwargs = fake_post.await_args.kwargs
    body = call_kwargs.get("json") or {}
    assert body.get("response_format") == {"type": "json_object"}, (
        "request body must carry response_format json_object for zai. "
        f"Got body keys: {sorted(body.keys())}"
    )


@pytest.mark.asyncio
async def test_chat_completion_skips_response_format_for_unknown_provider() -> None:
    """For unknown providers the body must NOT carry response_format.

    Belt-and-suspenders: misapplying response_format to a provider that
    doesn't support it could trigger a 400 from strict implementations.
    """
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://my-self-hosted.example.com/v1",
        model="custom-model",
        api_format="openai",
    )
    fake_post = _make_async_post(
        {"choices": [{"message": {"content": "hello"}}]},
    )
    with patch("httpx.AsyncClient.post", new=fake_post):
        out = await chat_completion(config, "system", "user")

    assert out == "hello"
    body = fake_post.await_args.kwargs.get("json") or {}
    assert "response_format" not in body, (
        "unknown provider should not get response_format hint"
    )


@pytest.mark.asyncio
async def test_chat_completion_warns_on_empty_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When message.content is empty/missing, _call_openai must WARN
    with the diagnostic context (model, finish_reason, usage, etc.).

    F2 (rc.24): empty responses were silently logged at INFO. Operators
    couldn't tell whether the model OOM'd context, hit a content
    filter, or refused — all looked identical. WARN-level logging with
    finish_reason fixes that.
    """
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5-turbo",
        api_format="openai",
    )

    fake_post = _make_async_post(
        {
            "choices": [
                {
                    "message": {"content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 0},
        }
    )

    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.llm_client")
    with patch("httpx.AsyncClient.post", new=fake_post):
        out = await chat_completion(config, "system prompt", "user prompt")

    assert out in (None, ""), (
        "empty content must propagate as None or empty string — caller "
        "differentiates via 'if not response' downstream"
    )

    # Assert we got the WARN with the diagnostic fields.
    matched = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "LLM returned empty content" in rec.getMessage()
    ]
    assert matched, (
        "expected a WARNING containing 'LLM returned empty content' from "
        "_call_openai; got log records:\n  "
        + "\n  ".join(f"{r.levelname}:{r.name}:{r.getMessage()}" for r in caplog.records)
    )
    msg = matched[0].getMessage()
    # Spot-check the key diagnostic fields.
    assert "glm-5-turbo" in msg, "WARN must include model name"
    assert "finish_reason=stop" in msg, "WARN must include finish_reason"
    assert "usage=" in msg, "WARN must include usage block"


@pytest.mark.asyncio
async def test_extract_facts_llm_warns_when_response_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``chat_completion`` returns None/empty, the extraction
    pipeline now WARNs (was INFO pre-rc.24).

    Regression shield for F2 — the rc.23 QA report's exact symptom
    (``extract_facts_llm: chat_completion returned None/empty`` was the
    only signal that auto-extraction had silently failed).
    """
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5-turbo",
        api_format="openai",
    )
    messages = [
        {"role": "user", "content": "I love hiking on weekends and live in Lisbon."},
        {"role": "assistant", "content": "Got it — noted."},
    ]

    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.extraction")
    # Patch chat_completion at its import site inside extraction.py.
    with patch(
        "totalreclaw.agent.extraction.chat_completion",
        new=AsyncMock(return_value=""),
    ):
        out = await extract_facts_llm(messages, mode="turn", llm_config=config)

    assert out == [], "empty response must yield zero facts"

    matched = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "chat_completion returned None/empty" in rec.getMessage()
    ]
    assert matched, (
        "extract_facts_llm must WARN (not INFO) when chat_completion "
        "returns empty — silent INFO is the rc.23 F2 symptom this fix "
        "guards against. Got log records:\n  "
        + "\n  ".join(
            f"{r.levelname}:{r.name}:{r.getMessage()}" for r in caplog.records
        )
    )


# ---------------------------------------------------------------------------
# rc.24 F2 follow-up — model-aware zai endpoint selection.
#
# Pedro is on the Z.AI Coding plan; the auto-extraction silently no-op'd
# when the Hermes-configured model was GLM-5.1 because GLM-5.x lives on
# the Standard PAYG endpoint, not Coding. ``zai_base_url_for_model`` is
# the new heuristic that routes per-model to the right endpoint, and
# ``chat_completion`` now ALSO auto-flips on a 200-empty response.
# ---------------------------------------------------------------------------


def test_zai_base_url_for_model_glm_5_routes_to_standard() -> None:
    """GLM-5.x → Standard PAYG endpoint."""
    assert zai_base_url_for_model("glm-5.1") == ZAI_STANDARD_BASE_URL
    assert zai_base_url_for_model("glm-5") == ZAI_STANDARD_BASE_URL
    assert zai_base_url_for_model("GLM-5.1") == ZAI_STANDARD_BASE_URL


def test_zai_base_url_for_model_glm_4_routes_to_coding() -> None:
    """GLM-4.x → Coding plan endpoint."""
    assert zai_base_url_for_model("glm-4.5") == ZAI_CODING_BASE_URL
    assert zai_base_url_for_model("glm-4.5-air") == ZAI_CODING_BASE_URL
    assert zai_base_url_for_model("glm-4.5-flash") == ZAI_CODING_BASE_URL
    assert zai_base_url_for_model("glm-4-turbo") == ZAI_CODING_BASE_URL


def test_zai_base_url_for_model_unknown_falls_back_to_coding() -> None:
    """Empty / unknown model → coding (historical default)."""
    assert zai_base_url_for_model("") == ZAI_CODING_BASE_URL
    assert zai_base_url_for_model("unknown-model") == ZAI_CODING_BASE_URL


def test_get_zai_base_url_respects_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZAI_BASE_URL env var beats the model heuristic."""
    monkeypatch.setenv(
        "ZAI_BASE_URL", "https://my-self-hosted-zai.example.com/v1"
    )
    # Model says GLM-5.1 (would route to Standard) but env override wins.
    assert (
        get_zai_base_url(model="glm-5.1")
        == "https://my-self-hosted-zai.example.com/v1"
    )


def test_get_zai_base_url_uses_model_heuristic_without_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no env override, model name picks the endpoint."""
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    assert get_zai_base_url(model="glm-5.1") == ZAI_STANDARD_BASE_URL
    assert get_zai_base_url(model="glm-4.5-flash") == ZAI_CODING_BASE_URL


@pytest.mark.asyncio
async def test_chat_completion_auto_flips_zai_endpoint_on_empty_content() -> None:
    """When zai's coding endpoint returns 200 with empty content AND
    we haven't tried the standard endpoint yet, ``chat_completion``
    must auto-flip to the standard endpoint (single retry, free of
    the normal retry budget) and re-issue the call.

    F2 (rc.24) — this is the "Coding-plan key + GLM-5.1" silent-empty
    scenario. Pre-rc.24, ``chat_completion`` returned None on the
    first call and the extraction pipeline silently no-op'd. Now the
    flip kicks in; the second call to the OTHER endpoint succeeds.
    """
    config = LLMConfig(
        api_key="sk-test",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-5.1",
        api_format="openai",
    )

    call_count = {"n": 0}

    async def fake_post(self, url, **kwargs):
        call_count["n"] += 1
        body = kwargs.get("json") or {}
        # First call (coding endpoint) → empty content. Second call
        # (standard endpoint) → real JSON.
        if call_count["n"] == 1:
            payload = {
                "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 0},
            }
        else:
            payload = {
                "choices": [{"message": {"content": '{"topics":[],"facts":[]}'}}],
            }
        return _FakeResp(payload)

    with patch("httpx.AsyncClient.post", new=fake_post):
        out = await chat_completion(config, "system", "user")

    assert out == '{"topics":[],"facts":[]}', (
        "after the auto-flip the second endpoint must succeed"
    )
    assert call_count["n"] == 2, (
        f"expected exactly 2 calls (first coding empty, second standard "
        f"success); got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_extract_facts_llm_returns_facts_for_valid_response() -> None:
    """Positive baseline: when the auxiliary LLM returns a parseable
    merged-topic JSON payload with one fact, ``extract_facts_llm`` must
    return that fact.

    This is the regression test the QA report explicitly asked for
    (F2 fix path #3): "add a regression test that runs auto-extraction
    with a short canned conversation against a mocked aux LLM and
    asserts ``len(extracted_facts) > 0``".
    """
    config = LLMConfig(
        api_key="sk-test",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5-turbo",
        api_format="openai",
    )
    messages = [
        {
            "role": "user",
            "content": (
                "I just moved to Lisbon and really love hiking trails near "
                "the coast. The Sintra path is my favourite — I do it every "
                "weekend with my dog Bruno."
            ),
        },
        {
            "role": "assistant",
            "content": "That sounds wonderful! Sintra has amazing trails.",
        },
    ]

    canned_payload = json.dumps(
        {
            "topics": ["hiking", "lisbon"],
            "facts": [
                {
                    "text": "User lives in Lisbon and hikes Sintra trails on weekends.",
                    "type": "claim",
                    "importance": 8,
                    "action": "ADD",
                    "source": "user",
                    "scope": "personal",
                    "volatility": "stable",
                }
            ],
        }
    )

    with patch(
        "totalreclaw.agent.extraction.chat_completion",
        new=AsyncMock(return_value=canned_payload),
    ):
        facts = await extract_facts_llm(messages, mode="turn", llm_config=config)

    assert len(facts) > 0, (
        "extract_facts_llm must return at least one fact for valid input — "
        "this guards against the F2 silent-empty regression"
    )
    assert facts[0].text.lower().startswith("user lives in lisbon")
    assert facts[0].type == "claim"
    assert facts[0].source == "user"
