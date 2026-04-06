"""
LLM client for TotalReclaw fact extraction.

This module re-exports from the generic ``totalreclaw.agent.llm_client``
for backward compatibility. New code should import from
``totalreclaw.agent.llm_client`` directly.
"""
from totalreclaw.agent.llm_client import (  # noqa: F401
    LLMConfig,
    PROVIDERS,
    detect_llm_config,
    chat_completion,
)
