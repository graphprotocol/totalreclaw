"""2.5.3rc4 — a z.ai Coding-plan Hermes setup extracts with glm-5.3-flash on
the Coding endpoint, and its extraction calls never go to Standard.

Covers the two real Hermes config shapes:
  (1) ``config.yaml`` model block with ``provider: zai`` and
      ``base_url: https://api.z.ai/api/coding/paas/v4``;
  (2) ``.env`` with ``GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4``
      (and ``ZAI_API_KEY``).
Before this fix the zai branch of ``read_hermes_llm_config`` read neither:
both shapes landed on Coding only because it is the default, a Standard URL
in either place was silently replaced by Coding, and the 1113 endpoint flip
could still send a Coding-plan user's calls to Standard.

All HTTP is mocked; no real API calls.
"""
from __future__ import annotations

import httpx
import pytest

from totalreclaw import user_settings
from totalreclaw.agent import llm_client
from totalreclaw.agent.llm_client import (
    LLMUpstreamOutageError,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    complete_with_extraction_fallback,
    derive_extraction_config,
    detect_llm_config,
    read_hermes_llm_config,
)

_BALANCE_1113 = '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'

_ENV_VARS_TO_CLEAR = (
    "ZAI_BASE_URL", "GLM_BASE_URL", "ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY",
    "HERMES_MODEL", "ZAI_MODEL", "GLM_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
    "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "XAI_API_KEY", "TOGETHER_API_KEY", "XDG_CONFIG_HOME",
)


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    for name in _ENV_VARS_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "config.yaml"))
    return tmp_path


def _write(home, config_yaml: str, env: str) -> None:
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (home / ".env").write_text(env, encoding="utf-8")


def _record_posts(monkeypatch, rule):
    calls: list[tuple[str, str]] = []

    async def post(client_self, url, **kwargs):
        endpoint = url.rsplit("/chat/completions", 1)[0]
        model = (kwargs.get("json") or {}).get("model", "")
        calls.append((endpoint, model))
        return rule(endpoint, model)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


def _ok(endpoint, model):
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"facts": []}'}}]},
                          request=httpx.Request("POST", endpoint))


def _err(status, body, endpoint="https://api.z.ai"):
    return httpx.Response(status, text=body, request=httpx.Request("POST", endpoint))


# ---------------------------------------------------------------------------
# The two real Coding-plan shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shape1_config_yaml_base_url_resolves_coding_glm_53_flash(hermes_home, monkeypatch) -> None:
    _write(
        hermes_home,
        "model:\n"
        "  default: glm-5.1\n"
        "  provider: zai\n"
        f"  base_url: {ZAI_CODING_BASE_URL}\n",
        "ZAI_API_KEY=sk-coding-plan\n",
    )
    chat = read_hermes_llm_config()
    assert chat is not None
    assert chat.base_url == ZAI_CODING_BASE_URL
    assert chat.endpoint_pinned is True

    ext = derive_extraction_config(chat)
    assert ext.model == "glm-5.3-flash"
    assert ext.base_url == ZAI_CODING_BASE_URL

    calls = _record_posts(monkeypatch, _ok)
    _, used = await complete_with_extraction_fallback(chat, "s", "u")
    assert used.model == "glm-5.3-flash"
    assert calls == [(ZAI_CODING_BASE_URL, "glm-5.3-flash")]


@pytest.mark.asyncio
async def test_shape2_glm_base_url_in_hermes_env_resolves_coding_glm_53_flash(hermes_home, monkeypatch) -> None:
    _write(
        hermes_home,
        "model:\n  default: glm-5.1\n  provider: zai\n",
        f"ZAI_API_KEY=sk-coding-plan\nGLM_BASE_URL={ZAI_CODING_BASE_URL}\n",
    )
    chat = read_hermes_llm_config()
    assert chat is not None
    assert chat.base_url == ZAI_CODING_BASE_URL
    assert chat.endpoint_pinned is True

    ext = derive_extraction_config(chat)
    assert ext.model == "glm-5.3-flash"
    assert ext.base_url == ZAI_CODING_BASE_URL

    calls = _record_posts(monkeypatch, _ok)
    _, used = await complete_with_extraction_fallback(chat, "s", "u")
    assert used.model == "glm-5.3-flash"
    assert calls == [(ZAI_CODING_BASE_URL, "glm-5.3-flash")]


def test_shape2_via_process_env_resolves_coding_glm_53_flash(hermes_home, monkeypatch) -> None:
    """Hermes loads ``.env`` into the gateway's environment, so the env-var
    resolver (``detect_llm_config``'s first tier) sees the same values."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-coding-plan")
    monkeypatch.setenv("GLM_BASE_URL", ZAI_CODING_BASE_URL)
    monkeypatch.setenv("HERMES_MODEL", "glm-5.1")
    chat = detect_llm_config()
    assert chat is not None
    assert (chat.base_url, chat.endpoint_pinned) == (ZAI_CODING_BASE_URL, True)
    ext = derive_extraction_config(chat)
    assert (ext.model, ext.base_url) == ("glm-5.3-flash", ZAI_CODING_BASE_URL)


# ---------------------------------------------------------------------------
# Never Standard for a Coding-plan setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_plan_1113_stays_on_coding_and_does_not_downgrade(hermes_home, monkeypatch) -> None:
    """1113 on the Coding plan is its rate limit. A configured Coding endpoint
    is never flipped to Standard (which would bill a PAYG balance), the call
    backs off on Coding, and glm-5.3-flash stays the extraction model."""
    _write(
        hermes_home,
        f"model:\n  default: glm-5.1\n  provider: zai\n  base_url: {ZAI_CODING_BASE_URL}\n",
        "ZAI_API_KEY=sk-coding-plan\n",
    )
    chat = read_hermes_llm_config()

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(llm_client._asyncio, "sleep", _no_sleep)
    monkeypatch.setenv("TOTALRECLAW_LLM_RETRY_BUDGET_MS", "600000")
    calls = _record_posts(monkeypatch, lambda endpoint, model: _err(429, _BALANCE_1113, endpoint))

    with pytest.raises(LLMUpstreamOutageError):
        await complete_with_extraction_fallback(chat, "s", "u")
    assert calls, "expected calls"
    assert {endpoint for endpoint, _ in calls} == {ZAI_CODING_BASE_URL}
    assert {model for _, model in calls} == {"glm-5.3-flash"}
    assert derive_extraction_config(chat).model == "glm-5.3-flash"


@pytest.mark.asyncio
async def test_unconfigured_endpoint_still_flips_for_payg_keys(hermes_home, monkeypatch) -> None:
    """No endpoint configured anywhere: the Coding default is not pinned, so a
    PAYG key keeps the existing 1113 flip to Standard."""
    _write(hermes_home, "model:\n  default: glm-5.1\n  provider: zai\n", "ZAI_API_KEY=sk-payg\n")
    chat = read_hermes_llm_config()
    assert (chat.base_url, chat.endpoint_pinned) == (ZAI_CODING_BASE_URL, False)

    def rule(endpoint, model):
        if endpoint == ZAI_CODING_BASE_URL or model == "glm-5.3-flash":
            return _err(429, _BALANCE_1113, endpoint)
        return _ok(endpoint, model)

    calls = _record_posts(monkeypatch, rule)
    _, used = await complete_with_extraction_fallback(chat, "s", "u")
    assert used.model == "glm-4.5-flash"
    assert (ZAI_STANDARD_BASE_URL, "glm-4.5-flash") in calls


# ---------------------------------------------------------------------------
# Standard counterparts + precedence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config_yaml,env",
    [
        (f"model:\n  default: glm-5.1\n  provider: zai\n  base_url: {ZAI_STANDARD_BASE_URL}\n",
         "ZAI_API_KEY=sk-payg\n"),
        ("model:\n  default: glm-5.1\n  provider: zai\n",
         f"ZAI_API_KEY=sk-payg\nGLM_BASE_URL={ZAI_STANDARD_BASE_URL}\n"),
    ],
    ids=["config-yaml", "glm-base-url"],
)
def test_standard_endpoint_in_either_shape_is_honoured(hermes_home, config_yaml, env) -> None:
    _write(hermes_home, config_yaml, env)
    chat = read_hermes_llm_config()
    assert (chat.base_url, chat.endpoint_pinned) == (ZAI_STANDARD_BASE_URL, True)
    ext = derive_extraction_config(chat)
    assert (ext.model, ext.base_url) == ("glm-4.5-flash", ZAI_STANDARD_BASE_URL)


def test_zai_base_url_beats_config_yaml_and_glm_base_url(hermes_home) -> None:
    _write(
        hermes_home,
        f"model:\n  default: glm-5.1\n  provider: zai\n  base_url: {ZAI_STANDARD_BASE_URL}\n",
        f"ZAI_API_KEY=k\nGLM_BASE_URL={ZAI_STANDARD_BASE_URL}\nZAI_BASE_URL={ZAI_CODING_BASE_URL}\n",
    )
    assert read_hermes_llm_config().base_url == ZAI_CODING_BASE_URL


@pytest.mark.asyncio
async def test_pinned_standard_1113_moves_to_next_candidate_without_backoff(hermes_home, monkeypatch) -> None:
    """A PAYG user who asked for glm-5.3-flash (paid on Standard) with no
    balance falls back to the chat model at once, on Standard."""
    _write(
        hermes_home,
        f"model:\n  default: glm-4.5-air\n  provider: zai\n  base_url: {ZAI_STANDARD_BASE_URL}\n",
        "ZAI_API_KEY=sk-payg\n",
    )
    user_settings.set_extraction_model("glm-5.3-flash")
    chat = read_hermes_llm_config()
    slept: list[float] = []

    async def _sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(llm_client._asyncio, "sleep", _sleep)
    calls = _record_posts(
        monkeypatch,
        lambda endpoint, model: _err(429, _BALANCE_1113, endpoint) if model == "glm-5.3-flash" else _ok(endpoint, model),
    )
    _, used = await complete_with_extraction_fallback(chat, "s", "u")
    assert used.model == "glm-4.5-air"
    assert slept == []
    assert {endpoint for endpoint, _ in calls} == {ZAI_STANDARD_BASE_URL}
