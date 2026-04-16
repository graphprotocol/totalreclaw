"""
Agent lifecycle functions for TotalReclaw.

High-level functions for auto-extraction and session debrief that can be
called from any agent framework's lifecycle hooks.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import AgentState

from .extraction import ExtractedFact, extract_facts_llm, extract_facts_heuristic
from .contradiction import detect_and_resolve_contradictions
from .debrief import generate_debrief

logger = logging.getLogger(__name__)

STORE_DEDUP_THRESHOLD = 0.85  # Cosine similarity for near-duplicate detection


def _fetch_recent_memories(state: "AgentState", loop: asyncio.AbstractEventLoop) -> list[dict]:
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

    Delegates to ``totalreclaw_core.find_best_near_duplicate`` (Rust/PyO3) when
    available, falling back to a pure-Python cosine loop otherwise.
    """
    if not embedding:
        return False

    # Filter to memories that actually have embeddings
    existing_with_emb = [
        m for m in existing_memories
        if m.get("embedding") and len(m["embedding"]) > 0
    ]
    if not existing_with_emb:
        return False

    try:
        import json as _json
        import totalreclaw_core

        new_embedding_json = _json.dumps(embedding)
        existing_json = _json.dumps([
            {"id": m.get("id", ""), "embedding": m["embedding"]}
            for m in existing_with_emb
        ])
        result = totalreclaw_core.find_best_near_duplicate(
            new_embedding_json, existing_json, threshold,
        )
        if result is not None:
            match = _json.loads(result)
            logger.debug(
                "Near-duplicate detected via core (sim=%.3f >= %.3f, fact=%s): skipping store",
                match.get("similarity", 0.0), threshold, match.get("fact_id", "?"),
            )
            return True
        return False
    except ImportError:
        logger.debug("totalreclaw_core not available, using Python cosine fallback")
    except Exception as exc:
        logger.debug("Rust find_best_near_duplicate failed, falling back to Python: %s", exc)

    # Fallback: pure-Python cosine similarity loop
    from totalreclaw.reranker import cosine_similarity

    for mem in existing_with_emb:
        sim = cosine_similarity(embedding, mem["embedding"])
        if sim >= threshold:
            logger.debug(
                "Near-duplicate detected (sim=%.3f >= %.3f): skipping store",
                sim, threshold,
            )
            return True
    return False


def auto_extract(state: "AgentState", mode: str = "turn", llm_config=None) -> list[str]:
    """Extract facts from conversation and store them.

    Tries LLM extraction first, falls back to heuristic if no LLM available.
    Handles ADD, UPDATE, and DELETE actions from LLM-guided extraction.
    Performs cosine-based near-duplicate detection before storing.

    Args:
        state: The AgentState instance (must be configured).
        mode: "turn" for incremental extraction, "full" for session-end flush.
        llm_config: Optional pre-resolved LLM configuration (e.g. from Hermes config).
            If not provided, ``extract_facts_llm`` falls back to env var detection.

    Returns:
        List of stored fact texts (for debrief context).
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
                extract_facts_llm(messages, mode=mode, existing_memories=existing_memories, llm_config=llm_config)
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

        # Contradiction detection: filter out facts that lose to existing vault claims
        try:
            facts = loop.run_until_complete(
                detect_and_resolve_contradictions(facts, client, logger)
            )
        except Exception as exc:
            logger.debug("Contradiction detection failed (proceeding with all facts): %s", exc)

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


def session_debrief(state: "AgentState", stored_fact_texts: Optional[list[str]] = None) -> None:
    """Run session debrief: extract broader context and store it.

    Args:
        state: The AgentState instance (must be configured).
        stored_fact_texts: Optional list of already-stored fact texts for dedup.
            If None, an empty list is used.
    """
    if not state.is_configured():
        return

    all_messages = state.get_all_messages()
    if len(all_messages) < 8:  # Minimum 4 turns
        return

    if stored_fact_texts is None:
        stored_fact_texts = []

    client = state.get_client()
    if not client:
        return

    try:
        loop = asyncio.new_event_loop()
        try:
            debrief_items = loop.run_until_complete(
                generate_debrief(all_messages, stored_fact_texts)
            )
            if debrief_items:
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
        logger.warning("Session debrief failed: %s", e)
