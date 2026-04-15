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

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

EXTRACTION_RATIO = 2.5     # Average facts per conversation chunk (empirical)
SECONDS_PER_BATCH = 45     # Estimated seconds to process one batch
DEFAULT_BATCH_SIZE = 25    # Chunks per batch
CHUNK_SIZE = 20            # Messages per conversation chunk (matches adapters)
INTER_CHUNK_DELAY = 2.0    # Seconds between LLM extraction calls (rate-limit mitigation)


class ImportEngine:
    """Agent-agnostic batch import engine.

    Parameters
    ----------
    client : TotalReclaw
        A configured TotalReclaw client instance.
    llm_extract : callable, optional
        Async callable with signature::

            async (messages: list[dict], timestamp: str) -> list[dict]

        Each returned dict should have: ``text``, ``type``, ``importance`` (1-10).
        If not provided, conversation-based sources (ChatGPT, Claude, Gemini)
        will not be extractable -- only pre-structured sources (Mem0) will work.
    """

    def __init__(
        self,
        client,  # TotalReclaw instance
        llm_extract: Optional[Callable[..., Awaitable[list[dict]]]] = None,
    ):
        self._client = client
        self._llm_extract = llm_extract

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

        facts_extracted = 0
        facts_stored = 0
        errors: list[str] = []
        chunks_with_no_facts = 0
        extraction_failures = 0

        for i, chunk in enumerate(batch):
            # Rate-limit: add delay between LLM calls (skip before the first one)
            if i > 0:
                await asyncio.sleep(INTER_CHUNK_DELAY)

            try:
                extracted = await self._extract_from_chunk(chunk)
                if not extracted:
                    chunks_with_no_facts += 1
                facts_extracted += len(extracted)

                for fact in extracted:
                    try:
                        await self._store_fact(fact)
                        facts_stored += 1
                    except Exception as e:
                        msg = str(e)
                        # Content fingerprint dedup (409) is expected
                        if '409' in msg or 'duplicate' in msg or 'fingerprint' in msg:
                            logger.debug("Skipped duplicate: %s", fact.get('text', '')[:60])
                        else:
                            errors.append(f"Store failed: {msg}")
                            if len(errors) >= 20:
                                errors.append("Error limit reached (20). Remaining facts in this batch skipped.")
                                break
            except Exception as e:
                extraction_failures += 1
                errors.append(f"Extraction failed for chunk '{chunk.title}': {repr(e)}")

            if len(errors) >= 20:
                break

        # Surface extraction problems as warnings so the user gets feedback
        if extraction_failures > 0:
            errors.insert(0, f"{extraction_failures} chunk(s) failed during LLM extraction")
        if chunks_with_no_facts > 0 and facts_extracted == 0:
            errors.insert(0,
                f"All {chunks_processed} chunks produced 0 facts. "
                "This usually means LLM extraction calls failed (timeout or rate limit). "
                "Check logs for details or retry the import."
            )
        elif chunks_with_no_facts > 0:
            errors.append(f"{chunks_with_no_facts}/{chunks_processed} chunks produced 0 facts (possible LLM failures)")

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

        facts_stored = 0
        errors: list[str] = []

        for fact in batch:
            try:
                fact_dict = {
                    'text': fact.text,
                    'type': fact.type,
                    'importance': fact.importance,
                }
                await self._store_fact(fact_dict)
                facts_stored += 1
            except Exception as e:
                msg = str(e)
                if '409' in msg or 'duplicate' in msg or 'fingerprint' in msg:
                    logger.debug("Skipped duplicate: %s", fact.text[:60])
                else:
                    errors.append(f"Store failed for '{fact.text[:60]}': {msg}")
                    if len(errors) >= 20:
                        errors.append("Error limit reached (20). Remaining facts skipped.")
                        break

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

    async def _extract_from_chunk(self, chunk: ConversationChunk) -> list[dict]:
        """Call the llm_extract callable to extract facts from a conversation chunk.

        Converts the chunk's messages to the format expected by the extractor:
        [{"role": "user"|"assistant", "content": "..."}]
        """
        # Normalize message format (adapters use 'text', extractors use 'content')
        messages = [
            {"role": m.get("role", "user"), "content": m.get("text", m.get("content", ""))}
            for m in chunk.messages
        ]

        timestamp = chunk.timestamp or ""
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

    async def _store_fact(self, fact: dict) -> str:
        """Embed and store a single fact via the client.

        Returns the stored fact ID.
        """
        text = fact["text"]
        importance = fact.get("importance", 5)
        # Normalize importance from 1-10 to 0.0-1.0
        importance_normalized = max(0.0, min(1.0, importance / 10.0))

        # Try to generate embedding (optional -- client works without it)
        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception:
            pass

        return await self._client.remember(
            text,
            embedding=embedding,
            importance=importance_normalized,
            source="import",
        )


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.time() * 1000)
