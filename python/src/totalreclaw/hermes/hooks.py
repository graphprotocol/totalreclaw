"""Lifecycle hooks for TotalReclaw Hermes plugin.

Thin adapter that wires the generic ``totalreclaw.agent`` lifecycle
functions into Hermes's hook registration system.
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

from totalreclaw.agent.lifecycle import (
    auto_extract as _auto_extract,
    session_debrief as _session_debrief,
    _is_near_duplicate,
    _fetch_recent_memories,
    STORE_DEDUP_THRESHOLD,
)
from totalreclaw.agent.recall import auto_recall
from totalreclaw.agent.extraction import extract_facts_llm, extract_facts_heuristic

logger = logging.getLogger(__name__)


def on_session_start(state: "PluginState", **kwargs) -> None:
    """Initialize client and check billing on session start."""
    session_id = kwargs.get("session_id", "")
    logger.debug("TotalReclaw on_session_start: %s", session_id)

    state.reset_turn_counter()

    if not state.is_configured():
        return

    # Check billing cache and update server-driven config
    try:
        billing = state.get_cached_billing()
        if billing:
            # Update extraction config from server
            state.update_from_billing(billing)

            used = billing.get("free_writes_used", 0)
            limit = max(billing.get("free_writes_limit", 500), 1)
            if used / limit > 0.8:
                pct = int(used / limit * 100)
                state.set_quota_warning(
                    f"TotalReclaw: {used}/{limit} memories used this month ({pct}%). "
                    "Consider upgrading to Pro for unlimited storage."
                )
                logger.info("TotalReclaw: Memory usage >80%% — quota warning set")
    except Exception:
        pass


def pre_llm_call(state: "PluginState", **kwargs) -> Optional[dict]:
    """Auto-recall on first turn, inject memories and quota warnings into context."""
    if not state.is_configured():
        return None

    is_first_turn = kwargs.get("is_first_turn", False)
    user_message = kwargs.get("user_message", "")

    context_parts: list[str] = []

    # Inject quota warning (once per session)
    quota_warning = state.get_quota_warning()
    if quota_warning:
        context_parts.append(quota_warning)
        state.clear_quota_warning()

    if is_first_turn and user_message:
        # Auto-recall relevant memories for the first turn
        try:
            memories_ctx = auto_recall(user_message, state, top_k=8)
            if memories_ctx:
                context_parts.append(memories_ctx)
        except Exception as e:
            logger.warning("TotalReclaw pre_llm_call auto-recall failed: %s", e)

    if not context_parts:
        return None

    return {"context": "\n\n".join(context_parts)}


def post_llm_call(state: "PluginState", **kwargs) -> None:
    """Auto-extract facts every N turns."""
    # Always track turns and messages (client may be configured mid-session)
    state.increment_turn()
    state.add_message("user", kwargs.get("user_message", ""))
    state.add_message("assistant", kwargs.get("assistant_response", ""))

    if not state.is_configured():
        return

    extraction_interval = state.get_extraction_interval()
    if state.turn_count % extraction_interval != 0:
        return

    # Extract and store facts
    try:
        _auto_extract(state, mode="turn")
    except Exception as e:
        logger.warning("TotalReclaw post_llm_call extraction failed: %s", e)


def on_session_end(state: "PluginState", **kwargs) -> None:
    """Comprehensive flush of unprocessed messages + session debrief."""
    if not state.is_configured():
        return

    if not state.has_unprocessed_messages():
        return

    try:
        stored_fact_texts: list[str] = []
        try:
            stored_fact_texts = _auto_extract(state, mode="full")
        except Exception as e:
            logger.warning("TotalReclaw on_session_end flush failed: %s", e)

        # Session debrief (after regular extraction)
        try:
            _session_debrief(state, stored_fact_texts=stored_fact_texts)
        except Exception as e:
            logger.warning("TotalReclaw on_session_end debrief failed: %s", e)
    finally:
        state.clear_messages()


# Backward-compatible alias used by tests
def _extract_and_store(state: "PluginState", mode: str = "turn") -> list[str]:
    """Backward-compatible wrapper for auto_extract."""
    return _auto_extract(state, mode=mode)
