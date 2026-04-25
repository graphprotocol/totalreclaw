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

#: Bug #6: when the user hasn't run ``totalreclaw_setup`` yet but is
#: asking a natural memory-related question, inject a one-time context
#: nudge telling the agent to offer setup. Otherwise Hermes's built-in
#: ``memory`` tool absorbs the request and the user never discovers
#: TotalReclaw is installed.
_SETUP_NUDGE = (
    "## TotalReclaw setup nudge (one-time)\n"
    "TotalReclaw is installed but not yet configured. If the user is "
    "asking about remembering things across sessions, encrypted memory, "
    "or cross-agent persistence, call `totalreclaw_setup` to generate a "
    "BIP-39 recovery phrase and enable the vault. After that, use "
    "`totalreclaw_remember` / `totalreclaw_recall` — they outrank any "
    "built-in 'memory' tool for durability + encryption."
)

#: Keywords that trigger the setup nudge when the user isn't configured.
#: Kept intentionally broad — a missed nudge on an unrelated question is
#: a no-op, but a missed nudge on a memory-relevant question means the
#: user sees "memory" work only for the session (via Hermes built-in)
#: and never learns about TotalReclaw.
_MEMORY_INTENT_KEYWORDS = (
    "remember", "recall", "forget", "memory", "memories", "note",
    "save", "store", "record", "encrypted", "persistent", "across",
    "vault", "preference", "what do you know",
)


def _looks_like_memory_intent(user_message: str) -> bool:
    """Cheap heuristic: does the message reference memory semantics?"""
    if not user_message:
        return False
    lower = user_message.lower()
    return any(kw in lower for kw in _MEMORY_INTENT_KEYWORDS)


def _get_hermes_llm_config():
    """Get LLM config from Hermes's own config files.

    Returns an LLMConfig from ~/.hermes/config.yaml + ~/.hermes/.env, or None.
    Imported lazily to avoid circular imports.
    """
    try:
        from .tools import _read_hermes_llm_config
        return _read_hermes_llm_config()
    except Exception:
        return None


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
    """Auto-recall on first turn, inject memories, quota warnings, and the
    unconfigured-user setup nudge (Bug #6) into context.

    When the plugin is installed but the user hasn't run
    ``totalreclaw_setup`` yet, a one-time nudge is injected on the first
    turn that references any memory intent. The nudge tells the Hermes
    agent to offer setup — preventing silent routing to Hermes's
    built-in ``memory`` tool.
    """
    user_message = kwargs.get("user_message", "")
    is_first_turn = kwargs.get("is_first_turn", False)

    # Bug #6 — unconfigured: one-time setup nudge, fires only when the
    # user message looks like a memory intent. We never return None here
    # so the Hermes agent sees the nudge as soon as memory semantics hit.
    # The "shown once" flag lives as an ad-hoc attribute on the state
    # instance — ``AgentState`` itself is owned by Phase 1's surface and
    # we don't want to add state-management methods there from the plugin.
    if not state.is_configured():
        shown = getattr(state, "_totalreclaw_setup_nudge_shown", False)
        if not shown and _looks_like_memory_intent(user_message):
            state._totalreclaw_setup_nudge_shown = True
            return {"context": _SETUP_NUDGE}
        return None

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
    """Auto-extract facts every N turns.

    Bug #5: when Hermes's LLM config can't be resolved AND no fallback
    env-vars are set, we surface a one-time quota-channel warning so the
    user sees the failure in their next assistant turn. Prior behavior
    was a debug-only log; users hit 7+ natural-conversation turns and
    watched no memories appear, with nothing to explain why.
    """
    # Always track turns and messages (client may be configured mid-session)
    state.increment_turn()
    state.add_message("user", kwargs.get("user_message", ""))
    state.add_message("assistant", kwargs.get("assistant_response", ""))

    if not state.is_configured():
        return

    extraction_interval = state.get_extraction_interval()
    if state.turn_count % extraction_interval != 0:
        return

    # Resolve LLM config and surface a user-visible warning if it fails.
    llm_config = _get_hermes_llm_config()
    if llm_config is None:
        # Also try env-var detection so non-Hermes-hosted agents work.
        try:
            from totalreclaw.agent.llm_client import detect_llm_config
            llm_config = detect_llm_config()
        except Exception:
            llm_config = None

    if llm_config is None:
        # Surface once per session via the existing quota-warning channel
        # so the user sees an explanation in their next assistant turn.
        _warned_attr = "_totalreclaw_llm_missing_warned"
        if not getattr(state, _warned_attr, False):
            setattr(state, _warned_attr, True)
            state.set_quota_warning(
                "TotalReclaw: automatic memory extraction is DISABLED — no "
                "LLM config was resolved. To enable: ensure ~/.hermes/"
                "config.yaml has a model + provider set AND ~/.hermes/.env "
                "contains the matching API key. (Fallback: export "
                "OPENAI_MODEL + OPENAI_API_KEY.) Until this is fixed, "
                "explicit `totalreclaw_remember` and `totalreclaw_recall` "
                "still work."
            )
            logger.warning(
                "TotalReclaw: no LLM config for auto-extraction; surfacing "
                "one-time warning via quota channel"
            )
        # Fall through — ``_auto_extract`` will itself log the silent-skip
        # path; it's safe to call with ``llm_config=None``. Callers that
        # mock ``extract_facts_llm`` directly still see the wiring path.

    # Extract and store facts (use Hermes LLM config so model name is resolved)
    try:
        _auto_extract(state, mode="turn", llm_config=llm_config)
    except Exception as e:
        logger.warning("TotalReclaw post_llm_call extraction failed: %s", e)


def on_session_end(state: "PluginState", **kwargs) -> None:
    """No-op. ``on_session_end`` is dispatched by hermes_cli at the end of
    every ``run_conversation()`` call — i.e. once per user turn, NOT at
    true session end. Session-end flush + debrief + message-buffer clear
    have moved to ``on_session_finalize``.

    Before 2.3.1rc16 this handler ran the flush + debrief and wiped
    ``state._messages`` in its ``finally`` block. Because the hook fires
    per-turn, the clear ran after every turn and ``totalreclaw_debrief``
    always saw <8 messages even in 10+ turn sessions (issue #101, parent
    #85 bug 5).
    """
    return None


def on_session_finalize(state: "PluginState", **kwargs) -> None:
    """Comprehensive flush of unprocessed messages + session debrief.

    Fires at true session boundaries (hermes_cli atexit, gateway session
    finalize). Per-turn auto-extraction runs from ``post_llm_call``; this
    handler catches residual unprocessed messages and runs the session
    debrief while the full conversation buffer is still intact.
    """
    if not state.is_configured():
        return

    try:
        stored_fact_texts: list[str] = []
        if state.has_unprocessed_messages():
            try:
                stored_fact_texts = _auto_extract(state, mode="full", llm_config=_get_hermes_llm_config())
            except Exception as e:
                logger.warning("TotalReclaw on_session_finalize flush failed: %s", e)

        try:
            _session_debrief(state, stored_fact_texts=stored_fact_texts)
        except Exception as e:
            logger.warning("TotalReclaw on_session_finalize debrief failed: %s", e)
    finally:
        state.clear_messages()


def on_session_reset(state: "PluginState", **kwargs) -> None:
    """User-initiated reset (``/reset``). Clean slate without the expensive
    debrief — a finalize would have fired first if the conversation was
    meant to be persisted.
    """
    state.clear_messages()
    state.reset_turn_counter()


# Backward-compatible alias used by tests
def _extract_and_store(state: "PluginState", mode: str = "turn") -> list[str]:
    """Backward-compatible wrapper for auto_extract."""
    return _auto_extract(state, mode=mode)
