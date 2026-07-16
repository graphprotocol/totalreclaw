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
from .debrief import generate_crystal
from .loop_runner import (
    run_sync,
    InterpreterShutdownError,
    is_interpreter_shutdown_error,
)
from .pending_drain import enqueue_messages

logger = logging.getLogger(__name__)


_INTERPRETER_SHUTDOWN_QUOTA_NOTE = (
    "TotalReclaw: auto-extraction was deferred because the chat process "
    "was already shutting down (CLI one-shot race). {n} message(s) were "
    "queued to ~/.totalreclaw/.pending_extract.jsonl and will be processed "
    "on your next session. No data was lost."
)

# Sentinel distinguishing "no session override applied" from "override to
# None" in ``auto_extract``'s save/restore of ``relay._session_id`` (#494).
_SESSION_OVERRIDE_UNSET = object()


def _owner_address(state: "AgentState") -> str:
    """Best-effort lookup of the configured owner address for queue keying.

    Falls back to the EOA address when the smart-account address hasn't
    been resolved yet (which is the case during early CLI -q invocations
    before any remember/recall call has run). Returns ``""`` when the
    state isn't configured — callers treat that as "skip queueing".
    """
    client = state.get_client()
    if not client:
        return ""
    sa = client.resolved_wallet_address
    if sa:
        return str(sa).lower()
    eoa = client.eoa_address
    return str(eoa).lower() if eoa else ""


def _owner_addresses(state: "AgentState") -> list[str]:
    """All known owner addresses for the configured client (EOA + SA).

    Returned as lower-cased, deduped, in stable order (EOA first when
    present). Empty list if the state is not configured. Used by drain
    callers (``has_pending`` / ``drain_pending``) to accept queue entries
    written under EITHER address — avoids the SA-vs-EOA timing race
    described in issue #169 (queue write picks whichever side
    ``_owner_address`` returned at shutdown; the next session's drain
    would silently miss if SA-resolution timing differs).
    """
    client = state.get_client()
    if not client:
        return []
    seen: set[str] = set()
    out: list[str] = []
    eoa = client.eoa_address
    if eoa:
        addr = str(eoa).lower()
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    sa = client.resolved_wallet_address
    if sa:
        addr = str(sa).lower()
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


STORE_DEDUP_THRESHOLD = 0.85  # Cosine similarity for near-duplicate detection

# Maximum facts per batched UserOperation — mirrors userop.MAX_BATCH_SIZE so
# we don't need to import from userop here (avoids a circular-import risk).
# If the userop constant ever changes, update both.
_LIFECYCLE_MAX_BATCH = 15


def _fetch_recent_memories(state: "AgentState") -> list[dict]:
    """Fetch recent memories from the vault for dedup context.

    Returns dicts with id, text, and embedding for both LLM dedup context and
    cosine-based near-duplicate detection.

    Re-raises ``InterpreterShutdownError`` so the outer ``auto_extract``
    handler can persist unprocessed messages to the drain queue (issue
    #148). Other failures are swallowed and surface as an empty list —
    dedup is best-effort.
    """
    client = state.get_client()
    if not client:
        return []
    try:
        # Use a generic query to get recent memories for dedup
        results = run_sync(client.recall("recent context", top_k=50))
        return [{"id": r.id, "text": r.text, "embedding": r.embedding} for r in results]
    except InterpreterShutdownError:
        raise
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            raise InterpreterShutdownError(str(e)) from e
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


def auto_extract(
    state: "AgentState",
    mode: str = "turn",
    llm_config=None,
    session_id_override: Optional[str] = None,
) -> list[str]:
    """Extract facts from conversation and store them.

    Tries LLM extraction first, falls back to heuristic if no LLM available.
    Handles ADD, UPDATE, and DELETE actions from LLM-guided extraction.
    Performs cosine-based near-duplicate detection before storing.

    Args:
        state: The AgentState instance (must be configured).
        mode: "turn" for incremental extraction, "full" for session-end flush.
        llm_config: Optional pre-resolved LLM configuration (e.g. from Hermes config).
            If not provided, ``extract_facts_llm`` falls back to env var detection.
        session_id_override: When set, the relay session tag (which the relay
            forwards as ``X-TotalReclaw-Session`` and the server stamps onto
            ``metadata.session_id``) is temporarily overridden for the duration
            of this extraction and restored afterwards. Used by the pending-drain
            path so re-extracted facts carry the ORIGINAL session's id rather
            than the draining session's (issue #494). An empty override is
            treated as "no session" (header omitted).

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

    # Drain-path session-id override (issue #494). Temporarily point the relay
    # session tag at the original session so server-side metadata.session_id is
    # stamped correctly, then restore. Reading it back inside the shutdown
    # handler below also lets a re-deferred drain re-enqueue the ORIGINAL id.
    relay = getattr(client, "_relay", None)
    _saved_session_id = _SESSION_OVERRIDE_UNSET
    if session_id_override is not None and relay is not None and hasattr(relay, "_session_id"):
        _saved_session_id = relay._session_id
        relay._session_id = session_id_override or None

    stored_texts: list[str] = []

    try:
        return _auto_extract_inner(
            state, mode, llm_config, messages, max_facts, client, stored_texts,
        )
    except InterpreterShutdownError:
        # The host interpreter is shutting down (atexit chain in
        # ``hermes chat -q`` one-shot mode). The persistent sync-loop runner
        # can't drive any more httpx work, so persist the unprocessed
        # buffer to disk and let the next session drain it. See
        # ``totalreclaw.agent.pending_drain`` and issue #148.
        #
        # Persist the source session id (issue #494) so the next-session drain
        # re-stamps facts with the original session. ``relay._session_id`` here
        # reflects any active override, so a re-deferred drain preserves it.
        owner = _owner_address(state)
        source_session_id = getattr(relay, "_session_id", None) if relay is not None else None
        if owner:
            persisted = enqueue_messages(owner, list(messages), session_id=source_session_id)
        else:
            persisted = False
        if persisted:
            logger.warning(
                "TotalReclaw: auto-extract deferred — interpreter shutdown "
                "race; %d msg(s) queued for next-session drain.",
                len(messages),
            )
            try:
                state.set_quota_warning(
                    _INTERPRETER_SHUTDOWN_QUOTA_NOTE.format(n=len(messages))
                )
            except Exception:
                pass
        else:
            logger.warning(
                "TotalReclaw: auto-extract deferred — interpreter shutdown "
                "race; persistence FAILED, %d msg(s) lost.",
                len(messages),
            )
        # Do NOT call ``state.mark_messages_processed()`` — leaving them
        # unprocessed is harmless (state dies with the process anyway) and
        # makes the failure visible to any in-process retry path.
        return []
    finally:
        # Restore the relay session tag if we overrode it (issue #494).
        if _saved_session_id is not _SESSION_OVERRIDE_UNSET:
            relay._session_id = _saved_session_id


def _auto_extract_inner(
    state: "AgentState",
    mode: str,
    llm_config,
    messages: list[dict],
    max_facts: int,
    client,
    stored_texts: list[str],
) -> list[str]:
    """Inner auto_extract body — kept separate so the outer wrapper can
    catch ``InterpreterShutdownError`` from any nested ``run_sync``."""

    # Fetch existing memories for both LLM dedup context and cosine dedup
    existing_memories = _fetch_recent_memories(state)

    # Try LLM extraction first
    facts: list[ExtractedFact] = []
    try:
        facts = run_sync(
            extract_facts_llm(messages, mode=mode, existing_memories=existing_memories, llm_config=llm_config)
        )
    except InterpreterShutdownError:
        raise
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            raise InterpreterShutdownError(str(e)) from e
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
        facts = run_sync(detect_and_resolve_contradictions(facts, client, logger))
    except InterpreterShutdownError:
        raise
    except Exception as exc:
        if is_interpreter_shutdown_error(exc):
            raise InterpreterShutdownError(str(exc)) from exc
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

    # Session-aware grouping: tag every atomic fact from this extraction with
    # the active conversation's session_id (the same id the session-end Crystal
    # carries), so a vault reader (SPA) can group a conversation's facts + its
    # Crystal together. Encrypted-blob-only via ``metadata.session_id``; absent
    # when there's no active conversation (unconfigured / pre-session).
    active_session_id = state.session_id

    for fact in facts:
        try:
            if fact.action == "NOOP":
                continue

            if fact.action == "DELETE":
                # Tombstone the old fact — one-at-a-time, no batch needed.
                if fact.existing_fact_id:
                    try:
                        run_sync(client.forget(fact.existing_fact_id))
                    except Exception as fe:
                        if is_interpreter_shutdown_error(fe):
                            raise InterpreterShutdownError(str(fe)) from fe
                        raise
                continue

            # For ADD and UPDATE: resolve embedding + run dedup, then enqueue.
            embedding = None
            try:
                from totalreclaw.embedding import get_embedding
                embedding = get_embedding(fact.text)
            except Exception as e:
                logger.debug("embed failed for fact %r: %s", fact.text[:40], e)
                # degrade: store without embedding, skip near-dup detection below

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
            if active_session_id:
                fact_dict["extra_metadata"] = {"session_id": active_session_id}
            pending_store.append((fact, embedding, fact_dict))

        except InterpreterShutdownError:
            raise
        except Exception as e:
            if is_interpreter_shutdown_error(e):
                raise InterpreterShutdownError(str(e)) from e
            logger.warning("Failed to prepare extracted fact for batch: %s", e)

    # Submit pending ADD/UPDATE facts in chunks of _LIFECYCLE_MAX_BATCH.
    for chunk_start in range(0, len(pending_store), _LIFECYCLE_MAX_BATCH):
        chunk = pending_store[chunk_start : chunk_start + _LIFECYCLE_MAX_BATCH]
        chunk_dicts = [fd for (_, _, fd) in chunk]

        try:
            # v1 write path — forward the taxonomy fields the extractor
            # populated (source/scope/reasoning/volatility). Defensive
            # fallback for source matches the plugin's
            # ``storeExtractedFacts`` handling.
            fact_ids = run_sync(
                client.remember_batch(chunk_dicts, source="hermes-auto")
            )
            # imp-15 telemetry: one structured line per submission so Axiom can
            # verify userop_count/fact_count ratio per #281 spec §6 T-6. The
            # chain-gate (Pro batch / free single) lands in a sister work-leaf;
            # until then lifecycle.py always batches → submission_path="batch",
            # userop_count=1 per chunk.
            _chain_id = client.resolved_chain_id
            logger.info(
                "submission_telemetry submission_path=%s fact_count=%d userop_count=%d chain_id=%s",
                "batch", len(chunk_dicts), 1, _chain_id,
            )
        except InterpreterShutdownError:
            raise
        except Exception as e:
            if is_interpreter_shutdown_error(e):
                raise InterpreterShutdownError(str(e)) from e
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
                            run_sync(client.forget(fact.existing_fact_id))
                        except InterpreterShutdownError:
                            raise
                        except Exception as fe:
                            if is_interpreter_shutdown_error(fe):
                                raise InterpreterShutdownError(str(fe)) from fe
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
            except InterpreterShutdownError:
                raise
            except Exception as e:
                if is_interpreter_shutdown_error(e):
                    raise InterpreterShutdownError(str(e)) from e
                logger.warning("Failed to process batch result for fact: %s", e)

    state.mark_messages_processed()
    return stored_texts


def session_debrief(
    state: "AgentState",
    stored_fact_texts: Optional[list[str]] = None,
    host_type: str = "chat",
) -> list[str]:
    """Run session debrief: generate one Crystal summary and store it.

    Replaces the old 5-item free-form debrief with a single structured
    Crystal (am-1). The Crystal is stored as v1 ``summary`` type with
    ``metadata.subtype="session_crystal"`` for filtered recall queries.

    Args:
        state: The AgentState instance (must be configured).
        stored_fact_texts: Optional list of already-stored fact texts for dedup.
        host_type: "chat" (Hermes/NanoClaw) or "coding" (OpenClaw/MCP).

    Returns:
        List containing the Crystal fact id, or empty list on failure.
    """
    if not state.is_configured():
        return []

    all_messages = state.get_all_messages()

    if stored_fact_texts is None:
        stored_fact_texts = []

    # Content-aware length gate. Hard floor: < 2 turns (4 messages) is too thin
    # to summarize. Between 4-7 messages, still crystallize IF the session
    # produced real content (>= 2 stored facts) — a crisp 2-3 turn topical
    # exchange (e.g. "book the Lisbon flight, aisle seat, under $400") deserves
    # its own Crystal, not just loose atomic facts. 4+ turns always qualify.
    if len(all_messages) < 4:
        return []
    if len(all_messages) < 8 and len(stored_fact_texts) < 2:
        return []

    client = state.get_client()
    if not client:
        return []

    stored_fact_ids: list[str] = []
    try:
        crystal = run_sync(generate_crystal(all_messages, stored_fact_texts, host_type=host_type))
        if crystal:
            # Tag the Crystal with the active conversation's session_id so it
            # groups with the same conversation's atomic facts in a vault reader
            # (SPA). ``to_metadata()`` only emits session_id when non-empty, so
            # this is a no-op when there's no active session.
            if not crystal.session_id and state.session_id:
                crystal.session_id = state.session_id
            try:
                fact_id = run_sync(
                    client.remember(
                        crystal.narrative,
                        importance=crystal.importance / 10.0,
                        source="hermes_debrief",
                        fact_type="summary",
                        provenance="derived",
                        scope="unspecified",
                        extra_metadata=crystal.to_metadata(),
                    )
                )
                if fact_id:
                    stored_fact_ids.append(fact_id)
                    logger.info("Session Crystal stored (id=%s, importance=%d)", fact_id, crystal.importance)
            except InterpreterShutdownError:
                raise
            except Exception as e:
                if is_interpreter_shutdown_error(e):
                    raise InterpreterShutdownError(str(e)) from e
                logger.warning("Failed to store Crystal: %s", e)
    except InterpreterShutdownError:
        logger.warning(
            "TotalReclaw: session Crystal deferred — interpreter shutdown race; "
            "next session will re-run extract on the drained messages."
        )
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            logger.warning(
                "TotalReclaw: session Crystal deferred — interpreter shutdown race."
            )
        else:
            logger.warning("Session Crystal generation failed: %s", e)
    return stored_fact_ids
