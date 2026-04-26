"""
Agent lifecycle functions for TotalReclaw.

High-level functions for auto-extraction and session debrief that can be
called from any agent framework's lifecycle hooks.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).

All sync work is driven through ``totalreclaw.agent.loop_runner.run_sync``
rather than per-call ``asyncio.new_event_loop`` pairs — see the module
docstring there for the rationale (QA-V1CLEAN-VPS-20260418 "Event loop is
closed" bug).
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import AgentState

from .extraction import ExtractedFact, extract_facts_llm, extract_facts_heuristic
from .contradiction import detect_and_resolve_contradictions
from .debrief import generate_debrief
from .loop_runner import (
    is_interpreter_shutdown_error,
    run_sync,
    run_sync_resilient,
)

logger = logging.getLogger(__name__)

STORE_DEDUP_THRESHOLD = 0.85  # Cosine similarity for near-duplicate detection


def _resilient_run(coro_factory):
    """Run an async work unit with shutdown-resilience.

    rc.23 finding #1 (umbrella #147 / sub #148): under ``hermes chat -q``
    the auto-extract / auto-recall / debrief work fires during process
    teardown and used to silently fail with ``cannot schedule new
    futures after interpreter shutdown``. Routing every per-fact /
    per-batch async call through ``run_sync_resilient`` gives the
    persistent loop runner a chance to rebuild its private executor and
    retry the awaitable on a fresh slate.
    """
    return run_sync_resilient(coro_factory)

# Maximum facts per batched UserOperation — mirrors userop.MAX_BATCH_SIZE so
# we don't need to import from userop here (avoids a circular-import risk).
# If the userop constant ever changes, update both.
_LIFECYCLE_MAX_BATCH = 15


def _fetch_recent_memories(state: "AgentState") -> list[dict]:
    """Fetch recent memories from the vault for dedup context.

    Returns dicts with id, text, and embedding for both LLM dedup context and
    cosine-based near-duplicate detection.
    """
    client = state.get_client()
    if not client:
        return []
    try:
        # Use a generic query to get recent memories for dedup. Wrapped
        # in a coroutine factory so the loop runner can retry on the
        # post-shutdown ``cannot schedule new futures`` RuntimeError.
        results = _resilient_run(lambda: client.recall("recent context", top_k=50))
        return [{"id": r.id, "text": r.text, "embedding": r.embedding} for r in results]
    except RuntimeError as exc:
        if is_interpreter_shutdown_error(exc):
            logger.warning(
                "TotalReclaw lifecycle: dedup-context fetch dropped due to "
                "interpreter-shutdown race (recall could not land). Auto-extraction "
                "will skip dedup for this batch."
            )
            return []
        raise
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

    # Fetch existing memories for both LLM dedup context and cosine dedup
    existing_memories = _fetch_recent_memories(state)

    # Try LLM extraction first. Resilient invocation so the auto-extract
    # call survives the rc.23 finding #1 shutdown race — without this,
    # CLI ``hermes chat -q`` users got ZERO durable memories per turn.
    facts: list[ExtractedFact] = []
    try:
        facts = _resilient_run(
            lambda: extract_facts_llm(
                messages,
                mode=mode,
                existing_memories=existing_memories,
                llm_config=llm_config,
            )
        )
    except RuntimeError as exc:
        if is_interpreter_shutdown_error(exc):
            logger.warning(
                "TotalReclaw lifecycle: LLM extraction dropped due to "
                "interpreter-shutdown race (mode=%s, %d unprocessed messages). "
                "Falling back to heuristic so at least the buffer is drained.",
                mode, len(messages),
            )
            facts = []
        else:
            logger.debug("LLM extraction failed, falling back to heuristic: %s", exc)
    except Exception as e:
        logger.debug("LLM extraction failed, falling back to heuristic: %s", e)

    # Fall back to heuristic if LLM returned nothing
    if not facts:
        facts = extract_facts_heuristic(messages, max_facts)

    if not facts:
        state.mark_messages_processed()
        return []

    # Cap to max_facts
    facts = facts[:max_facts]

    # Contradiction detection: filter out facts that lose to existing vault claims
    try:
        facts = _resilient_run(
            lambda: detect_and_resolve_contradictions(facts, client, logger)
        )
    except RuntimeError as exc:
        if is_interpreter_shutdown_error(exc):
            logger.warning(
                "TotalReclaw lifecycle: contradiction detection dropped due to "
                "interpreter-shutdown race; proceeding with all %d facts unchanged.",
                len(facts),
            )
        else:
            logger.debug(
                "Contradiction detection failed (proceeding with all facts): %s", exc,
            )
    except Exception as exc:
        logger.debug("Contradiction detection failed (proceeding with all facts): %s", exc)

    # -----------------------------------------------------------------------
    # Two-pass approach:
    #   Pass 1 — handle NOOP / DELETE actions individually (no batch needed).
    #   Pass 2 — collect ADD / UPDATE facts that survive dedup into a pending
    #            list, then submit in chunks of _LIFECYCLE_MAX_BATCH via
    #            client.remember_batch().  UPDATE tombstones (forget old id)
    #            are issued after the batch that stored the replacement.
    # -----------------------------------------------------------------------

    # pending_store: list of (fact, embedding, fact_dict) for batch submission
    pending_store: list[tuple[ExtractedFact, Optional[list[float]], dict]] = []

    for fact in facts:
        try:
            if fact.action == "NOOP":
                continue

            if fact.action == "DELETE":
                # Tombstone the old fact — one-at-a-time, no batch needed.
                if fact.existing_fact_id:
                    _delete_id = fact.existing_fact_id
                    _resilient_run(lambda fid=_delete_id: client.forget(fid))
                continue

            # For ADD and UPDATE: resolve embedding + run dedup, then enqueue.
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

            # Build the fact dict — mirrors the kwargs passed to remember() so
            # remember_batch() sees an identical payload.
            fact_dict: dict = {
                "text": fact.text,
                "embedding": embedding,
                "importance": fact.importance / 10.0,  # Normalize 1-10 to 0.0-1.0
                "fact_type": fact.type,
                "entities": fact.entities,
                "confidence": fact.confidence,
                "provenance": fact.source or "user-inferred",
                "scope": fact.scope or "unspecified",
                "reasoning": fact.reasoning,
                "volatility": fact.volatility,
            }
            pending_store.append((fact, embedding, fact_dict))

        except Exception as e:
            logger.warning("Failed to prepare extracted fact for batch: %s", e)

    # Submit pending ADD/UPDATE facts in chunks of _LIFECYCLE_MAX_BATCH.
    for chunk_start in range(0, len(pending_store), _LIFECYCLE_MAX_BATCH):
        chunk = pending_store[chunk_start : chunk_start + _LIFECYCLE_MAX_BATCH]
        chunk_dicts = [fd for (_, _, fd) in chunk]

        try:
            # v1 write path — forward the taxonomy fields the extractor
            # populated (source/scope/reasoning/volatility). Defensive
            # fallback for source matches the plugin's
            # ``storeExtractedFacts`` handling. Resilient call so a
            # batch fired during process exit survives the post-shutdown
            # executor race (rc.23 finding #1).
            _chunk_dicts = chunk_dicts
            fact_ids = _resilient_run(
                lambda d=_chunk_dicts: client.remember_batch(d, source="hermes-auto")
            )
        except Exception as e:
            if is_interpreter_shutdown_error(e):
                logger.warning(
                    "TotalReclaw lifecycle: remember_batch dropped %d facts due to "
                    "interpreter-shutdown race (CLI auto-extract during process "
                    "exit). Memories not persisted.",
                    len(chunk),
                )
            else:
                logger.warning(
                    "remember_batch failed for chunk of %d facts: %s", len(chunk), e
                )
            # On a total batch failure, none of the facts in this chunk landed.
            # Log each one individually so the caller can diagnose.
            for fact, _, _ in chunk:
                logger.warning(
                    "Fact not stored (batch failure): %s", fact.text[:80]
                )
            continue

        # fact_ids is a list[str] in the same order as chunk_dicts.
        # Process per-fact results: log stored facts and issue UPDATE tombstones.
        for i, (fact, _, _) in enumerate(chunk):
            try:
                fact_id = fact_ids[i] if i < len(fact_ids) else None
                if fact_id:
                    stored_texts.append(fact.text)
                    # For UPDATE: tombstone the old fact after the replacement
                    # has been successfully stored.
                    if fact.action == "UPDATE" and fact.existing_fact_id:
                        try:
                            _old_id = fact.existing_fact_id
                            _resilient_run(lambda fid=_old_id: client.forget(fid))
                        except Exception as fe:
                            logger.warning(
                                "Failed to tombstone old fact %s after UPDATE: %s",
                                fact.existing_fact_id, fe,
                            )
                else:
                    # remember_batch returned a short list — treat as failure.
                    logger.warning(
                        "Fact not stored (no id returned by batch): %s",
                        fact.text[:80],
                    )
            except Exception as e:
                logger.warning("Failed to process batch result for fact: %s", e)

    state.mark_messages_processed()
    return stored_texts


def session_debrief(
    state: "AgentState",
    stored_fact_texts: Optional[list[str]] = None,
) -> list[str]:
    """Run session debrief: extract broader context and store it.

    Args:
        state: The AgentState instance (must be configured).
        stored_fact_texts: Optional list of already-stored fact texts for dedup.
            If None, an empty list is used.

    Returns:
        List of newly-stored debrief fact ids. Empty on short sessions,
        unconfigured state, LLM unavailability, or any interior failure.
        The return type widened from ``None`` in 2.1.0 so the explicit
        ``totalreclaw_debrief`` tool can surface per-fact ids back to the
        user. The auto ``on_session_end`` path ignores the return value —
        behavior-compatible.
    """
    if not state.is_configured():
        return []

    all_messages = state.get_all_messages()
    if len(all_messages) < 8:  # Minimum 4 turns
        return []

    if stored_fact_texts is None:
        stored_fact_texts = []

    client = state.get_client()
    if not client:
        return []

    stored_fact_ids: list[str] = []
    try:
        debrief_items = _resilient_run(
            lambda: generate_debrief(all_messages, stored_fact_texts)
        )
        if debrief_items:
            for item in debrief_items:
                try:
                    # v1 debrief items are summaries with derived
                    # provenance — the assistant-side debrief pipeline
                    # synthesized them from the conversation, so the
                    # v1 source is always "derived" (plugin parity).
                    # Capture loop variables explicitly so the
                    # coroutine factory closure binds the right values.
                    _item = item
                    fact_id = _resilient_run(
                        lambda it=_item: client.remember(
                            it.text,
                            importance=it.importance / 10.0,
                            source="hermes_debrief",
                            fact_type="summary",
                            provenance="derived",
                            scope="unspecified",
                        )
                    )
                    if fact_id:
                        stored_fact_ids.append(fact_id)
                except Exception as e:
                    if is_interpreter_shutdown_error(e):
                        logger.warning(
                            "TotalReclaw debrief: dropped a summary item due to "
                            "interpreter-shutdown race; remaining items will still "
                            "attempt to store.",
                        )
                    else:
                        logger.warning("Failed to store debrief item: %s", e)
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            logger.warning(
                "TotalReclaw debrief: generate_debrief dropped due to "
                "interpreter-shutdown race (CLI session-end during process exit). "
                "Conversation summary not persisted.",
            )
        else:
            logger.warning("Session debrief failed: %s", e)
    return stored_fact_ids
