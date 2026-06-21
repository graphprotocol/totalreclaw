"""
Agent-agnostic batch import engine for TotalReclaw.

Orchestrates the full import pipeline: parse -> batch -> extract -> embed -> store.

Any Python agent can use this engine. It requires:
  1. A ``TotalReclaw`` client instance (handles crypto, storage, embedding)
  2. An ``llm_extract`` callable for LLM-based fact extraction from conversation chunks

Usage::

    from totalreclaw.import_engine import ImportEngine
    from totalreclaw import TotalReclaw

    client = TotalReclaw(recovery_phrase="...")
    engine = ImportEngine(client=client, llm_extract=my_extraction_fn)

    # Dry run -- see what would be imported
    estimate = engine.estimate(source="gemini", file_path="/path/to/file.html")

    # Process one batch
    result = await engine.process_batch(
        source="gemini", file_path="/path/to/file.html",
        offset=0, batch_size=25,
    )
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict
from typing import Callable, Awaitable, Optional

from .import_adapters import (
    get_adapter,
    list_sources,
    AdapterParseResult,
    BatchImportResult,
    NormalizedFact,
    ConversationChunk,
)
from ._smart_import import (
    SmartImportContext,
    is_chunk_skipped,
    run_smart_import_pipeline,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

EXTRACTION_RATIO = 2.5     # Average facts per conversation chunk (empirical)
SECONDS_PER_BATCH = 45     # Estimated seconds to process one batch
DEFAULT_BATCH_SIZE = 25    # Chunks per batch
CHUNK_SIZE = 20            # Messages per conversation chunk (matches adapters)
INTER_CHUNK_DELAY = 2.0    # Seconds between LLM extraction calls (rate-limit mitigation)

# Max concurrent LLM extractions within one import batch (#392 Part 1). A
# semaphore bounds concurrency, not request rate, so INTER_CHUNK_DELAY pacing
# is retained inside each permit to avoid glm/zai 429s. Env-tunable
# (TOTALRECLAW_IMPORT_CONCURRENCY), conservative default. Clamped to [1, 10].
IMPORT_CONCURRENCY_DEFAULT = 4


def _import_concurrency() -> int:
    """Resolve import extraction concurrency from env, clamped to [1, 10]."""
    import os
    raw = os.environ.get("TOTALRECLAW_IMPORT_CONCURRENCY")
    if not raw:
        return IMPORT_CONCURRENCY_DEFAULT
    try:
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return IMPORT_CONCURRENCY_DEFAULT


# Maximum facts per batched UserOperation. Mirrors userop.MAX_BATCH_SIZE; kept
# local to avoid importing from userop here (consistent with lifecycle.py's
# _LIFECYCLE_MAX_BATCH). If the userop constant changes, update both.
IMPORT_MAX_BATCH_SIZE = 15

# Gnosis mainnet chain ID — the Pro-tier chain where batched UserOps are
# economically meaningful. Free-tier (Base Sepolia, 84532) falls back to
# per-fact submission per the spec §5 chain-gate. Per PRD-IMP, imports run
# Pro-only, so this gate evaluates true in practice; the explicit check keeps
# the cost claim self-verifying and tolerates future free-tier import flows.
_GNOSIS_CHAIN_ID = 100

# #356 — Crystal (session-summary) constants. One Crystal per imported
# conversation: a `type="summary"` claim whose metadata.subtype="session_crystal"
# ties it (+ the conversation's atomic facts) to a shared session_id. The SPA
# renders this as a session card headline; the atomic facts sit beneath.
_CRYSTAL_IMPORTANCE = 8           # anchored "high" per the v1 importance rubric
_METADATA_SUBTYPE_SESSION_CRYSTAL = "session_crystal"
# Valid v1 MemorySource for imported memories (the old "import" was not a valid
# MemorySource — see #356 Problem 2).
_IMPORT_PROVENANCE = "external"


def _uuid7() -> str:
    """Return a UUIDv7 (time-ordered) string.

    Python's stdlib has no ``uuid.uuid7`` before 3.14, so we build one: 48-bit
    big-endian Unix-ms timestamp, version nibble 7, RFC-4122 variant, the rest
    random. Time-ordered so a vault reader can sort sessions chronologically
    without a separate timestamp. Encrypted-blob-only — never on-chain.
    """
    import os
    import uuid

    unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    b = bytearray(unix_ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # RFC-4122 variant
    return str(uuid.UUID(bytes=bytes(b)))


def _as_str_list(v) -> list:
    """Coerce an LLM-returned value into a clean list[str] (≤8 entries)."""
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return out[:8]


def _derive_title_from_facts(facts: list[dict], max_len: int = 60) -> str:
    """Derive a Crystal title from the highest-importance extracted fact.

    This is the fallback used when the LLM is unavailable or returns bad JSON.
    Picks the fact with the highest importance score and truncates to max_len
    chars. The result is a specific, fact-grounded headline rather than a
    generic "Gemini session" or chunk title.
    """
    if not facts:
        return "Imported session"
    # Sort by importance descending; break ties by fact text length (prefer
    # longer, more specific descriptions over short generic ones).
    best = max(facts, key=lambda f: (f.get("importance", 5), len(f.get("text", ""))))
    text = (best.get("text") or "").strip()
    if not text:
        return "Imported session"
    if len(text) <= max_len:
        return text
    # Truncate at a word boundary
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return (truncated + "…") if truncated else text[:max_len]


def _extract_json_object(raw: str):
    """Best-effort parse of the first top-level JSON object in *raw*.

    LLMs often wrap JSON in prose / code fences; grab the outermost
    ``{ ... }`` and parse it. Returns the dict or None.
    """
    import json

    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


class ImportEngine:
    """Agent-agnostic batch import engine.

    Parameters
    ----------
    client : TotalReclaw
        A configured TotalReclaw client instance.
    llm_extract : callable, optional
        Async callable with signature::

            async (messages: list[dict], timestamp: str) -> list[dict]
            async (messages: list[dict], timestamp: str, *,
                   enriched_system_prompt: str | None) -> list[dict]

        Each returned dict should have: ``text``, ``type``, ``importance`` (1-10).
        If not provided, conversation-based sources (ChatGPT, Claude, Gemini)
        will not be extractable -- only pre-structured sources (Mem0) will work.
        Smart-import (imp-4) calls this callable with a profile-enriched
        system prompt via the ``enriched_system_prompt`` keyword; callables
        that don't accept the kwarg are still invoked (introspection
        fallback) so existing integrations keep working.
    llm_completion : callable, optional
        Async callable with signature ``async (prompt: str) -> str | None``.
        Used by the smart-import profile + triage passes to call the LLM
        directly with a self-contained prompt. When omitted, smart-import
        is disabled and the engine falls back to blind extraction (same
        behavior as core wheels < 2.2.0).
    enable_smart_import : bool, default True
        Master switch for the smart-import pipeline. Set to ``False`` to
        force blind extraction even when ``llm_completion`` is wired (used
        by tests + as a safety hatch).
    base_extraction_prompt : str, optional
        Extraction system prompt to enrich. When omitted, the engine
        resolves ``EXTRACTION_SYSTEM_PROMPT`` from
        :mod:`totalreclaw.agent.extraction` lazily so callers don't have
        to import it. Tests can inject a stub here to avoid the lazy
        import.
    """

    def __init__(
        self,
        client,  # TotalReclaw instance
        llm_extract: Optional[Callable[..., Awaitable[list[dict]]]] = None,
        *,
        llm_completion: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        enable_smart_import: bool = True,
        base_extraction_prompt: Optional[str] = None,
    ):
        self._client = client
        self._llm_extract = llm_extract
        self._llm_completion = llm_completion
        self._enable_smart_import = enable_smart_import
        self._base_extraction_prompt = base_extraction_prompt
        #: Cached smart-import context — built once per engine instance on
        #: the first ``process_batch`` call with chunk-based content. None
        #: until the pipeline has run; ``_smart_import_attempted`` is the
        #: sentinel that distinguishes "not yet tried" from "tried, fell
        #: back" (the latter caches a None so we don't retry per batch).
        self._smart_ctx: Optional[SmartImportContext] = None
        self._smart_import_attempted: bool = False
        #: Cached introspection of whether ``llm_extract`` accepts the
        #: ``enriched_system_prompt`` kwarg. Computed on first call.
        self._llm_extract_accepts_enriched: Optional[bool] = None
        #: Cached semantic session assignments: list of sessions, each a list of
        #: chunk indices. Computed once per ImportEngine instance on the first
        #: ``_process_chunk_batch`` call (embedding all chunks is expensive).
        #: None means "not yet computed".
        self._session_assignments: Optional[list[list[int]]] = None
        #: Cached turn counts per session (parallel to _session_assignments).
        #: Used to detect singleton sessions (< 2 turns = no Crystal).
        self._session_turn_counts: Optional[list[int]] = None
        #: Cached STABLE session_id per session (parallel to
        #: _session_assignments). Minted ONCE in _get_session_assignments and
        #: reused across every batch — so a session whose chunks straddle a
        #: batch boundary keeps a single session_id (was a per-batch regen bug).
        self._session_ids: Optional[list[str]] = None
        #: Cross-batch accumulation state for Crystal emission. A session can
        #: span multiple _process_chunk_batch calls; we accumulate its facts
        #: and only emit its Crystal once, on whichever batch completes it.
        #: chunk indices processed so far (across all batches):
        self._processed_chunk_indices: set[int] = set()
        #: session_id -> list of extracted facts seen so far across batches:
        self._session_facts_accum: dict[str, list[dict]] = {}
        #: session_ids whose Crystal has already been emitted (emit-once guard):
        self._crystallized_session_ids: set[str] = set()

    # ── Estimate ─────────────────────────────────────────────────────────

    def estimate(
        self,
        source: str,
        file_path: Optional[str] = None,
        content: Optional[str] = None,
    ) -> dict:
        """Parse the source file and return an import estimate (no side effects).

        Returns a dict with:
            source, total_chunks, total_facts (pre-structured), total_messages,
            estimated_facts, estimated_user_ops, estimated_minutes, batch_size,
            num_batches, warnings, errors, has_chunks, has_facts, source_metadata
        """
        adapter = get_adapter(source)
        parsed = adapter.parse(content=content, file_path=file_path)

        has_chunks = len(parsed.chunks) > 0
        has_facts = len(parsed.facts) > 0

        total_chunks = len(parsed.chunks)
        total_facts = len(parsed.facts)

        # Estimate how many facts the LLM will extract from chunks
        estimated_from_chunks = int(math.ceil(total_chunks * EXTRACTION_RATIO))
        estimated_facts = total_facts + estimated_from_chunks

        # How many items need processing in batches
        processable = total_chunks if has_chunks else total_facts
        num_batches = max(1, int(math.ceil(processable / DEFAULT_BATCH_SIZE)))
        estimated_minutes = round(num_batches * SECONDS_PER_BATCH / 60, 1)

        return {
            "source": source,
            "total_chunks": total_chunks,
            "total_facts": total_facts,
            "total_messages": parsed.total_messages,
            "estimated_facts": estimated_facts,
            "estimated_user_ops": estimated_facts,  # 1 UserOp per fact
            "estimated_minutes": estimated_minutes,
            "batch_size": DEFAULT_BATCH_SIZE,
            "num_batches": num_batches,
            "has_chunks": has_chunks,
            "has_facts": has_facts,
            "warnings": parsed.warnings,
            "errors": parsed.errors,
            "source_metadata": parsed.source_metadata,
        }

    # ── Process Batch ────────────────────────────────────────────────────

    async def process_batch(
        self,
        source: str,
        file_path: Optional[str] = None,
        content: Optional[str] = None,
        offset: int = 0,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> BatchImportResult:
        """Process one batch of an import.

        Parses the source, slices items at [offset:offset+batch_size],
        extracts facts (via LLM for chunks, directly for pre-structured),
        embeds them, and stores via client.remember().

        Call repeatedly with increasing offset until result.is_complete is True.
        """
        start_ms = _now_ms()
        errors: list[str] = []

        # Parse
        adapter = get_adapter(source)
        parsed = adapter.parse(content=content, file_path=file_path)

        if parsed.errors and not parsed.facts and not parsed.chunks:
            return BatchImportResult(
                success=False,
                batch_offset=offset,
                batch_size=batch_size,
                chunks_processed=0,
                total_chunks=0,
                facts_extracted=0,
                facts_stored=0,
                remaining_chunks=0,
                is_complete=True,
                errors=parsed.errors,
                duration_ms=_now_ms() - start_ms,
            )

        has_chunks = len(parsed.chunks) > 0

        if has_chunks:
            return await self._process_chunk_batch(
                parsed, offset, batch_size, start_ms, source,
            )
        else:
            return await self._process_fact_batch(
                parsed, offset, batch_size, start_ms, source,
            )

    # ── Internal: Chunk Batch (conversation-based sources) ───────────────

    async def _process_chunk_batch(
        self,
        parsed: AdapterParseResult,
        offset: int,
        batch_size: int,
        start_ms: int,
        source: str = "",
    ) -> BatchImportResult:
        """Process a batch of conversation chunks via LLM extraction with
        semantic session grouping.

        Semantic session grouping (feat/semantic-session-grouping):
        Instead of treating each chunk as its own session (the old #356 approach
        that created one Crystal per 30-min window), we now:

          1. Flatten ALL chunks (not just the current batch slice) into an ordered
             turn stream: each chunk's messages are paired as user→assistant turns
             with the chunk's timestamp for the time-gap check.
          2. Embed each turn's ``prompt + " " + reply`` text via the local Harrier
             model (L2-normalised 640d vectors — identical to the production
             recall path).
          3. Call ``segment_sessions(timestamps, embeddings, 1800, 0.55)`` to
             group turns into semantic sessions via centroid-walk segmentation.
          4. For each semantic session:
               - If it has ≥2 turns: mint one UUIDv7 ``session_id``, tag all
                 facts with it, and emit ONE Crystal.
               - If it has exactly 1 turn (singleton): tag facts with
                 ``source=external`` + ``import_source`` ONLY — no
                 ``session_id``, no Crystal. Singletons are typically standalone
                 one-off queries; emitting a Crystal for them would create noise.
          5. Segmentation runs once per ImportEngine instance and is cached.
             Store batching (Gnosis UserOps) is unaffected.

        The slice [offset:offset+batch_size] controls WHICH CHUNKS are extracted
        in this call (rate-limit / multi-call resume), but segment_sessions is
        computed over the FULL chunk list and its result is cached — so a session
        that spans multiple batches gets a single session_id regardless of how
        many process_batch() calls it takes to exhaust the chunks.
        """
        total_chunks = len(parsed.chunks)
        batch = parsed.chunks[offset:offset + batch_size]
        chunks_processed = len(batch)
        remaining = max(0, total_chunks - offset - chunks_processed)
        is_complete = (offset + chunks_processed) >= total_chunks

        if not batch:
            return BatchImportResult(
                success=True,
                batch_offset=offset,
                batch_size=batch_size,
                chunks_processed=0,
                total_chunks=total_chunks,
                facts_extracted=0,
                facts_stored=0,
                remaining_chunks=remaining,
                is_complete=True,
                duration_ms=_now_ms() - start_ms,
            )

        if not self._llm_extract:
            return BatchImportResult(
                success=False,
                batch_offset=offset,
                batch_size=batch_size,
                chunks_processed=0,
                total_chunks=total_chunks,
                facts_extracted=0,
                facts_stored=0,
                remaining_chunks=remaining,
                is_complete=True,
                errors=[
                    "No llm_extract callable provided. Conversation-based "
                    "sources require an LLM for fact extraction."
                ],
                duration_ms=_now_ms() - start_ms,
            )

        # Smart-import (imp-4): build profile + triage decisions from the
        # FULL chunks list so context spans the whole file. Runs once per
        # ImportEngine instance and is cached for later batches.
        smart_ctx = await self._maybe_run_smart_import(parsed.chunks)
        chunks_skipped = 0

        # ── Semantic session grouping ─────────────────────────────────────────
        # Compute (or reuse cached) session assignments for ALL chunks. Each
        # "turn" here is one chunk (the adapter's 30-min windows already enforce
        # coarse boundaries; the semantic step refines within them).
        # Within a chunk, turns are user→assistant pairs. For segmentation
        # purposes we treat each CHUNK as a single "turn" (embedding over the
        # combined messages). This is a conscious simplification: cross-chunk
        # semantic grouping is the primary value-add; within-chunk turn-level
        # segmentation would require re-parsing messages and is out of scope
        # for this pass.
        session_assignments = await self._get_session_assignments(parsed.chunks)

        # Build a map: chunk_global_index -> (session_id, is_singleton).
        # is_singleton is True when the session has < 2 TURNS (not chunks).
        # CRITICAL: session_ids come from the CACHED self._session_ids (minted
        # once in _get_session_assignments), NOT regenerated here — otherwise a
        # session straddling a batch boundary would get a different id per batch
        # and its facts would never group together. Same applies to turn counts.
        turn_counts = self._session_turn_counts or []
        session_ids = self._session_ids or []
        session_is_singleton: dict[int, bool] = {}  # chunk_index -> bool
        session_id_map: dict[int, str] = {}  # chunk_index -> session_id (shared)
        # chunk_index -> session index (into session_assignments), for accum keys
        chunk_to_session_idx: dict[int, int] = {}
        for sess_i, sess_chunk_indices in enumerate(session_assignments):
            sid = session_ids[sess_i] if sess_i < len(session_ids) else _uuid7()
            turn_count = turn_counts[sess_i] if sess_i < len(turn_counts) else len(sess_chunk_indices)
            singleton = turn_count < 2
            for idx in sess_chunk_indices:
                session_is_singleton[idx] = singleton
                session_id_map[idx] = sid
                chunk_to_session_idx[idx] = sess_i

        facts_extracted = 0
        errors: list[str] = []
        chunks_with_no_facts = 0
        extraction_failures = 0
        all_extracted: list[dict] = []
        # Per-chunk "0 facts" diagnostics (issue #389 follow-up).
        chunk_diagnostics: list[dict] = []
        reason_counts: dict[str, int] = {}

        # Extraction phase (#392 Part 1: concurrent). Two passes:
        #   1. Sequentially decide which chunks to extract (smart-import SKIP
        #      decisions are made up-front so chunks_skipped is right and
        #      skipped chunks never reach the LLM).
        #   2. Fire the LLM extractions concurrently under a bounded semaphore,
        #      then accumulate results IN CHUNK-INDEX ORDER (asyncio.gather
        #      preserves input order regardless of completion order), so the
        #      #376 diagnostics, session/Crystal tagging, and per-chunk error
        #      reporting behave exactly as the sequential version did.
        enriched = smart_ctx.enriched_system_prompt if smart_ctx else None
        to_extract: list[tuple[int, ConversationChunk]] = []
        for i, chunk in enumerate(batch):
            global_index = offset + i
            if smart_ctx is not None:
                skipped, reason = is_chunk_skipped(global_index, smart_ctx.decisions)
                if skipped:
                    logger.info(
                        "import: skipping chunk %d/%d: '%s' (%s)",
                        global_index + 1, total_chunks, chunk.title, reason,
                    )
                    chunks_skipped += 1
                    continue
            to_extract.append((global_index, chunk))

        concurrency = _import_concurrency()
        sem: Optional[asyncio.Semaphore] = asyncio.Semaphore(concurrency) if concurrency > 1 else None

        async def _extract_one(chunk: ConversationChunk):
            if sem is not None:
                # Concurrent: the semaphore bounds in-flight requests and
                # natural LLM latency paces the refill, so no artificial delay
                # (it would just serialize waves). glm/zai tolerate the small
                # concurrency burst; dial TOTALRECLAW_IMPORT_CONCURRENCY=1 to
                # fall back to the paced sequential path if a provider 429s.
                async with sem:
                    return await self._extract_from_chunk(chunk, enriched_system_prompt=enriched)
            # Sequential fallback (concurrency == 1): keep the original
            # inter-call pacing so a conservative setting still rate-limits.
            if INTER_CHUNK_DELAY > 0:
                await asyncio.sleep(INTER_CHUNK_DELAY)
            return await self._extract_from_chunk(chunk, enriched_system_prompt=enriched)

        # return_exceptions so one chunk's failure doesn't cancel the batch.
        raw_results = await asyncio.gather(
            *[_extract_one(chunk) for _, chunk in to_extract],
            return_exceptions=True,
        )

        # NOTE: the original sequential loop short-circuited at >=20 errors and
        # skipped remaining chunks' LLM calls. Under concurrency all calls have
        # already fired by this point, so the cap now only stops *accumulating*
        # later chunks' results (same stored-facts outcome at the cap).
        for (global_index, chunk), res in zip(to_extract, raw_results):
            if isinstance(res, BaseException):
                extraction_failures += 1
                errors.append(f"Extraction failed for chunk '{chunk.title}': {repr(res)}")
                if len(errors) >= 20:
                    break
                continue

            extracted, zero_reason = res
            if not extracted:
                chunks_with_no_facts += 1
                chunk_diagnostics.append({
                    "index": global_index,
                    "title": chunk.title,
                    "reason": zero_reason,
                })
                reason_counts[zero_reason] = reason_counts.get(zero_reason, 0) + 1
            facts_extracted += len(extracted)

            is_singleton = session_is_singleton.get(global_index, True)
            session_id = session_id_map.get(global_index)

            if is_singleton:
                # Singleton: external provenance + import_source, NO session_id,
                # NO Crystal. This matches the spec for standalone quick queries.
                for f in extracted:
                    f["provenance"] = _IMPORT_PROVENANCE
                    meta = f.get("extra_metadata")
                    if not isinstance(meta, dict):
                        meta = {}
                    if source:
                        meta["import_source"] = source
                    f["extra_metadata"] = meta
            else:
                # Multi-turn session: tag with the STABLE session_id +
                # provenance, and accumulate facts cross-batch so the
                # Crystal (emitted on the batch that completes the session)
                # summarizes the WHOLE session, not just this batch's slice.
                for f in extracted:
                    self._tag_import_fact(f, session_id, source)
                if session_id is not None:
                    self._session_facts_accum.setdefault(session_id, []).extend(extracted)

            all_extracted.extend(extracted)


        # Mark EVERY chunk in this batch slice as processed — including chunks
        # that were smart-import-skipped or that failed extraction. A chunk is
        # "processed" once we've passed over it; otherwise a session containing
        # a skipped chunk would never be considered complete and would never get
        # a Crystal. This set persists across batches on the instance.
        self._processed_chunk_indices.update(range(offset, offset + chunks_processed))

        # ── Crystal emission (cross-batch complete) ───────────────────────────
        # Emit ONE Crystal per multi-turn session, exactly once, on whichever
        # batch completes the session (i.e. once ALL of its chunks have been
        # processed across all batches so far). Facts are pulled from the
        # cross-batch accumulator so the Crystal summarizes the WHOLE session
        # even when its chunks straddled a batch boundary.
        turn_counts = self._session_turn_counts or []
        crystals_to_emit: list[dict] = []
        for sess_i, sess_chunk_indices in enumerate(session_assignments):
            # Singleton check uses TURN count (not chunk count): a session with
            # 1 chunk but 2+ turns (merged Gemini entries) is NOT a singleton.
            sess_turn_count = turn_counts[sess_i] if sess_i < len(turn_counts) else len(sess_chunk_indices)
            if sess_turn_count < 2:
                continue  # singleton — no Crystal

            sid = session_id_map.get(sess_chunk_indices[0]) if sess_chunk_indices else None
            if sid is None or sid in self._crystallized_session_ids:
                continue  # no id, or already crystallized on an earlier batch

            # Complete = all of this session's chunks processed (any batch).
            if not all(idx in self._processed_chunk_indices for idx in sess_chunk_indices):
                continue  # still waiting on chunks in a later batch

            sess_facts = self._session_facts_accum.get(sid, [])
            if not sess_facts:
                # Session complete but produced no facts (all extractions empty
                # or skipped). Mark crystallized so we don't retry it forever.
                self._crystallized_session_ids.add(sid)
                continue

            # Gather all chunks for this session for the Crystal prompt context.
            sess_chunks = [parsed.chunks[idx] for idx in sess_chunk_indices
                           if idx < len(parsed.chunks)]
            try:
                crystal = await self._make_crystal(sess_chunks, sess_facts, sid, source)
                if crystal is not None:
                    crystals_to_emit.append(crystal)
            except Exception as e:
                logger.debug("Crystal emission failed for session %s: %s", sid, e)
            finally:
                # Emit-once guard: mark crystallized whether or not _make_crystal
                # produced output, so we never double-emit on a later batch.
                self._crystallized_session_ids.add(sid)

        # Prepend Crystals so the SPA receives the session header before its facts.
        all_extracted = crystals_to_emit + all_extracted

        # Store phase: one batched UserOp per ≤15-fact chunk on Gnosis, or
        # per-fact remember() loop on free-tier / unresolvable chain.
        facts_stored, store_errors = await self._store_facts_chunked(all_extracted)
        if store_errors:
            errors.extend(store_errors)

        attempted = chunks_processed - chunks_skipped
        if extraction_failures > 0:
            errors.insert(0, f"{extraction_failures} chunk(s) failed during LLM extraction")
        zero_summary = _summarize_zero_fact_reasons(reason_counts)
        if attempted > 0 and chunks_with_no_facts >= attempted and facts_extracted == 0:
            errors.insert(0,
                f"All {attempted} extracted chunks produced 0 facts — {zero_summary}. "
                "Set TOTALRECLAW_LOG=DEBUG to log per-chunk extractor responses, "
                "or inspect chunk_diagnostics for which chunks failed and why."
            )
        elif chunks_with_no_facts > 0:
            errors.append(
                f"{chunks_with_no_facts}/{attempted} chunks produced 0 facts — {zero_summary}. "
                "Set TOTALRECLAW_LOG=DEBUG to log per-chunk extractor responses, "
                "or inspect chunk_diagnostics for which chunks failed and why."
            )

        smart_import_summary = None
        if smart_ctx is not None:
            smart_import_summary = {
                "extract_count": smart_ctx.extract_count,
                "skip_count": smart_ctx.skip_count,
                "profile_duration_ms": smart_ctx.duration_ms,
            }

        return BatchImportResult(
            success=facts_stored > 0 or (not errors and chunks_with_no_facts == 0),
            batch_offset=offset,
            batch_size=batch_size,
            chunks_processed=chunks_processed,
            total_chunks=total_chunks,
            facts_extracted=facts_extracted,
            facts_stored=facts_stored,
            remaining_chunks=remaining,
            is_complete=is_complete,
            errors=errors,
            duration_ms=_now_ms() - start_ms,
            chunks_skipped=chunks_skipped,
            smart_import=smart_import_summary,
            chunk_diagnostics=chunk_diagnostics or None,
        )

    # ── Internal: Fact Batch (pre-structured sources) ────────────────────

    async def _process_fact_batch(
        self,
        parsed: AdapterParseResult,
        offset: int,
        batch_size: int,
        start_ms: int,
        source: str = "",
    ) -> BatchImportResult:
        """Process a batch of pre-structured facts (Mem0, MCP Memory, etc.).

        #356: fact-based sources have no conversation boundaries, so (per the
        spec) we don't mint per-conversation sessions or Crystals here — but we
        still stamp external provenance + ``import_source`` so the SPA badges
        "Imported from <source>". Conversation sources (Crystals) are the
        priority and get the full treatment in ``_process_chunk_batch``.
        """
        total = len(parsed.facts)
        batch = parsed.facts[offset:offset + batch_size]
        processed = len(batch)
        remaining = max(0, total - offset - processed)
        is_complete = (offset + processed) >= total

        if not batch:
            return BatchImportResult(
                success=True,
                batch_offset=offset,
                batch_size=batch_size,
                chunks_processed=0,
                total_chunks=total,
                facts_extracted=0,
                facts_stored=0,
                remaining_chunks=remaining,
                is_complete=True,
                duration_ms=_now_ms() - start_ms,
            )

        # Convert NormalizedFact dataclass instances to the dict shape the
        # store helper expects. Routes through the chunked-batch helper:
        # one ≤15-fact remember_batch UserOp on Gnosis, per-fact remember()
        # everywhere else (spec §5).
        fact_dicts = [
            {
                'text': fact.text,
                'type': fact.type,
                'importance': fact.importance,
                # #356 — external provenance + provider badge (no session/Crystal
                # for flat fact sources).
                'provenance': _IMPORT_PROVENANCE,
                'extra_metadata': ({'import_source': source} if source else None),
            }
            for fact in batch
        ]
        facts_stored, errors = await self._store_facts_chunked(fact_dicts)

        return BatchImportResult(
            success=facts_stored > 0 or not errors,
            batch_offset=offset,
            batch_size=batch_size,
            chunks_processed=processed,
            total_chunks=total,
            facts_extracted=processed,
            facts_stored=facts_stored,
            remaining_chunks=remaining,
            is_complete=is_complete,
            errors=errors,
            duration_ms=_now_ms() - start_ms,
        )

    # ── Internal Helpers ─────────────────────────────────────────────────

    async def _extract_from_chunk(
        self,
        chunk: ConversationChunk,
        enriched_system_prompt: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Call the llm_extract callable to extract facts from a conversation chunk.

        Converts the chunk's messages to the format expected by the extractor:
        [{"role": "user"|"assistant", "content": "..."}]

        When ``enriched_system_prompt`` is provided AND the configured
        ``llm_extract`` callable accepts the ``enriched_system_prompt``
        kwarg, the prompt is forwarded so the LLM extraction uses the
        smart-import profile context (matches the plugin's call shape in
        ``extractFacts(messages, 'full', existing, enrichedSystemPrompt)``).
        Otherwise the kwarg is silently dropped so older callables keep
        working unchanged.

        Returns ``(valid_facts, reason)`` where ``reason`` is None when facts
        were produced, otherwise a ``REASON_*`` constant explaining why this
        chunk yielded nothing (issue #389 follow-up) — used to populate
        ``BatchImportResult.chunk_diagnostics``.
        """
        # Normalize message format (adapters use 'text', extractors use 'content')
        messages = [
            {"role": m.get("role", "user"), "content": m.get("text", m.get("content", ""))}
            for m in chunk.messages
        ]

        timestamp = chunk.timestamp or ""
        if enriched_system_prompt and self._extractor_accepts_enriched_kwarg():
            extracted = await self._llm_extract(
                messages, timestamp, enriched_system_prompt=enriched_system_prompt,
            )
        else:
            extracted = await self._llm_extract(messages, timestamp)

        # Validate and normalize extracted facts. Track why candidates are
        # dropped so an empty result can be classified (issue #389).
        valid: list[dict] = []
        dropped_text = 0
        dropped_importance = 0
        dropped_nondict = 0
        for item in (extracted or []):
            if not isinstance(item, dict):
                dropped_nondict += 1
                continue
            text = str(item.get("text", "")).strip()
            if len(text) < 5:
                dropped_text += 1
                continue

            fact_type = str(item.get("type", "fact"))
            valid_types = {"fact", "preference", "decision", "episodic", "goal", "context", "summary", "rule"}
            if fact_type not in valid_types:
                fact_type = "fact"

            importance = item.get("importance", 5)
            try:
                importance = max(1, min(10, int(importance)))
            except (ValueError, TypeError):
                importance = 5

            # Skip low-importance facts (same threshold as auto-extraction)
            if importance < 6:
                dropped_importance += 1
                continue

            valid.append({
                "text": text[:512],
                "type": fact_type,
                "importance": importance,
            })

        reason = _classify_zero_fact_reason(
            len(valid), len(extracted or []), dropped_text, dropped_importance, dropped_nondict,
        )
        return valid, reason

    # ── Smart-import helpers (imp-4) ────────────────────────────────────

    def _extractor_accepts_enriched_kwarg(self) -> bool:
        """Introspect ``self._llm_extract`` to detect ``enriched_system_prompt``.

        Cached on the engine instance so we pay the ``inspect`` cost once.
        Callables defined as ``async def fn(messages, timestamp, **kwargs)``
        also count — ``**kwargs`` swallows arbitrary kwargs, so we should
        forward in that case too.
        """
        if self._llm_extract_accepts_enriched is not None:
            return self._llm_extract_accepts_enriched

        accepts = False
        try:
            import inspect

            sig = inspect.signature(self._llm_extract)
            for param in sig.parameters.values():
                if param.name == "enriched_system_prompt":
                    accepts = True
                    break
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    accepts = True
                    break
        except (TypeError, ValueError):
            # Builtins / C-level callables / partials without a signature.
            # Safe default is "doesn't accept" so we keep the 2-arg shape.
            accepts = False

        self._llm_extract_accepts_enriched = accepts
        return accepts

    async def _maybe_run_smart_import(
        self,
        chunks: list[ConversationChunk],
    ) -> Optional[SmartImportContext]:
        """Build (or return cached) smart-import context for the current import.

        Runs once per ``ImportEngine`` instance. Subsequent batches reuse
        the cached context — a small optimisation over the plugin which
        rebuilds the profile on every batch call (the plugin keeps engine
        lifecycle short; in Hermes the engine survives the full background
        import task so caching is correct).
        """
        if self._smart_import_attempted:
            return self._smart_ctx
        self._smart_import_attempted = True

        if not self._enable_smart_import:
            logger.info("smart_import: disabled via enable_smart_import=False")
            return None

        if self._llm_completion is None:
            return None

        base_prompt = self._base_extraction_prompt
        if base_prompt is None:
            # Lazy import — avoids forcing the agent.extraction module load
            # path on callers that don't use smart-import (e.g. Mem0-only
            # fact imports, tests with a stub base_extraction_prompt).
            try:
                from totalreclaw.agent.extraction import EXTRACTION_SYSTEM_PROMPT
                base_prompt = EXTRACTION_SYSTEM_PROMPT
            except Exception as e:  # noqa: BLE001
                logger.info(
                    "smart_import: could not resolve EXTRACTION_SYSTEM_PROMPT (%s); "
                    "falling back to blind extraction", e,
                )
                return None

        self._smart_ctx = await run_smart_import_pipeline(
            chunks=chunks,
            llm_completion=self._llm_completion,
            base_extraction_prompt=base_prompt,
            logger_override=logger,
        )
        return self._smart_ctx

    async def _get_session_assignments(
        self,
        chunks: list,
    ) -> list[list[int]]:
        """Compute (or return cached) semantic session assignments for all chunks.

        TURN-BASED SEGMENTATION: We flatten all chunks into individual turns
        (user->assistant pairs) and run segment_sessions over turns. A "turn"
        here is defined as one user->assistant exchange. Within a chunk, pairs
        are formed from the messages list. The turn-level approach means:
          - A Gemini chunk with 4 messages (2 user+2 assistant) = 2 turns
          - Two 2-turn chunks close in time = 4 turns in potentially 1 session
          - A single-message chunk = 1 turn = singleton session (no Crystal)

        Returns: list of sessions, each session is a list of CHUNK indices
        (into the ``chunks`` parameter). Sessions that contain turns from
        multiple chunks map each chunk to the session containing its first turn.

        Caches the result on the instance for consistency across batches.
        Falls back to "one chunk = one session" if embedding fails.
        """
        if self._session_assignments is not None:
            return self._session_assignments

        if not chunks:
            self._session_assignments = []
            self._session_turn_counts = []
            self._session_ids = []
            return self._session_assignments

        from totalreclaw.session_segmentation import segment_sessions
        from datetime import datetime

        # ── Flatten chunks -> turns ─────────────────────────────────────────
        # Each turn = (chunk_index, timestamp, prompt_text, reply_text).
        # For segmentation the "turn text" = prompt + " " + reply.
        # For timestamp we use the chunk timestamp (all messages in a chunk
        # share the same 30-min window boundary — the adapter already broke
        # on time gaps; within a chunk turns are contiguous and we use the
        # chunk timestamp for all of them since per-message timestamps are
        # not available).
        turns: list[tuple[int, object, str]] = []  # (chunk_idx, ts, text)
        for chunk_idx, chunk in enumerate(chunks):
            ts_raw = getattr(chunk, "timestamp", None)
            ts_float = None
            if ts_raw:
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    ts_float = dt.timestamp()
                except Exception:
                    pass

            msgs = getattr(chunk, "messages", None) or []
            # Pair messages: user -> assistant. Skip unpaired trailing messages.
            i = 0
            while i < len(msgs):
                m = msgs[i]
                role = (m.get("role") or "user") if isinstance(m, dict) else "user"
                if role == "user":
                    text_u = (m.get("text") or m.get("content") or "") if isinstance(m, dict) else str(m)
                    text_a = ""
                    if i + 1 < len(msgs):
                        next_m = msgs[i + 1]
                        next_role = (next_m.get("role") or "assistant") if isinstance(next_m, dict) else "assistant"
                        if next_role == "assistant":
                            text_a = (next_m.get("text") or next_m.get("content") or "") if isinstance(next_m, dict) else str(next_m)
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                    combined = (text_u + " " + text_a).strip()[:1000]
                    turns.append((chunk_idx, ts_float, combined))
                else:
                    i += 1

            # If no user messages found, treat the whole chunk as a single turn
            if not any(t[0] == chunk_idx for t in turns):
                all_text = " ".join(
                    (m.get("text") or m.get("content") or "") if isinstance(m, dict) else str(m)
                    for m in msgs
                ).strip()[:1000]
                turns.append((chunk_idx, ts_float, all_text))

        if not turns:
            # All chunks are empty — one-chunk-per-session fallback.
            self._session_assignments = [[i] for i in range(len(chunks))]
            self._session_turn_counts = [1 for _ in range(len(chunks))]
            self._session_ids = [_uuid7() for _ in self._session_assignments]
            return self._session_assignments

        # ── Embed turns ─────────────────────────────────────────────────────
        timestamps = [t[1] for t in turns]
        embeddings = []
        for _, _, text in turns:
            try:
                from totalreclaw.embedding import get_embedding
                emb = get_embedding(text) if text else [0.0] * 640
            except Exception:
                emb = [1.0] + [0.0] * 639
            embeddings.append(emb)

        # ── Segment turns -> sessions of turns ──────────────────────────────
        try:
            turn_sessions = segment_sessions(
                timestamps, embeddings,
                gap_seconds=1800,
                sim_threshold=0.55,
            )
        except Exception as e:
            logger.warning("session_segmentation failed (%s); falling back to one-chunk-per-session", e)
            self._session_assignments = [[i] for i in range(len(chunks))]
            self._session_turn_counts = [1 for _ in range(len(chunks))]
            self._session_ids = [_uuid7() for _ in self._session_assignments]
            return self._session_assignments

        # ── Map turn-level sessions to chunk-level sessions ──────────────────
        # A chunk belongs to the session that contains its FIRST turn.
        # We also track the TOTAL number of turns per session for the
        # singleton check (< 2 turns = singleton, no Crystal).
        chunk_to_session: dict[int, int] = {}
        session_turn_counts: list[int] = []
        for sess_idx, turn_indices in enumerate(turn_sessions):
            session_turn_counts.append(len(turn_indices))
            for tidx in turn_indices:
                chunk_idx = turns[tidx][0]
                if chunk_idx not in chunk_to_session:
                    chunk_to_session[chunk_idx] = sess_idx

        # Rebuild as list of chunk-index lists (one per turn-level session).
        # Sessions are ordered by first-appearing chunk.
        session_chunks: list[list[int]] = [[] for _ in turn_sessions]
        for chunk_idx in range(len(chunks)):
            sess_idx = chunk_to_session.get(chunk_idx)
            if sess_idx is None:
                # Chunk had no turns at all — append as own session.
                session_chunks.append([chunk_idx])
                session_turn_counts.append(0)
            else:
                if chunk_idx not in session_chunks[sess_idx]:
                    session_chunks[sess_idx].append(chunk_idx)

        # Remove empty sessions (can happen if a turn-session mapped to 0 chunks).
        self._session_assignments = [s for s in session_chunks if s]

        # Attach turn counts to each session for the singleton check in
        # _process_chunk_batch. We store it separately on the instance.
        self._session_turn_counts: list[int] = []
        for s in self._session_assignments:
            # Sum turns from all turn_sessions that contain chunks of this session.
            total_turns = sum(
                session_turn_counts[sess_idx]
                for sess_idx, tc in enumerate(turn_sessions)
                if any(turns[tidx][0] in s for tidx in tc)
            )
            self._session_turn_counts.append(total_turns)

        # Mint a STABLE session_id per session ONCE here. Reused across every
        # batch so cross-batch sessions keep one id (and one Crystal).
        self._session_ids = [_uuid7() for _ in self._session_assignments]

        logger.debug(
            "session_segmentation: %d chunks -> %d turn-sessions -> %d sessions "
            "(%d singletons)",
            len(chunks),
            len(turn_sessions),
            len(self._session_assignments),
            sum(1 for s in self._session_assignments
                if self._session_turn_counts[self._session_assignments.index(s)] < 2),
        )
        return self._session_assignments

    @staticmethod
    def _prepare_fact_payload(fact: dict) -> dict:
        """Build the per-fact payload shape shared by ``client.remember`` and
        ``client.remember_batch``.

        Normalises importance from the 1-10 input scale to the 0.0-1.0 scale
        the client expects, and best-effort attaches an embedding.
        """
        text = fact["text"]
        importance = fact.get("importance", 5)
        importance_normalized = max(0.0, min(1.0, importance / 10.0))

        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception:
            pass

        payload = {
            "text": text,
            "importance": importance_normalized,
            "embedding": embedding,
        }
        # #356 — carry v1 provenance + typed metadata (session_id, import_source,
        # Crystal fields) through to the canonical claim. Imports set
        # provenance="external"; the engine attaches per-conversation session_id +
        # import_source in extra_metadata. Only include keys that are present so
        # non-import callers are unaffected.
        if fact.get("provenance"):
            payload["provenance"] = fact["provenance"]
        if fact.get("fact_type"):
            payload["fact_type"] = fact["fact_type"]
        if fact.get("extra_metadata"):
            payload["extra_metadata"] = fact["extra_metadata"]
        return payload

    @staticmethod
    def _tag_import_fact(fact: dict, session_id: str, source: str) -> None:
        """Stamp an imported atomic fact (in place) with v1 external provenance
        + session/provider metadata (#356)."""
        fact["provenance"] = _IMPORT_PROVENANCE
        meta = fact.get("extra_metadata")
        if not isinstance(meta, dict):
            meta = {}
        meta["session_id"] = session_id
        if source:
            meta["import_source"] = source
        fact["extra_metadata"] = meta

    @staticmethod
    def _crystal_prompt(chunk, facts: list[dict]) -> str:
        """Build the one-shot summary prompt for a conversation's Crystal.

        The LLM is instructed to return a JSON object with BOTH a ``title``
        (short human-readable headline, <=60 chars) and a ``summary`` (<=200-char
        gist). The title is generated from the GROUPED FACTS so the headline
        reflects the actual substance of the session, not just the raw transcript.

        Pedro's explicit request (2026-06-12): title must be derived from the
        session's grouped facts, not just the transcript. The prompt provides
        both the facts (primary signal) and the transcript (context) so the LLM
        can generate a focused headline like "Buying a Kia EV — range, pricing,
        financing" rather than a vague summary of the conversation flow.
        """
        msgs = getattr(chunk, "messages", None) or []
        # Cap the transcript we feed the summarizer (first + last few turns is
        # plenty to characterize a conversation; keeps tokens bounded).
        def _msg_text(m):
            if isinstance(m, dict):
                return f"{m.get('role', 'user')}: {(m.get('text') or m.get('content') or '')}"
            return str(m)
        head = [_msg_text(m) for m in msgs[:6]]
        tail = [_msg_text(m) for m in msgs[-4:]] if len(msgs) > 6 else []
        transcript = "\n".join(head + (["..."] + tail if tail else []))[:4000]
        # Facts listed first — they are the PRIMARY signal for the title.
        # Sort by importance descending so the highest-value facts anchor the title.
        sorted_facts = sorted(facts, key=lambda f: f.get("importance", 5), reverse=True)
        fact_lines = "\n".join(
            f"- [{f.get('type', 'fact')}] (importance={f.get('importance', 5)}) {f.get('text', '')}"
            for f in sorted_facts[:20]
        )[:2000]
        return (
            "You are given the EXTRACTED FACTS from a conversation plus the conversation "
            "transcript as context. Generate a compact JSON object summarizing the session.\n"
            "Return ONLY JSON, no prose. Schema:\n"
            '{"title": "<=60-char human headline, e.g. \'Buying a Kia EV — range, pricing, financing\' "' + ', '
            '"summary": "<=200-char one-line gist of what was actually discussed", '
            '"key_outcomes": ["decisions or results"], '
            '"open_threads": ["unresolved follow-ups"], '
            '"topics_discussed": ["short topic tags"]}\n\n'
            f"Extracted facts (primary signal for the title):\n{fact_lines}\n\n"
            f"Conversation transcript (context):\n{transcript}\n"
        )

    async def _make_crystal(
        self, session_chunks, facts: list[dict], session_id: str, source: str
    ) -> Optional[dict]:
        """Build one Crystal (session-summary) claim for an imported session.

        Uses ``self._llm_completion`` for a SINGLE call that returns BOTH a
        ``title`` (short human-readable headline, <=60 chars) and a ``summary``
        (<=200-char gist).  The title is the Crystal's primary text — it's what
        the SPA renders as the session-card headline in VaultRow.

        Title selection:
          1. LLM returns ``{"title": "...", "summary": "..."}`` — use ``title``
             as Crystal text AND store it in ``metadata["session_title"]``.
          2. LLM fails (no LLM, JSON parse error, or empty title field) — derive
             the title from the highest-importance extracted fact (truncated to
             ~60 chars).  Never fall back to the raw chunk title or the generic
             "Gemini session" / "Imported conversation" string.

        When no ``llm_completion`` is wired we skip the Crystal entirely rather
        than emit a low-value synthetic (the atomic facts still carry the shared
        session_id + provenance, so they remain grouped). "One summary call
        per conversation" per #356.
        """
        if self._llm_completion is None:
            return None

        # Build a representative "chunk" for the prompt from all chunks in the
        # session (combines messages from every chunk for transcript context).
        class _SessionProxy:
            def __init__(self, chunks_list):
                self.title = None
                self.messages = []
                for c in chunks_list:
                    self.messages.extend(getattr(c, "messages", None) or [])

        proxy_chunk = _SessionProxy(session_chunks)

        crystal_title: Optional[str] = None
        summary_text: Optional[str] = None
        key_outcomes: list = []
        open_threads: list = []
        topics: list = []

        try:
            raw = await self._llm_completion(self._crystal_prompt(proxy_chunk, facts))
            data = _extract_json_object(raw) if raw else None
            if isinstance(data, dict):
                t = (data.get("title") or "").strip()
                crystal_title = t[:60] if t else None
                s = (data.get("summary") or "").strip()
                summary_text = s or None
                key_outcomes = _as_str_list(data.get("key_outcomes"))
                open_threads = _as_str_list(data.get("open_threads"))
                topics = _as_str_list(data.get("topics_discussed"))
        except Exception as e:  # never let a Crystal failure break the import
            logger.debug("crystal summary LLM call failed: %s", e)

        # Fallback: derive title from the highest-importance extracted fact
        if not crystal_title:
            crystal_title = _derive_title_from_facts(facts)

        # The Crystal's text IS the title (what the SPA renders as the card
        # headline in VaultRow -> item.claim.text). summary is stored in metadata.
        meta: dict = {
            "subtype": _METADATA_SUBTYPE_SESSION_CRYSTAL,
            "session_id": session_id,
            "session_title": crystal_title,
        }
        if summary_text:
            meta["session_summary"] = summary_text
        if source:
            meta["import_source"] = source
        if key_outcomes:
            meta["key_outcomes"] = key_outcomes
        if open_threads:
            meta["open_threads"] = open_threads
        if topics:
            meta["topics_discussed"] = topics

        return {
            "text": crystal_title[:512],
            "importance": _CRYSTAL_IMPORTANCE,
            "fact_type": "summary",
            "provenance": _IMPORT_PROVENANCE,
            "extra_metadata": meta,
        }

    async def _store_fact(self, fact: dict) -> str:
        """Embed and store a single fact via the client.

        Returns the stored fact ID.
        """
        payload = self._prepare_fact_payload(fact)
        return await self._client.remember(
            payload["text"],
            embedding=payload["embedding"],
            importance=payload["importance"],
            source="import",
            # #356 — v1 provenance + typed metadata (session_id, import_source,
            # Crystal fields). Defaults preserve prior behavior for callers that
            # don't set them.
            provenance=payload.get("provenance", "user"),
            fact_type=payload.get("fact_type", "claim"),
            extra_metadata=payload.get("extra_metadata"),
        )

    async def _resolve_chain_id_safely(self) -> Optional[int]:
        """Resolve ``client.chain_id`` defensively.

        Returns the resolved chain id, or ``None`` if resolution fails (test
        clients without a real ``_ensure_chain_id`` coroutine, offline runs,
        etc.). Callers must treat ``None`` as "not Gnosis" and use the
        per-fact fallback path.
        """
        try:
            return await self._client._ensure_chain_id()
        except Exception:
            return None

    async def _store_facts_chunked(
        self,
        facts: list[dict],
    ) -> tuple[int, list[str]]:
        """Store ``facts`` via chunked batched UserOps when the client is on
        Gnosis (chain 100); otherwise via per-fact ``client.remember`` calls.

        Per spec ``docs/specs/imp/281-gnosis-batching-chain-gate.md`` §5 +
        decomposition imp-11: buffer facts into groups of ≤15 and submit one
        ``client.remember_batch`` per group on chain 100. PRD-IMP's Pro-only
        import gate guarantees ``chain_id == 100`` on this code path; the
        explicit check keeps the cost claim self-verifying.

        Returns ``(facts_stored, errors)`` so callers can aggregate into the
        existing ``BatchImportResult`` shape.

        Dedup (HTTP 409 / fingerprint) is logged at DEBUG and not counted as
        an error. Other failures are surfaced via the returned error list,
        capped at 20 entries to match the per-fact loop's contract.
        """
        if not facts:
            return 0, []

        chain_id = await self._resolve_chain_id_safely()
        errors: list[str] = []
        facts_stored = 0

        if chain_id == _GNOSIS_CHAIN_ID:
            for chunk_start in range(0, len(facts), IMPORT_MAX_BATCH_SIZE):
                chunk = facts[chunk_start:chunk_start + IMPORT_MAX_BATCH_SIZE]
                payloads = [self._prepare_fact_payload(f) for f in chunk]
                try:
                    ids = await self._client.remember_batch(payloads, source="import")
                    facts_stored += len(ids)
                    if len(ids) < len(chunk):
                        logger.debug(
                            "remember_batch returned %d ids for %d-fact chunk "
                            "(likely fingerprint dedup of subset)",
                            len(ids), len(chunk),
                        )
                except Exception as e:
                    msg = str(e)
                    if '409' in msg or 'duplicate' in msg or 'fingerprint' in msg:
                        logger.debug(
                            "Batch of %d facts rejected as duplicate", len(chunk),
                        )
                    else:
                        errors.append(
                            f"Batch store failed ({len(chunk)} facts): {msg}"
                        )
                        if len(errors) >= 20:
                            errors.append(
                                "Error limit reached (20). Remaining facts in this batch skipped."
                            )
                            break
            return facts_stored, errors

        # Per-fact fallback: free-tier / non-Gnosis / chain probe failed.
        for fact in facts:
            try:
                await self._store_fact(fact)
                facts_stored += 1
            except Exception as e:
                msg = str(e)
                if '409' in msg or 'duplicate' in msg or 'fingerprint' in msg:
                    logger.debug("Skipped duplicate: %s", fact.get('text', '')[:60])
                else:
                    errors.append(f"Store failed: {msg}")
                    if len(errors) >= 20:
                        errors.append(
                            "Error limit reached (20). Remaining facts in this batch skipped."
                        )
                        break

        return facts_stored, errors


# ── Per-chunk "0 facts" diagnostics (issue #389 follow-up) ──────────────
# When a chunk produces no storable facts, classify *why* so the aggregate
# message + BatchImportResult.chunk_diagnostics can name each failing chunk
# instead of the old vague "(possible LLM failures)" line. Engine-only: we
# cannot split empty-LLM-response from parse-failure without widening the
# llm_extract contract, so both collapse to REASON_EXTRACTOR_EMPTY
# (TOTALRECLAW_LOG=DEBUG still sub-splits them inside the extractor).
REASON_EXTRACTOR_EMPTY = "extractor_empty"
REASON_FILTERED_IMPORTANCE = "filtered_importance"
REASON_FILTERED_TEXT = "filtered_text"
REASON_FILTERED = "filtered"
REASON_MALFORMED = "malformed"


def _classify_zero_fact_reason(
    valid_count: int,
    raw_count: int,
    dropped_text: int,
    dropped_importance: int,
    dropped_nondict: int,
) -> Optional[str]:
    """Classify why a chunk produced 0 storable facts.

    Returns None when the chunk actually yielded facts (no diagnostic). Buckets:
    extractor returned nothing; all candidates filtered for importance < 6; all
    filtered for text < 5 chars; all items malformed (non-dict); or a mix.
    """
    if valid_count > 0:
        return None
    if raw_count == 0:
        return REASON_EXTRACTOR_EMPTY
    # Every item was a non-dict (malformed extractor output) — distinct from a
    # fact that failed a threshold. Only triggers when nothing else was dropped.
    if dropped_nondict and not dropped_text and not dropped_importance:
        return REASON_MALFORMED
    if dropped_importance and not dropped_text:
        return REASON_FILTERED_IMPORTANCE
    if dropped_text and not dropped_importance:
        return REASON_FILTERED_TEXT
    return REASON_FILTERED


def _summarize_zero_fact_reasons(reason_counts: dict) -> str:
    """Human-readable "N <reason>" summary, joined by '; '. 'unknown' if empty."""
    parts: list[str] = []
    for reason, label in (
        (REASON_EXTRACTOR_EMPTY, "extractor returned no facts"),
        (REASON_FILTERED_IMPORTANCE, "filtered below importance (<6)"),
        (REASON_FILTERED_TEXT, "filtered (text <5 chars)"),
        (REASON_MALFORMED, "extractor returned malformed (non-dict) output"),
        (REASON_FILTERED, "filtered (other)"),
    ):
        n = reason_counts.get(reason, 0)
        if n:
            parts.append(f"{n} {label}")
    return "; ".join(parts) if parts else "unknown"


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.time() * 1000)
