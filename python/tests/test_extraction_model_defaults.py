"""2.5.3rc4 — extraction model defaults, runtime fallback, and user override.

Users never pick an extraction model: each provider has ordered, tested
candidates (endpoint-aware for z.ai), a candidate that proves unusable at
runtime is skipped for the next one (ending at the chat model), and a user
who does want a specific model says so in chat — the agent calls
``totalreclaw_set_extraction_model``, which persists to
``~/.totalreclaw/settings.json`` and applies on the next extraction.

All HTTP is mocked at ``httpx.AsyncClient.post``; no real API calls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from totalreclaw import user_settings
from totalreclaw.agent import llm_client
from totalreclaw.agent.llm_client import (
    LLMConfig,
    LLMUpstreamOutageError,
    ZAI_CODING_BASE_URL,
    ZAI_STANDARD_BASE_URL,
    _EXTRACTION_MODEL_CANDIDATES_BY_PROVIDER,
    _ZAI_STANDARD_EXTRACTION_CANDIDATES,
    classify_model_unusable,
    complete_with_extraction_fallback,
    default_extraction_candidates,
    describe_extraction_model,
    extraction_model_plan,
    resolve_extraction_model,
)

# Captured at import, before conftest's autouse fixture redirects it.
from totalreclaw.user_settings import settings_path as _real_settings_path

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_LLM_CLIENT = REPO_ROOT / "skill" / "plugin" / "llm" / "llm-client.ts"

OPENAI_BASE = "https://api.openai.com/v1"


def _zai(base_url: str = ZAI_CODING_BASE_URL, model: str = "glm-5.1") -> LLMConfig:
    return LLMConfig(api_key="sk-test", base_url=base_url, model=model, api_format="openai")


def _ok(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": f'{{"model": "{model}"}}'}}]},
        request=httpx.Request("POST", "https://example.invalid"),
    )


def _err(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("POST", "https://example.invalid"))


_BALANCE_1113 = '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'


class _Router:
    """Fake ``httpx.AsyncClient.post``: ``rule(url, model) -> Response``.
    Records every (endpoint, model) it served."""

    def __init__(self, rule):
        self.rule = rule
        self.calls: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> None:
        async def post(client_self, url, **kwargs):
            model = (kwargs.get("json") or {}).get("model", "")
            endpoint = url.rsplit("/chat/completions", 1)[0]
            self.calls.append((endpoint, model))
            return self.rule(endpoint, model)

        monkeypatch.setattr(httpx.AsyncClient, "post", post)

    def models(self) -> list[str]:
        return [m for _, m in self.calls]


@pytest.fixture
def no_sleep(monkeypatch):
    """Make chat_completion's retry backoff instant (and count the sleeps)."""
    slept: list[float] = []

    async def _sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(llm_client._asyncio, "sleep", _sleep)
    return slept


async def _extract(router, monkeypatch, chat: LLMConfig):
    router.install(monkeypatch)
    return await complete_with_extraction_fallback(chat, "system", "user")


# ---------------------------------------------------------------------------
# 1. Candidate table + resolution order
# ---------------------------------------------------------------------------


def test_zai_candidates_depend_on_endpoint() -> None:
    assert default_extraction_candidates("zai", ZAI_CODING_BASE_URL) == ("glm-5.3-flash", "glm-4.5-flash")
    assert default_extraction_candidates("zai", ZAI_STANDARD_BASE_URL) == ("glm-4.5-flash",)
    assert default_extraction_candidates("zai", ZAI_STANDARD_BASE_URL + "/") == ("glm-4.5-flash",)
    # No / unknown base URL -> the provider default (Coding for z.ai).
    assert default_extraction_candidates("ZAI") == ("glm-5.3-flash", "glm-4.5-flash")
    assert default_extraction_candidates("self-hosted", "http://localhost:8000/v1") == ()


def test_plan_defaults_end_with_chat_model() -> None:
    plan = extraction_model_plan("zai", "glm-5.1", ZAI_CODING_BASE_URL)
    assert plan.candidates == ("glm-5.3-flash", "glm-4.5-flash", "glm-5.1")
    assert plan.source == "default"

    std = extraction_model_plan("zai", "glm-5.1", ZAI_STANDARD_BASE_URL)
    assert std.candidates == ("glm-4.5-flash", "glm-5.1")


def test_plan_unknown_provider_uses_chat_model_only() -> None:
    plan = extraction_model_plan("self-hosted", "my-local-model", "http://localhost:8000/v1")
    assert plan.candidates == ("my-local-model",)
    assert plan.source == "chat-model"


def test_plan_cheap_chat_model_is_used_as_is() -> None:
    plan = extraction_model_plan("openai", "gpt-4.1-mini", OPENAI_BASE)
    assert plan.candidates == ("gpt-4.1-mini",)
    assert plan.source == "chat-model-cheap"


def test_plan_precedence_env_over_setting_over_defaults(monkeypatch) -> None:
    user_settings.set_extraction_model("glm-4.5-flash")
    plan = extraction_model_plan("zai", "glm-5.1", ZAI_CODING_BASE_URL)
    assert plan.candidates == ("glm-4.5-flash", "glm-5.1")
    assert plan.source == "setting"

    # The setting beats a cheap chat model too.
    plan = extraction_model_plan("openai", "gpt-4.1-mini", OPENAI_BASE)
    assert plan.candidates == ("glm-4.5-flash", "gpt-4.1-mini")

    monkeypatch.setenv("TOTALRECLAW_EXTRACTION_MODEL", "glm-5.3-flash")
    plan = extraction_model_plan("zai", "glm-5.1", ZAI_CODING_BASE_URL)
    assert plan.candidates == ("glm-5.3-flash", "glm-5.1")
    assert plan.source == "env"

    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL")
    user_settings.set_extraction_model("default")
    assert extraction_model_plan("zai", "glm-5.1", ZAI_CODING_BASE_URL).source == "default"


def test_plan_dedupes_when_override_equals_chat_model(monkeypatch) -> None:
    monkeypatch.setenv("TOTALRECLAW_EXTRACTION_MODEL", "glm-5.1")
    assert extraction_model_plan("zai", "glm-5.1").candidates == ("glm-5.1",)


def test_corrupt_settings_file_does_not_break_resolution() -> None:
    path = user_settings.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert extraction_model_plan("zai", "glm-5.1").source == "default"


# ---------------------------------------------------------------------------
# 2. Runtime fallback, per error class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_uses_first_candidate(monkeypatch, caplog) -> None:
    caplog.set_level("INFO", logger="totalreclaw.agent.llm_client")
    router = _Router(lambda url, model: _ok(model))
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert cfg.model == "glm-5.3-flash"
    assert "glm-5.3-flash" in response
    assert cfg.base_url == ZAI_CODING_BASE_URL and cfg.api_key == "sk-test"

    # The gateway log names the model once (not every turn), never the key.
    await _extract(router, monkeypatch, _zai())
    announced = [r.getMessage() for r in caplog.records if "extraction_model=" in r.getMessage()]
    assert len(announced) == 1
    assert "extraction_model=glm-5.3-flash" in announced[0] and "source=default" in announced[0]
    assert "sk-test" not in caplog.text


@pytest.mark.parametrize(
    "status,body",
    [
        (404, '{"error":{"code":"model_not_found","message":"The model does not exist"}}'),
        (400, '{"error":{"code":"1211","message":"Unknown Model, please check the model code."}}'),
        (401, '{"error":"Unauthorized"}'),
        (403, '{"error":{"message":"You do not have access to this model"}}'),
    ],
)
@pytest.mark.asyncio
async def test_model_unusable_falls_back_and_is_remembered(monkeypatch, caplog, status, body) -> None:
    router = _Router(lambda url, model: _err(status, body) if model == "glm-5.3-flash" else _ok(model))
    caplog.set_level("INFO", logger="totalreclaw.agent.llm_client")

    response, cfg = await _extract(router, monkeypatch, _zai())
    assert cfg.model == "glm-4.5-flash"
    assert router.models() == ["glm-5.3-flash", "glm-4.5-flash"]
    fallback_logs = [r for r in caplog.records if "falling back to glm-4.5-flash" in r.getMessage()]
    assert len(fallback_logs) == 1 and fallback_logs[0].levelname == "INFO"

    # Next turn goes straight to the working model: no re-probe.
    router.calls.clear()
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert router.models() == ["glm-4.5-flash"]
    assert resolve_extraction_model(_zai()) == ("glm-4.5-flash", "fallback")


@pytest.mark.asyncio
async def test_all_candidates_unusable_ends_at_chat_model(monkeypatch) -> None:
    router = _Router(lambda url, model: _ok(model) if model == "glm-5.1" else _err(404, "not found"))
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert cfg.model == "glm-5.1"
    assert router.models() == ["glm-5.3-flash", "glm-4.5-flash", "glm-5.1"]


@pytest.mark.asyncio
async def test_chat_model_errors_propagate(monkeypatch) -> None:
    """The last candidate (chat model) is never skipped and its errors are
    the caller's to handle, exactly as before."""
    router = _Router(lambda url, model: _err(404, "not found"))
    router.install(monkeypatch)
    chat = LLMConfig(api_key="k", base_url="http://localhost:8000/v1", model="local", api_format="openai")
    with pytest.raises(httpx.HTTPStatusError):
        await complete_with_extraction_fallback(chat, "s", "u")
    # Still the first (only) candidate next time.
    assert resolve_extraction_model(chat) == ("local", "chat-model")


@pytest.mark.asyncio
async def test_unrelated_400_does_not_downgrade(monkeypatch) -> None:
    router = _Router(lambda url, model: _err(400, '{"error":{"message":"messages: field required"}}'))
    router.install(monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        await complete_with_extraction_fallback(_zai(), "s", "u")
    assert router.models() == ["glm-5.3-flash"]
    assert resolve_extraction_model(_zai())[0] == "glm-5.3-flash"


@pytest.mark.asyncio
async def test_zai_1113_on_both_endpoints_falls_back_fast(monkeypatch, no_sleep) -> None:
    """PAYG key without balance on the default (Coding) URL: glm-5.3-flash
    gets 1113 on Coding and on Standard. The fallback must not spend the
    ~60s retry budget, and glm-4.5-flash (free on Standard) answers."""

    def rule(url, model):
        if model == "glm-5.3-flash":
            return _err(429, _BALANCE_1113)
        if url == ZAI_CODING_BASE_URL:
            return _err(429, _BALANCE_1113)  # no Coding plan
        return _ok(model)

    router = _Router(rule)
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert cfg.model == "glm-4.5-flash"
    assert no_sleep == [], "1113 fallback must not back off"
    assert router.calls[:2] == [
        (ZAI_CODING_BASE_URL, "glm-5.3-flash"),
        (ZAI_STANDARD_BASE_URL, "glm-5.3-flash"),
    ]
    assert resolve_extraction_model(_zai()) == ("glm-4.5-flash", "fallback")


@pytest.mark.asyncio
async def test_soft_fallback_expires_after_cooldown(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(llm_client._time, "monotonic", lambda: clock["t"])
    llm_client._mark_unusable(ZAI_CODING_BASE_URL, "glm-5.3-flash", soft=True)
    assert resolve_extraction_model(_zai())[0] == "glm-4.5-flash"

    clock["t"] += llm_client._SOFT_FALLBACK_COOLDOWN_S + 1
    assert resolve_extraction_model(_zai()) == ("glm-5.3-flash", "default")


@pytest.mark.asyncio
async def test_single_empty_retries_next_without_skipping(monkeypatch) -> None:
    empties = {"glm-5.3-flash": 1}

    def rule(url, model):
        if empties.get(model, 0) > 0:
            empties[model] -= 1
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]},
                                  request=httpx.Request("POST", url))
        return _ok(model)

    router = _Router(rule)
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert cfg.model == "glm-4.5-flash"
    # One empty is not "persistent": the preferred model stays first.
    assert resolve_extraction_model(_zai())[0] == "glm-5.3-flash"
    router.calls.clear()
    response, cfg = await _extract(router, monkeypatch, _zai())
    assert router.models() == ["glm-5.3-flash"]


@pytest.mark.asyncio
async def test_persistent_empty_skips_candidate(monkeypatch) -> None:
    def rule(url, model):
        if model == "glm-5.3-flash":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]},
                                  request=httpx.Request("POST", url))
        return _ok(model)

    router = _Router(rule)
    for _ in range(llm_client._EMPTY_STREAK_LIMIT):
        await _extract(router, monkeypatch, _zai())
    assert resolve_extraction_model(_zai()) == ("glm-4.5-flash", "fallback")
    router.calls.clear()
    await _extract(router, monkeypatch, _zai())
    assert router.models() == ["glm-4.5-flash"]


@pytest.mark.parametrize("status", [503, 429])
@pytest.mark.asyncio
async def test_transient_errors_never_downgrade(monkeypatch, no_sleep, status) -> None:
    """5xx and ordinary rate limits are retried by chat_completion and
    surface as an outage; the preferred model stays first."""
    monkeypatch.setenv("TOTALRECLAW_LLM_RETRY_BUDGET_MS", "600000")
    body = '{"error":{"code":"1302","message":"Rate limit reached"}}' if status == 429 else "upstream down"
    router = _Router(lambda url, model: _err(status, body))
    router.install(monkeypatch)
    with pytest.raises(LLMUpstreamOutageError):
        await complete_with_extraction_fallback(_zai(), "s", "u")
    assert set(router.models()) == {"glm-5.3-flash"}, "no other candidate is tried on a transient error"
    assert resolve_extraction_model(_zai()) == ("glm-5.3-flash", "default")


@pytest.mark.asyncio
async def test_timeouts_never_downgrade(monkeypatch, no_sleep) -> None:
    async def boom(client_self, url, **kwargs):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setenv("TOTALRECLAW_LLM_RETRY_BUDGET_MS", "600000")
    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    with pytest.raises(LLMUpstreamOutageError):
        await complete_with_extraction_fallback(_zai(), "s", "u")
    assert resolve_extraction_model(_zai()) == ("glm-5.3-flash", "default")


def test_classify_model_unusable() -> None:
    assert classify_model_unusable(404, "")
    assert classify_model_unusable(401, "")
    assert classify_model_unusable(403, "")
    assert classify_model_unusable(400, '{"error":{"code":"1211"}}')
    assert classify_model_unusable(400, "The model `x` does not exist")
    # Not about the model: content filter, malformed request, rate limit, 5xx.
    assert classify_model_unusable(400, '{"error":{"code":"1301"}}') is None
    assert classify_model_unusable(400, "bad request") is None
    assert classify_model_unusable(429, "rate limit") is None
    assert classify_model_unusable(500, "model not found") is None


# ---------------------------------------------------------------------------
# 3. User override: settings file, Hermes tool, CLI, status
# ---------------------------------------------------------------------------


def test_settings_path_honours_state_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    assert _real_settings_path() == tmp_path / "settings.json"


def test_settings_roundtrip_is_atomic_and_clearable() -> None:
    assert user_settings.get_extraction_model() is None
    assert user_settings.set_extraction_model("  glm-5.3-flash ") == "glm-5.3-flash"
    assert json.loads(user_settings.settings_path().read_text())["extraction_model"] == "glm-5.3-flash"
    assert not list(user_settings.settings_path().parent.glob(".settings.json.*.tmp"))
    for clear in ("", "default", "Auto"):
        user_settings.set_extraction_model("x")
        assert user_settings.set_extraction_model(clear) is None
        assert user_settings.get_extraction_model() is None


@pytest.fixture
def zai_chat_config(monkeypatch):
    """Make the Hermes tool / status resolve a z.ai Coding chat config."""
    from totalreclaw.hermes import tools

    monkeypatch.setattr(tools, "_read_hermes_llm_config", lambda: _zai())
    return _zai()


@pytest.mark.asyncio
async def test_set_extraction_model_tool_applies_without_restart(monkeypatch, zai_chat_config) -> None:
    from totalreclaw.hermes import tools

    # A previous fallback decision must not hide the user's new choice.
    llm_client._mark_unusable(ZAI_CODING_BASE_URL, "glm-4.5-flash", soft=False)

    out = json.loads(await tools.set_extraction_model({"model": "glm-4.5-flash"}, state=None))
    assert out["ok"] is True
    assert out["setting"] == "glm-4.5-flash"
    assert out["effective_model"] == "glm-4.5-flash"
    assert out["source"] == "setting"
    assert "glm-4.5-flash" in out["message"] and "restart" in out["message"]

    # The very next extraction uses it.
    router = _Router(lambda url, model: _ok(model))
    response, cfg = await _extract(router, monkeypatch, zai_chat_config)
    assert router.models() == ["glm-4.5-flash"]

    out = json.loads(await tools.set_extraction_model({"model": "default"}, state=None))
    assert out["setting"] is None
    assert out["effective_model"] == "glm-5.3-flash"
    assert out["source"] == "default"


@pytest.mark.asyncio
async def test_set_extraction_model_tool_reports_env_precedence(monkeypatch, zai_chat_config) -> None:
    from totalreclaw.hermes import tools

    monkeypatch.setenv("TOTALRECLAW_EXTRACTION_MODEL", "glm-5.1")
    out = json.loads(await tools.set_extraction_model({"model": "glm-4.5-flash"}, state=None))
    assert out["setting"] == "glm-4.5-flash"
    assert out["effective_model"] == "glm-5.1"
    assert "TOTALRECLAW_EXTRACTION_MODEL" in out["message"]


@pytest.mark.asyncio
async def test_set_extraction_model_tool_rejects_non_string() -> None:
    from totalreclaw.hermes import tools

    out = json.loads(await tools.set_extraction_model({"model": 42}, state=None))
    assert "error" in out
    assert user_settings.get_extraction_model() is None


def test_status_payload_includes_extraction_model(zai_chat_config) -> None:
    from totalreclaw.hermes import tools

    assert tools._extraction_model_info() == {
        "model": "glm-5.3-flash",
        "source": "default",
        "chat_model": "glm-5.1",
        "candidates": ["glm-5.3-flash", "glm-4.5-flash", "glm-5.1"],
        "base_url": ZAI_CODING_BASE_URL,
    }
    assert describe_extraction_model(None)["source"] == "unavailable"


def test_tool_is_registered_and_in_manifest() -> None:
    import yaml
    from totalreclaw.hermes import schemas

    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "totalreclaw" / "hermes" / "plugin.yaml").read_text()
    )
    assert "totalreclaw_set_extraction_model" in manifest["provides_tools"]
    assert schemas.SET_EXTRACTION_MODEL["name"] == "totalreclaw_set_extraction_model"
    assert schemas.SET_EXTRACTION_MODEL["parameters"]["required"] == ["model"]

    import totalreclaw.hermes as hermes_plugin

    registered: list[str] = []

    class _Ctx:
        def register_tool(self, name, **kw):
            registered.append(name)

        def register_hook(self, *a, **kw):
            pass

    hermes_plugin.register(_Ctx())
    assert "totalreclaw_set_extraction_model" in registered


def test_cli_extraction_model_sets_and_prints(monkeypatch, capsys) -> None:
    from totalreclaw import cli

    monkeypatch.setattr(llm_client, "detect_llm_config", lambda: _zai())
    assert cli.main(["extraction-model", "glm-4.5-flash"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["model"] == "glm-4.5-flash" and out["source"] == "setting"
    assert out["setting"] == "glm-4.5-flash"

    assert cli.main(["extraction-model", "default"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["model"] == "glm-5.3-flash" and out["setting"] is None


# ---------------------------------------------------------------------------
# 4. Startup warning + cross-client parity
# ---------------------------------------------------------------------------


def test_extraction_model_env_var_is_not_reported_as_removed() -> None:
    """#609 / #612: the variable works, so the startup warning must not
    call it removed. TOTALRECLAW_LLM_MODEL really is gone."""
    from totalreclaw.agent import state

    assert "TOTALRECLAW_EXTRACTION_MODEL" not in state._REMOVED_ENV_VARS
    assert "TOTALRECLAW_LLM_MODEL" in state._REMOVED_ENV_VARS


def _parse_ts_string_list(src: str) -> list[str]:
    return re.findall(r"'([^']*)'", src)


@pytest.mark.skipif(not TS_LLM_CLIENT.exists(), reason="TS plugin sources not present")
def test_candidate_tables_match_openclaw_plugin() -> None:
    """Same provider + endpoint -> same extraction candidates on OpenClaw
    (``CHEAP_MODEL_CANDIDATES_BY_PROVIDER``) and Hermes."""
    src = TS_LLM_CLIENT.read_text(encoding="utf-8")
    block = re.search(
        r"export const CHEAP_MODEL_CANDIDATES_BY_PROVIDER[^=]*=\s*\{(.*?)\n\};", src, re.S
    )
    assert block, "CHEAP_MODEL_CANDIDATES_BY_PROVIDER not found in llm-client.ts"
    ts_table = {
        m.group(1): tuple(_parse_ts_string_list(m.group(2)))
        for m in re.finditer(r"^\s*([a-z]+):\s*\[([^\]]*)\]", block.group(1), re.M)
    }
    assert ts_table == _EXTRACTION_MODEL_CANDIDATES_BY_PROVIDER

    std = re.search(r"export const ZAI_STANDARD_CHEAP_MODEL_CANDIDATES[^=]*=\s*\[([^\]]*)\]", src)
    assert std, "ZAI_STANDARD_CHEAP_MODEL_CANDIDATES not found in llm-client.ts"
    assert tuple(_parse_ts_string_list(std.group(1))) == _ZAI_STANDARD_EXTRACTION_CANDIDATES
