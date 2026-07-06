"""F1 / internal#437 — the privacy disclosure must NAME the provider.

``_extraction_provider_label`` did ``getattr(config, "provider", None)`` but
``LLMConfig`` has no ``provider`` field (only api_key/base_url/model/api_format),
so the label always fell through to the generic "your configured LLM provider"
and the disclosure never told the user which LLM would read their conversations.

The fix derives the provider name from ``config.base_url`` and includes the
model name. Tests drive REAL ``LLMConfig`` instances through the resolver.
"""
from __future__ import annotations

from unittest.mock import patch

from totalreclaw.agent.llm_client import LLMConfig
from totalreclaw.hermes import tools


def _label_with_config(cfg):
    with patch("totalreclaw.agent.llm_client.read_hermes_llm_config", return_value=None), \
         patch("totalreclaw.agent.llm_client.detect_llm_config", return_value=cfg):
        return tools._extraction_provider_label()


def test_zai_label_names_provider_and_model():
    cfg = LLMConfig(
        api_key="secret",
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-4.6",
        api_format="openai",
    )
    label = _label_with_config(cfg)
    assert "z.ai" in label.lower()
    assert "glm-4.6" in label


def test_openai_label():
    cfg = LLMConfig(
        api_key="secret", base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini", api_format="openai",
    )
    label = _label_with_config(cfg)
    assert "OpenAI" in label
    assert "gpt-4.1-mini" in label


def test_anthropic_label():
    cfg = LLMConfig(
        api_key="secret", base_url="https://api.anthropic.com/v1",
        model="claude-haiku-4-5-20251001", api_format="anthropic",
    )
    label = _label_with_config(cfg)
    assert "Anthropic" in label


def test_no_config_falls_back_to_generic():
    with patch("totalreclaw.agent.llm_client.read_hermes_llm_config", return_value=None), \
         patch("totalreclaw.agent.llm_client.detect_llm_config", return_value=None):
        assert tools._extraction_provider_label() == "your configured LLM provider"
