"""
Auto-recall for TotalReclaw agent integrations.

Searches the encrypted vault and returns relevant memories formatted
as context that can be injected into the LLM prompt.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from .loop_runner import run_sync

if TYPE_CHECKING:
    from .state import AgentState

logger = logging.getLogger(__name__)


def auto_recall(
    query: str,
    state: "AgentState",
    top_k: int = 8,
) -> Optional[str]:
    """Search the vault and return formatted context string, or None.

    This is a synchronous wrapper suitable for use in agent hooks that may
    not be async. Uses the process-wide sync loop runner so that the
    underlying ``httpx.AsyncClient`` (cached on the RelayClient) is never
    orphaned across short-lived loops — historically the cause of
    ``RuntimeError: Event loop is closed`` in v2.0.1 (QA-V1CLEAN-VPS-20260418).

    Args:
        query: The user's message or search query.
        state: The AgentState instance (must be configured).
        top_k: Maximum number of results to return.

    Returns:
        Formatted string of relevant memories, or None if no results.
    """
    if not state.is_configured() or not query:
        return None

    client = state.get_client()
    if not client:
        return None

    try:
        results = run_sync(client.recall(query, top_k=top_k))

        if results:
            memories = "\n".join(f"- [{r.category}] {r.text}" for r in results)
            return f"## Relevant memories from TotalReclaw\n{memories}"
    except Exception as e:
        logger.warning("TotalReclaw auto-recall failed: %s", e)

    return None


async def auto_recall_async(
    query: str,
    state: "AgentState",
    top_k: int = 8,
) -> Optional[str]:
    """Async version of auto_recall.

    Args:
        query: The user's message or search query.
        state: The AgentState instance (must be configured).
        top_k: Maximum number of results to return.

    Returns:
        Formatted string of relevant memories, or None if no results.
    """
    if not state.is_configured() or not query:
        return None

    client = state.get_client()
    if not client:
        return None

    try:
        results = await client.recall(query, top_k=top_k)
        if results:
            memories = "\n".join(f"- [{r.category}] {r.text}" for r in results)
            return f"## Relevant memories from TotalReclaw\n{memories}"
    except Exception as e:
        logger.warning("TotalReclaw auto-recall failed: %s", e)

    return None
