"""z.ai GLM extraction efficiency + empty-content observability.

Scope
-----
These tests cover three z.ai-related hardening measures for the extraction
path. They are EFFICIENCY + OBSERVABILITY, not the #376 fix (see note).

z.ai's GLM-4.5-series-and-up models — including the cheap extraction
workhorse ``glm-4.5-flash`` — default to ``thinking: {"type": "enabled"}``.
With thinking on, the model spends generation budget emitting
chain-of-thought into ``message.reasoning_content`` before answering. For
our cheap, JSON-shaped extraction prompts that reasoning pass adds latency
and burns completion tokens for no quality gain, and in edge cases (very
small token budgets) can split the answer across ``reasoning_content`` with
an empty ``content``.

  Evidence:
    - z.ai docs: ``thinking`` request param, default "enabled", allowed
      "enabled"/"disabled"; reasoning lands in ``message.reasoning_content``;
      applies to GLM-4.5 series and higher.
    - Community reports of ``{"content":"","reasoning_content":"..."}``
      content/reasoning splits at tight token budgets.

NOTE — NOT the #376 fix
-----------------------
Thinking mode is NOT the root cause of #376. At a realistic max_tokens
budget GLM populates ``content`` with thinking on or off; the #376 QA
empties were instant 200-empty responses (quota/throttle, ~14ms), with no
``reasoning_content`` to recover. The real #376 fix — detect
quota-exhausted instant-empty and surface an actionable "z.ai quota
exhausted" error — is tracked separately. This change reduces wasted work
and makes any residual empty-content case loud and diagnosable.

Changes covered
---------------
1. EFFICIENCY: ``_call_openai`` sends ``thinking: {"type": "disabled"}`` for
   z.ai endpoints so the answer lands in ``content`` without a reasoning
   pass. SAME provider, SAME endpoint, SAME key — only the request body
   changes. NOT sent to non-z.ai providers (would 400 on OpenAI et al.).
2. HARDENING: when ``content`` is empty, ``_call_openai`` falls back to
   ``reasoning_content`` so any provider/version that ignores the flag
   still yields the payload (the parser strips ``<think>`` and
   bracket-scans for the JSON body).
3. OBSERVABILITY: when both ``content`` and ``reasoning_content`` are empty,
   ``_call_openai`` WARNs with the full response shape (#318 rich diag), and
   when the cheap model AND the chat-model fallback both empty, the
   extraction no-op WARNING is ACTIONABLE — it names
   ``TOTALRECLAW_EXTRACTION_MODEL``.

All tests mock ``httpx`` / ``chat_completion`` — NO network, NO real keys.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent import extraction
from totalreclaw.agent.extraction import extract_facts_llm, extract_facts_compaction
from totalreclaw.agent.llm_client import (
    LLMConfig,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    _is_zai_base_url,
    chat_completion,
    derive_extraction_config,
)


# ---------------------------------------------------------------------------
# Helpers — minimal httpx.Response double (mirrors the convention in
# test_extraction_empty_response_visibility.py).
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(
        self, payload: dict, status_code: int = 200, headers: dict | None = None
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        # _call_openai's rich empty-content diag scans response headers for
        # rate-limit/quota markers; mirror httpx.Response.headers.
        self.headers = headers or {}

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


def _post_returning(payload: dict, status_code: int = 200) -> AsyncMock:
    return AsyncMock(return_value=_FakeResp(payload, status_code=status_code))


_LONG_MSG = (
    "Hi, I'm Pedro. I live in Porto and I use PostgreSQL for all my side "
    "projects — never MySQL. I also prefer Vim over VS Code."
)


# ---------------------------------------------------------------------------
# 1. z.ai endpoint detection (gates the thinking-mode fix).
# ---------------------------------------------------------------------------


def test_is_zai_base_url_matches_both_endpoints() -> None:
    assert _is_zai_base_url(ZAI_CODING_BASE_URL)
    assert _is_zai_base_url(ZAI_STANDARD_BASE_URL)
    # Trailing slash / case variations still match (substring on lower()).
    assert _is_zai_base_url("https://API.Z.AI/api/coding/paas/v4/")


def test_is_zai_base_url_rejects_other_providers() -> None:
    assert not _is_zai_base_url("https://api.openai.com/v1")
    assert not _is_zai_base_url("https://api.anthropic.com/v1")
    assert not _is_zai_base_url("https://generativelanguage.googleapis.com/v1beta/openai")
    assert not _is_zai_base_url("https://my-self-hosted.example.com/v1")
    assert not _is_zai_base_url("")


# ---------------------------------------------------------------------------
# 2. PRIMARY FIX — thinking:{type:disabled} is sent for z.ai, and ONLY z.ai.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [ZAI_CODING_BASE_URL, ZAI_STANDARD_BASE_URL])
async def test_zai_request_disables_thinking(endpoint: str) -> None:
    """The z.ai request body MUST carry ``thinking: {"type": "disabled"}``.

    Efficiency: disabling thinking skips the chain-of-thought pass on cheap
    JSON extraction calls (faster, fewer completion tokens) and avoids the
    content/reasoning_content split at tight token budgets.
    """
    cfg = LLMConfig(api_key="k", base_url=endpoint, model="glm-4.5-flash", api_format="openai")
    fake = _post_returning({"choices": [{"message": {"content": '{"topics":[],"facts":[]}'}}]})
    with patch("httpx.AsyncClient.post", new=fake):
        out = await chat_completion(cfg, "system", "user")

    assert out == '{"topics":[],"facts":[]}'
    body = fake.await_args.kwargs.get("json") or {}
    assert body.get("thinking") == {"type": "disabled"}, (
        "z.ai request must disable thinking so the answer lands in content; "
        f"got body keys {sorted(body.keys())}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1",  # routed via _call_openai only if api_format=openai; still must not carry thinking
        "https://my-self-hosted.example.com/v1",
    ],
)
async def test_non_zai_request_omits_thinking(base_url: str) -> None:
    """``thinking`` is a z.ai-only field. Sending it to OpenAI (and other
    strict OpenAI-spec providers) risks a 400, so it must be absent for
    every non-z.ai endpoint."""
    cfg = LLMConfig(api_key="k", base_url=base_url, model="some-model", api_format="openai")
    fake = _post_returning({"choices": [{"message": {"content": "ok"}}]})
    with patch("httpx.AsyncClient.post", new=fake):
        await chat_completion(cfg, "system", "user")
    body = fake.await_args.kwargs.get("json") or {}
    assert "thinking" not in body, f"non-z.ai provider {base_url} must not get a thinking field"


# ---------------------------------------------------------------------------
# 3. BELT-AND-SUSPENDERS — empty content recovers from reasoning_content.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_content_recovers_from_reasoning_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If a provider ignores ``thinking:disabled`` and still returns empty
    ``content`` with the payload in ``reasoning_content``, ``_call_openai``
    must recover it instead of dropping the turn."""
    cfg = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-4.5-flash", api_format="openai")
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": '{"topics": ["identity"], "facts": []}',
                },
                "finish_reason": "stop",
            }
        ]
    }
    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.llm_client")
    with patch("httpx.AsyncClient.post", new=_post_returning(payload)):
        out = await chat_completion(cfg, "system", "user")

    assert out == '{"topics": ["identity"], "facts": []}', "must recover the reasoning_content payload"
    # And it should announce the recovery (so operators know thinking mode leaked through).
    assert any(
        "recovering from reasoning_content" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "recovery must be logged at WARNING"


@pytest.mark.asyncio
async def test_empty_content_and_empty_reasoning_warns_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When BOTH ``content`` and ``reasoning_content`` are empty, the
    original loud "LLM returned empty content" WARNING still fires and the
    call returns empty (no false recovery)."""
    cfg = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-4.5-flash", api_format="openai")
    payload = {
        "choices": [{"message": {"content": "", "reasoning_content": "   "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 0},
    }
    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.llm_client")
    with patch("httpx.AsyncClient.post", new=_post_returning(payload)):
        out = await chat_completion(cfg, "system", "user")

    assert out in (None, "")
    assert any(
        "LLM returned empty content" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), "genuinely-empty response must still WARN with response shape"
    # Must NOT have claimed a recovery.
    assert not any(
        "recovering from reasoning_content" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_truly_empty_diag_surfaces_full_response_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Supersedes PR #318: when ``content`` is empty with NO recoverable
    ``reasoning_content``, the WARN must carry the FULL response shape so an
    operator can tell a quota/throttle instant-empty (rl headers, HTTP 200,
    no reasoning) apart from a token-budget/formatting issue (finish_reason,
    usage) WITHOUT needing DEBUG. This is the observability guarantee behind
    the #376 forensics.
    """
    cfg = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-4.5-flash", api_format="openai")
    payload = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12},
    }
    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.llm_client")
    with patch("httpx.AsyncClient.post", new=_post_returning(payload)):
        out = await chat_completion(cfg, "system", "user")

    assert out in (None, "")
    diag = [
        r.getMessage()
        for r in caplog.records
        if "LLM returned empty content" in r.getMessage()
    ]
    assert diag, "truly-empty response must emit the rich diag"
    msg = diag[0]
    # Field-level guarantees (the #318 contribution over a bare WARN string).
    assert "finish_reason=length" in msg
    assert "reasoning_content_present=False" in msg
    assert "reasoning_content_chars=0" in msg
    assert "message_keys=" in msg
    assert "http_status=200" in msg
    assert "rl_headers=" in msg
    assert "usage=" in msg


@pytest.mark.asyncio
async def test_reasoning_content_ignored_when_content_present() -> None:
    """``content`` wins when present — reasoning_content is only a fallback."""
    cfg = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-4.5-flash", api_format="openai")
    payload = {
        "choices": [
            {"message": {"content": "REAL ANSWER", "reasoning_content": "should be ignored"}}
        ]
    }
    with patch("httpx.AsyncClient.post", new=_post_returning(payload)):
        out = await chat_completion(cfg, "system", "user")
    assert out == "REAL ANSWER"


# ---------------------------------------------------------------------------
# 4. z.ai model-selection (the derivation feeding the request).
# ---------------------------------------------------------------------------


def test_derive_extraction_config_zai_uses_flash_same_endpoint_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The derived extraction config swaps to ``glm-4.5-flash`` while
    keeping the user's endpoint + key (so the thinking-mode fix above runs
    against the same Coding/Standard endpoint the user is entitled to)."""
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    chat = LLMConfig(api_key="sk-coding", base_url=ZAI_CODING_BASE_URL, model="glm-5.1", api_format="openai")
    ext = derive_extraction_config(chat)
    assert ext.model == "glm-4.5-flash"
    assert ext.base_url == ZAI_CODING_BASE_URL, "no endpoint flip — key is endpoint-scoped"
    assert ext.api_key == "sk-coding"
    assert ext.api_format == "openai"


def test_extraction_model_override_pins_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator escape hatch named in the actionable warning actually
    pins the extraction model verbatim (so following the advice works)."""
    monkeypatch.setenv("TOTALRECLAW_EXTRACTION_MODEL", "glm-5.1")
    chat = LLMConfig(api_key="sk", base_url=ZAI_CODING_BASE_URL, model="glm-5.1", api_format="openai")
    ext = derive_extraction_config(chat)
    assert ext.model == "glm-5.1"
    assert ext.base_url == ZAI_CODING_BASE_URL


# ---------------------------------------------------------------------------
# 5. Full path: empty -> #376 fallback -> ACTIONABLE warning.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_empty_emits_actionable_warning_naming_env_var(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the cheap model AND the #376 chat-model fallback both empty,
    the final no-op WARNING must be ACTIONABLE: name
    ``TOTALRECLAW_EXTRACTION_MODEL`` and a concrete value to try.

    This is the safety net required so the failure is NEVER silent again.
    """
    chat = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-5.1", api_format="openai")

    seen: list[str] = []

    async def fake_cc(cfg, system, user, *a, **k):
        seen.append(cfg.model)
        return ""  # both attempts empty

    monkeypatch_target = "totalreclaw.agent.extraction.chat_completion"
    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.extraction")
    with patch(monkeypatch_target, new=fake_cc):
        out = await extract_facts_llm([{"role": "user", "content": _LONG_MSG}], llm_config=chat)

    assert out == []
    # The #376 fallback must have fired: cheap model first, chat model second.
    assert seen == ["glm-4.5-flash", "glm-5.1"], seen

    final = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "NO-OP" in r.getMessage()
    ]
    assert final, "expected a final NO-OP warning after both attempts emptied"
    msg = final[-1]
    assert "TOTALRECLAW_EXTRACTION_MODEL" in msg, "warning must name the override env var (actionable)"
    assert "glm-4.5-flash" in msg, "warning should name the known-good default to try"
    # Names the cheap model and the chat-model fallback that were tried.
    assert "glm-5.1" in msg


@pytest.mark.asyncio
async def test_compaction_double_empty_emits_actionable_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The compaction path's empty-response warning is likewise actionable
    (compaction is the last-chance capture, so a silent no-op is data loss)."""
    chat = LLMConfig(api_key="k", base_url=ZAI_CODING_BASE_URL, model="glm-5.1", api_format="openai")

    async def fake_cc(cfg, system, user, *a, **k):
        return ""

    caplog.set_level(logging.WARNING, logger="totalreclaw.agent.extraction")
    with patch("totalreclaw.agent.extraction.chat_completion", new=fake_cc):
        out = await extract_facts_compaction(
            [{"role": "user", "content": _LONG_MSG}], llm_config=chat
        )

    assert out == []
    final = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "NO-OP" in r.getMessage()
    ]
    assert final, "expected a NO-OP warning from compaction"
    assert "TOTALRECLAW_EXTRACTION_MODEL" in final[-1]


@pytest.mark.asyncio
async def test_thinking_disabled_makes_extraction_produce_facts_end_to_end() -> None:
    """End-to-end happy path on z.ai with thinking disabled: a mocked z.ai
    response (JSON in ``content``) flows through ``extract_facts_llm`` and
    yields a fact, while the issued request carries
    ``thinking: {"type": "disabled"}`` and the cheap model. Guards the
    efficiency path + request shape against regression.
    """
    chat = LLMConfig(api_key="sk-zai", base_url=ZAI_CODING_BASE_URL, model="glm-5.1", api_format="openai")
    canned = json.dumps(
        {
            "topics": ["identity"],
            "facts": [
                {
                    "text": "User lives in Porto and uses PostgreSQL for side projects.",
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

    # Drive at the httpx layer so the real _call_openai (with the thinking
    # field + content read) is exercised, not just a stubbed chat_completion.
    captured_bodies: list[dict] = []

    async def fake_post(self, url, **kwargs):
        captured_bodies.append(kwargs.get("json") or {})
        return _FakeResp({"choices": [{"message": {"content": canned}}]})

    with patch("httpx.AsyncClient.post", new=fake_post):
        facts = await extract_facts_llm(
            [{"role": "user", "content": _LONG_MSG}], mode="turn", llm_config=chat
        )

    assert len(facts) > 0, "extraction must produce facts on z.ai now that thinking is disabled"
    assert facts[0].type == "claim"
    # The extraction request actually disabled thinking + used the cheap model.
    assert captured_bodies, "no request was issued"
    first = captured_bodies[0]
    assert first.get("thinking") == {"type": "disabled"}
    assert first.get("model") == "glm-4.5-flash"
