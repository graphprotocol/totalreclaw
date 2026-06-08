"""
Auto-recall for TotalReclaw agent integrations.

Searches the encrypted vault and returns relevant memories formatted
as context that can be injected into the LLM prompt.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from .loop_runner import run_sync

if TYPE_CHECKING:
    from .state import AgentState

logger = logging.getLogger(__name__)


def _fmt_date(created_at) -> str:
    """Format a Unix-seconds timestamp as 'YYYY-MM-DD', or '' if missing/invalid.

    created_at may be a float, int, or None (legacy entries pre-dating the
    createdAt subgraph field). The empty string signals to the caller to omit
    the date tag entirely rather than rendering a placeholder.
    """
    if not created_at:
        return ""
    try:
        return datetime.fromtimestamp(float(created_at), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _format_recall_context(results) -> str:
    """Format a list of RerankerResult objects as the LLM recall context block.

    Each memory line includes its recorded date when available:
      - [category] (YYYY-MM-DD) text
      - [category] text    (when date is absent)

    A current-date + temporal-reasoning header is prepended so the LLM
    can compute time deltas without guessing the reference point.
    """
    def _line(r):
        d = _fmt_date(getattr(r, "created_at", None))
        return f"- [{r.category}] ({d}) {r.text}" if d else f"- [{r.category}] {r.text}"

    memories = "\n".join(_line(r) for r in results)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        f"## Relevant memories from TotalReclaw\n"
        f"The current date is {today}. Each memory is tagged with the date it was "
        f"recorded. When the question involves timing or duration, reason carefully "
        f"about the dates and compute differences precisely.\n"
    )
    return f"{header}{memories}"


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
            return _format_recall_context(results)
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
            return _format_recall_context(results)
    except Exception as e:
        logger.warning("TotalReclaw auto-recall failed: %s", e)

    return None
