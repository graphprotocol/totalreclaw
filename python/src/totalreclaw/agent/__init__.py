"""
TotalReclaw Generic Agent Integration Layer.

Framework-agnostic building blocks for integrating TotalReclaw E2E encrypted
memory into any Python agent framework. The Hermes plugin, LangChain adapter,
and other integrations are thin wrappers around this module.

Usage::

    from totalreclaw.agent import AgentState, auto_recall, auto_extract, session_debrief

    # Initialize state (auto-configures from env/credentials)
    state = AgentState()
    # or with explicit credentials:
    state = AgentState(recovery_phrase="abandon abandon ...")

    # On first user message:
    context = auto_recall("user's message", state)

    # After each turn:
    state.add_message("user", user_msg)
    state.add_message("assistant", assistant_resp)
    state.increment_turn()
    if state.turn_count % state.get_extraction_interval() == 0:
        auto_extract(state)

    # At session end:
    auto_extract(state, mode="full")
    session_debrief(state)
"""
from __future__ import annotations

from .state import (
    AgentState,
    DEFAULT_EXTRACTION_INTERVAL,
    DEFAULT_MAX_FACTS,
    DEFAULT_MIN_IMPORTANCE,
    BILLING_CACHE_TTL,
    STORE_DEDUP_THRESHOLD,
)
from .extraction import (
    ExtractedFact,
    extract_facts_llm,
    extract_facts_heuristic,
    extract_facts_compaction,
    deduplicate_facts_by_embedding,
    is_product_meta_request,
    EXTRACTION_SYSTEM_PROMPT,
    COMPACTION_SYSTEM_PROMPT,
)
from .recall import auto_recall, auto_recall_async
from .debrief import (
    DebriefItem,
    generate_debrief,
    parse_debrief_response,
    DEBRIEF_SYSTEM_PROMPT,
)
from .llm_client import (
    LLMConfig,
    detect_llm_config,
    chat_completion,
    PROVIDERS,
)
from .lifecycle import auto_extract, session_debrief

__all__ = [
    # State
    "AgentState",
    "DEFAULT_EXTRACTION_INTERVAL",
    "DEFAULT_MAX_FACTS",
    "DEFAULT_MIN_IMPORTANCE",
    "BILLING_CACHE_TTL",
    "STORE_DEDUP_THRESHOLD",
    # Extraction
    "ExtractedFact",
    "extract_facts_llm",
    "extract_facts_heuristic",
    "extract_facts_compaction",
    "deduplicate_facts_by_embedding",
    "is_product_meta_request",
    "EXTRACTION_SYSTEM_PROMPT",
    "COMPACTION_SYSTEM_PROMPT",
    # Recall
    "auto_recall",
    "auto_recall_async",
    # Debrief
    "DebriefItem",
    "generate_debrief",
    "parse_debrief_response",
    "DEBRIEF_SYSTEM_PROMPT",
    # LLM Client
    "LLMConfig",
    "detect_llm_config",
    "chat_completion",
    "PROVIDERS",
    # Lifecycle
    "auto_extract",
    "session_debrief",
]
