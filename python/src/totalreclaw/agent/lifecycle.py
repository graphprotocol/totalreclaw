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
import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import AgentState

from .extraction import (
    TURN_MODE_WINDOW_MESSAGES,
    ExtractedFact,
    build_dedup_context_block,
    extract_facts_heuristic,
    extract_facts_llm,
)
from .contradiction import detect_and_resolve_contradictions
from .pin_guard import apply_pin_guard, fail_closed_actions
from .debrief import Crystal, generate_crystal
from .loop_runner import (
    run_sync,
    InterpreterShutdownError,
    is_interpreter_shutdown_error,
)
from .pending_drain import enqueue_messages
from .pending_store import PendingStoreQueue
from .write_quota import (
    fill_write_quota_details,
    format_write_quota_notice,
    format_write_recovered_notice,
    lookup_write_quota_details,
)
from ..forget_guard import ForgetLedger, is_forget_request
from ..pii import auto_storable, drop_sensitive_facts
from ..relay import RelayReadBlocked, RelayWriteQuotaExceeded
from ..write_gate import (
    decide_write,
    ledger_for,
    merge_candidates,
    record_write,
    results_to_candidates,
)

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


def _is_write_quota_error(exc: BaseException) -> bool:
    """True when a write failed because the relay refused it for quota.

    The bundler path raises the typed ``RelayWriteQuotaExceeded`` (#747).
    The looser checks (any 403, or a message mentioning "quota") are kept
    for other callers and older relays; they only drive the billing-cache
    invalidation, never the user notice or the retry queue, which need the
    typed error.
    """
    if isinstance(exc, RelayWriteQuotaExceeded):
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 403:
        return True
    return "quota" in str(exc).lower()


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


# ---------------------------------------------------------------------------
# Topical dedup context (PRD-04 DEP-3 / F2)
#
# Before extraction the LLM is shown the existing memories it should compare
# the new facts against, so it can answer UPDATE / DELETE / NOOP instead of
# storing a duplicate. Pre-DEP-3 this was ``recall("recent context",
# top_k=50)`` with no embedding: a keyword search for "recent" / "context"
# plus the newest facts, which only covered vaults of about 50 facts or less.
# ---------------------------------------------------------------------------

#: Topical candidates, fetched through the same path as ``totalreclaw_recall``.
DEDUP_TOPICAL_TOP_K = 20

#: Newest active facts by block time, regardless of topic.
DEDUP_RECENT_LIMIT = 10

#: Max memory lines in the prompt block (core ``DEFAULT_DEDUP_CONTEXT_CAP``).
DEDUP_CONTEXT_CAP = 30

#: Lexical recall query = the last 4 window messages joined by one space, cut
#: to 500 characters (the OpenClaw plugin's numbers). The cut bounds the
#: word-trapdoor fan-out against the relay's per-user read limit.
DEDUP_QUERY_LAST_MESSAGES = 4
DEDUP_QUERY_MAX_CHARS = 500

#: Candidate pool used when the state cannot report the server-tuned one.
_DEFAULT_CANDIDATE_POOL = 250


def _extraction_window(messages: list[dict], mode: str) -> list[dict]:
    """The messages the extraction LLM sees (same slice as ``extract_facts_llm``)."""
    if mode == "turn":
        return messages[-TURN_MODE_WINDOW_MESSAGES:]
    return list(messages)


def _window_contents(window: list[dict]) -> list[str]:
    out: list[str] = []
    for m in window:
        text = str(m.get("content") or "").strip() if isinstance(m, dict) else ""
        if text:
            out.append(text)
    return out


def _dedup_query_text(window: list[dict]) -> str:
    """Lexical recall query for the topical fetch (see ``DEDUP_QUERY_*``)."""
    joined = " ".join(_window_contents(window[-DEDUP_QUERY_LAST_MESSAGES:]))
    return joined[:DEDUP_QUERY_MAX_CHARS]


def _embed_extraction_window(window: list[dict]) -> Optional[list[float]]:
    """Embedding of the whole extraction window, or ``None``.

    Never starts the one-time model download: when the model is neither
    loaded nor cached, topical recall runs lexically only.
    """
    text = "\n".join(_window_contents(window))
    if not text:
        return None
    from totalreclaw import embedding as _embedding

    if not _embedding.is_model_available_locally():
        logger.debug("Dedup context: embedding model not local; topical recall is lexical-only")
        return None
    try:
        vector = _embedding.get_embedding(text)
    except InterpreterShutdownError:
        raise
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            raise InterpreterShutdownError(str(e)) from e
        logger.debug("Dedup context: window embedding failed: %s", e)
        return None
    return list(vector) if vector else None


def _best_effort(label: str, fetch) -> list:
    """Run one dedup-context sub-fetch; any failure gives ``[]``.

    Interpreter shutdown must reach ``auto_extract``'s drain-queue handler
    (issue #148), so it is re-raised.
    """
    try:
        out = fetch()
        return out if isinstance(out, list) else []
    except InterpreterShutdownError:
        raise
    except RelayReadBlocked as e:
        # Reads are paused (quota / rate limit): best-effort, stay quiet (#662).
        logger.debug("Dedup context: %s skipped, reads paused (%s)", label, e)
        return []
    except Exception as e:
        if is_interpreter_shutdown_error(e):
            raise InterpreterShutdownError(str(e)) from e
        logger.debug("Dedup context: %s failed: %s", label, e)
        return []


def _tag(items: list, section: str) -> list[dict]:
    out: list[dict] = []
    for m in items or []:
        if isinstance(m, dict) and m.get("id") and m.get("text"):
            out.append({
                "id": str(m["id"]),
                "text": str(m["text"]),
                "embedding": m.get("embedding"),
                "pinned": bool(m.get("pinned")),
                "section": section,
            })
    return out


def _fetch_recent_memories(
    state: "AgentState",
    messages: Optional[list[dict]] = None,
    mode: str = "turn",
) -> list[dict]:
    """Existing memories for the extraction dedup context and the store gate.

    (The name predates PRD-04 DEP-3 and is kept: ``hermes.hooks`` and tests
    import it.) Returns ``{"id", "text", "embedding", "pinned", "section"}``
    dicts, deduplicated by id, in this order:

    * ``topical`` — top ``DEDUP_TOPICAL_TOP_K`` of ``client.recall`` on the
      extraction window (query embedding + word / LSH / entity trapdoors),
      with candidate embeddings for the cosine gate;
    * ``ledger`` — facts this process wrote in the last few minutes, which the
      subgraph may not have indexed yet (#501);
    * ``recent`` — the newest ``DEDUP_RECENT_LIMIT`` facts.

    ``pinned`` comes from the decrypted blob (search results carry it since
    DEP-5). Each fetch is best-effort: a failure leaves its section empty.
    ``InterpreterShutdownError`` is re-raised (issue #148).
    """
    client = state.get_client()
    if not client:
        return []

    window = _extraction_window(messages or [], mode)
    query = _dedup_query_text(window)

    def _topical() -> list:
        if not query:
            return []
        query_embedding = _embed_extraction_window(window)
        try:
            pool = int(state.get_max_candidate_pool())
        except Exception:
            pool = _DEFAULT_CANDIDATE_POOL
        results = run_sync(
            client.recall(
                query,
                query_embedding=query_embedding,
                max_candidates=pool,
                top_k=DEDUP_TOPICAL_TOP_K,
                include_embeddings=True,
            )
        )
        return results_to_candidates(results)

    def _recent() -> list:
        fetch = getattr(client, "fetch_recent_facts", None)
        if fetch is None:
            return []
        return run_sync(fetch(limit=DEDUP_RECENT_LIMIT))

    topical = _tag(_best_effort("topical recall", _topical), "topical")
    ledger = _tag(ledger_for(client).candidates(), "ledger")
    recent = _tag(_best_effort("recent fetch", _recent), "recent")
    memories = merge_candidates(client, topical, ledger, recent)
    logger.info(
        "dedup_context topical=%d ledger=%d recent=%d union=%d pinned=%d",
        len(topical), len(ledger), len(recent), len(memories),
        sum(1 for m in memories if m.get("pinned")),
    )
    return memories


def _dedup_context_block(memories: list[dict]) -> str:
    """The "Existing memories" prompt block for ``memories`` (core
    ``build_dedup_context``): pinned facts first and marked read-only, then
    topical, then just-written and newest; at most ``DEDUP_CONTEXT_CAP``
    lines; ``""`` when empty."""
    pinned = [m for m in memories if m.get("pinned")]
    topical = [m for m in memories if not m.get("pinned") and m.get("section", "topical") == "topical"]
    recent = [m for m in memories if not m.get("pinned") and m.get("section") in ("ledger", "recent")]
    try:
        return build_dedup_context_block(topical, pinned, recent, DEDUP_CONTEXT_CAP)
    except Exception as e:
        logger.warning("Dedup context formatting failed; extracting without it: %s", e)
        return ""


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


def _forget_ledger(client) -> Optional[ForgetLedger]:
    """The client's forget ledger, or ``None`` (unconfigured, mocks, errors)."""
    try:
        ledger = getattr(client, "forget_ledger", None)
    except Exception:
        return None
    return ledger if isinstance(ledger, ForgetLedger) else None


def _restates_forgotten(
    text: str,
    embedding: Optional[list[float]],
    ledger: Optional[ForgetLedger],
) -> Optional[str]:
    """Why *text* must not be stored, or ``None`` (internal#605 / #640).

    Two cases: a note about a deletion ("User asked to forget that ..."),
    which restates the forgotten content and is never a useful memory; and a
    restatement of a fact the user explicitly forgot (see
    :mod:`totalreclaw.forget_guard`). Write-time, client-side only.
    """
    if is_forget_request(text):
        return "forget-request note"
    if ledger is None:
        return None
    try:
        match = ledger.match(text, embedding)
    except Exception as e:
        logger.debug("forget ledger match failed: %s", type(e).__name__)
        return None
    if match:
        return f"restates forgotten fact {match.get('id', '?')} ({match.get('reason', '?')})"
    return None


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

    # Existing memories for the LLM dedup context (PRD-04 DEP-3) and the
    # store-time write gate: topical + just-written (ledger) + newest.
    existing_memories = _fetch_recent_memories(state, messages, mode)
    dedup_context = _dedup_context_block(existing_memories)

    # Try LLM extraction first
    facts: list[ExtractedFact] = []
    try:
        facts = run_sync(
            extract_facts_llm(
                messages,
                mode=mode,
                existing_memories=existing_memories,
                llm_config=llm_config,
                dedup_context=dedup_context,
            )
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

    # PRD-04 DEP-4: automatic extraction stores only facts the core PII
    # filter allows — block AND flag are dropped (logged by category only).
    # Runs before every log line below that prints fact text.
    facts = drop_sensitive_facts(facts)

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

    # Pin contract (PRD-04 F1 / DEP-5): an LLM UPDATE/DELETE never tombstones
    # a pinned fact. Must run before the action loop below, which is the only
    # place auto-extraction issues tombstones.
    try:
        facts = run_sync(apply_pin_guard(facts, client, logger))
    except InterpreterShutdownError:
        raise
    except Exception as exc:
        if is_interpreter_shutdown_error(exc):
            raise InterpreterShutdownError(str(exc)) from exc
        logger.warning(
            "Pin guard failed (%s); no UPDATE/DELETE tombstones this cycle",
            type(exc).__name__,
        )
        facts = fail_closed_actions(facts, logger)

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

    # Facts the user explicitly forgot (#605).
    forget_ledger = _forget_ledger(client)

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

            # Never re-create what the user asked to forget (#605 / #640).
            # The extractor still reads text that restates it: the original
            # turn, the forget turn, or an answer built from the host's own
            # session search. An UPDATE is dropped whole, so its target is
            # not tombstoned either.
            why = _restates_forgotten(fact.text, embedding, forget_ledger)
            if why:
                logger.info("Skipping extracted %s: %s", fact.action, why)
                continue

            # Store-time write gate (internal #632 / #501): normalized exact
            # match + cosine >= 0.85 against the fetched memories, facts this
            # process wrote in the last few minutes (not yet indexed), and
            # facts already accepted from this batch. UPDATE actions always
            # store (they supersede the old fact). A near duplicate of a
            # pinned fact is stored: the prompt asks the LLM to ADD new
            # information about a pin; the pin itself is never touched.
            if fact.action != "UPDATE":
                in_batch = [
                    {"id": f"pending-{i}", "text": pf.text, "embedding": pe}
                    for i, (pf, pe, _) in enumerate(pending_store)
                ]
                decision = decide_write(
                    fact.text,
                    embedding,
                    merge_candidates(client, existing_memories, in_batch),
                    mode="auto",
                )
                if not decision.store:
                    logger.info(
                        "Skipping %s duplicate of %s",
                        decision.match.kind, decision.match.fact_id,
                    )
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
    wrote_ok = False
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
        except RelayWriteQuotaExceeded as e:
            # #747: the relay refused the batch for quota. Keep what landed
            # before the refusal, queue the rest of this chunk AND every later
            # chunk (they would be refused too) for a retry, and tell the user.
            results = list(e.batch_results or [])
            results += [None] * (len(chunk) - len(results))
            _process_batch_results(client, chunk, results, stored_texts, log_missing=False)
            refused = [fd for (_, _, fd), fid in zip(chunk, results) if not fid]
            refused += [
                fd for (_, _, fd) in pending_store[chunk_start + _LIFECYCLE_MAX_BATCH:]
            ]
            _handle_write_quota(state, client, e, refused)
            break
        except Exception as e:
            if is_interpreter_shutdown_error(e):
                raise InterpreterShutdownError(str(e)) from e
            logger.warning(
                "remember_batch failed for chunk of %d facts: %s", len(chunk), e
            )
            if _is_write_quota_error(e):
                # Stale usage numbers would hide the quota warning until the
                # cache TTL runs out; drop them so the next session start
                # re-fetches billing status and warns.
                try:
                    state.invalidate_billing_cache()
                except Exception:
                    pass
            # On a total batch failure, none of the facts in this chunk landed.
            # Log each one individually so the caller can diagnose.
            for fact, _, _ in chunk:
                logger.warning(
                    "Fact not stored (batch failure): %s", fact.text[:80]
                )
            continue

        wrote_ok = True
        _process_batch_results(
            client, chunk, _per_fact_ids(fact_ids, len(chunk)), stored_texts, log_missing=True
        )

    state.mark_messages_processed()
    if wrote_ok:
        # A write just went through, so the quota allows writes again: store
        # facts an earlier refusal queued (#747). Never raises.
        _after_successful_write(state, client)
    return stored_texts


def _process_batch_results(
    client,
    chunk: list,
    fact_ids: list,
    stored_texts: list[str],
    *,
    log_missing: bool,
) -> None:
    """Per-fact bookkeeping after a batch: record stored facts and issue the
    UPDATE tombstones for replacements that landed. ``fact_ids[i]`` is the id
    of ``chunk[i]`` or falsy when it was not stored."""
    for i, (fact, fact_embedding, _) in enumerate(chunk):
        try:
            fact_id = fact_ids[i] if i < len(fact_ids) else None
            if fact_id:
                stored_texts.append(fact.text)
                # The next gate check (any path) sees this fact before
                # the subgraph indexes it (#501).
                record_write(client, fact_id, fact.text, fact_embedding)
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
            elif log_missing:
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


def _per_fact_ids(ids, n: int) -> list:
    """One entry per submitted fact: its id, or ``None`` when it was not
    stored (#751).

    ``remember_batch`` returns the stored ids only, so after a skipped group
    the list is shorter than the input and position no longer says which
    fact an id belongs to. Its ``batch_results`` does. A plain list (a stub
    client) is taken as already lined up.
    """
    results = getattr(ids, "batch_results", None)
    if not isinstance(results, list):
        results = list(ids or [])
    return list(results[:n]) + [None] * (n - len(results))


# ---------------------------------------------------------------------------
# Write-quota refusal: keep, tell, retry (#747)
#
# A refusal used to drop the facts with a log line and tell nobody. Now the
# refused facts go to an encrypted per-account queue (``agent.pending_store``),
# the user is told once per session through the quota-warning slot, and the
# queue is stored once a write succeeds again. Drained facts are stored as
# plain ADDs: a queued UPDATE does not tombstone its old fact weeks later,
# when that fact may have been pinned or changed.
# ---------------------------------------------------------------------------

#: Facts stored per drain call: one batch, so a drain never stalls a turn or
#: a session start for long. The rest wait for the next trigger.
DRAIN_MAX_FACTS = _LIFECYCLE_MAX_BATCH

#: Newest facts the drain checks queued facts against (one query per drain).
DRAIN_RECENT_LIMIT = 50

#: A queued fact that fails this many drains for a non-quota reason is dropped.
_DRAIN_MAX_ATTEMPTS = 5


def _pending_store(client) -> Optional[PendingStoreQueue]:
    """The client's retry queue, or ``None`` (unconfigured, mocks, errors)."""
    try:
        queue = getattr(client, "pending_store", None)
    except Exception:
        return None
    return queue if isinstance(queue, PendingStoreQueue) else None


def _announce_write_quota(
    state: "AgentState", client, err: RelayWriteQuotaExceeded, *, queued: int, lost: int,
) -> None:
    """Queue the refusal notice for the next turn, once per session."""
    if getattr(state, "write_quota_announced", False) is True:
        return
    try:
        run_sync(lookup_write_quota_details(err, client))
    except Exception:  # incl. InterpreterShutdownError: word it without numbers
        fill_write_quota_details(err, None)
    notice = format_write_quota_notice(err, queued=queued, lost=lost)
    announce = getattr(state, "announce_write_quota", None)
    try:
        if callable(announce):
            announce(notice)
        else:
            state.set_quota_warning(notice)
    except Exception as e:
        logger.debug("write quota notice not queued: %s", e)


def _handle_write_quota(
    state: "AgentState", client, err: RelayWriteQuotaExceeded, refused: list[dict],
    *, what: str = "auto-extracted facts",
) -> None:
    try:
        state.invalidate_billing_cache()
    except Exception:
        pass
    queued = 0
    queue = _pending_store(client)
    if queue is not None and refused:
        try:
            queued = queue.add(refused, reason="write_quota")
        except Exception as e:
            logger.warning("pending store: could not queue refused facts: %s", e)
    lost = max(0, len(refused) - queued)
    logger.warning(
        "TotalReclaw: relay refused %s (write quota used up); "
        "%d queued for retry, %d lost",
        what, queued, lost,
    )
    _announce_write_quota(state, client, err, queued=queued, lost=lost)


def _after_successful_write(state: "AgentState", client) -> None:
    saved = drain_pending_stores(state, client)
    if getattr(state, "write_quota_announced", False) is True:
        try:
            state.push_quota_notice(format_write_recovered_notice(saved))
            state.reset_write_quota_announcement()
        except Exception as e:
            logger.debug("write recovered notice not queued: %s", e)


def drain_pending_stores(
    state: "AgentState", client=None, *, max_facts: int = DRAIN_MAX_FACTS,
) -> int:
    """Store up to *max_facts* queued facts (oldest first). Returns how many
    were stored. Never raises.

    Each queued fact is re-embedded and checked before it is written: a fact
    the user has since forgotten (#605) or one already in the vault (the
    newest :data:`DRAIN_RECENT_LIMIT` facts plus this process's recent
    writes, e.g. the same fact re-extracted after saving resumed) is dropped.
    Facts the relay still refuses go back to the front of the queue.
    """
    client = client if client is not None else state.get_client()
    queue = _pending_store(client)
    if queue is None:
        return 0
    try:
        entries = queue.take(max_facts)
    except Exception as e:
        logger.warning("pending store: cannot read the queue: %s", e)
        return 0
    if not entries:
        return 0
    try:
        return _drain_entries(state, client, queue, entries)
    except Exception as e:  # incl. InterpreterShutdownError before any write
        logger.warning("pending store: drain failed before writing (%s); facts kept", e)
        queue.put_back(entries)
        return 0


def _drain_entries(state: "AgentState", client, queue: PendingStoreQueue, entries: list[dict]) -> int:
    recent: list[dict] = []
    fetch = getattr(client, "fetch_recent_facts", None)
    if fetch is not None:
        recent = _tag(
            _best_effort("recent fetch", lambda: run_sync(fetch(limit=DRAIN_RECENT_LIMIT))),
            "recent",
        )
    ledger = _forget_ledger(client)

    to_store: list[dict] = []
    to_store_entries: list[dict] = []
    for entry in entries:
        fact = dict(entry["fact"])
        if _is_session_crystal(fact):
            # Stored the way session_debrief stores it: forget scrub per
            # sentence, no write gate, no embedding (#749).
            fact = _scrub_queued_crystal(fact, ledger)
            if fact is None:
                logger.info("pending store: dropping a queued session Crystal (all of it was forgotten)")
                continue
            to_store.append(fact)
            to_store_entries.append(entry)
            continue
        text = fact.get("text") or ""
        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception as e:
            logger.debug("embed failed for queued fact: %s", e)
        why = _restates_forgotten(text, embedding, ledger)
        if why:
            logger.info("pending store: dropping queued fact: %s", why)
            continue
        in_batch = [
            {"id": f"pending-{i}", "text": f["text"], "embedding": f.get("embedding")}
            for i, f in enumerate(to_store)
        ]
        decision = decide_write(text, embedding, merge_candidates(client, recent, in_batch), mode="auto")
        if not decision.store:
            logger.info(
                "pending store: dropping queued %s duplicate of %s",
                decision.match.kind, decision.match.fact_id,
            )
            continue
        if embedding is not None:
            fact["embedding"] = embedding
        to_store.append(fact)
        to_store_entries.append(entry)

    if not to_store:
        return 0

    try:
        ids = run_sync(client.remember_batch(to_store, source="hermes-auto"))
    except RelayWriteQuotaExceeded as e:
        results = list(e.batch_results or [])
        results += [None] * (len(to_store) - len(results))
        stored = 0
        back = []
        for fact, entry, fact_id in zip(to_store, to_store_entries, results):
            if fact_id:
                record_write(client, fact_id, fact["text"], fact.get("embedding"))
                stored += 1
            else:
                back.append(entry)
        queue.put_back(back)
        try:
            state.invalidate_billing_cache()
        except Exception:
            pass
        _announce_write_quota(state, client, e, queued=len(back), lost=0)
        logger.info("pending store: relay still refuses writes; %d fact(s) stay queued", len(back))
        return stored
    except Exception as e:
        if isinstance(e, InterpreterShutdownError) or is_interpreter_shutdown_error(e):
            queue.put_back(to_store_entries)
            return 0
        kept, dropped = _retry_later(queue, to_store_entries)
        logger.warning(
            "pending store: queued facts not stored yet (%s); %d kept, %d dropped after %d attempts",
            e, kept, dropped, _DRAIN_MAX_ATTEMPTS,
        )
        return 0

    stored = 0
    not_stored = []
    for fact, entry, fact_id in zip(to_store, to_store_entries, _per_fact_ids(ids, len(to_store))):
        if fact_id:
            record_write(client, fact_id, fact["text"], fact.get("embedding"))
            stored += 1
        else:
            not_stored.append(entry)
    if not_stored:
        # Skipped by the batch writer (#751). The relay never reports a
        # duplicate, so this is not proof the fact is in the vault: retry it
        # (the write gate drops it if it is), up to the attempt limit.
        kept, dropped = _retry_later(queue, not_stored)
        logger.warning(
            "pending store: %d queued fact(s) not stored; %d kept, %d dropped after %d attempts",
            len(not_stored), kept, dropped, _DRAIN_MAX_ATTEMPTS,
        )
    logger.info("pending store: saved %d queued fact(s)", stored)
    return stored


def _retry_later(queue: PendingStoreQueue, entries: list[dict]) -> tuple[int, int]:
    """Put *entries* back with one more attempt counted, dropping those
    that reach :data:`_DRAIN_MAX_ATTEMPTS`. Returns ``(kept, dropped)``."""
    retry = []
    for entry in entries:
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        if entry["attempts"] < _DRAIN_MAX_ATTEMPTS:
            retry.append(entry)
    queue.put_back(retry)
    return len(retry), len(entries) - len(retry)


def crystal_gate_passes(n_messages: int, n_stored_facts: int) -> bool:
    """Content-aware length gate for a session Crystal.

    Hard floor: < 2 turns (4 messages) is too thin to summarize. Between 4-7
    messages, still crystallize IF the session produced real content (>= 2
    stored facts) — a crisp 2-3 turn topical exchange (e.g. "book the Lisbon
    flight, aisle seat, under $400") deserves its own Crystal, not just loose
    atomic facts. 4+ turns always qualify.
    """
    if n_messages < 4:
        return False
    if n_messages < 8 and n_stored_facts < 2:
        return False
    return True


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _scrub_crystal(crystal, ledger: Optional[ForgetLedger]):
    """Remove the parts of a session Crystal that restate a forgotten fact or
    note a deletion. Returns the Crystal, or ``None`` when its narrative has
    nothing left (internal#605 / #640)."""
    has_entries = ledger is not None and len(ledger) > 0

    def _keep(text: str) -> bool:
        if is_forget_request(text):
            return False
        if not has_entries:
            return True
        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception:
            embedding = None
        return _restates_forgotten(text, embedding, ledger) is None

    sentences = [s for s in _SENTENCE_SPLIT.split(crystal.narrative.strip()) if s]
    kept = [s for s in sentences if _keep(s)]
    dropped = len(sentences) - len(kept)
    for name in ("key_outcomes", "open_threads", "lessons", "topics_discussed"):
        items = getattr(crystal, name, None) or []
        survivors = [i for i in items if _keep(i)]
        dropped += len(items) - len(survivors)
        setattr(crystal, name, survivors)
    if dropped:
        logger.info("Session Crystal: dropped %d part(s) about forgotten facts", dropped)
    if not kept:
        return None
    crystal.narrative = " ".join(kept)
    return crystal


def _is_session_crystal(fact: dict) -> bool:
    meta = fact.get("extra_metadata")
    return isinstance(meta, dict) and meta.get("subtype") == "session_crystal"


def _scrub_queued_crystal(fact: dict, ledger: Optional[ForgetLedger]) -> Optional[dict]:
    """A queued session Crystal with the parts the user forgot since it was
    queued removed, or ``None`` when nothing is left (#749).

    The metadata is rebuilt with ``Crystal.to_metadata``, the producer
    ``session_debrief`` uses, so the stored Crystal matches one written at
    session end."""
    meta = fact.get("extra_metadata") or {}

    def _list(name: str) -> list[str]:
        value = meta.get(name)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    crystal = Crystal(
        narrative=fact.get("text") or "",
        key_outcomes=_list("key_outcomes"),
        open_threads=_list("open_threads"),
        lessons=_list("lessons"),
        session_id=meta.get("session_id") or "",
        source_message_ids=_list("source_message_ids"),
        files_affected=_list("files_affected"),
        topics_discussed=_list("topics_discussed"),
    )
    crystal = _scrub_crystal(crystal, ledger)
    if crystal is None:
        return None
    return {**fact, "text": crystal.narrative, "extra_metadata": crystal.to_metadata()}


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

    if not crystal_gate_passes(len(all_messages), len(stored_fact_texts)):
        return []

    client = state.get_client()
    if not client:
        return []

    stored_fact_ids: list[str] = []
    try:
        crystal = run_sync(generate_crystal(all_messages, stored_fact_texts, host_type=host_type))
        if crystal and not all(
            auto_storable(t)
            for t in [crystal.narrative, *crystal.key_outcomes, *crystal.open_threads, *crystal.lessons]
        ):
            # PRD-04 DEP-4: the Crystal is written automatically, so it follows
            # the auto-extraction rule — skip it if any part is not ``allow``.
            logger.info("Session Crystal skipped: sensitive content (see pii log)")
            crystal = None
        if crystal:
            # The Crystal reads the whole conversation, including a fact the
            # user forgot mid-session (#605 / #640). Drop those parts.
            crystal = _scrub_crystal(crystal, _forget_ledger(client))
        if crystal:
            # Tag the Crystal with the active conversation's session_id so it
            # groups with the same conversation's atomic facts in a vault reader
            # (SPA). ``to_metadata()`` only emits session_id when non-empty, so
            # this is a no-op when there's no active session.
            if not crystal.session_id and state.session_id:
                crystal.session_id = state.session_id
            fields = {
                "importance": crystal.importance / 10.0,
                "fact_type": "summary",
                "provenance": "derived",
                "scope": "unspecified",
                "extra_metadata": crystal.to_metadata(),
            }
            try:
                fact_id = run_sync(
                    client.remember(crystal.narrative, source="hermes_debrief", **fields)
                )
                if fact_id:
                    stored_fact_ids.append(fact_id)
                    logger.info("Session Crystal stored (id=%s, importance=%d)", fact_id, crystal.importance)
            except InterpreterShutdownError:
                raise
            except RelayWriteQuotaExceeded as e:
                # #749: keep it for later, like refused auto-extracted facts.
                _handle_write_quota(
                    state, client, e, [{"text": crystal.narrative, **fields}],
                    what="the session Crystal",
                )
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
