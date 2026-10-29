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
import math
import os
import re
import time as _time
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
# 2.5.3rc4: each provider has an ORDERED candidate list (endpoint-aware
# for z.ai) instead of one model, and a candidate that proves unusable at
# runtime is skipped in favour of the next one, ending at the chat model.
# Users never have to pick a model.
#
# Overrides (see :func:`extraction_model_plan`)
#   ``TOTALRECLAW_EXTRACTION_MODEL`` env var (operator), then the user's
#   ``extraction_model`` setting in ``~/.totalreclaw/settings.json``, which
#   the agent sets via the ``totalreclaw_set_extraction_model`` tool when
#   the user asks for a specific model. No restart needed for the setting.
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


#: Ordered extraction-model candidates per provider (lower-case name).
#: The first entry is the default; later entries are tried, in order, only
#: when an earlier one turns out to be unusable for this user (see
#: :func:`complete_with_extraction_fallback`). The user's chat model is
#: always appended as the final fallback, so these lists only hold models
#: we know exist on the provider's default endpoint today.
#:
#: Must match ``skill/plugin/llm/llm-client.ts::CHEAP_MODEL_CANDIDATES_BY_PROVIDER``
#: entry-for-entry (tests/test_extraction_model_defaults.py checks it) so
#: the same z.ai Coding-plan user gets the SAME extraction model on
#: OpenClaw and Hermes: Pedro's "one provider, both clients" invariant.
#:
#: zai: ``glm-5.3-flash`` is the better extractor (internal benchmark,
#: 2026-10) and is included in the Coding plan; ``glm-4.5-flash`` is the
#: older workhorse. Standard (PAYG) endpoint: see
#: :data:`_ZAI_STANDARD_EXTRACTION_CANDIDATES`.
#:
#: anthropic: ``claude-haiku-4-5-20251001`` (date-pinned, as in the TS
#: plugin since 3.3.1).
#:
#: gemini / google: ``gemini-2.5-flash-lite``; the previous value
#: ``gemini-flash-lite`` is not a Gemini model id. ``gemini-flash-lite-latest``
#: (Google's alias for the newest Flash-Lite) is the fallback.
#:
#: openrouter: OpenRouter's slug for Haiku 4.5 is ``anthropic/claude-haiku-4.5``;
#: the dated Anthropic id stays as the second try.
_EXTRACTION_MODEL_CANDIDATES_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "zai": ("glm-5.3-flash", "glm-4.5-flash"),
    "anthropic": ("claude-haiku-4-5-20251001",),
    "openai": ("gpt-4.1-mini",),
    "gemini": ("gemini-2.5-flash-lite", "gemini-flash-lite-latest"),
    "google": ("gemini-2.5-flash-lite", "gemini-flash-lite-latest"),
    "mistral": ("mistral-small-latest",),
    "groq": ("llama-3.3-70b-versatile",),
    "deepseek": ("deepseek-chat",),
    "openrouter": ("anthropic/claude-haiku-4.5", "anthropic/claude-haiku-4-5-20251001"),
    "xai": ("grok-2",),
    "together": ("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
    "cerebras": ("llama3.3-70b",),
}

#: z.ai Standard (PAYG) endpoint candidates. Measured 2026-10-29 with a
#: real key: ``glm-5.3-flash`` is paid there (HTTP 429 / code 1113
#: "Insufficient balance" without a balance) while ``glm-4.5-flash`` is
#: free, so PAYG users are not silently billed for extraction.
_ZAI_STANDARD_EXTRACTION_CANDIDATES: tuple[str, ...] = ("glm-4.5-flash",)

#: First candidate per provider. Kept for callers and tests that want a
#: single default; mirrors the TS plugin's ``CHEAP_MODEL_BY_PROVIDER``.
_DEFAULT_EXTRACTION_MODEL_BY_PROVIDER: dict[str, str] = {
    provider: candidates[0]
    for provider, candidates in _EXTRACTION_MODEL_CANDIDATES_BY_PROVIDER.items()
}


def default_extraction_candidates(provider: str, base_url: str = "") -> tuple[str, ...]:
    """Built-in candidates for ``provider`` on ``base_url`` (``()`` if none).

    Only z.ai is endpoint-dependent today. An unknown or empty base URL
    gets the provider's default list (for z.ai that is Coding, which is
    also :func:`get_zai_base_url`'s default).
    """
    provider_lower = (provider or "").strip().lower()
    if provider_lower == "zai" and (base_url or "").rstrip("/") == ZAI_STANDARD_BASE_URL:
        return _ZAI_STANDARD_EXTRACTION_CANDIDATES
    return _EXTRACTION_MODEL_CANDIDATES_BY_PROVIDER.get(provider_lower, ())


def is_cheap_model(model: str) -> bool:
    """Word-boundary check: does the model name signal cheap tier?

    Used by :func:`extraction_model_plan` to short-circuit when the user
    already chats on a cheap model (e.g. ``glm-4.5-flash`` or
    ``gpt-4.1-mini``).

    Word-boundary so ``gemini-2.5-pro`` is NOT mis-detected as cheap
    just because it contains ``mini``. Mirrors the rc.22 fix that
    landed in the OpenClaw plugin.
    """
    if not model:
        return False
    return _CHEAP_INDICATOR_RE.search(model) is not None


@dataclass(frozen=True)
class ExtractionModelPlan:
    """Ordered extraction models to try, and why the first one was chosen.

    ``candidates`` always ends with the user's chat model (deduplicated),
    so the last resort is the model the user already runs. ``source`` is
    one of ``env`` / ``setting`` / ``chat-model-cheap`` / ``default`` /
    ``chat-model``; surfaced by ``totalreclaw_status``.
    """

    candidates: tuple[str, ...]
    source: str


def _dedupe(models: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for m in models:
        if m and m not in out:
            out.append(m)
    return tuple(out)


def extraction_model_plan(
    provider: str, chat_model: str, base_url: str = ""
) -> ExtractionModelPlan:
    """Resolve the ordered extraction-model candidates.

    Precedence (first hit wins):
      1. ``TOTALRECLAW_EXTRACTION_MODEL`` env var (operator override).
      2. The user's setting in ``~/.totalreclaw/settings.json`` (written by
         the ``totalreclaw_set_extraction_model`` tool / ``totalreclaw
         extraction-model`` CLI). Read on every call, so a change applies
         on the next extraction without a restart.
      3. The chat model itself when its name already signals a cheap tier
         (``flash``, ``mini``, ``haiku``... at a word boundary).
      4. The built-in per-provider, per-endpoint candidates
         (:func:`default_extraction_candidates`).
      5. The chat model, for providers with no built-in default.

    Every plan ends with the chat model, so an unusable pick degrades to
    the model the user already runs rather than to no extraction.

    Same-provider rule: every candidate swaps into the user's existing
    :class:`LLMConfig` with NO change to ``api_key``, ``base_url``, or
    ``api_format``.
    """
    override = os.environ.get("TOTALRECLAW_EXTRACTION_MODEL", "").strip()
    if override:
        return ExtractionModelPlan(_dedupe([override, chat_model]), "env")
    try:
        from totalreclaw.user_settings import get_extraction_model

        setting = get_extraction_model()
    except Exception:  # never let a settings problem break extraction
        setting = None
    if setting:
        return ExtractionModelPlan(_dedupe([setting, chat_model]), "setting")
    if is_cheap_model(chat_model):
        return ExtractionModelPlan(_dedupe([chat_model]), "chat-model-cheap")
    defaults = default_extraction_candidates(provider, base_url)
    if defaults:
        return ExtractionModelPlan(_dedupe([*defaults, chat_model]), "default")
    # Unknown provider: use whatever the user configured. Better to
    # over-pay for extraction than to silently disable it.
    return ExtractionModelPlan(_dedupe([chat_model]), "chat-model")


def cheap_extraction_model_for(provider: str, chat_model: str, base_url: str = "") -> str:
    """Return the first extraction candidate (see :func:`extraction_model_plan`).

    Kept for existing callers; it does not consult the runtime fallback
    state. Use :func:`resolve_extraction_model` for the model that will
    actually be used next.
    """
    return extraction_model_plan(provider, chat_model, base_url).candidates[0]


# ---------------------------------------------------------------------------
# Runtime fallback state (2.5.3rc4).
#
# When an extraction candidate proves unusable for this user (model not
# found, not permitted, no balance, persistently empty), it is skipped for
# a while so later turns go straight to the next candidate instead of
# re-probing. Process-local on purpose: a restart (new key, new plan)
# re-probes once, which costs one failed round trip.
# ---------------------------------------------------------------------------

#: (base_url, model) -> monotonic time until which the model is skipped.
#: ``math.inf`` for a definitive "this model does not exist / is not
#: allowed"; a cooldown for signals that could be temporary.
_UNUSABLE_EXTRACTION_MODELS: dict[tuple[str, str], float] = {}

#: (base_url, model) -> consecutive empty responses.
_EMPTY_STREAKS: dict[tuple[str, str], int] = {}

#: Consecutive empty responses after which a candidate is skipped.
_EMPTY_STREAK_LIMIT = 2

#: Last (base_url, model) announced in the ``extraction_model=`` INFO line,
#: so the gateway log shows each change once instead of every turn.
_LAST_ANNOUNCED_EXTRACTION_MODEL: list[tuple[str, str]] = []

#: How long a soft-failed candidate (1113 / empty) is skipped. z.ai also
#: reports Coding-plan rate limits with code 1113, so it must not be a
#: permanent downgrade; after this the preferred model is tried again.
_SOFT_FALLBACK_COOLDOWN_S = 30 * 60.0


def reset_extraction_fallback_state() -> None:
    """Forget every runtime fallback decision (used when the user changes
    the extraction model, and by tests)."""
    _UNUSABLE_EXTRACTION_MODELS.clear()
    _EMPTY_STREAKS.clear()
    _LAST_ANNOUNCED_EXTRACTION_MODEL.clear()


def _fallback_key(base_url: str, model: str) -> tuple[str, str]:
    return ((base_url or "").rstrip("/"), model)


def _is_skipped(base_url: str, model: str) -> bool:
    until = _UNUSABLE_EXTRACTION_MODELS.get(_fallback_key(base_url, model))
    if until is None:
        return False
    if until > _time.monotonic():
        return True
    del _UNUSABLE_EXTRACTION_MODELS[_fallback_key(base_url, model)]
    return False


def _mark_unusable(base_url: str, model: str, *, soft: bool) -> None:
    _UNUSABLE_EXTRACTION_MODELS[_fallback_key(base_url, model)] = (
        _time.monotonic() + _SOFT_FALLBACK_COOLDOWN_S if soft else math.inf
    )


def usable_extraction_candidates(
    plan: ExtractionModelPlan, base_url: str
) -> tuple[str, ...]:
    """``plan.candidates`` minus the ones currently skipped. The last
    candidate (the chat model) is never skipped."""
    *head, last = plan.candidates
    return tuple(m for m in head if not _is_skipped(base_url, m)) + (last,)


def resolve_extraction_model(
    chat_config: LLMConfig, *, provider_hint: Optional[str] = None
) -> tuple[str, str]:
    """``(model, source)`` that the next extraction will use.

    ``source`` is the plan source, or ``fallback`` when runtime fallback
    has moved past the plan's first choice.
    """
    provider = (provider_hint or _infer_provider_from_base_url(chat_config.base_url)).lower()
    plan = extraction_model_plan(provider, chat_config.model, chat_config.base_url)
    model = usable_extraction_candidates(plan, chat_config.base_url)[0]
    return model, (plan.source if model == plan.candidates[0] else "fallback")


def derive_extraction_config(
    chat_config: LLMConfig, *, provider_hint: Optional[str] = None
) -> LLMConfig:
    """Build the extraction-side :class:`LLMConfig` from a chat config.

    Reuses the chat config's API key, endpoint and auth shape verbatim and
    swaps the model for the next usable extraction candidate
    (:func:`resolve_extraction_model`).

    ``provider_hint`` is required when the chat config doesn't carry
    a provider name directly (LLMConfig only has base_url + api_format).
    Callers that resolved provider during config detection (e.g.
    ``read_hermes_llm_config``) can pass it through; otherwise we
    infer from base_url substring.
    """
    model, _source = resolve_extraction_model(chat_config, provider_hint=provider_hint)
    # Same provider, same key, same api_format, SAME ENDPOINT.
    # Pedro 2026-04-26: extraction stays on the SAME endpoint the user
    # configured for chat. The candidates are chosen per endpoint
    # (:func:`default_extraction_candidates`); re-resolving the endpoint
    # from the model family would drift from the user's pinned
    # ``ZAI_BASE_URL`` / ``OPENAI_BASE_URL`` and their key's scope.
    return LLMConfig(
        api_key=chat_config.api_key,
        base_url=chat_config.base_url,
        model=model,
        api_format=chat_config.api_format,
    )


def describe_extraction_model(
    chat_config: Optional[LLMConfig], *, provider_hint: Optional[str] = None
) -> dict[str, Any]:
    """JSON-safe summary of the extraction model for status surfaces.

    ``{"model", "source", "chat_model", "candidates"}``; ``source`` is
    ``env`` / ``setting`` / ``chat-model-cheap`` / ``default`` /
    ``chat-model`` / ``fallback``, or ``unavailable`` when no chat config
    resolves (extraction is disabled). Never includes the API key.
    """
    if chat_config is None:
        return {"model": None, "source": "unavailable", "chat_model": None, "candidates": []}
    provider = (provider_hint or _infer_provider_from_base_url(chat_config.base_url)).lower()
    plan = extraction_model_plan(provider, chat_config.model, chat_config.base_url)
    model, source = resolve_extraction_model(chat_config, provider_hint=provider_hint)
    return {
        "model": model,
        "source": source,
        "chat_model": chat_config.model,
        "candidates": list(plan.candidates),
    }


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
# agent framework. This resolves the CHAT config; the extraction model is
# derived from it by :func:`extraction_model_plan`. (TOTALRECLAW_LLM_MODEL
# was removed in the v1 env cleanup.)
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
    #
    # Hermes 0.10.0+ writes the active model name under ``model.default``
    # (``model: { default: glm-5-turbo, provider: zai }``) rather than
    # ``model.model`` — see internal#395. Read ``model`` first for
    # back-compat, then fall back to ``default``.
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider_nested = model_cfg.get("provider", "") if isinstance(model_cfg.get("provider"), str) else ""
    model_nested = model_cfg.get("model", "") if isinstance(model_cfg.get("model"), str) else ""
    if not model_nested:
        model_nested = model_cfg.get("default", "") if isinstance(model_cfg.get("default"), str) else ""
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

    This is the CHAT config. The extraction model is derived from it by
    :func:`extraction_model_plan` (which honours
    ``TOTALRECLAW_EXTRACTION_MODEL`` and the user's setting).

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


# Hard ceiling on a server-suggested Retry-After delay. zai / OpenAI / etc.
# occasionally return Retry-After values in the minutes (e.g. 300s for a
# daily-quota wall) — we don't want a single 429 to block a session for
# 5 minutes. If the suggested delay exceeds this, surface an outage so the
# caller can fall back to the heuristic extraction path immediately.
_RETRY_AFTER_CEILING_S = 60.0


def _parse_retry_after(response: "httpx.Response") -> Optional[float]:
    """Parse RFC 7231 ``Retry-After`` from an httpx response.

    Accepts the two standard formats: delta-seconds (e.g. ``"5"``) and an
    HTTP-date (e.g. ``"Fri, 30 Apr 2026 18:30:00 GMT"``). Returns the
    delay in seconds (always non-negative), or ``None`` when the header
    is missing, malformed, or specifies a past timestamp.

    Headers come from the upstream verbatim, so a buggy provider could
    send anything; the parser swallows ValueError and returns ``None``
    rather than raising — the caller falls back to the static backoff.
    """
    if response is None:
        return None
    raw = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    try:
        secs = float(raw)
        return max(0.0, secs)
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime
        import datetime as _dt
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.timezone.utc)
        delta = (target - _dt.datetime.now(tz=_dt.timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


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


class LLMBalanceExhaustedError(RuntimeError):
    """Raised by :func:`chat_completion` with ``fail_fast_on_balance=True``
    when BOTH z.ai endpoints answer 429 / "Insufficient balance" (1113) for
    the model.

    For an extraction candidate that is not the last resort, this usually
    means the model is not included in the user's plan (e.g. glm-5.3-flash
    on a PAYG key with no balance), so the caller moves to the next
    candidate instead of spending the retry budget. z.ai also reports
    Coding-plan rate limits with 1113, which is why the caller treats it
    as a temporary skip, not a permanent one.
    """


async def chat_completion(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.0,
    *,
    fail_fast_on_balance: bool = False,
) -> Optional[str]:
    """Call LLM chat completion with retry/backoff.

    rc.3 — retry budget is 5 attempts with 2→4→8→16→32s backoff
    (total ~62s). Configurable via ``TOTALRECLAW_LLM_RETRY_BUDGET_MS``.
    On retry exhaustion raises :class:`LLMUpstreamOutageError` instead of
    returning ``None`` so the extraction pipeline can differentiate from
    a parseable-but-empty response.

    On a 429, the response's ``Retry-After`` header (delta-seconds OR
    HTTP-date) is honored when it exceeds the static backoff for the
    current attempt — capped at ``_RETRY_AFTER_CEILING_S``. A suggested
    delay above the ceiling is treated as an outage and surfaces
    ``LLMUpstreamOutageError`` immediately so the caller can fall back
    to the heuristic extraction path instead of pinning the session.

    zai-specific: when a 429 response carries the "Insufficient balance"
    signature AND the current base URL is one of the two known zai
    endpoints, flips to the other endpoint and retries ONCE (separate
    from the normal retry budget). Logs the flip at INFO.

    With ``fail_fast_on_balance=True`` (extraction candidates that have a
    fallback), "Insufficient balance" on both endpoints raises
    :class:`LLMBalanceExhaustedError` at once instead of backing off.

    Returns ``None`` only when the underlying API response parsed
    successfully but contained no text content (empty choice, etc.).

    Raises
    ------
    LLMUpstreamOutageError
        After all retries are exhausted on retryable errors (429 / timeout),
        or when a 429 ``Retry-After`` exceeds ``_RETRY_AFTER_CEILING_S``.
    LLMBalanceExhaustedError
        Only with ``fail_fast_on_balance=True``; see above.
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
            elif (
                zai_fallback_attempted
                and e.response.status_code == 429
                and is_zai_balance_error(err_body)
                and active.base_url != config.base_url
            ):
                if fail_fast_on_balance:
                    raise LLMBalanceExhaustedError(
                        f"z.ai reports insufficient balance for {active.model} "
                        "on both endpoints"
                    ) from e
                # The other endpoint has no balance either, so the first
                # "Insufficient balance" was the Coding plan's rate limit, which
                # z.ai reports with the same 1113 body. Go back to the user's
                # endpoint and back off like any other 429.
                logger.info(
                    "zai endpoint %s also reports no balance; returning to %s and backing off",
                    active.base_url, config.base_url,
                )
                active.base_url = config.base_url

            is_retryable = e.response.status_code in (429, 502, 503, 504)
            if not is_retryable:
                if _is_zai_base_url(active.base_url) and '"1301"' in err_body:
                    # z.ai's content filter refused the input or output. It is
                    # deterministic for this text, so retrying cannot help;
                    # say so plainly instead of a bare "HTTP 400".
                    logger.warning(
                        "z.ai content filter refused the request (code 1301, model=%s); "
                        "nothing is extracted from this window",
                        active.model,
                    )
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
            # Honor server-suggested Retry-After on 429s when it's longer
            # than our static backoff. Most providers (zai, OpenAI, Anthropic,
            # OpenRouter) return Retry-After on rate-limit responses; ignoring
            # it means we hammer the wall and burn the retry budget on
            # guaranteed-to-fail attempts. Capped at _RETRY_AFTER_CEILING_S
            # so a daily-quota response (300s+) doesn't pin the session.
            if e.response.status_code == 429:
                retry_after = _parse_retry_after(e.response)
                if retry_after is not None and retry_after > delay:
                    if retry_after > _RETRY_AFTER_CEILING_S:
                        raise LLMUpstreamOutageError(
                            f"LLM upstream outage (Retry-After={retry_after:.0f}s exceeds ceiling {_RETRY_AFTER_CEILING_S:.0f}s): {err_str[:200]}",
                            attempts=attempt,
                            last_status=last_status,
                        ) from e
                    delay = retry_after
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
        except httpx.TransportError as e:
            # Timeouts and other transport failures (dropped connection, DNS,
            # protocol error) are transient: retry them with backoff. Before
            # this, everything but a timeout fell to the catch-all below and
            # the extraction was dropped as an empty answer with no retry.
            last_exc = e
            kind = "timeout" if isinstance(e, httpx.TimeoutException) else type(e).__name__
            if attempt >= _MAX_RETRIES:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage ({'timeouts' if kind == 'timeout' else kind} on {_MAX_RETRIES} attempts)",
                    attempts=attempt,
                    last_status=None,
                ) from e

            delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
            if cumulative_delay_s + delay > budget_s:
                raise LLMUpstreamOutageError(
                    f"LLM upstream outage (budget {budget_s:.0f}s exhausted after {attempt} attempts): {kind}",
                    attempts=attempt,
                    last_status=None,
                ) from e
            cumulative_delay_s += delay
            if attempt == 1:
                logger.warning(
                    "LLM call %s (attempt %d/%d, retrying in %.0fs)",
                    kind, attempt, _MAX_RETRIES, delay,
                )
            else:
                logger.debug(
                    "LLM retry after %s (attempt %d/%d, wait %.0fs)",
                    kind, attempt, _MAX_RETRIES, delay,
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


#: Error-body fragments meaning "this MODEL is not available to this key"
#: (OpenAI ``model_not_found``, "does not exist" / "unknown model" wording
#: across OpenAI-compatible providers, z.ai codes 1211 model does not
#: exist / 1220 no permission for the model / 1221 model taken offline).
#: Deliberately narrow: a malformed request, the content filter (1301) or
#: a quota error must not demote a model that works.
_MODEL_UNUSABLE_BODY_RE = re.compile(
    r"model_not_found|model not found|does not exist|unknown model|invalid model"
    r"|no such model|not a valid model|unsupported model|not have access"
    r'|"1211"|"1220"|"1221"',
    re.IGNORECASE,
)


def classify_model_unusable(status: int, body: str) -> Optional[str]:
    """Short reason when an HTTP error says the extraction MODEL is unusable
    (missing, not permitted), or ``None`` for errors that say nothing about
    the model (bad request, rate limit, outage). Rate limits and 5xx never
    reach here: :func:`chat_completion` retries them and raises
    :class:`LLMUpstreamOutageError`, which is never a reason to downgrade.
    """
    if status == 404:
        return "model not found (HTTP 404)"
    if status in (401, 403):
        return f"model not permitted (HTTP {status})"
    if status in (400, 422) and _MODEL_UNUSABLE_BODY_RE.search(body or ""):
        return f"model rejected (HTTP {status})"
    return None


def _log_extraction_fallback(model: str, next_model: str, base_url: str, reason: str) -> None:
    logger.info(
        "TotalReclaw extraction model %s unusable on %s (%s); falling back to %s",
        model, base_url, reason, next_model,
    )


async def complete_with_extraction_fallback(
    chat_config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    provider_hint: Optional[str] = None,
    completion: Any = None,
) -> tuple[Optional[str], LLMConfig]:
    """Run an extraction completion, walking the extraction candidates.

    Tries each usable candidate from :func:`extraction_model_plan` in
    order (same key, same endpoint as the chat config) and returns
    ``(response, config_that_answered)``. Moves to the next candidate when
    the current one is unusable:

    * HTTP 404, 401/403, or a 400/422 naming the model as unknown / not
      permitted: skipped for the rest of the process;
    * z.ai "Insufficient balance" (1113) on both endpoints, or
      :data:`_EMPTY_STREAK_LIMIT` empty answers in a row: skipped for
      :data:`_SOFT_FALLBACK_COOLDOWN_S`, then tried again;
    * a single empty answer: the next candidate is tried for this call only
      (the #376 recovery), without skipping the model.

    Transient failures (timeouts, 5xx, ordinary rate limits) propagate
    from :func:`chat_completion` unchanged and never downgrade anything.
    The last candidate is the chat model; its errors propagate as-is.

    ``completion`` defaults to :func:`chat_completion`; callers pass their
    own module-level reference so tests can patch it there.
    """
    call = completion or chat_completion
    base_url = chat_config.base_url
    provider = (provider_hint or _infer_provider_from_base_url(base_url)).lower()
    plan = extraction_model_plan(provider, chat_config.model, base_url)
    candidates = usable_extraction_candidates(plan, base_url)

    config = chat_config
    for i, model in enumerate(candidates):
        config = LLMConfig(
            api_key=chat_config.api_key,
            base_url=base_url,
            model=model,
            api_format=chat_config.api_format,
        )
        next_model = candidates[i + 1] if i + 1 < len(candidates) else None
        kwargs: dict[str, Any] = {}
        if next_model is not None and _is_zai_base_url(base_url):
            kwargs["fail_fast_on_balance"] = True
        try:
            response = await call(config, system_prompt, user_prompt, **kwargs)
        except LLMBalanceExhaustedError:
            if next_model is None:  # pragma: no cover — only raised when asked
                raise
            _mark_unusable(base_url, model, soft=True)
            _log_extraction_fallback(model, next_model, base_url, "insufficient balance (z.ai 1113)")
            continue
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text or ""
            except Exception:
                body = ""
            reason = classify_model_unusable(e.response.status_code, body)
            if reason is None or next_model is None:
                raise
            _mark_unusable(base_url, model, soft=False)
            _log_extraction_fallback(model, next_model, base_url, reason)
            continue

        key = _fallback_key(base_url, model)
        if response:
            _EMPTY_STREAKS.pop(key, None)
            if _LAST_ANNOUNCED_EXTRACTION_MODEL != [key]:
                _LAST_ANNOUNCED_EXTRACTION_MODEL[:] = [key]
                logger.info(
                    "TotalReclaw extraction LLM: provider=%s chat_model=%s -> "
                    "extraction_model=%s (source=%s, base_url=%s)",
                    provider or "unknown", chat_config.model, model,
                    plan.source if model == plan.candidates[0] else "fallback",
                    base_url,
                )
            return response, config
        if next_model is None:
            return response, config
        streak = _EMPTY_STREAKS.get(key, 0) + 1
        if streak >= _EMPTY_STREAK_LIMIT:
            _EMPTY_STREAKS.pop(key, None)
            _mark_unusable(base_url, model, soft=True)
            _log_extraction_fallback(
                model, next_model, base_url, f"empty response {streak} times in a row"
            )
        else:
            _EMPTY_STREAKS[key] = streak
            logger.info(
                "TotalReclaw extraction model %s returned empty content; "
                "retrying this turn with %s",
                model, next_model,
            )
    return None, config


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


#: z.ai models that refuse ``thinking: {"type": "disabled"}`` (400, code 1210:
#: "This model always engages in thinking and cannot be disabled"), learned at
#: runtime so only the first call per process pays the rejected round-trip
#: (GLM 5.3 Flash is the first such model).
_ZAI_ALWAYS_THINKING_MODELS: set[str] = set()

#: Completion budget floor for always-thinking models: their reasoning tokens
#: count against ``max_completion_tokens``, so the extraction-sized budget
#: would let the reasoning pass crowd out the answer.
_ALWAYS_THINKING_MIN_TOKENS = 8192


def _is_zai_thinking_rejected(resp: httpx.Response) -> bool:
    """True for z.ai's 400/1210 answer to a request that disabled thinking."""
    if resp.status_code != 400:
        return False
    text = resp.text or ""
    return "1210" in text and "thinking" in text.lower()


def _is_zai_base_url(base_url: str) -> bool:
    """Return True for either z.ai endpoint (Coding ``/coding/paas/v4`` or
    Standard ``/paas/v4``).

    Used to gate the ``thinking: {"type": "disabled"}`` request body field
    (see :func:`_call_openai`). z.ai's GLM models default to a server-side
    *thinking* mode that spends generation budget emitting chain-of-thought
    into ``message.reasoning_content``. For cheap, structured extraction
    calls that reasoning budget is pure waste — disabling it is an
    efficiency win (faster, fewer completion tokens) and reduces the chance
    of a content/``reasoning_content`` split. (NB: thinking mode is NOT the
    root cause of #376 — at a realistic token budget GLM populates
    ``content`` with thinking on or off; the #376 empties were
    environmental/quota. See the empty-content recovery + diag below.)
    """
    if not base_url:
        return False
    return "z.ai" in base_url.lower()


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
    if _is_zai_base_url(config.base_url):
        # EFFICIENCY: z.ai's GLM-4.5-series-and-up models (including the
        # cheap extraction workhorse ``glm-4.5-flash``) default to
        # ``thinking: {"type": "enabled"}``. With thinking on, the model
        # spends generation budget emitting chain-of-thought into
        # ``message.reasoning_content`` before answering. For our cheap,
        # JSON-shaped extraction prompts that reasoning pass adds latency
        # and burns completion tokens for no quality gain.
        #
        # Sending ``thinking: {"type": "disabled"}`` tells GLM to answer
        # directly in ``content``. z.ai's documented parameter (default
        # "enabled"; allowed "enabled"/"disabled"); applies to GLM-4.5
        # series and higher and is a no-op on providers that ignore it.
        # SAME provider, SAME endpoint, SAME key — only the request body
        # changes. We deliberately do NOT flip the endpoint (the
        # Coding-plan key is endpoint-scoped; flipping 401s).
        #
        # NOTE: this is NOT the #376 fix. At a realistic max_tokens budget
        # GLM populates ``content`` regardless of thinking mode; the #376
        # QA empties were instant 200-empty responses (quota/throttle,
        # ~14ms), not thinking-mode content/reasoning splits. Disabling
        # thinking is an efficiency + hardening measure that ALSO removes
        # one possible empty-content path; the real #376 fix (detect
        # quota-exhausted instant-empty → surface an actionable error) is
        # tracked separately. The recovery + rich diag below cover any
        # residual empty-content case.
        #
        # Models that always think (GLM 5.3 Flash) reject the field with
        # 400/1210; for those we send the server default instead.
        if config.model in _ZAI_ALWAYS_THINKING_MODELS:
            body["max_completion_tokens"] = max(max_tokens, _ALWAYS_THINKING_MIN_TOKENS)
        else:
            body["thinking"] = {"type": "disabled"}

    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }
        resp = await client.post(f"{config.base_url}/chat/completions", headers=headers, json=body)
        if "thinking" in body and _is_zai_thinking_rejected(resp):
            # The model cannot turn thinking off. Retry once with the
            # server default and remember the model for later calls.
            _ZAI_ALWAYS_THINKING_MODELS.add(config.model)
            logger.info(
                "z.ai model %s cannot disable thinking; retrying with the server default",
                config.model,
            )
            del body["thinking"]
            body["max_completion_tokens"] = max(max_tokens, _ALWAYS_THINKING_MIN_TOKENS)
            resp = await client.post(f"{config.base_url}/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {}) or {}
        content = message.get("content")
        if not content:
            # OBSERVABILITY + HARDENING: GLM (and some other reasoning
            # models, e.g. Gemma on OpenAI-compat endpoints) can leave
            # ``content`` empty while placing the actual answer in
            # ``reasoning_content`` when thinking mode is active. We send
            # ``thinking: {"type": "disabled"}`` for z.ai above so this
            # path should not trigger there, but if a provider/version
            # ignores that flag we recover the payload here instead of
            # silently dropping the extraction turn. The extraction parser
            # already strips ``<think>`` wrappers and bracket-scans for the
            # JSON body, so a reasoning_content blob that wraps the JSON
            # parses cleanly. (Recovery is a safety net, not the #376 fix —
            # #376's empties were truly empty, no reasoning_content to
            # recover; those fall through to the rich diag below.)
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                logger.warning(
                    "LLM returned empty content but non-empty "
                    "reasoning_content (provider=%s, model=%s, "
                    "reasoning_chars=%d) — recovering from reasoning_content. "
                    "If this recurs on z.ai, ensure the request is not "
                    "overriding thinking back to enabled.",
                    config.base_url,
                    config.model,
                    len(reasoning),
                )
                return reasoning
            # Truly empty (no content AND no reasoning_content to recover).
            # Surface the FULL response shape at WARN so the cause is
            # diagnosable on the next run without DEBUG (#318): raw HTTP
            # status + rate-limit/quota headers + a body snippet, on top of
            # finish_reason/usage/message-keys. This is what distinguishes an
            # exhausted/throttled key (instant 200-empty, rl headers) from a
            # token-budget or formatting issue (finish_reason=length, usage).
            rl_headers = {
                k: v for k, v in resp.headers.items()
                if any(t in k.lower() for t in ("ratelimit", "quota", "x-request-id", "retry-after"))
            }
            logger.warning(
                "LLM returned empty content (provider=%s, model=%s, "
                "http_status=%s, system_chars=%d, user_chars=%d, finish_reason=%s, "
                "usage=%s, json_response_format=%s, message_keys=%s, "
                "reasoning_content_present=%s, reasoning_content_chars=%d, "
                "rl_headers=%s, body_snippet=%r)",
                config.base_url,
                config.model,
                resp.status_code,
                len(system_prompt or ""),
                len(user_prompt or ""),
                choice.get("finish_reason"),
                data.get("usage"),
                "response_format" in body,
                sorted(message.keys()),
                reasoning is not None,
                len(reasoning or ""),
                rl_headers,
                str(message)[:400],
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
