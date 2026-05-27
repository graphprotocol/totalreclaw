"""Smart-import two-pass pipeline for Python imports.

Mirrors ``runSmartImportPipeline`` in ``skill/plugin/index.ts`` (TS plugin).
All prompt construction and response parsing is delegated to
``totalreclaw_core`` PyO3 bindings so the Python pipeline is byte-equivalent
to the plugin pipeline by construction — same Rust code on both sides.

The orchestration runs as:

  1. ``chunks_to_summaries``        — extract first+last messages per chunk
  2. ``build_profile_batch_prompt`` — per-batch profile, LLM call,
     ``parse_profile_batch_response`` — produces a ``PartialProfile``
  3. ``build_profile_merge_prompt`` — merge N partial profiles (if N>1),
     LLM call, ``parse_profile_response`` — produces a ``UserProfile``
  4. ``build_triage_prompt``        — per-batch EXTRACT/SKIP classification,
     ``parse_triage_response``       — produces ``ChunkDecision[]``
  5. ``enrich_extraction_prompt``   — inject profile context into the base
     extraction prompt; downstream extractor uses the enriched prompt.

All JSON crossings (Python ↔ Rust) happen through the PyO3 ``str`` boundary.

This module is intentionally framework-agnostic: it takes a chunks list and
an async ``llm_completion`` callback. The caller (``ImportEngine``) wires it
to its own LLM config so profile + triage calls reuse the same model the
agent uses for chat.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: Batch size for profile + triage LLM calls. Matches the plugin (50).
PROFILE_BATCH_SIZE = 50
TRIAGE_BATCH_SIZE = 50


@dataclass
class SmartImportContext:
    """Output of the smart-import pipeline; consumed by ``ImportEngine``."""

    #: JSON-serialized ``UserProfile`` (passed back into core for triage/enrich).
    profile_json: str
    #: Triage decisions keyed by chunk_index. Each entry has
    #: ``chunk_index`` (int), ``decision`` ("EXTRACT" | "SKIP"), ``reason`` (str).
    decisions: list[dict] = field(default_factory=list)
    #: Extraction system prompt with profile context prepended.
    enriched_system_prompt: str = ""
    #: Count of chunks marked EXTRACT (or with no decision — defaults to EXTRACT).
    extract_count: int = 0
    #: Count of chunks marked SKIP.
    skip_count: int = 0
    #: Wall-clock duration of the pipeline in ms.
    duration_ms: int = 0


def is_chunk_skipped(chunk_index: int, decisions: list[dict]) -> tuple[bool, str]:
    """Return ``(skipped, reason)`` for a chunk index.

    Defaults to EXTRACT (safe default) when no decision exists for the
    chunk. Mirrors the plugin's ``isChunkSkipped``.
    """
    for d in decisions:
        if d.get("chunk_index") == chunk_index and d.get("decision") == "SKIP":
            return True, str(d.get("reason") or "triage: skip")
    return False, ""


def _has_core_smart_import_support() -> bool:
    """Probe ``totalreclaw_core`` for the 8 smart-import PyO3 bindings.

    Older core wheels (<2.2.0) may not expose these. We return ``False`` so
    the caller can fall back to blind extraction without raising.
    """
    try:
        import totalreclaw_core  # noqa: F401  (probe import only)
    except Exception:
        return False
    required = (
        "chunks_to_summaries",
        "build_profile_batch_prompt",
        "parse_profile_batch_response",
        "build_profile_merge_prompt",
        "parse_profile_response",
        "build_triage_prompt",
        "parse_triage_response",
        "enrich_extraction_prompt",
    )
    return all(hasattr(totalreclaw_core, name) for name in required)


def _chunks_to_core_payload(chunks: list) -> list[dict]:
    """Serialize ``ConversationChunk`` objects into the Rust shape.

    Core expects ``[{index, title, messages: [{role, content}], timestamp}]``.
    The Python ``ConversationChunk`` adapter type uses ``messages[*].text``
    (matching the TS plugin); we rename to ``content`` for the WASM/PyO3
    boundary, matching what the plugin's ``runSmartImportPipeline`` sends.
    """
    payload: list[dict] = []
    for i, chunk in enumerate(chunks):
        messages_out = []
        for m in chunk.messages:
            # ConversationChunk.messages entries are plain dicts (see
            # import_adapters/types.py); accept both 'text' (canonical) and
            # 'content' (defensive — extractors may have already normalized).
            content = m.get("text") if isinstance(m, dict) else None
            if content is None and isinstance(m, dict):
                content = m.get("content", "")
            messages_out.append({
                "role": (m.get("role", "user") if isinstance(m, dict) else "user"),
                "content": content or "",
            })
        payload.append({
            "index": i,
            "title": chunk.title or "Untitled",
            "messages": messages_out,
            "timestamp": chunk.timestamp,
        })
    return payload


async def run_smart_import_pipeline(
    chunks: list,
    llm_completion: Optional[Callable[[str], Awaitable[Optional[str]]]],
    base_extraction_prompt: str,
    logger_override: Optional[logging.Logger] = None,
) -> Optional[SmartImportContext]:
    """Run the smart-import pipeline.

    Returns ``None`` if smart-import is unavailable (no LLM, no chunks,
    core lacks bindings, or any step errors) so the caller can fall back
    to blind extraction. Never raises.

    Parameters
    ----------
    chunks
        List of ``ConversationChunk`` adapter objects (the full chunks list,
        not a slice — profile is built from the whole file for context).
    llm_completion
        Async callable: ``(prompt: str) -> Optional[str]``. Receives a
        single user prompt and returns the model's text completion. Should
        already encode any system instructions if the model needs them
        (smart-import prompts are self-contained per
        ``smart_import.rs``).
    base_extraction_prompt
        The extraction system prompt to enrich. Typically
        ``EXTRACTION_SYSTEM_PROMPT`` from ``agent.extraction``.
    """
    log = logger_override or logger

    if not chunks:
        log.info("smart_import: no chunks provided; falling back to blind extraction")
        return None
    if llm_completion is None:
        log.info("smart_import: no llm_completion callable; falling back to blind extraction")
        return None
    if not _has_core_smart_import_support():
        log.info(
            "smart_import: totalreclaw_core lacks smart-import bindings; "
            "falling back to blind extraction"
        )
        return None

    import totalreclaw_core

    pipeline_start = time.time()

    try:
        # Step 0: chunks -> summaries (first + last message per chunk)
        core_chunks = _chunks_to_core_payload(chunks)
        summaries_json = totalreclaw_core.chunks_to_summaries(json.dumps(core_chunks))
        summaries = json.loads(summaries_json)

        if not summaries:
            log.info("smart_import: chunks_to_summaries returned 0 summaries; falling back")
            return None

        # Step 1a: build partial profiles in PROFILE_BATCH_SIZE batches
        profile_start = time.time()
        partials: list[dict] = []

        for i in range(0, len(summaries), PROFILE_BATCH_SIZE):
            batch = summaries[i : i + PROFILE_BATCH_SIZE]
            prompt = totalreclaw_core.build_profile_batch_prompt(json.dumps(batch))
            response = await llm_completion(prompt)
            if not response:
                log.warning(
                    "smart_import: empty profile response for batch %d (skipping)",
                    i // PROFILE_BATCH_SIZE + 1,
                )
                continue
            partial_json = totalreclaw_core.parse_profile_batch_response(response)
            partials.append(json.loads(partial_json))

        if not partials:
            log.warning("smart_import: no profile batches succeeded; falling back to blind extraction")
            return None

        # Step 1b: merge partial profiles (or promote single partial)
        if len(partials) == 1:
            # Single batch — convert PartialProfile fields to UserProfile shape.
            p = partials[0]
            profile = {
                "identity": p.get("identity"),
                "themes": p.get("themes") or [],
                "projects": p.get("projects") or [],
                "stack": p.get("stack") or [],
                "decisions": p.get("decisions") or [],
                "interests": p.get("interests") or [],
                "skip_patterns": p.get("skip_patterns") or [],
            }
        else:
            merge_prompt = totalreclaw_core.build_profile_merge_prompt(json.dumps(partials))
            merge_response = await llm_completion(merge_prompt)
            if not merge_response:
                log.warning("smart_import: empty merge response; falling back to blind extraction")
                return None
            profile_json_str = totalreclaw_core.parse_profile_response(merge_response)
            profile = json.loads(profile_json_str)

        profile_json = json.dumps(profile)
        profile_duration = int((time.time() - profile_start) * 1000)
        log.info(
            "smart_import: profile built in %dms (themes=%d, skip_patterns=%d)",
            profile_duration,
            len(profile.get("themes") or []),
            len(profile.get("skip_patterns") or []),
        )

        # Step 2: triage chunks in TRIAGE_BATCH_SIZE batches
        triage_start = time.time()
        all_decisions: list[dict] = []
        for i in range(0, len(summaries), TRIAGE_BATCH_SIZE):
            batch = summaries[i : i + TRIAGE_BATCH_SIZE]
            triage_prompt = totalreclaw_core.build_triage_prompt(profile_json, json.dumps(batch))
            triage_response = await llm_completion(triage_prompt)
            if not triage_response:
                log.warning(
                    "smart_import: empty triage response for batch %d; defaulting all to EXTRACT",
                    i // TRIAGE_BATCH_SIZE + 1,
                )
                for j in range(i, min(i + TRIAGE_BATCH_SIZE, len(summaries))):
                    all_decisions.append({
                        "chunk_index": j,
                        "decision": "EXTRACT",
                        "reason": "triage LLM unavailable",
                    })
                continue
            batch_decisions = json.loads(
                totalreclaw_core.parse_triage_response(triage_response)
            )
            all_decisions.extend(batch_decisions)

        triage_duration = int((time.time() - triage_start) * 1000)
        extract_count = sum(1 for d in all_decisions if d.get("decision") != "SKIP")
        skip_count = sum(1 for d in all_decisions if d.get("decision") == "SKIP")
        log.info(
            "smart_import: triage done in %dms (extract=%d, skip=%d, total=%d)",
            triage_duration, extract_count, skip_count, len(chunks),
        )

        # Step 3: enrich extraction prompt with profile context
        enriched = totalreclaw_core.enrich_extraction_prompt(
            profile_json, base_extraction_prompt
        )

        total_duration = int((time.time() - pipeline_start) * 1000)
        log.info("smart_import: pipeline complete in %dms", total_duration)

        return SmartImportContext(
            profile_json=profile_json,
            decisions=all_decisions,
            enriched_system_prompt=enriched,
            extract_count=extract_count,
            skip_count=skip_count,
            duration_ms=total_duration,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("smart_import: pipeline failed (%s); falling back to blind extraction", e)
        return None
