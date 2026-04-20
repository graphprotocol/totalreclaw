"""
LLM client for TotalReclaw fact extraction.

Auto-detects the user's LLM provider from environment variables
AND — since 2.2.2 — from ``~/.hermes/config.yaml`` + ``~/.hermes/.env``
so Hermes plugin users don't have to duplicate their model choice as
an ``OPENAI_MODEL`` env var (Bug #4, QA 2026-04-20).

Supports OpenAI-compatible APIs and Anthropic Messages API. Uses a
cheap/fast model for extraction to minimize cost.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    api_format: str  # "openai" or "anthropic"


# Provider detection: (provider, env_vars, default_base_url, api_format)
# No hardcoded model names — uses whatever the user configured via their
# agent framework. The TOTALRECLAW_EXTRACTION_MODEL / TOTALRECLAW_LLM_MODEL
# user-facing override was removed in the v1 env cleanup. Model selection
# goes through agent-framework config + `OPENAI_MODEL` / `ANTHROPIC_MODEL`.
PROVIDERS = [
    ("zai", ["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"], "https://api.z.ai/api/coding/paas/v4", "openai"),
    ("anthropic", ["ANTHROPIC_API_KEY"], "https://api.anthropic.com/v1", "anthropic"),
    ("openai", ["OPENAI_API_KEY"], "https://api.openai.com/v1", "openai"),
    ("groq", ["GROQ_API_KEY"], "https://api.groq.com/openai/v1", "openai"),
    ("deepseek", ["DEEPSEEK_API_KEY"], "https://api.deepseek.com/v1", "openai"),
    ("openrouter", ["OPENROUTER_API_KEY"], "https://openrouter.ai/api/v1", "openai"),
    ("gemini", ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "https://generativelanguage.googleapis.com/v1beta/openai", "openai"),
    ("mistral", ["MISTRAL_API_KEY"], "https://api.mistral.ai/v1", "openai"),
    ("xai", ["XAI_API_KEY"], "https://api.x.ai/v1", "openai"),
    ("together", ["TOGETHER_API_KEY"], "https://api.together.xyz/v1", "openai"),
]


# Map Hermes provider names to (API key candidates, default base URL).
# Kept aligned with ``PROVIDERS`` above — if you add a provider there,
# add it here too.
_HERMES_PROVIDER_KEY_MAP = {
    "zai": (["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"], "https://api.z.ai/api/coding/paas/v4"),
    "openai": (["OPENAI_API_KEY"], "https://api.openai.com/v1"),
    "anthropic": (["ANTHROPIC_API_KEY"], "https://api.anthropic.com/v1"),
    "openrouter": (["OPENROUTER_API_KEY"], "https://openrouter.ai/api/v1"),
    "groq": (["GROQ_API_KEY"], "https://api.groq.com/openai/v1"),
    "deepseek": (["DEEPSEEK_API_KEY"], "https://api.deepseek.com/v1"),
    "mistral": (["MISTRAL_API_KEY"], "https://api.mistral.ai/v1"),
    "gemini": (["GEMINI_API_KEY", "GOOGLE_API_KEY"], "https://generativelanguage.googleapis.com/v1beta/openai"),
    "xai": (["XAI_API_KEY"], "https://api.x.ai/v1"),
    "together": (["TOGETHER_API_KEY"], "https://api.together.xyz/v1"),
}


def _candidate_hermes_config_paths() -> list[Path]:
    """Return the ordered candidate paths for the Hermes config file.

    ``$HERMES_CONFIG`` (full file path) wins, then the XDG location,
    then the legacy ``~/.hermes/`` dir. We return all that exist and
    let the caller try each in order — the first parseable one with a
    model + provider wins.
    """
    paths: list[Path] = []
    env_override = os.environ.get("HERMES_CONFIG")
    if env_override:
        paths.append(Path(env_override).expanduser())

    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        paths.append(Path(xdg_home).expanduser() / "hermes" / "config.yaml")
    paths.append(Path.home() / ".config" / "hermes" / "config.yaml")
    paths.append(Path.home() / ".hermes" / "config.yaml")
    return paths


def _read_hermes_env_file(env_path: Path) -> dict[str, str]:
    """Parse a ``~/.hermes/.env`` style file into a dict.

    Minimal KEY=VALUE parser — does not handle quoting or multi-line
    values. Good enough for the Hermes-shaped ``.env`` file.
    """
    env_vars: dict[str, str] = {}
    if not env_path.exists():
        return env_vars
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            # Strip surrounding quotes if present.
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            env_vars[k.strip()] = v
    except OSError:
        return {}
    return env_vars


def _extract_provider_and_model(cfg: dict) -> tuple[str, str]:
    """Accept BOTH top-level and nested shapes.

    Hermes writes ``provider: zai`` + ``model: glm-5-turbo`` as top-level
    keys in ``~/.hermes/config.yaml``. Prior to 2.2.2 this helper only
    read the nested ``model: { provider, model }`` shape, so every Hermes
    user had to keep an ``OPENAI_MODEL`` env var around — see the QA
    report at ``docs/notes/QA-hermes-RC-2.2.1-20260420.md`` Finding #4.
    Accepting both shapes lets users configure it once in ``config.yaml``.
    """
    # Try top-level first (the actual Hermes shape).
    provider = cfg.get("provider") if isinstance(cfg.get("provider"), str) else ""
    model = cfg.get("model") if isinstance(cfg.get("model"), str) else ""
    if provider and model:
        return provider, model

    # Fall back to nested ``model: { provider, model }`` (defensive —
    # future-proofs against Hermes reorganizing its schema).
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider_nested = model_cfg.get("provider", "") if isinstance(model_cfg.get("provider"), str) else ""
    model_nested = model_cfg.get("model", "") if isinstance(model_cfg.get("model"), str) else ""
    if provider_nested and model_nested:
        return provider_nested, model_nested

    return "", ""


def read_hermes_llm_config() -> Optional[LLMConfig]:
    """Read the LLM provider config from Hermes's own config files.

    Hermes stores its config in ``~/.hermes/config.yaml`` (provider +
    model) and ``~/.hermes/.env`` (API keys). This reads both to build an
    LLM config that matches what Hermes itself uses — no separate env
    vars needed.

    Resolution order:
      1. ``$HERMES_CONFIG`` (full file path)
      2. ``$XDG_CONFIG_HOME/hermes/config.yaml``
      3. ``~/.config/hermes/config.yaml``
      4. ``~/.hermes/config.yaml`` (legacy)

    Accepts the YAML shape BOTH as top-level
    ``{provider: zai, model: glm-5-turbo}`` AND nested
    ``{model: {provider: zai, model: glm-5-turbo}}``.

    Returns ``None`` if no config is found, if it can't be parsed, or
    if the required provider/model fields are missing. Logs a WARNING
    at the resolution point so operators can tell WHERE the model came
    from (or why it failed).
    """
    try:
        import yaml
    except ImportError:
        # PyYAML is a dep via setuptools; if it's not installed we simply
        # cannot read YAML. Don't blow up — just skip Hermes detection.
        logger.debug("read_hermes_llm_config: PyYAML not installed; skipping")
        return None

    for config_path in _candidate_hermes_config_paths():
        if not config_path.exists():
            continue

        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.debug(
                "read_hermes_llm_config: %s unparseable (%s); trying next candidate",
                config_path, e,
            )
            continue

        if not isinstance(cfg, dict):
            continue

        provider, model = _extract_provider_and_model(cfg)
        if not provider or not model:
            logger.debug(
                "read_hermes_llm_config: %s missing provider/model keys",
                config_path,
            )
            continue

        provider_lower = provider.lower()
        key_names, default_base_url = _HERMES_PROVIDER_KEY_MAP.get(provider_lower, ([], ""))
        if not key_names:
            logger.debug(
                "read_hermes_llm_config: unknown provider %r in %s",
                provider, config_path,
            )
            continue

        # Read adjacent .env for API keys. Candidate .env paths live in
        # the same directory as the config (so ~/.hermes/.env pairs
        # with ~/.hermes/config.yaml, etc.).
        env_vars = _read_hermes_env_file(config_path.parent / ".env")

        api_key: Optional[str] = None
        for kn in key_names:
            api_key = env_vars.get(kn) or os.environ.get(kn)
            if api_key:
                break

        if not api_key:
            logger.debug(
                "read_hermes_llm_config: no API key found for provider %r "
                "in %s or env (tried %s)",
                provider, config_path.parent / ".env", ", ".join(key_names),
            )
            continue

        base_url = (
            env_vars.get("GLM_BASE_URL")
            or env_vars.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or default_base_url
        )
        api_format = "anthropic" if provider_lower == "anthropic" else "openai"

        logger.info(
            "TotalReclaw LLM config resolved from Hermes config: %s (provider=%s, model=%s)",
            config_path, provider, model,
        )
        return LLMConfig(api_key=api_key, base_url=base_url, model=model, api_format=api_format)

    return None


def detect_llm_config(configured_model: Optional[str] = None) -> Optional[LLMConfig]:
    """Auto-detect LLM provider and model from environment variables
    and (for Hermes users) from ``~/.hermes/config.yaml`` + ``.env``.

    Uses the agent's configured model by default. No hardcoded model lists
    to maintain — just uses whatever the user set up.

    Resolution order (first hit wins):
      1. ``configured_model`` + env-var API key (e.g. plugin-passed model).
      2. ``OPENAI_MODEL`` / ``ANTHROPIC_MODEL`` / ``LLM_MODEL`` env vars
         paired with any matching provider API key.
      3. ``~/.hermes/config.yaml`` + ``~/.hermes/.env`` (Bug #4 fix —
         Hermes users don't need to duplicate the model as an env var).

    The ``TOTALRECLAW_EXTRACTION_MODEL`` / ``TOTALRECLAW_LLM_MODEL`` overrides
    were removed in the v1 env cleanup — agent-framework config is now the
    single source of truth for the extraction model.

    Base URL priority:
      1. OPENAI_BASE_URL (for OpenAI-compatible custom providers)
      2. Provider default
    """
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    # Common env vars for configured model name
    env_model = (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("LLM_MODEL")
    )

    for _provider, env_vars, default_base_url, api_format in PROVIDERS:
        for env_var in env_vars:
            api_key = os.environ.get(env_var)
            if api_key:
                model = configured_model or env_model
                if not model:
                    # No model configured via env — skip this provider so
                    # we fall through to the Hermes-config fallback below.
                    # Previously logged a WARNING per provider, which was
                    # noisy when the user had keys for several providers
                    # but hadn't set OPENAI_MODEL. Debug-log instead.
                    logger.debug(
                        "detect_llm_config: %s API key found but no model "
                        "configured via env; continuing",
                        _provider,
                    )
                    continue

                # For OpenAI provider, respect OPENAI_BASE_URL
                if _provider == "openai" and openai_base_url:
                    resolved_base_url = openai_base_url.rstrip("/")
                else:
                    resolved_base_url = default_base_url

                logger.info(
                    "TotalReclaw LLM config resolved from env vars "
                    "(provider=%s, model=%s)",
                    _provider, model,
                )
                return LLMConfig(
                    api_key=api_key,
                    base_url=resolved_base_url,
                    model=model,
                    api_format=api_format,
                )

    # Last resort: try the Hermes config.yaml + .env pair. This is the
    # path that was broken until 2.2.2 — Hermes users with a valid
    # config.yaml but no OPENAI_MODEL env var ended up here with None
    # and saw silent extraction failures (QA Finding #4, 2026-04-20).
    hermes_config = read_hermes_llm_config()
    if hermes_config is not None:
        return hermes_config

    logger.debug("detect_llm_config: no LLM config resolved from env or Hermes")
    return None


# Retry/backoff settings for LLM calls
_MAX_RETRIES = 3
_BACKOFF_DELAYS = [5.0, 10.0, 20.0]  # seconds between retries
_LLM_TIMEOUT = 120.0  # seconds (extraction prompts with long conversation text need this)


async def chat_completion(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> Optional[str]:
    """Call LLM chat completion with retry/backoff. Returns assistant response text or None."""
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            if config.api_format == "anthropic":
                return await _call_anthropic(config, system_prompt, user_prompt, max_tokens, temperature)
            else:
                return await _call_openai(config, system_prompt, user_prompt, max_tokens, temperature)
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            last_exc = e
            # Retry on timeout or 429 rate limit
            is_rate_limit = isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429
            is_timeout = isinstance(e, httpx.TimeoutException)
            if (is_rate_limit or is_timeout) and attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_DELAYS[attempt] if attempt < len(_BACKOFF_DELAYS) else _BACKOFF_DELAYS[-1]
                logger.warning(
                    "LLM call failed (attempt %d/%d, retrying in %.0fs): %s",
                    attempt + 1, _MAX_RETRIES, delay, repr(e),
                )
                await _asyncio.sleep(delay)
                continue
            logger.warning("LLM call failed (attempt %d/%d, no more retries): %s", attempt + 1, _MAX_RETRIES, repr(e))
            return None
        except Exception as e:
            logger.warning("LLM call failed: %s", repr(e))
            return None

    logger.warning("LLM call exhausted all retries: %s", repr(last_exc))
    return None


async def _call_openai(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{config.base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
            json={
                "model": config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_completion_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")


async def _call_anthropic(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{config.base_url}/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": config.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block.get("text")
        return None
