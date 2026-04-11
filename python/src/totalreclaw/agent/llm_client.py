"""
LLM client for TotalReclaw fact extraction.

Auto-detects the user's LLM provider from environment variables.
Supports OpenAI-compatible APIs and Anthropic Messages API.
Uses a cheap/fast model for extraction to minimize cost.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import os
from dataclasses import dataclass
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
# agent framework. Override with TOTALRECLAW_EXTRACTION_MODEL if needed.
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


def detect_llm_config(configured_model: Optional[str] = None) -> Optional[LLMConfig]:
    """Auto-detect LLM provider and model from environment variables.

    Uses the agent's configured model by default. No hardcoded model lists
    to maintain — just uses whatever the user set up.

    Model priority:
      1. TOTALRECLAW_EXTRACTION_MODEL (optional override for power users)
      2. configured_model (passed from agent framework)
      3. OPENAI_MODEL / ANTHROPIC_MODEL (common env vars)

    Base URL priority:
      1. OPENAI_BASE_URL (for OpenAI-compatible custom providers)
      2. Provider default
    """
    override_model = os.environ.get("TOTALRECLAW_EXTRACTION_MODEL")
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
                model = override_model or configured_model or env_model
                if not model:
                    logger.warning(
                        "TotalReclaw: %s API key found but no model configured. "
                        "Set TOTALRECLAW_EXTRACTION_MODEL or OPENAI_MODEL.",
                        _provider,
                    )
                    continue

                # For OpenAI provider, respect OPENAI_BASE_URL
                if _provider == "openai" and openai_base_url:
                    resolved_base_url = openai_base_url.rstrip("/")
                else:
                    resolved_base_url = default_base_url

                return LLMConfig(
                    api_key=api_key,
                    base_url=resolved_base_url,
                    model=model,
                    api_format=api_format,
                )
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
