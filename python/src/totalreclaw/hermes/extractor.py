"""
LLM-guided fact extraction for TotalReclaw Hermes plugin.

This module re-exports from the generic ``totalreclaw.agent.extraction``
for backward compatibility. New code should import from
``totalreclaw.agent.extraction`` directly.
"""
from totalreclaw.agent.extraction import (  # noqa: F401
    ExtractedFact,
    VALID_TYPES,
    VALID_ACTIONS,
    EXTRACTION_SYSTEM_PROMPT,
    _truncate_messages,
    _parse_response,
    extract_facts_llm,
    extract_facts_heuristic,
)
