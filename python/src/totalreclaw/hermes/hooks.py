"""Lifecycle hooks for TotalReclaw Hermes plugin."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

from .debrief import generate_debrief
from .extractor import extract_facts_llm, extract_facts_heuristic, ExtractedFact

logger = logging.getLogger(__name__)

STORE_DEDUP_THRESHOLD = 0.85  # Cosine similarity for near-duplicate detection


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
            client = state.get_client()
            if client:
                loop = asyncio.new_event_loop()
                try:
                    results = loop.run_until_complete(
                        client.recall(user_message, top_k=8)
                    )
                finally:
                    loop.close()

                if results:
                    memories = "\n".join(
                        f"- {r.text}" for r in results
                    )
                    context_parts.append(f"## Relevant memories from TotalReclaw\n{memories}")
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
        _extract_and_store(state, mode="turn")
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
            stored_fact_texts = _extract_and_store(state, mode="full")
        except Exception as e:
            logger.warning("TotalReclaw on_session_end flush failed: %s", e)

        # Session debrief (after regular extraction)
        try:
            all_messages = state.get_all_messages()
            if len(all_messages) >= 8:  # Minimum 4 turns
                loop = asyncio.new_event_loop()
                try:
                    debrief_items = loop.run_until_complete(
                        generate_debrief(all_messages, stored_fact_texts)
                    )
                    if debrief_items:
                        client = state.get_client()
                        if client:
                            for item in debrief_items:
                                try:
                                    loop.run_until_complete(
                                        client.remember(
                                            item.text,
                                            importance=item.importance / 10.0,
                                            source="hermes_debrief",
                                        )
                                    )
                                except Exception as e:
                                    logger.warning("Failed to store debrief item: %s", e)
                finally:
                    loop.close()
        except Exception as e:
            logger.warning("TotalReclaw on_session_end debrief failed: %s", e)
    finally:
        state.clear_messages()


def _fetch_recent_memories(state: "PluginState", loop: asyncio.AbstractEventLoop) -> list[dict]:
    """Fetch recent memories from the vault for dedup context.

    Returns dicts with id, text, and embedding for both LLM dedup context and
    cosine-based near-duplicate detection.
    """
    client = state.get_client()
    if not client:
        return []
    try:
        # Use a generic query to get recent memories for dedup
        results = loop.run_until_complete(
            client.recall("recent context", top_k=50)
        )
        return [{"id": r.id, "text": r.text, "embedding": r.embedding} for r in results]
    except Exception as e:
        logger.debug("Failed to fetch recent memories for dedup: %s", e)
        return []


def _is_near_duplicate(
    embedding: list[float],
    existing_memories: list[dict],
    threshold: float = STORE_DEDUP_THRESHOLD,
) -> bool:
    """Check if a fact's embedding is a near-duplicate of any existing memory.

    Returns True if cosine similarity >= threshold with any existing memory.
    """
    if not embedding:
        return False

    from totalreclaw.reranker import cosine_similarity

    for mem in existing_memories:
        mem_emb = mem.get("embedding")
        if mem_emb and len(mem_emb) > 0:
            sim = cosine_similarity(embedding, mem_emb)
            if sim >= threshold:
                logger.debug(
                    "Near-duplicate detected (sim=%.3f >= %.3f): skipping store",
                    sim, threshold,
                )
                return True
    return False


def _extract_and_store(state: "PluginState", mode: str = "turn") -> list[str]:
    """Extract facts from conversation and store them.

    Tries LLM extraction first, falls back to heuristic if no LLM available.
    Handles ADD, UPDATE, and DELETE actions from LLM-guided extraction.
    Performs cosine-based near-duplicate detection before storing.

    Returns list of stored fact texts (for debrief context).
    """
    messages = state.get_unprocessed_messages()
    if not messages:
        return []

    max_facts = state.get_max_facts_per_extraction()

    # Format messages for extraction (used by heuristic fallback check)
    msg_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in messages if m.get("content")
    )
    if not msg_text.strip():
        return []

    client = state.get_client()
    if not client:
        return []

    stored_texts: list[str] = []
    loop = asyncio.new_event_loop()
    try:
        # Fetch existing memories for both LLM dedup context and cosine dedup
        existing_memories = _fetch_recent_memories(state, loop)

        # Try LLM extraction first
        facts: list[ExtractedFact] = []
        try:
            facts = loop.run_until_complete(
                extract_facts_llm(messages, mode=mode, existing_memories=existing_memories)
            )
        except Exception as e:
            logger.debug("LLM extraction failed, falling back to heuristic: %s", e)

        # Fall back to heuristic if LLM returned nothing
        if not facts:
            facts = extract_facts_heuristic(messages, max_facts)

        if not facts:
            return []

        # Cap to max_facts
        facts = facts[:max_facts]

        for fact in facts:
            try:
                if fact.action == "NOOP":
                    continue

                if fact.action == "DELETE":
                    # Tombstone the old fact
                    if fact.existing_fact_id:
                        loop.run_until_complete(
                            client.forget(fact.existing_fact_id)
                        )
                    continue

                # For ADD and UPDATE: store the new fact
                embedding = None
                try:
                    from totalreclaw.embedding import get_embedding
                    embedding = get_embedding(fact.text)
                except Exception:
                    pass

                # Store-time near-duplicate detection (skip if cosine sim >= threshold)
                # UPDATE actions always store (they supersede the old fact)
                if fact.action != "UPDATE" and embedding and existing_memories:
                    if _is_near_duplicate(embedding, existing_memories):
                        logger.debug("Skipping near-duplicate fact: %s", fact.text[:80])
                        continue

                loop.run_until_complete(
                    client.remember(
                        fact.text,
                        embedding=embedding,
                        importance=fact.importance / 10.0,  # Normalize 1-10 to 0.0-1.0
                        source="hermes-auto",
                    )
                )
                stored_texts.append(fact.text)

                # For UPDATE: also tombstone the old fact after storing the new one
                if fact.action == "UPDATE" and fact.existing_fact_id:
                    loop.run_until_complete(
                        client.forget(fact.existing_fact_id)
                    )

            except Exception as e:
                logger.warning("Failed to store/process extracted fact: %s", e)
    finally:
        loop.close()

    state.mark_messages_processed()
    return stored_texts
