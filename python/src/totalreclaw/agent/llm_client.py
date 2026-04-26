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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


#: Per-provider canonical model-name env var candidates. Checked in order;
#: first hit wins. ``HERMES_MODEL`` is Hermes 0.10.0's session-level setting
#: that always reflects the active model regardless of provider. Generic
#: fallbacks (``OPENAI_MODEL`` / ``ANTHROPIC_MODEL`` / ``LLM_MODEL``) keep
#: pre-0.10.0 installs working.
_PROVIDER_MODEL_ENV_VARS = {
    "zai": ["HERMES_MODEL", "ZAI_MODEL", "GLM_MODEL"],
    "openai": ["HERMES_MODEL", "OPENAI_MODEL"],
    "anthropic": ["HERMES_MODEL", "ANTHROPIC_MODEL"],
    "openrouter": ["HERMES_MODEL", "OPENROUTER_MODEL"],
    "groq": ["HERMES_MODEL", "GROQ_MODEL"],
    "deepseek": ["HERMES_MODEL", "DEEPSEEK_MODEL"],
    "mistral": ["HERMES_MODEL", "MISTRAL_MODEL"],
    "gemini": ["HERMES_MODEL", "GEMINI_MODEL", "GOOGLE_MODEL"],
    "xai": ["HERMES_MODEL", "XAI_MODEL"],
    "together": ["HERMES_MODEL", "TOGETHER_MODEL"],
}


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    api_format: str  # "openai" or "anthropic"


# zai exposes TWO public endpoints:
#   - CODING: backs GLM Coding Plan subscriptions (default here).
#   - STANDARD: backs PAYG balances.
# A coding-plan key hitting STANDARD (or vice-versa) returns HTTP 429 with
# body "Insufficient balance or no resource package. Please recharge." —
# misleading because the subscription is in good standing. The rc.3
# auto-fallback in :func:`chat_completion` flips between these two when it
# detects that error signature.
ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_STANDARD_BASE_URL = "https://api.z.ai/api/paas/v4"


# ---------------------------------------------------------------------------
# rc.24 F2 — same-provider cheap-model selection for auto-extraction.
#
# Pedro's design directive (2026-04-26):
#   - Reuse the SAME provider, endpoint, and API key that's already
#     powering the agent's main chat. NEVER introduce a second provider
#     just for extraction (no Anthropic-Haiku-as-fallback shape). NEVER
#     re-route the endpoint based on model name (the user's API key is
#     scoped to ONE endpoint; flipping would 401).
#   - WITHIN that provider+endpoint, pick a cheaper / faster MODEL.
#   - Mirror the OpenClaw rc.22 plugin's auth-profiles UX:
#     ``CHEAP_MODEL_BY_PROVIDER`` table + word-boundary cheap-indicator
#     pattern (so chat model names like ``gemini-2.5-pro`` aren't
#     mis-detected as "already cheap" because they contain ``mini``).
#
# Reference impl (canonical, byte-aligned where possible):
#   ``skill/plugin/llm-client.ts::CHEAP_MODEL_BY_PROVIDER`` +
#   ``deriveCheapModel`` + ``CHEAP_INDICATORS`` / ``CHEAP_INDICATOR_RE``.
#   Pedro's directive 2026-04-26: "OpenClaw rc.22 already SOLVED this
#   exact problem; mirror that pattern instead of inventing".
#
# This replaces the rc.23 status-quo of "extraction uses the user's
# chat model verbatim" which (a) charged GPT-4 prices for what the
# extraction prompt only needs at ~7B-class quality, and (b) hit the
# F2 silent-empty-content bug when GLM-5.1-on-Coding was the chat
# model (GLM-5.1 isn't supported on the Coding endpoint; the API
# returns HTTP 200 with empty body).
#
# Override
#   ``TOTALRECLAW_EXTRACTION_MODEL`` env var pins the extraction model
#   verbatim. Operator escape hatch.
# ---------------------------------------------------------------------------


#: Cheap-tier indicator tokens. Matched at hyphen / underscore / dot
#: boundaries (see :data:`_CHEAP_INDICATOR_RE`) so chat model names
#: like ``gemini-2.5-pro`` aren't mis-detected as "cheap" because they
#: contain ``mini``. Mirrors ``skill/plugin/llm-client.ts::CHEAP_INDICATORS``
#: for parity with the OpenClaw plugin.
_CHEAP_INDICATORS: tuple[str, ...] = (
    "flash", "mini", "nano", "haiku", "small", "lite", "fast",
)


_CHEAP_INDICATOR_RE = re.compile(
    r"(?:^|[-_/.])(?:" + "|".join(_CHEAP_INDICATORS) + r")(?:[-_/.]|$)",
    re.IGNORECASE,
)


#: Per-provider default extraction model. Maps the user-configured
#: provider name (lower-case) to the cheap / fast sibling used for
#: auto-extraction.
#:
#: Pinned to match ``skill/plugin/llm-client.ts::CHEAP_MODEL_BY_PROVIDER``
#: byte-for-byte (where the same provider exists on both clients) so
#: that the same Z.AI Coding-plan user sees the SAME extraction model
#: across OpenClaw and Hermes — Pedro's "one provider, both clients"
#: invariant.
#:
#: zai: ``glm-4.5-flash`` is the proven-on-Coding-plan workhorse per
#: prior QA cycles. Available on BOTH Coding and Standard endpoints.
#:
#: anthropic: ``claude-haiku-4-5-20251001`` — date-pinned to match the
#: TS plugin (3.3.1 lifted the alias to the dated identifier per
#: provider's deprecation policy).
#:
#: openai: ``gpt-4.1-mini`` — TS plugin's canonical pick. Cheap, fast,
#: same JSON-output quality as gpt-4 for our extraction prompt.
#:
#: gemini / google: ``gemini-flash-lite`` — Google's cheapest tier.
#: Both keys present so legacy ``provider: google`` configs resolve.
#:
#: groq: ``llama-3.3-70b-versatile`` — Groq's fastest extraction-tier.
#:
#: openrouter: routes through Anthropic Haiku (same UX as anthropic).
_DEFAULT_EXTRACTION_MODEL_BY_PROVIDER: dict[str, str] = {
    "zai": "glm-4.5-flash",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-flash-lite",
    "google": "gemini-flash-lite",
    "mistral": "mistral-small-latest",
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "openrouter": "anthropic/claude-haiku-4-5-20251001",
    "xai": "grok-2",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "cerebras": "llama3.3-70b",
}


def is_cheap_model(model: str) -> bool:
    """Word-boundary check: does the model name signal cheap tier?

    Used by :func:`cheap_extraction_model_for` to short-circuit when
    the user already pinned a cheap model for chat (e.g. they
    configured ``glm-4.5-flash`` or ``gpt-4.1-mini`` directly).

    Word-boundary so ``gemini-2.5-pro`` is NOT mis-detected as cheap
    just because it contains ``mini``. Mirrors the rc.22 fix that
    landed in the OpenClaw plugin.
    """
    if not model:
        return False
    return _CHEAP_INDICATOR_RE.search(model) is not None


def cheap_extraction_model_for(provider: str, chat_model: str) -> str:
    """Return the same-provider cheap model to use for auto-extraction.

    Mirrors ``skill/plugin/llm-client.ts::deriveCheapModel`` —
    Pedro's "use the OpenClaw rc.22 pattern" directive 2026-04-26.

    Resolution order:
      1. ``TOTALRECLAW_EXTRACTION_MODEL`` env var (operator override).
      2. If ``chat_model`` already matches the cheap-indicator pattern
         (``flash``, ``mini``, ``haiku``, etc. at a word boundary),
         use it as-is — no redundant swap.
      3. Per-provider default from
         :data:`_DEFAULT_EXTRACTION_MODEL_BY_PROVIDER`.
      4. Fallback to the chat model when no cheap mapping exists for
         the provider — preserves rc.23 behaviour for unknown providers
         (better to over-pay for extraction than silently disable it).

    Same-provider rule: the returned model is meant to swap into the
    user's existing :class:`LLMConfig` with NO change to ``api_key``,
    ``base_url``, or ``api_format``.
    """
    override = os.environ.get("TOTALRECLAW_EXTRACTION_MODEL", "").strip()
    if override:
        return override
    if is_cheap_model(chat_model):
        return chat_model
    provider_lower = (provider or "").strip().lower()
    cheap = _DEFAULT_EXTRACTION_MODEL_BY_PROVIDER.get(provider_lower)
    if cheap:
        return cheap
    # Unknown provider — use whatever the user had configured. Better
    # to over-pay for extraction than to silently disable it.
    return chat_model


def derive_extraction_config(
    chat_config: LLMConfig, *, provider_hint: Optional[str] = None
) -> LLMConfig:
    """Build the extraction-side :class:`LLMConfig` from a chat config.

    Reuses the chat config's API key + auth shape verbatim and swaps
    the model for the same-provider cheap pick. For zai, also
    re-resolves the base URL based on the EXTRACTION model's family
    so a GLM-5.x chat config doesn't try to issue extraction calls
    against the Standard endpoint when the cheap model lives on
    Coding (or vice-versa).

    ``provider_hint`` is required when the chat config doesn't carry
    a provider name directly (LLMConfig only has base_url + api_format).
    Callers that resolved provider during config detection (e.g.
    ``read_hermes_llm_config``) can pass it through; otherwise we
    infer from base_url substring.
    """
    provider = (provider_hint or _infer_provider_from_base_url(chat_config.base_url)).lower()
    cheap_model = cheap_extraction_model_for(provider, chat_config.model)

    # Same provider, same key, same api_format, SAME ENDPOINT.
    # Pedro 2026-04-26: extraction stays on the SAME endpoint the user
    # configured for chat. The cheap pick is verified to be available
    # on whichever endpoint the user runs (GLM-4.5-flash works on both
    # zai Coding + Standard; gpt-4o-mini works on the OpenAI default
    # endpoint; etc.). Re-resolving the endpoint based on model family
    # would (a) drift away from the user's pinned ``ZAI_BASE_URL`` /
    # ``OPENAI_BASE_URL`` override, and (b) introduce a per-call
    # endpoint flip the user didn't ask for.
    base_url = chat_config.base_url

    if cheap_model != chat_config.model:
        logger.info(
            "TotalReclaw extraction LLM auto-selected: provider=%s "
            "chat_model=%s -> extraction_model=%s (base_url=%s)",
            provider, chat_config.model, cheap_model, base_url,
        )

    return LLMConfig(
        api_key=chat_config.api_key,
        base_url=base_url,
        model=cheap_model,
        api_format=chat_config.api_format,
    )


def _infer_provider_from_base_url(base_url: str) -> str:
    """Best-effort provider name from base URL.

    Used when ``derive_extraction_config`` doesn't get an explicit
    ``provider_hint``. Substring match against known provider hosts;
    falls back to "" (which makes ``cheap_extraction_model_for``
    return the user's chat model unchanged).
    """
    if not base_url:
        return ""
    lower = base_url.lower()
    if "z.ai" in lower:
        return "zai"
    if "api.openai.com" in lower:
        return "openai"
    if "api.anthropic.com" in lower:
        return "anthropic"
    if "groq.com" in lower:
        return "groq"
    if "deepseek.com" in lower:
        return "deepseek"
    if "openrouter.ai" in lower:
        return "openrouter"
    if "googleapis.com" in lower or "generativelanguage" in lower:
        return "gemini"
    if "mistral.ai" in lower:
        return "mistral"
    if "x.ai" in lower:
        return "xai"
    if "together.xyz" in lower:
        return "together"
    return ""


def get_zai_base_url() -> str:
    """Resolve the zai base URL for CHAT (user-facing) calls.

    Precedence:
      1. ``ZAI_BASE_URL`` env var (explicit operator override)
      2. Default: coding endpoint (coding-plan-biased; the rc.3
         auto-fallback hops to the standard endpoint on an
         "Insufficient balance" 429).

    Documented in Hermes SKILL.md — GLM Coding Plan users can leave
    unset; PAYG users SHOULD set ``ZAI_BASE_URL=https://api.z.ai/api/paas/v4``
    to avoid the fallback round-trip on every first call.

    Note: extraction-side endpoint resolution is handled by
    :func:`derive_extraction_config`, which picks the same endpoint
    as the user's chat config (per Pedro 2026-04-26 — extraction
    reuses the user's provider+endpoint, only the model swaps to the
    cheap pick). This helper is for chat-side resolution only.
    """
    raw = os.environ.get("ZAI_BASE_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    return ZAI_CODING_BASE_URL


# Provider detection: (provider, env_vars, default_base_url, api_format)
# No hardcoded model names — uses whatever the user configured via their
# agent framework. The TOTALRECLAW_EXTRACTION_MODEL / TOTALRECLAW_LLM_MODEL
# user-facing override was removed in the v1 env cleanup. Model selection
# goes through agent-framework config + `OPENAI_MODEL` / `ANTHROPIC_MODEL`.
#
# NOTE: zai's base URL is looked up lazily via :func:`get_zai_base_url` at
# config-resolution time so ``ZAI_BASE_URL`` propagates without a module
# re-import. This table is kept for the non-zai providers.
PROVIDERS = [
    ("zai", ["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"], ZAI_CODING_BASE_URL, "openai"),
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
    "zai": (["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"], ZAI_CODING_BASE_URL),
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


def is_zai_balance_error(message: str) -> bool:
    """Detect the zai "Insufficient balance" error signature (case-insensitive)."""
    if not message:
        return False
    m = message.lower()
    return "insufficient balance" in m or "no resource package" in m


def zai_fallback_base_url(current_base_url: str) -> Optional[str]:
    """Pick the OTHER zai endpoint when the current one returns a balance error.

    Returns ``None`` when ``current_base_url`` is neither of the known zai
    endpoints — the caller should then skip the fallback branch and fall
    back on normal retries.
    """
    if not current_base_url:
        return None
    normalized = current_base_url.rstrip("/")
    if normalized == ZAI_CODING_BASE_URL:
        return ZAI_STANDARD_BASE_URL
    if normalized == ZAI_STANDARD_BASE_URL:
        return ZAI_CODING_BASE_URL
    return None


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


def _read_hermes_auth_json(auth_path: Path) -> Optional[dict]:
    """Parse ``~/.hermes/auth.json`` into a dict, or return ``None``.

    Hermes 0.10.0 moved active-provider credentials out of ``config.yaml``
    and into ``auth.json::credential_pool``. We read the whole JSON and
    let the caller pick what it needs; on any parse / IO error we return
    ``None`` and the caller falls through to the next resolution path.
    """
    if not auth_path.exists():
        return None
    try:
        return json.loads(auth_path.read_text())
    except (OSError, ValueError) as e:
        logger.debug("_read_hermes_auth_json: %s unparseable (%s)", auth_path, e)
        return None


def _pick_credential_from_pool(
    auth: dict,
) -> tuple[str, str, str]:
    """Pick the first usable (provider, api_key, base_url) triple from
    ``auth.json::credential_pool``.

    Hermes 0.10.0 shape::

        {
          "credential_pool": {
            "zai": [{"access_token": "...", "base_url": "...", ...}, ...],
            ...
          }
        }

    We walk providers in the order they appear in the dict (Python 3.7+
    preserves insertion order → matches the order Hermes registered them).
    Within each provider, the first credential with an ``access_token``
    wins. Returns ``("", "", "")`` when nothing usable is found.
    """
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict):
        return "", "", ""
    for provider, creds in pool.items():
        if not isinstance(creds, list):
            continue
        for cred in creds:
            if not isinstance(cred, dict):
                continue
            token = cred.get("access_token")
            if not isinstance(token, str) or not token:
                continue
            base_url = cred.get("base_url") if isinstance(cred.get("base_url"), str) else ""
            return str(provider), token, base_url
    return "", "", ""


def _resolve_hermes_model_from_runtime_env(
    provider_lower: str, env_vars: dict[str, str]
) -> str:
    """Resolve the active model name from Hermes runtime env vars.

    Checks the provider-specific candidates from
    :data:`_PROVIDER_MODEL_ENV_VARS` (e.g. ``ZAI_MODEL`` for zai) plus the
    generic ``HERMES_MODEL`` fallback. ``env_vars`` is the parsed
    ``~/.hermes/.env`` dict; it wins over process env so a stale
    ``OPENAI_MODEL`` export from an earlier shell can't shadow the
    current Hermes session model.
    """
    candidates = _PROVIDER_MODEL_ENV_VARS.get(
        provider_lower,
        # Unknown provider — fall back to generic vars only.
        ["HERMES_MODEL", "LLM_MODEL"],
    )
    for name in candidates:
        val = env_vars.get(name) or os.environ.get(name)
        if val and val.strip():
            return val.strip()
    # Final generic fallback in case the provider table is out of date.
    for name in ("HERMES_MODEL", "LLM_MODEL"):
        val = env_vars.get(name) or os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return ""


def _read_hermes_llm_config_from_auth_json(
    hermes_dir: Path,
) -> Optional[LLMConfig]:
    """Build an :class:`LLMConfig` from ``hermes_dir/auth.json`` + ``.env``.

    This is the Hermes 0.10.0 resolution path. ``auth.json`` supplies
    provider + API key + base_url via ``credential_pool``; the active
    model is read from the runtime-env vars Hermes writes to ``.env``
    (``HERMES_MODEL`` / ``{PROVIDER}_MODEL``). Returns ``None`` on any
    missing piece — callers fall through to the next resolver.
    """
    auth = _read_hermes_auth_json(hermes_dir / "auth.json")
    if auth is None:
        return None
    provider, api_key, auth_base_url = _pick_credential_from_pool(auth)
    if not provider or not api_key:
        logger.debug(
            "_read_hermes_llm_config_from_auth_json: no usable credential in %s",
            hermes_dir / "auth.json",
        )
        return None

    provider_lower = provider.lower()
    env_vars = _read_hermes_env_file(hermes_dir / ".env")
    model = _resolve_hermes_model_from_runtime_env(provider_lower, env_vars)
    if not model:
        logger.debug(
            "_read_hermes_llm_config_from_auth_json: no model env var set "
            "for provider %r (tried %s)",
            provider,
            ", ".join(
                _PROVIDER_MODEL_ENV_VARS.get(
                    provider_lower, ["HERMES_MODEL", "LLM_MODEL"]
                )
            ),
        )
        return None

    # base_url precedence:
    #   1. Explicit ZAI_BASE_URL override (matches the legacy path).
    #   2. auth.json credential base_url (Hermes's own chosen endpoint).
    #   3. Default for this provider.
    if provider_lower == "zai":
        zai_override = env_vars.get("ZAI_BASE_URL") or os.environ.get("ZAI_BASE_URL")
        if zai_override and zai_override.strip():
            base_url = zai_override.strip().rstrip("/")
        elif auth_base_url:
            base_url = auth_base_url.rstrip("/")
        else:
            base_url = get_zai_base_url()
    else:
        _, default_base_url = _HERMES_PROVIDER_KEY_MAP.get(provider_lower, ([], ""))
        base_url = (
            (auth_base_url.rstrip("/") if auth_base_url else "")
            or env_vars.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or default_base_url
        )

    api_format = "anthropic" if provider_lower == "anthropic" else "openai"
    logger.info(
        "TotalReclaw LLM config resolved from Hermes auth.json + .env "
        "(provider=%s, model=%s, base_url=%s)",
        provider, model, base_url,
    )
    return LLMConfig(
        api_key=api_key, base_url=base_url, model=model, api_format=api_format
    )


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

    Hermes 0.10.0 (2026-04-16) moved the active provider + API key out
    of ``config.yaml`` into ``~/.hermes/auth.json::credential_pool`` and
    the active model into runtime-set env vars (``HERMES_MODEL`` plus
    provider-specific ``{PROVIDER}_MODEL``). After the legacy YAML path
    fails, we fall through to an ``auth.json``-based resolver so 0.10.0
    installs work without a TR-side config change. Fix for internal#97.

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
            # Legacy YAML shape empty — try the Hermes 0.10.0 auth.json
            # path in the same directory before moving to the next YAML
            # candidate. The YAML file still serves as the "this is a
            # Hermes install" marker so we don't read auth.json from an
            # unrelated directory.
            hermes_010 = _read_hermes_llm_config_from_auth_json(config_path.parent)
            if hermes_010 is not None:
                return hermes_010
            logger.debug(
                "read_hermes_llm_config: %s missing provider/model keys "
                "(and auth.json fallback unavailable)",
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

        # For zai, ``ZAI_BASE_URL`` (env or Hermes .env) wins over the
        # hardcoded coding-endpoint default so users on the PAYG tier can
        # point at the STANDARD endpoint without the auto-fallback tax.
        if provider_lower == "zai":
            zai_override = env_vars.get("ZAI_BASE_URL") or os.environ.get("ZAI_BASE_URL")
            if zai_override and zai_override.strip():
                base_url = zai_override.strip().rstrip("/")
            else:
                base_url = get_zai_base_url()
        else:
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
    # Generic model-env vars, used as the last-resort fallback below when
    # no provider-specific ``{PROVIDER}_MODEL`` / ``HERMES_MODEL`` is set.
    env_model_generic = (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("HERMES_MODEL")
    )

    for _provider, env_vars, default_base_url, api_format in PROVIDERS:
        for env_var in env_vars:
            api_key = os.environ.get(env_var)
            if api_key:
                # Provider-specific model vars (e.g. ``ZAI_MODEL``,
                # ``GROQ_MODEL``) take precedence over the generic ones so
                # Hermes 0.10.0 users with a single ``ZAI_MODEL=...`` in
                # ``.env`` resolve here without needing an extra
                # ``OPENAI_MODEL`` export. Fix for internal#97.
                env_model = _resolve_hermes_model_from_runtime_env(_provider, {}) or env_model_generic
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
                elif _provider == "zai":
                    # Respect ZAI_BASE_URL env override (rc.3). Coding-plan
                    # users can leave unset; PAYG users set it explicitly
                    # to https://api.z.ai/api/paas/v4.
                    resolved_base_url = get_zai_base_url()
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


def validate_llm_config_at_load(
    context: str = "plugin-load",
) -> tuple[Optional[LLMConfig], Optional[str]]:
    """Resolve LLM config once at plugin load and loudly WARN if missing.

    Prior to internal#97 (rc.15), TR's auto-extraction silently logged
    one DEBUG line per turn when :func:`detect_llm_config` returned
    ``None`` — users saw 27+ identical log lines for a 27-turn session
    with zero memories written, and no hint as to why. Per Pedro's
    directive ("fail loud"), this validator runs once at plugin
    registration so operators see a single, actionable WARNING at
    startup instead of a per-turn stream.

    Returns ``(config, None)`` on success, ``(None, reason)`` on
    failure. The reason string is a short diagnostic intended for
    inclusion in the WARNING body and for surfacing via the
    quota-warning channel on first turn.
    """
    config = detect_llm_config()
    if config is not None:
        logger.info(
            "TotalReclaw [%s]: LLM config OK (provider_base=%s, model=%s).",
            context, config.base_url, config.model,
        )
        return config, None

    # Build an actionable reason by re-probing the two resolution paths.
    hermes_dir = Path.home() / ".hermes"
    auth_exists = (hermes_dir / "auth.json").exists()
    env_exists = (hermes_dir / ".env").exists()
    yaml_exists = (hermes_dir / "config.yaml").exists()
    has_any_api_key = any(
        os.environ.get(v) for v in (
            "ZAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
            "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY",
        )
    )
    has_any_model = any(
        os.environ.get(v) for v in (
            "HERMES_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL",
            "ZAI_MODEL", "GLM_MODEL", "OPENROUTER_MODEL", "GROQ_MODEL",
            "DEEPSEEK_MODEL", "MISTRAL_MODEL", "XAI_MODEL", "TOGETHER_MODEL",
            "GEMINI_MODEL", "GOOGLE_MODEL",
        )
    )
    if not (yaml_exists or auth_exists):
        reason = (
            "no Hermes install detected at ~/.hermes (and no "
            "{PROVIDER}_API_KEY + {PROVIDER}_MODEL in env)"
        )
    elif auth_exists and not has_any_model:
        reason = (
            "Hermes auth.json found but no model env var set — Hermes "
            "0.10.0 writes HERMES_MODEL / {PROVIDER}_MODEL to "
            "~/.hermes/.env at session start. Is the session running?"
        )
    elif not has_any_api_key and not auth_exists:
        reason = (
            "no provider API key found — set {PROVIDER}_API_KEY in env "
            "or ~/.hermes/.env (e.g. ZAI_API_KEY, OPENAI_API_KEY)"
        )
    else:
        reason = (
            "config files exist but resolution failed — enable DEBUG "
            "logging on 'totalreclaw.agent.llm_client' for details"
        )

    logger.warning(
        "TotalReclaw [%s]: automatic memory extraction is DISABLED — %s. "
        "Extraction will be skipped until this is fixed; explicit "
        "totalreclaw_remember / totalreclaw_recall still work. Files checked: "
        "~/.hermes/config.yaml=%s, ~/.hermes/auth.json=%s, ~/.hermes/.env=%s.",
        context, reason,
        "present" if yaml_exists else "missing",
        "present" if auth_exists else "missing",
        "present" if env_exists else "missing",
    )
    return None, reason


# Retry/backoff settings for LLM calls — rc.3 lifts budget to 5 attempts
# with 2→4→8→16→32s backoff (total ~62s). Configurable via
# ``TOTALRECLAW_LLM_RETRY_BUDGET_MS`` env var (accepts ms for parity with
# the TypeScript plugin). 120s timeout per attempt stays.
_MAX_RETRIES = 5
_BACKOFF_DELAYS = [2.0, 4.0, 8.0, 16.0, 32.0]  # seconds between retries
_LLM_TIMEOUT = 120.0  # seconds (extraction prompts with long conversation text need this)


def _default_retry_budget_s() -> float:
    """Budget in seconds. Read lazily so tests can monkey-patch env.

    The lower bound is 1ms to allow tests to exercise the budget-short-circuit
    path quickly; in production the caller picks something in the 10s+ range
    via the env var (default 60s).
    """
    raw = os.environ.get("TOTALRECLAW_LLM_RETRY_BUDGET_MS", "").strip()
    if raw:
        try:
            return max(0.001, int(raw) / 1000.0)
        except ValueError:
            pass
    return 60.0


class LLMUpstreamOutageError(RuntimeError):
    """Raised by :func:`chat_completion` when the extraction LLM upstream is
    unreachable after the full retry budget is exhausted.

    The extraction pipeline recognises this via ``except
    LLMUpstreamOutageError`` and can choose to queue the message batch for
    retry next turn, surface a one-time notification, or skip silently.
    Historically :func:`chat_completion` returned ``None`` on retry
    exhaustion; that conflated "transient outage" with "parseable empty
    response", so callers could not distinguish. The structured error
    preserves that information.

    Attributes
    ----------
    attempts : int
        Number of attempts made before giving up (1-based).
    last_status : Optional[int]
        HTTP status code from the last attempt, if the last error was an
        HTTP error. ``None`` for timeouts / connection failures.
    """

    def __init__(self, message: str, attempts: int, last_status: Optional[int] = None):
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status


async def chat_completion(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> Optional[str]:
    """Call LLM chat completion with retry/backoff.

    rc.3 — retry budget is 5 attempts with 2→4→8→16→32s backoff
    (total ~62s). Configurable via ``TOTALRECLAW_LLM_RETRY_BUDGET_MS``.
    On retry exhaustion raises :class:`LLMUpstreamOutageError` instead of
    returning ``None`` so the extraction pipeline can differentiate from
    a parseable-but-empty response.

    zai-specific: when a 429 response carries the "Insufficient balance"
    signature AND the current base URL is one of the two known zai
    endpoints, flips to the other endpoint and retries ONCE (separate
    from the normal retry budget). Logs the flip at INFO.

    Returns ``None`` only when the underlying API response parsed
    successfully but contained no text content (empty choice, etc.).

    Raises
    ------
    LLMUpstreamOutageError
        After all retries are exhausted on retryable errors (429 / timeout).
    """
    # Make a mutable copy so the zai fallback branch can swap base_url
    # without mutating the caller's dataclass.
    active = LLMConfig(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        api_format=config.api_format,
    )
    budget_s = _default_retry_budget_s()
    cumulative_delay_s = 0.0
    zai_fallback_attempted = False
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None

    attempt = 0
    while attempt < _MAX_RETRIES:
        attempt += 1
        try:
            if active.api_format == "anthropic":
                return await _call_anthropic(active, system_prompt, user_prompt, max_tokens, temperature)
            else:
                # rc.24 F2: empty-content responses are surfaced with a
                # WARN inside ``_call_openai`` (not silent INFO). The
                # extraction-side fix is :func:`derive_extraction_config`
                # — it picks a same-provider cheap model proven to work
                # on the user's existing endpoint, so the empty-content
                # symptom is rare to begin with. We deliberately do NOT
                # auto-flip the zai endpoint on empty content because
                # the user's chosen endpoint reflects their plan + the
                # cheap-pick is endpoint-compatible by construction.
                return await _call_openai(active, system_prompt, user_prompt, max_tokens, temperature)
        except httpx.HTTPStatusError as e:
            last_exc = e
            last_status = e.response.status_code
            err_body = ""
            try:
                err_body = e.response.text or ""
            except Exception:
                err_body = ""
            err_str = f"{e.response.status_code}: {err_body}"

            # ── zai "Insufficient balance" auto-fallback ──
            if (
                not zai_fallback_attempted
                and e.response.status_code == 429
                and is_zai_balance_error(err_body)
            ):
                fallback = zai_fallback_base_url(active.base_url)
                if fallback:
                    zai_fallback_attempted = True
                    old_url = active.base_url
                    active.base_url = fallback
                    logger.info(
                        "zai endpoint auto-fallback: %s → %s due to "
                        '"Insufficient balance" response',
                        old_url, fallback,
                    )
                    # Retry immediately — this flip is "free" and does not
                    # consume the normal retry budget.
                    attempt -= 1
                    continue

            is_retryable = e.response.status_code in (429, 502, 503, 504)
            if not is_retryable:
                # Non-retryable HTTP error (400/401/403/404/etc.) — propagate
                # as-is so callers can distinguish config errors from outages.
                logger.warning(
                    "LLM call failed (non-retryable HTTP %d): %s",
                    e.response.status_code, repr(e),
                )
                raise

            # Retryable. Either schedule a backoff or surface the outage if
            # we've exhausted attempts / the retry budget.
            if attempt >= _MAX_RETRIES:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage (exhausted {_MAX_RETRIES} attempts): {err_str[:200]}",
                    attempts=attempt,
                    last_status=last_status,
                ) from e

            delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
            if cumulative_delay_s + delay > budget_s:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage (budget {budget_s:.0f}s exhausted after {attempt} attempts): {err_str[:200]}",
                    attempts=attempt,
                    last_status=last_status,
                ) from e
            cumulative_delay_s += delay
            # Only log the FIRST retry at WARNING; subsequent retries at
            # DEBUG to avoid spamming long outages.
            if attempt == 1:
                logger.warning(
                    "LLM call failed (attempt %d/%d, retrying in %.0fs): %s",
                    attempt, _MAX_RETRIES, delay, repr(e),
                )
            else:
                logger.debug(
                    "LLM retry (attempt %d/%d, wait %.0fs): %s",
                    attempt, _MAX_RETRIES, delay, repr(e),
                )
            await _asyncio.sleep(delay)
            continue
        except httpx.TimeoutException as e:
            last_exc = e
            if attempt >= _MAX_RETRIES:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage (timeouts on {_MAX_RETRIES} attempts)",
                    attempts=attempt,
                    last_status=None,
                ) from e

            delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
            if cumulative_delay_s + delay > budget_s:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage (budget {budget_s:.0f}s exhausted after {attempt} attempts): timeout",
                    attempts=attempt,
                    last_status=None,
                ) from e
            cumulative_delay_s += delay
            if attempt == 1:
                logger.warning(
                    "LLM call timeout (attempt %d/%d, retrying in %.0fs)",
                    attempt, _MAX_RETRIES, delay,
                )
            else:
                logger.debug(
                    "LLM retry after timeout (attempt %d/%d, wait %.0fs)",
                    attempt, _MAX_RETRIES, delay,
                )
            await _asyncio.sleep(delay)
            continue
        except Exception as e:
            logger.warning("LLM call failed: %s", repr(e))
            return None

    # Fell through the loop with a retryable error on the final attempt —
    # surface structured outage.
    raise LLMUpstreamOutageError(
        f"LLM upstream outage (exhausted {_MAX_RETRIES} attempts): {repr(last_exc)}",
        attempts=_MAX_RETRIES,
        last_status=last_status,
    )


#: Providers that return a base_url containing one of these substrings
#: are known to honour ``response_format: {"type": "json_object"}`` and
#: BENEFIT from it for structured-output prompts (extraction, compaction,
#: comparative re-rank). Z.AI / GLM in particular is observed to return
#: empty content on the merged-extraction prompt without it (F2 in QA
#: report ``QA-hermes-RC-2.3.1-rc.23-20260426.md``). OpenAI also supports
#: it but the prompt already guides JSON output reliably; sending the hint
#: doesn't hurt.
#:
#: Match logic is a substring check on ``LLMConfig.base_url`` rather than
#: a provider-name table because ``base_url`` is the only field we have
#: at call time once the config is built.
_JSON_OBJECT_PROVIDER_HINTS: tuple[str, ...] = (
    "z.ai",                  # both /coding/paas/v4 and /paas/v4 endpoints
    "api.openai.com",
    "groq.com",
    "openrouter.ai",
    "deepseek.com",
    "mistral.ai",
    "x.ai",
    "together.xyz",
)


def _supports_json_object_response_format(base_url: str) -> bool:
    """Return True if this provider should be sent
    ``response_format: {"type": "json_object"}`` on extraction calls.

    Conservative match: substring match on the base URL. Adding new
    providers is safe — incorrect inclusion only makes the API ignore
    the field on providers that don't support it (OpenAI-spec-compatible
    providers either honour the field or drop it silently).
    """
    if not base_url:
        return False
    lower = base_url.lower()
    return any(hint in lower for hint in _JSON_OBJECT_PROVIDER_HINTS)


async def _call_openai(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    """OpenAI-spec chat-completions call.

    F2 fix (rc.24, ref ``QA-hermes-RC-2.3.1-rc.23-20260426.md``):

    1. For known structured-output-aware providers (zai/GLM, OpenAI,
       Groq, DeepSeek, OpenRouter, Mistral, x.ai, Together) we now send
       ``response_format: {"type": "json_object"}``. GLM-5.1 in
       particular returned EMPTY content for the merged-extraction
       prompt without this hint — no error, just an empty
       ``message.content`` — which broke auto-extraction silently in
       rc.23 QA. Sending the hint flips it to deterministic JSON
       output. Providers that ignore the field are unaffected.

    2. When the response parses successfully but ``message.content`` is
       empty / missing, we now WARN with the request payload size,
       model, and the OpenAI ``finish_reason`` so an empty extraction
       call is loud at the source. Previously this surfaced as the
       single line ``extract_facts_llm: chat_completion returned
       None/empty`` at INFO level, which gave operators no way to
       tell whether the model returned junk, hit a content filter, or
       OOM'd a context window.
    """
    body: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if _supports_json_object_response_format(config.base_url):
        # Hint to the provider that we want valid JSON. Combined with
        # the system prompt's "Return JSON ..." instruction, this is
        # what flips zai/GLM from empty-content responses to structured
        # output. See F2 in the rc.23 QA report.
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        resp = await client.post(
            f"{config.base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = choice.get("message", {}).get("content")
        if not content:
            # F2 — empty responses must be loud, not silent. Prior to
            # rc.24 this was logged as a generic "returned None/empty"
            # downstream in extraction.py at INFO. Surface the actual
            # response shape (finish_reason, usage) at WARN here so the
            # operator can correlate empty extraction batches with the
            # provider response without enabling DEBUG.
            logger.warning(
                "LLM returned empty content (provider=%s, model=%s, "
                "system_chars=%d, user_chars=%d, finish_reason=%s, usage=%s, "
                "json_response_format=%s)",
                config.base_url,
                config.model,
                len(system_prompt or ""),
                len(user_prompt or ""),
                choice.get("finish_reason"),
                data.get("usage"),
                "response_format" in body,
            )
        return content


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
