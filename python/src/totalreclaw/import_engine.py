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
                parsed, offset, batch_size, start_ms,
            )
        else:
            return await self._process_fact_batch(
                parsed, offset, batch_size, start_ms,
            )

    # ── Internal: Chunk Batch (conversation-based sources) ───────────────

    async def _process_chunk_batch(
        self,
        parsed: AdapterParseResult,
        offset: int,
        batch_size: int,
        start_ms: int,
    ) -> BatchImportResult:
        """Process a batch of conversation chunks via LLM extraction."""
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
        # FULL chunks list (not just this batch slice) so context spans the
        # whole file. Runs once per ImportEngine instance and is cached for
        # later batches. Falls back gracefully when llm_completion is None,
        # core lacks bindings, or any step fails.
        smart_ctx = await self._maybe_run_smart_import(parsed.chunks)
        chunks_skipped = 0

        facts_extracted = 0
        errors: list[str] = []
        chunks_with_no_facts = 0
        extraction_failures = 0
        all_extracted: list[dict] = []

        # Extraction phase: collect facts from every chunk in the batch so the
        # subsequent store phase can chunk across chunks into Gnosis-batched
        # UserOps (spec §5). Per-chunk granularity is preserved for extraction
        # errors and "no facts produced" warnings.
        extracted_chunk_count = 0
        for i, chunk in enumerate(batch):
            global_index = offset + i

            # Smart-import: skip chunks classified as SKIP by triage. We
            # still count them toward chunks_processed (matches plugin
            # behavior in handleBatchImport at index.ts:2773-2778) but they
            # never reach the LLM extractor.
            if smart_ctx is not None:
                skipped, reason = is_chunk_skipped(global_index, smart_ctx.decisions)
                if skipped:
                    logger.info(
                        "import: skipping chunk %d/%d: '%s' (%s)",
                        global_index + 1, total_chunks, chunk.title, reason,
                    )
                    chunks_skipped += 1
                    continue

            # Rate-limit: add delay between LLM calls (skip before the first
            # actually-extracted call so triaged-out chunks don't waste time).
            if extracted_chunk_count > 0:
                await asyncio.sleep(INTER_CHUNK_DELAY)
            extracted_chunk_count += 1

            try:
                extracted = await self._extract_from_chunk(
                    chunk,
                    enriched_system_prompt=(
                        smart_ctx.enriched_system_prompt if smart_ctx else None
                    ),
                )
                if not extracted:
                    chunks_with_no_facts += 1
                facts_extracted += len(extracted)
                all_extracted.extend(extracted)
            except Exception as e:
                extraction_failures += 1
                errors.append(f"Extraction failed for chunk '{chunk.title}': {repr(e)}")

            if len(errors) >= 20:
                break

        # Store phase: one batched UserOp per ≤15-fact chunk on Gnosis, or a
        # per-fact remember() loop on free-tier / unresolvable chain.
        facts_stored, store_errors = await self._store_facts_chunked(all_extracted)
        if store_errors:
            errors.extend(store_errors)

        # Surface extraction problems as warnings so the user gets feedback.
        # Triaged-out chunks aren't "0-fact failures" so we subtract
        # chunks_skipped from the denominator before deciding whether to
        # raise the "all chunks produced 0 facts" alarm (otherwise a clean
        # all-SKIP batch would look like a silent LLM failure).
        attempted = chunks_processed - chunks_skipped
        if extraction_failures > 0:
            errors.insert(0, f"{extraction_failures} chunk(s) failed during LLM extraction")
        if attempted > 0 and chunks_with_no_facts >= attempted and facts_extracted == 0:
            errors.insert(0,
                f"All {attempted} extracted chunks produced 0 facts. "
                "This usually means LLM extraction calls failed (timeout or rate limit). "
                "Check logs for details or retry the import."
            )
        elif chunks_with_no_facts > 0:
            errors.append(
                f"{chunks_with_no_facts}/{attempted} chunks produced 0 facts (possible LLM failures)"
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
        )

    # ── Internal: Fact Batch (pre-structured sources) ────────────────────

    async def _process_fact_batch(
        self,
        parsed: AdapterParseResult,
        offset: int,
        batch_size: int,
        start_ms: int,
    ) -> BatchImportResult:
        """Process a batch of pre-structured facts (Mem0, MCP Memory, etc.)."""
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
    ) -> list[dict]:
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

        # Validate and normalize extracted facts
        valid: list[dict] = []
        for item in (extracted or []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if len(text) < 5:
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
                continue

            valid.append({
                "text": text[:512],
                "type": fact_type,
                "importance": importance,
            })

        return valid

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

        return {
            "text": text,
            "importance": importance_normalized,
            "embedding": embedding,
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


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.time() * 1000)
