"""Regression tests for internal#97 — Hermes 0.10.0 config-schema drift.

Hermes 0.10.0 (2026-04-16) left ``~/.hermes/config.yaml`` with empty
top-level ``provider`` + ``model`` keys and moved the active credentials
into ``~/.hermes/auth.json::credential_pool`` + runtime-written env vars
in ``~/.hermes/.env`` (``HERMES_MODEL`` / ``{PROVIDER}_MODEL``). Before
rc.15, :func:`read_hermes_llm_config` only understood the legacy shape
and returned ``None`` every turn, producing the silent extraction
disable reported at
https://github.com/p-diogo/totalreclaw-internal/issues/97.

Shape of these tests:
  - Build a throwaway ``~/.hermes``-like dir via ``tmp_path``.
  - Point TR at it by setting ``HERMES_CONFIG=<tmp_path>/config.yaml``.
  - Pin the exact JSON/YAML/.env shapes we observed on the VPS.
  - Assert TR returns a valid :class:`LLMConfig` (not ``None``).
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from totalreclaw.agent.llm_client import (
    LLMConfig,
    ZAI_CODING_BASE_URL,
    _pick_credential_from_pool,
    _read_hermes_auth_json,
    _resolve_hermes_model_from_runtime_env,
    detect_llm_config,
    read_hermes_llm_config,
    validate_llm_config_at_load,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_hermes_010_fixture(
    root: Path,
    *,
    provider: str = "zai",
    model_env_key: str = "HERMES_MODEL",
    model_value: str = "glm-5-turbo",
    include_zai_base_url_in_env: bool = False,
    auth_base_url: str = "https://api.z.ai/api/coding/paas/v4",
) -> Path:
    """Write a minimal Hermes 0.10.0-shaped ``.hermes`` dir into ``root``.

    Returns the path to ``config.yaml`` so callers can feed it to
    ``HERMES_CONFIG`` for deterministic resolution without depending on
    the real home directory.
    """
    hermes = root / ".hermes"
    hermes.mkdir()
    # 0.10.0-shaped config.yaml: blank model + empty providers block.
    (hermes / "config.yaml").write_text(
        textwrap.dedent("""\
            model: ''
            providers: {}
            fallback_providers: []
        """)
    )
    # auth.json with a single credential in credential_pool.
    (hermes / "auth.json").write_text(
        json.dumps({
            "version": 1,
            "providers": {},
            "credential_pool": {
                provider: [
                    {
                        "id": "abc",
                        "label": f"{provider.upper()}_API_KEY",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": f"env:{provider.upper()}_API_KEY",
                        "access_token": f"sk-fake-{provider}-key",
                        "base_url": auth_base_url,
                    }
                ]
            },
        })
    )
    # .env: runtime-written model var (what Hermes 0.10.0 actually does).
    env_lines = [f"{model_env_key}={model_value}"]
    if include_zai_base_url_in_env:
        env_lines.append("ZAI_BASE_URL=https://api.z.ai/api/paas/v4")
    (hermes / ".env").write_text("\n".join(env_lines) + "\n")
    return hermes / "config.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestReadHermesAuthJson:
    def test_missing_file_returns_none(self, tmp_path):
        assert _read_hermes_auth_json(tmp_path / "auth.json") is None

    def test_unparseable_returns_none(self, tmp_path):
        p = tmp_path / "auth.json"
        p.write_text("not-json")
        assert _read_hermes_auth_json(p) is None

    def test_well_formed_returns_dict(self, tmp_path):
        p = tmp_path / "auth.json"
        p.write_text(json.dumps({"credential_pool": {"zai": []}}))
        result = _read_hermes_auth_json(p)
        assert isinstance(result, dict)
        assert "credential_pool" in result


class TestPickCredentialFromPool:
    def test_empty_pool(self):
        assert _pick_credential_from_pool({}) == ("", "", "")
        assert _pick_credential_from_pool({"credential_pool": {}}) == ("", "", "")

    def test_first_usable_wins(self):
        auth = {
            "credential_pool": {
                "zai": [
                    {"access_token": "key1", "base_url": "https://a"},
                    {"access_token": "key2", "base_url": "https://b"},
                ]
            }
        }
        assert _pick_credential_from_pool(auth) == (
            "zai", "key1", "https://a",
        )

    def test_skips_credential_without_token(self):
        auth = {
            "credential_pool": {
                "zai": [
                    {"access_token": "", "base_url": "https://a"},
                    {"access_token": "keyok", "base_url": "https://b"},
                ]
            }
        }
        assert _pick_credential_from_pool(auth) == ("zai", "keyok", "https://b")

    def test_multiple_providers_ordered(self):
        # Dict insertion order preserved → first provider wins.
        auth = {
            "credential_pool": {
                "openai": [{"access_token": "oai-key", "base_url": ""}],
                "zai": [{"access_token": "zai-key", "base_url": ""}],
            }
        }
        provider, _, _ = _pick_credential_from_pool(auth)
        assert provider == "openai"


class TestResolveHermesModelFromRuntimeEnv:
    def test_provider_specific_env_var_wins(self, monkeypatch):
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("ZAI_MODEL", raising=False)
        assert _resolve_hermes_model_from_runtime_env(
            "zai", {"ZAI_MODEL": "glm-5-turbo"},
        ) == "glm-5-turbo"

    def test_hermes_model_fallback(self, monkeypatch):
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("ZAI_MODEL", raising=False)
        assert _resolve_hermes_model_from_runtime_env(
            "zai", {"HERMES_MODEL": "glm-5-turbo"},
        ) == "glm-5-turbo"

    def test_process_env_fallback(self, monkeypatch):
        monkeypatch.setenv("ZAI_MODEL", "glm-4.5")
        assert _resolve_hermes_model_from_runtime_env("zai", {}) == "glm-4.5"

    def test_env_file_trumps_process_env(self, monkeypatch):
        # ``.env`` from disk (dict passed in) should win over a stale
        # process-env export — a user restarting Hermes with a new model
        # should see the new model, not a shell export from before.
        monkeypatch.setenv("ZAI_MODEL", "glm-old")
        assert _resolve_hermes_model_from_runtime_env(
            "zai", {"ZAI_MODEL": "glm-new"},
        ) == "glm-new"

    def test_unknown_provider_falls_back_to_generic(self, monkeypatch):
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        assert _resolve_hermes_model_from_runtime_env(
            "unknown-provider", {"LLM_MODEL": "gpt-generic"},
        ) == "gpt-generic"


# ---------------------------------------------------------------------------
# Integration: read_hermes_llm_config() with Hermes 0.10.0 layout
# ---------------------------------------------------------------------------


class TestReadHermesLlmConfig010:
    """Pins the regression for internal#97.

    Pre-rc.15 these tests all failed (returned ``None``).
    """

    def test_issue_97_repro_zai_with_hermes_model(self, tmp_path, monkeypatch):
        # Exactly the VPS shape: config.yaml blank, auth.json has one
        # zai credential, .env has HERMES_MODEL.
        cfg_path = _write_hermes_010_fixture(
            tmp_path, provider="zai", model_env_key="HERMES_MODEL",
            model_value="glm-5-turbo",
        )
        monkeypatch.setenv("HERMES_CONFIG", str(cfg_path))
        # Clear any legacy env that might accidentally resolve first.
        for v in ("ZAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "OPENAI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL",
                  "HERMES_MODEL", "ZAI_MODEL", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config = read_hermes_llm_config()
        assert config is not None, "regression: returned None on Hermes 0.10.0 shape"
        assert isinstance(config, LLMConfig)
        assert config.model == "glm-5-turbo"
        assert config.api_key == "sk-fake-zai-key"
        assert config.api_format == "openai"
        # auth.json's base_url should flow through.
        assert config.base_url.rstrip("/") == "https://api.z.ai/api/coding/paas/v4"

    def test_issue_97_provider_specific_model_env(self, tmp_path, monkeypatch):
        cfg_path = _write_hermes_010_fixture(
            tmp_path, provider="zai", model_env_key="ZAI_MODEL",
            model_value="glm-4.5",
        )
        monkeypatch.setenv("HERMES_CONFIG", str(cfg_path))
        for v in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config = read_hermes_llm_config()
        assert config is not None
        assert config.model == "glm-4.5"

    def test_openai_provider_via_auth_json(self, tmp_path, monkeypatch):
        cfg_path = _write_hermes_010_fixture(
            tmp_path, provider="openai", model_env_key="HERMES_MODEL",
            model_value="gpt-5-mini", auth_base_url="https://api.openai.com/v1",
        )
        monkeypatch.setenv("HERMES_CONFIG", str(cfg_path))
        for v in ("OPENAI_MODEL", "OPENAI_API_KEY", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config = read_hermes_llm_config()
        assert config is not None
        assert config.model == "gpt-5-mini"
        assert config.api_format == "openai"
        assert config.base_url == "https://api.openai.com/v1"

    def test_zai_base_url_env_still_wins(self, tmp_path, monkeypatch):
        cfg_path = _write_hermes_010_fixture(
            tmp_path, provider="zai", model_env_key="HERMES_MODEL",
            model_value="glm-5-turbo",
        )
        monkeypatch.setenv("HERMES_CONFIG", str(cfg_path))
        # Explicit env override trumps auth.json's base_url.
        monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
        for v in ("HERMES_MODEL", "ZAI_MODEL", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config = read_hermes_llm_config()
        assert config is not None
        assert config.base_url == "https://api.z.ai/api/paas/v4"

    def test_no_auth_json_returns_none(self, tmp_path, monkeypatch):
        # 0.10.0-shaped config.yaml but no auth.json — simulates a
        # corrupted Hermes dir. Should return None rather than crash.
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text("model: ''\nproviders: {}\n")
        (hermes / ".env").write_text("HERMES_MODEL=glm-5-turbo\n")
        monkeypatch.setenv("HERMES_CONFIG", str(hermes / "config.yaml"))
        # Point HOME at tmp_path so the fallback ~/.hermes candidate
        # doesn't accidentally resolve against the developer's real dir.
        monkeypatch.setenv("HOME", str(tmp_path))
        for v in ("ZAI_API_KEY", "OPENAI_API_KEY", "HERMES_MODEL",
                  "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        assert read_hermes_llm_config() is None

    def test_auth_json_without_model_env_returns_none(self, tmp_path, monkeypatch):
        # auth.json present but no .env / no model var set anywhere.
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text("model: ''\nproviders: {}\n")
        (hermes / "auth.json").write_text(json.dumps({
            "credential_pool": {
                "zai": [{"access_token": "sk-x", "base_url": "https://a"}]
            }
        }))
        monkeypatch.setenv("HERMES_CONFIG", str(hermes / "config.yaml"))
        monkeypatch.setenv("HOME", str(tmp_path))
        for v in ("HERMES_MODEL", "ZAI_MODEL", "OPENAI_MODEL", "LLM_MODEL",
                  "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        assert read_hermes_llm_config() is None

    def test_legacy_yaml_shape_still_works(self, tmp_path, monkeypatch):
        # Back-compat: old-style config.yaml should still resolve without
        # touching auth.json.
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text(
            "provider: zai\nmodel: glm-4.5\n"
        )
        (hermes / ".env").write_text("ZAI_API_KEY=sk-legacy\n")
        monkeypatch.setenv("HERMES_CONFIG", str(hermes / "config.yaml"))
        for v in ("ZAI_API_KEY", "HERMES_MODEL", "ZAI_MODEL", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config = read_hermes_llm_config()
        assert config is not None
        assert config.model == "glm-4.5"
        assert config.api_key == "sk-legacy"


# ---------------------------------------------------------------------------
# Integration: detect_llm_config() env path with provider-specific *_MODEL
# ---------------------------------------------------------------------------


class TestDetectLlmConfigProviderSpecificModel:
    def test_zai_model_without_openai_model_resolves(self, monkeypatch, tmp_path):
        # Only ZAI_MODEL + ZAI_API_KEY in env — pre-rc.15 this would miss
        # because only OPENAI_MODEL / ANTHROPIC_MODEL / LLM_MODEL were
        # consulted for model names.
        for v in ("OPENAI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL",
                  "HERMES_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
                  "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
                  "GEMINI_API_KEY", "GOOGLE_API_KEY", "GLM_API_KEY",
                  "Z_AI_API_KEY", "XDG_CONFIG_HOME", "HERMES_CONFIG"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("ZAI_API_KEY", "sk-zai-env")
        monkeypatch.setenv("ZAI_MODEL", "glm-5-turbo")
        # Neutralize any real ~/.hermes — point HOME at empty tmp_path
        # so the Hermes fallback doesn't accidentally resolve.
        monkeypatch.setenv("HOME", str(tmp_path))

        config = detect_llm_config()
        assert config is not None
        assert config.model == "glm-5-turbo"
        assert config.api_key == "sk-zai-env"


# ---------------------------------------------------------------------------
# Load-time validator
# ---------------------------------------------------------------------------


class TestValidateLlmConfigAtLoad:
    def test_resolution_success_returns_config(self, tmp_path, monkeypatch):
        cfg_path = _write_hermes_010_fixture(
            tmp_path, provider="zai", model_env_key="HERMES_MODEL",
            model_value="glm-5-turbo",
        )
        monkeypatch.setenv("HERMES_CONFIG", str(cfg_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        for v in ("ZAI_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL",
                  "ANTHROPIC_MODEL", "LLM_MODEL", "HERMES_MODEL", "ZAI_MODEL"):
            monkeypatch.delenv(v, raising=False)

        config, reason = validate_llm_config_at_load(context="test")
        assert config is not None
        assert reason is None

    def test_resolution_failure_returns_reason(self, tmp_path, monkeypatch, caplog):
        # Empty ~/.hermes dir → no config, no auth, no env vars.
        monkeypatch.setenv("HOME", str(tmp_path))
        for v in ("ZAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
                  "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
                  "GEMINI_API_KEY", "GOOGLE_API_KEY", "GLM_API_KEY",
                  "Z_AI_API_KEY", "HERMES_MODEL", "OPENAI_MODEL",
                  "ANTHROPIC_MODEL", "LLM_MODEL", "ZAI_MODEL", "GLM_MODEL",
                  "OPENROUTER_MODEL", "GROQ_MODEL", "DEEPSEEK_MODEL",
                  "MISTRAL_MODEL", "XAI_MODEL", "TOGETHER_MODEL",
                  "GEMINI_MODEL", "GOOGLE_MODEL", "HERMES_CONFIG",
                  "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        with caplog.at_level("WARNING", logger="totalreclaw.agent.llm_client"):
            config, reason = validate_llm_config_at_load(context="test-fail")
        assert config is None
        assert reason and isinstance(reason, str)
        # One loud WARNING, not per-turn DEBUG.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("automatic memory extraction is DISABLED" in r.message
                   for r in warnings), "no loud load-time WARNING emitted"

    def test_reason_diagnoses_missing_model_env(self, tmp_path, monkeypatch):
        # auth.json present but no model env → reason should point at
        # the missing HERMES_MODEL / {PROVIDER}_MODEL.
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        (hermes / "config.yaml").write_text("model: ''\nproviders: {}\n")
        (hermes / "auth.json").write_text(json.dumps({
            "credential_pool": {
                "zai": [{"access_token": "sk-x", "base_url": "https://a"}]
            }
        }))
        monkeypatch.setenv("HERMES_CONFIG", str(hermes / "config.yaml"))
        monkeypatch.setenv("HOME", str(tmp_path))
        for v in ("HERMES_MODEL", "ZAI_MODEL", "OPENAI_MODEL", "LLM_MODEL",
                  "XDG_CONFIG_HOME"):
            monkeypatch.delenv(v, raising=False)

        config, reason = validate_llm_config_at_load(context="test-no-model")
        assert config is None
        assert reason is not None
        # Wording is informational — pin the stable-ish substrings.
        assert (
            "model env var" in reason.lower()
            or "config files exist" in reason.lower()
        )
