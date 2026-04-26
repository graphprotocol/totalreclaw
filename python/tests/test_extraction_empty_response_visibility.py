"""F2 (rc.24) — auto-extraction empty-response visibility +
same-provider cheap-model selection regression test.

Background
----------
QA report ``QA-hermes-RC-2.3.1-rc.23-20260426.md`` Finding F2: Z.AI /
GLM-5.1 returned EMPTY content for the merged-extraction prompt during
an entire 27-turn conversation. The auto-extraction pipeline silently
no-op'd on every turn, logging only the single line::

    extract_facts_llm: chat_completion returned None/empty

at INFO level. Operators had no way to tell what shape the response
had, what the model actually returned, or why content was empty.

rc.24 fix (per Pedro's design directive 2026-04-26)
---------------------------------------------------
1. ``_call_openai`` sends ``response_format: {"type": "json_object"}``
   to known structured-output-aware providers (zai/GLM, OpenAI, Groq,
   DeepSeek, OpenRouter, Mistral, x.ai, Together).

2. When ``message.content`` IS empty the call site WARNs with the
   request payload sizes, model name, ``finish_reason``, and ``usage``.
   The downstream extraction pipeline ALSO bumps from INFO to WARN.

3. The PRIMARY fix: ``derive_extraction_config`` swaps the user's
   chat model for a same-provider CHEAP model on the SAME endpoint.
   GLM-5.1 (chat) → GLM-4.5-flash (extraction) on the same zai
   endpoint. Mirrors the OpenClaw plugin's auth-profiles UX —
   user configures one provider, we reuse it transparently. NEVER
   introduces a second provider (no Anthropic-Haiku-as-fallback).

These tests guard all three invariants. They do NOT call out to a
real provider — they mock ``httpx.AsyncClient.post`` to drive specific
response shapes and assert behaviour.
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
    _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER,
    _supports_json_object_response_format,
    chat_completion,
    cheap_extraction_model_for,
    derive_extraction_config,
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
# rc.24 F2 PRIMARY FIX — same-provider cheap-model selection.
#
# Pedro 2026-04-26: extraction reuses the SAME provider already powering
# the agent's chat. WITHIN that provider, pick a cheaper sibling for
# extraction (the user's flagship chat model is overkill + expensive +
# in the GLM-5.1-on-Coding case, returns silent empty content).
# Mirrors the OpenClaw plugin's auth-profiles UX — one provider config
# does double duty.
#
# Pin examples (kept hand-curated; provider lineups change slowly):
#   zai      glm-5.1                -> glm-4.5-flash
#   openai   gpt-4 / gpt-4-turbo    -> gpt-4o-mini
#   anthropic claude-sonnet-4-5     -> claude-haiku-4-5
#   groq     llama-3.3-70b          -> llama-3.1-8b-instant
# ---------------------------------------------------------------------------


def test_cheap_extraction_model_for_zai_picks_glm_45_flash() -> None:
    """Z.AI provider — extraction defaults to glm-4.5-flash regardless
    of which GLM the user picked for chat.

    Proven on Coding plan per prior QA cycles (Pedro 2026-04-26).
    Available on BOTH Coding + Standard endpoints, so it works for
    every zai user without needing endpoint-aware logic.
    """
    assert cheap_extraction_model_for("zai", "glm-5.1") == "glm-4.5-flash"
    assert cheap_extraction_model_for("zai", "glm-4.5") == "glm-4.5-flash"
    assert cheap_extraction_model_for("zai", "glm-5-turbo") == "glm-4.5-flash"
    # Case-insensitive provider name.
    assert cheap_extraction_model_for("ZAI", "glm-5.1") == "glm-4.5-flash"


def test_cheap_extraction_model_for_openai_picks_gpt_41_mini() -> None:
    """Mirrors OpenClaw plugin rc.22's ``CHEAP_MODEL_BY_PROVIDER['openai']``
    pin. Cross-client byte-parity so a single Z.AI Coding-plan user
    sees the SAME cheap model for both OpenClaw and Hermes auto-extract."""
    assert cheap_extraction_model_for("openai", "gpt-4") == "gpt-4.1-mini"
    assert cheap_extraction_model_for("openai", "gpt-4-turbo") == "gpt-4.1-mini"
    # Non-cheap "gpt-4o" gets swapped; cheap-indicator-matching strings
    # like "gpt-4o-mini" pass through unchanged.
    assert cheap_extraction_model_for("openai", "gpt-4o") == "gpt-4.1-mini"


def test_cheap_extraction_model_for_anthropic_picks_haiku() -> None:
    """Anthropic provider — extraction defaults to
    ``claude-haiku-4-5-20251001`` (date-pinned, matches the TS
    plugin). NEVER introduces Anthropic if the user's chat provider is
    something else (that happens at the call-site level via
    ``derive_extraction_config``)."""
    assert (
        cheap_extraction_model_for("anthropic", "claude-sonnet-4-5")
        == "claude-haiku-4-5-20251001"
    )
    assert (
        cheap_extraction_model_for("anthropic", "claude-opus-4")
        == "claude-haiku-4-5-20251001"
    )


def test_cheap_extraction_model_passthrough_when_chat_already_cheap() -> None:
    """If the user's chat model name matches the cheap-indicator
    pattern (``flash``, ``mini``, ``haiku``, etc. at a word boundary),
    extraction reuses it verbatim — no redundant swap.

    Mirrors the rc.22 deriveCheapModel short-circuit."""
    # Already-cheap models pass through.
    assert (
        cheap_extraction_model_for("zai", "glm-4.5-flash") == "glm-4.5-flash"
    )
    assert (
        cheap_extraction_model_for("openai", "gpt-4.1-mini") == "gpt-4.1-mini"
    )
    assert (
        cheap_extraction_model_for("anthropic", "claude-haiku-4-5")
        == "claude-haiku-4-5"
    )


def test_cheap_extraction_model_no_false_positive_on_gemini_pro() -> None:
    """Word-boundary regex must NOT match ``mini`` inside ``gemini``.

    Real bug from OpenClaw 3.3.1 testing: the original
    ``.includes('mini')`` check let ``gemini-2.5-pro`` pass through
    as "already cheap". The word-boundary regex fixes it."""
    # gemini-2.5-pro should NOT be detected as already-cheap; the cheap
    # default for gemini provider takes over.
    assert (
        cheap_extraction_model_for("gemini", "gemini-2.5-pro")
        == "gemini-flash-lite"
    )
    # gemini-flash-lite IS already cheap (via 'flash' + 'lite').
    assert (
        cheap_extraction_model_for("gemini", "gemini-flash-lite")
        == "gemini-flash-lite"
    )


def test_cheap_extraction_model_unknown_provider_returns_chat_model() -> None:
    """Unknown provider — fall back to the user's chat model. Better
    to over-pay for extraction than to silently disable it."""
    assert (
        cheap_extraction_model_for("self-hosted-llm", "my-fancy-model")
        == "my-fancy-model"
    )
    assert cheap_extraction_model_for("", "any-model") == "any-model"


def test_cheap_extraction_model_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TOTALRECLAW_EXTRACTION_MODEL`` env var pins the extraction
    model verbatim — escape hatch for operators."""
    monkeypatch.setenv("TOTALRECLAW_EXTRACTION_MODEL", "gpt-4o")
    # Provider says zai/GLM-4.5-flash, env override wins.
    assert cheap_extraction_model_for("zai", "glm-5.1") == "gpt-4o"
    # Override also wins for unknown-provider fallback path.
    assert cheap_extraction_model_for("self-hosted", "anything") == "gpt-4o"


def test_cheap_extraction_model_pinned_defaults_table() -> None:
    """Smoke check the per-provider defaults table — protects against
    accidental rename / deletion of an entry. Adding a NEW provider
    requires a corresponding cheap pick; this test enumerates the
    contract.

    Cross-client byte-parity is enforced for the providers shared with
    the OpenClaw plugin's ``CHEAP_MODEL_BY_PROVIDER`` table — Pedro
    invariant: "OpenClaw rc.22 user with Z.AI must see the same cheap
    extraction model on Hermes".
    """
    expected_providers = {
        "zai", "openai", "anthropic", "groq", "deepseek",
        "openrouter", "gemini", "google", "mistral", "xai",
        "together", "cerebras",
    }
    assert set(_DEFAULT_EXTRACTION_MODEL_BY_PROVIDER.keys()) >= expected_providers

    # Cross-client parity pins (must match
    # ``skill/plugin/llm-client.ts::CHEAP_MODEL_BY_PROVIDER``).
    assert _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["zai"] == "glm-4.5-flash"
    assert (
        _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["anthropic"]
        == "claude-haiku-4-5-20251001"
    )
    assert _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["openai"] == "gpt-4.1-mini"
    assert _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["gemini"] == "gemini-flash-lite"
    assert _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["google"] == "gemini-flash-lite"
    assert (
        _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["groq"] == "llama-3.3-70b-versatile"
    )
    assert (
        _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER["openrouter"]
        == "anthropic/claude-haiku-4-5-20251001"
    )


def test_derive_extraction_config_zai_keeps_endpoint() -> None:
    """rc.24 design rule: the extraction config uses the SAME
    endpoint as the user's chat config. We do NOT re-route the zai
    endpoint based on the cheap-model family — that would override
    the user's pinned ``ZAI_BASE_URL`` choice.

    GLM-4.5-flash works on both Coding + Standard, so same-endpoint
    is safe regardless of which the user runs.
    """
    chat_config = LLMConfig(
        api_key="sk-zai",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-5.1",
        api_format="openai",
    )
    ext = derive_extraction_config(chat_config)
    assert ext.model == "glm-4.5-flash"
    assert ext.base_url == ZAI_CODING_BASE_URL, (
        "extraction config must reuse the chat endpoint, not flip"
    )
    # Same key, same auth shape.
    assert ext.api_key == chat_config.api_key
    assert ext.api_format == chat_config.api_format

    # Same logic on Standard endpoint.
    chat_config_std = LLMConfig(
        api_key="sk-zai",
        base_url=ZAI_STANDARD_BASE_URL,
        model="glm-5.1",
        api_format="openai",
    )
    ext_std = derive_extraction_config(chat_config_std)
    assert ext_std.base_url == ZAI_STANDARD_BASE_URL


def test_derive_extraction_config_openai() -> None:
    """OpenAI provider — same key + same base_url, model swaps to
    gpt-4.1-mini (cross-client parity with OpenClaw plugin)."""
    chat = LLMConfig(
        api_key="sk-openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4",
        api_format="openai",
    )
    ext = derive_extraction_config(chat)
    assert ext.model == "gpt-4.1-mini"
    assert ext.base_url == "https://api.openai.com/v1"
    assert ext.api_key == "sk-openai"


def test_derive_extraction_config_anthropic_keeps_api_format() -> None:
    """Anthropic uses the Messages API (api_format='anthropic'). The
    extraction config must preserve that — we don't accidentally route
    Anthropic-shaped calls through the OpenAI-shape path."""
    chat = LLMConfig(
        api_key="sk-ant",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-5",
        api_format="anthropic",
    )
    ext = derive_extraction_config(chat)
    assert ext.model == "claude-haiku-4-5-20251001"
    assert ext.api_format == "anthropic"
    assert ext.base_url == "https://api.anthropic.com/v1"


def test_derive_extraction_config_unknown_provider_no_op() -> None:
    """Self-hosted / unknown provider: cheap-model resolves to the
    user's chat model, so the derived config is essentially a copy."""
    chat = LLMConfig(
        api_key="sk-custom",
        base_url="https://my-self-hosted-llm.example.com/v1",
        model="my-fancy-model",
        api_format="openai",
    )
    ext = derive_extraction_config(chat)
    assert ext.model == "my-fancy-model", (
        "unknown provider must keep the user's chat model — no silent "
        "extraction disable, no surprise provider swap"
    )


@pytest.mark.asyncio
async def test_extract_facts_llm_uses_cheap_extraction_model() -> None:
    """End-to-end: when ``extract_facts_llm`` receives a chat-side
    LLMConfig (e.g. zai/GLM-5.1), the actual ``chat_completion`` call
    fires with the cheap extraction model (GLM-4.5-flash) and the
    user's same key + endpoint.

    This is the rc.24 F2 primary fix in action: GLM-5.1 returned empty
    content for the merged-extraction prompt; GLM-4.5-flash works.
    """
    chat_cfg = LLMConfig(
        api_key="sk-zai",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-5.1",
        api_format="openai",
    )

    canned = json.dumps(
        {
            "topics": ["test"],
            "facts": [
                {
                    "text": "User test fact for cheap-extraction-model assertion.",
                    "type": "claim",
                    "importance": 7,
                    "action": "ADD",
                    "source": "user",
                    "scope": "personal",
                    "volatility": "stable",
                }
            ],
        }
    )

    captured_configs: list[LLMConfig] = []

    async def fake_chat_completion(config, system, user, **kwargs):
        captured_configs.append(config)
        return canned

    messages = [
        {"role": "user", "content": "I love hiking on weekends and live in Lisbon."},
        {"role": "assistant", "content": "Got it."},
    ]

    with patch(
        "totalreclaw.agent.extraction.chat_completion",
        new=fake_chat_completion,
    ):
        facts = await extract_facts_llm(messages, mode="turn", llm_config=chat_cfg)

    assert len(facts) > 0
    assert captured_configs, "chat_completion was never called"
    used = captured_configs[0]
    assert used.model == "glm-4.5-flash", (
        f"extraction must call with the cheap zai model glm-4.5-flash, "
        f"got {used.model!r}"
    )
    assert used.api_key == chat_cfg.api_key, (
        "cheap-model swap must reuse the user's API key — same provider"
    )
    assert used.base_url == chat_cfg.base_url, (
        "cheap-model swap must reuse the user's endpoint — Pedro: "
        "extraction stays on the SAME endpoint as chat"
    )


# ---------------------------------------------------------------------------
# Pedro's coordinator-requested asserts (2026-04-26 follow-up):
#
#   1. Token from coding endpoint STAYS on coding endpoint regardless
#      of which GLM the user picked for chat. No endpoint flip — that
#      would 401 on a Coding-plan key hitting Standard.
#   2. Extraction model defaults to glm-4.5-flash for any zai config.
#   3. 401/429 on extraction call surfaces a user-actionable error,
#      not a silent empty.
# ---------------------------------------------------------------------------


def test_zai_coding_token_stays_on_coding_endpoint_regardless_of_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pedro's assert #1: a Coding-plan user's key MUST NOT flip to
    Standard just because the chat model name says ``glm-5.1``. The
    auth token is scoped to Coding; flipping would 401.

    rc.24 design: extraction reuses the chat config's endpoint
    verbatim. ``derive_extraction_config`` swaps the model only.
    """
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)

    # Simulate every chat model the user might pick.
    for chat_model in ("glm-5.1", "glm-5", "glm-5-turbo", "glm-4.5", "glm-4.5-air"):
        chat = LLMConfig(
            api_key="sk-coding-plan-token",
            base_url=ZAI_CODING_BASE_URL,
            model=chat_model,
            api_format="openai",
        )
        ext = derive_extraction_config(chat)
        assert ext.base_url == ZAI_CODING_BASE_URL, (
            f"Coding-plan token must stay on Coding endpoint for "
            f"chat_model={chat_model!r}; got {ext.base_url!r}"
        )
        assert ext.api_key == "sk-coding-plan-token"


def test_zai_standard_token_stays_on_standard_endpoint_regardless_of_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the Coding assert above for Standard PAYG users."""
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)

    for chat_model in ("glm-5.1", "glm-5", "glm-4.5-flash"):
        chat = LLMConfig(
            api_key="sk-standard-payg-token",
            base_url=ZAI_STANDARD_BASE_URL,
            model=chat_model,
            api_format="openai",
        )
        ext = derive_extraction_config(chat)
        assert ext.base_url == ZAI_STANDARD_BASE_URL


def test_extraction_model_defaults_to_glm_45_flash_for_any_zai_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pedro's assert #2: every zai chat config — regardless of
    endpoint or chat model — resolves extraction to glm-4.5-flash."""
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)

    for endpoint in (ZAI_CODING_BASE_URL, ZAI_STANDARD_BASE_URL):
        for chat_model in ("glm-5.1", "glm-5", "glm-5-turbo", "glm-4.5"):
            chat = LLMConfig(
                api_key="sk",
                base_url=endpoint,
                model=chat_model,
                api_format="openai",
            )
            ext = derive_extraction_config(chat)
            assert ext.model == "glm-4.5-flash", (
                f"every zai config must default extraction to "
                f"glm-4.5-flash; got {ext.model!r} for endpoint="
                f"{endpoint!r} chat_model={chat_model!r}"
            )


def test_extraction_model_passes_through_when_chat_already_uses_cheap_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case: user pre-pinned ``glm-4.5-flash`` for chat. The
    cheap-pick is the same string — no change, no log noise."""
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)
    chat = LLMConfig(
        api_key="sk",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-4.5-flash",
        api_format="openai",
    )
    ext = derive_extraction_config(chat)
    assert ext.model == "glm-4.5-flash"


@pytest.mark.asyncio
async def test_extraction_401_surfaces_actionable_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pedro's assert #3: a 401 from the extraction call must surface
    a user-actionable error rather than a silent empty.

    A 401 on extraction means the user's API key doesn't have access
    to the (cheap) extraction model on their endpoint — likely a
    plan-tier issue. We must NOT swallow this and silently no-op;
    the WARN log + raised non-retryable error gives operators the
    signal to investigate.
    """
    cfg = LLMConfig(
        api_key="sk-rejected",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-4.5-flash",
        api_format="openai",
    )

    import httpx

    class _401Resp:
        status_code = 401

        def json(self):
            return {"error": "Unauthorized"}

        @property
        def text(self):
            return "Unauthorized"

        def raise_for_status(self):
            req = MagicMock()
            raise httpx.HTTPStatusError(
                "401 Unauthorized", request=req, response=self
            )

    async def fake_post(self, url, **kwargs):
        return _401Resp()

    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.llm_client")
    with patch("httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(httpx.HTTPStatusError):
            await chat_completion(cfg, "system", "user")

    matched = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "non-retryable HTTP 401" in rec.getMessage()
    ]
    assert matched, (
        "401 must produce an actionable WARNING (not silent empty). "
        "Got log records:\n  "
        + "\n  ".join(
            f"{r.levelname}:{r.name}:{r.getMessage()}" for r in caplog.records
        )
    )


@pytest.mark.asyncio
async def test_extraction_429_balance_error_triggers_existing_endpoint_flip() -> None:
    """The existing rc.3 ``Insufficient balance`` 429 auto-flip must
    survive — that's the right error-code-driven flip (different from
    the wrong empty-200 flip we removed in this rc.24 follow-up).

    A Coding-plan user whose key was migrated to Standard (and
    vice-versa) gets an "Insufficient balance" 429 on the wrong
    endpoint; the existing flip recovers transparently.
    """
    cfg = LLMConfig(
        api_key="sk",
        base_url=ZAI_CODING_BASE_URL,
        model="glm-4.5-flash",
        api_format="openai",
    )

    import httpx

    call_count = {"n": 0}

    class _429Resp:
        status_code = 429

        @property
        def text(self):
            return (
                '{"error":"Insufficient balance or no resource '
                'package. Please recharge."}'
            )

        def json(self):
            return {"error": "Insufficient balance"}

        def raise_for_status(self):
            req = MagicMock()
            raise httpx.HTTPStatusError(
                "429 too many", request=req, response=self
            )

    class _OkResp:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": '{"topics":[],"facts":[]}'}}]
            }

        def raise_for_status(self):
            return None

    async def fake_post(self, url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _429Resp()
        return _OkResp()

    with patch("httpx.AsyncClient.post", new=fake_post):
        out = await chat_completion(cfg, "system", "user")

    assert out == '{"topics":[],"facts":[]}'
    assert call_count["n"] == 2, (
        "expected exactly 2 calls (first coding 429-balance, second "
        f"standard success); got {call_count['n']}"
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
