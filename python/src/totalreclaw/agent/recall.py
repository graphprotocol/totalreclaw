"""
Auto-recall for TotalReclaw agent integrations.

Searches the encrypted vault and returns relevant memories formatted
as context that can be injected into the LLM prompt.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional, TYPE_CHECKING

import totalreclaw_core

from .loop_runner import run_sync

if TYPE_CHECKING:
    from .state import AgentState

logger = logging.getLogger(__name__)


def _fmt_date(created_at) -> str:
    """Format a Unix-seconds timestamp as 'YYYY-MM-DD', or '' if missing/invalid.

    created_at may be a float, int, or None (legacy entries pre-dating the
    createdAt subgraph field). The empty string signals to the caller to omit
    the date tag entirely rather than rendering a placeholder.

    Delegates to :func:`totalreclaw_core.format_memory_date` — thin shim kept
    for backward-compatible imports (hermes/tools.py and tests reference it).
    """
    if not created_at:
        return ""
    try:
        return totalreclaw_core.format_memory_date(int(float(created_at)))
    except Exception:
        return ""


def _format_recall_context(results) -> str:
    """Format a list of RerankerResult objects as the LLM recall context block.

    Each memory line includes its recorded date when available:
      - [category] (YYYY-MM-DD) text
      - [category] text    (when date is absent)

    A current-date + temporal-reasoning header is prepended so the LLM
    can compute time deltas without guessing the reference point.

    Delegates to :func:`totalreclaw_core.format_recall_context` — the shared
    Rust implementation is byte-identical to the previous pure-Python body.
    """
    items = [
        {
            "category": str(r.category),
            "text": str(r.text),
            "created_at": int(float(getattr(r, "created_at", None) or 0)),
        }
        for r in results
    ]
    return totalreclaw_core.format_recall_context(json.dumps(items), int(time.time()))


def auto_recall_with_status(
    query: str,
    state: "AgentState",
    top_k: int = 8,
) -> tuple[Optional[str], str]:
    """Like :func:`auto_recall`, but also reports WHY there is no context.

    Returns ``(context, status)`` with status one of:

    * ``"ok"`` — context contains formatted memories.
    * ``"empty"`` — the pipeline ran cleanly but returned zero results.
    * ``"error"`` — the recall pipeline raised (network, relay, decrypt, …).
    * ``"unconfigured"`` — no credentials / no client / empty query.

    Callers that only want the context keep using :func:`auto_recall`; the
    recall drivers (Hermes hook + MemoryProvider) use this variant so the
    recall-health notice (:mod:`totalreclaw.recall_health`, internal#486
    follow-up) can distinguish "vault has nothing relevant" from "the read
    path is broken" instead of both collapsing into a silent ``None``.
    """
    if not state.is_configured() or not query:
        return None, "unconfigured"

    client = state.get_client()
    if not client:
        return None, "unconfigured"

    try:
        results = run_sync(
            client.recall(
                query, top_k=top_k, max_candidates=state.get_max_candidate_pool()
            )
        )
    except Exception as e:
        logger.warning("TotalReclaw auto-recall failed: %s", e)
        return None, "error"

    if results:
        return _format_recall_context(results), "ok"
    return None, "empty"


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
    context, _status = auto_recall_with_status(query, state, top_k=top_k)
    return context


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
        results = await client.recall(
            query, top_k=top_k, max_candidates=state.get_max_candidate_pool()
        )
        if results:
            return _format_recall_context(results)
    except Exception as e:
        logger.warning("TotalReclaw auto-recall failed: %s", e)

    return None
