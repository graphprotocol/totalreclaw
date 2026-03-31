"""
LLM client for TotalReclaw fact extraction.

Auto-detects the user's LLM provider from environment variables.
Supports OpenAI-compatible APIs and Anthropic Messages API.
Uses a cheap/fast model for extraction to minimize cost.
"""
from __future__ import annotations

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


# Provider detection order and their env vars
PROVIDERS = [
    ("anthropic", ["ANTHROPIC_API_KEY"], "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001", "anthropic"),
    ("openai", ["OPENAI_API_KEY"], "https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    ("groq", ["GROQ_API_KEY"], "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "openai"),
    ("deepseek", ["DEEPSEEK_API_KEY"], "https://api.deepseek.com/v1", "deepseek-chat", "openai"),
    ("openrouter", ["OPENROUTER_API_KEY"], "https://openrouter.ai/api/v1", "anthropic/claude-haiku-4-5-20251001", "openai"),
    ("gemini", ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai"),
    ("mistral", ["MISTRAL_API_KEY"], "https://api.mistral.ai/v1", "mistral-small-latest", "openai"),
    ("xai", ["XAI_API_KEY"], "https://api.x.ai/v1", "grok-2", "openai"),
    ("together", ["TOGETHER_API_KEY"], "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "openai"),
]


def detect_llm_config() -> Optional[LLMConfig]:
    """Auto-detect LLM provider from environment variables.

    Override model with TOTALRECLAW_LLM_MODEL env var.
    """
    override_model = os.environ.get("TOTALRECLAW_LLM_MODEL")

    for _provider, env_vars, base_url, default_model, api_format in PROVIDERS:
        for env_var in env_vars:
            api_key = os.environ.get(env_var)
            if api_key:
                model = override_model or default_model
                return LLMConfig(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    api_format=api_format,
                )
    return None


async def chat_completion(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.0,
) -> Optional[str]:
    """Call LLM chat completion. Returns assistant response text or None."""
    try:
        if config.api_format == "anthropic":
            return await _call_anthropic(config, system_prompt, user_prompt, max_tokens, temperature)
        else:
            return await _call_openai(config, system_prompt, user_prompt, max_tokens, temperature)
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        return None


async def _call_openai(
    config: LLMConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
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
    async with httpx.AsyncClient(timeout=30.0) as client:
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
