"""Smart-import pipeline tests (imp-4).

Covers:
  - The standalone pipeline (``run_smart_import_pipeline``) with mocked LLM
    responses, asserting it routes through every PyO3 binding in the right
    order and returns a ``SmartImportContext`` with correct counts.
  - ``ImportEngine`` integration: triaged-SKIP chunks bypass the extractor,
    extractor receives ``enriched_system_prompt`` only when its signature
    accepts the kwarg, and result fields ``chunks_skipped`` /
    ``smart_import`` are populated.
  - Graceful fallback: no LLM completion callable, empty chunks, or
    smart-import disabled all reproduce the pre-imp-4 blind-extraction
    behaviour.
  - Plugin parity: the enriched system prompt the Python pipeline emits
    is byte-identical to what ``totalreclaw_core.enrich_extraction_prompt``
    produces directly — same Rust function the TS plugin calls via WASM.

The tests use mocked LLM completions so they're hermetic and fast (no
network, no provider credentials). The PyO3 bindings on
``totalreclaw_core`` are exercised for real — the Rust functions are
pure and have no side effects.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw_core

from totalreclaw._smart_import import (
    SmartImportContext,
    is_chunk_skipped,
    run_smart_import_pipeline,
)
from totalreclaw.import_adapters.types import ConversationChunk
from totalreclaw.import_engine import ImportEngine


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def _make_chunks(n: int = 3) -> list[ConversationChunk]:
    """Three chunks covering one technical, one trivial, one project."""
    return [
        ConversationChunk(
            title="Kubernetes setup",
            messages=[
                {"role": "user", "text": "How do I set up a Kubernetes cluster on GCP?"},
                {"role": "assistant", "text": "Use GKE: ..."},
                {"role": "user", "text": "Nodepool autoscaling worked perfectly."},
            ],
            timestamp="2026-01-15T10:00:00Z",
        ),
        ConversationChunk(
            title="Pasta recipe",
            messages=[
                {"role": "user", "text": "How do I make pasta carbonara?"},
                {"role": "assistant", "text": "Standard recipe: ..."},
            ],
            timestamp="2026-01-16T12:00:00Z",
        ),
        ConversationChunk(
            title="TotalReclaw imports",
            messages=[
                {"role": "user", "text": "Need to wire smart-import into Hermes."},
                {"role": "assistant", "text": "Use the PyO3 bindings on totalreclaw_core..."},
            ],
            timestamp="2026-01-17T09:00:00Z",
        ),
    ][:n]


_PROFILE_BATCH_RESPONSE = json.dumps({
    "identity": "Backend engineer working on TotalReclaw",
    "themes": ["Kubernetes", "memory systems"],
    "projects": ["TotalReclaw", "GKE migration"],
    "stack": ["Python", "Rust", "GCP"],
    "decisions": [],
    "interests": ["distributed systems"],
    "skip_patterns": ["recipes", "weather"],
})


def _make_triage_response(n: int, skip_indices: set[int]) -> str:
    """Build a triage response JSON for ``n`` chunks; given indices = SKIP."""
    return json.dumps([
        {
            "index": i,
            "decision": "SKIP" if i in skip_indices else "EXTRACT",
            "reason": "matches skip pattern" if i in skip_indices else "valuable",
        }
        for i in range(n)
    ])


# ---------------------------------------------------------------------------
# is_chunk_skipped — pure helper
# ---------------------------------------------------------------------------


class TestIsChunkSkipped:
    def test_returns_true_when_decision_is_skip(self) -> None:
        decisions = [{"chunk_index": 1, "decision": "SKIP", "reason": "trivial Q&A"}]
        skipped, reason = is_chunk_skipped(1, decisions)
        assert skipped is True
        assert reason == "trivial Q&A"

    def test_returns_false_when_decision_is_extract(self) -> None:
        decisions = [{"chunk_index": 0, "decision": "EXTRACT", "reason": ""}]
        skipped, reason = is_chunk_skipped(0, decisions)
        assert skipped is False
        assert reason == ""

    def test_defaults_to_extract_when_index_missing(self) -> None:
        """Safe default: a chunk with no decision should be extracted."""
        skipped, _ = is_chunk_skipped(42, [{"chunk_index": 0, "decision": "SKIP"}])
        assert skipped is False

    def test_returns_default_reason_when_skip_has_no_reason(self) -> None:
        decisions = [{"chunk_index": 0, "decision": "SKIP"}]
        _, reason = is_chunk_skipped(0, decisions)
        assert reason == "triage: skip"


# ---------------------------------------------------------------------------
# run_smart_import_pipeline — direct tests
# ---------------------------------------------------------------------------


class TestRunSmartImportPipeline:
    def test_returns_none_without_llm_completion(self) -> None:
        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=_make_chunks(),
            llm_completion=None,
            base_extraction_prompt="BASE",
        ))
        assert ctx is None

    def test_returns_none_for_empty_chunks(self) -> None:
        # Even with a real-looking llm_completion, no chunks => None.
        async def llm(prompt: str) -> Optional[str]:
            return "{}"

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=[],
            llm_completion=llm,
            base_extraction_prompt="BASE",
        ))
        assert ctx is None

    def test_full_pipeline_with_mocked_llm(self) -> None:
        """End-to-end: profile + triage + enrich with 3 chunks, 1 SKIP."""
        chunks = _make_chunks(3)
        calls: list[str] = []

        async def llm(prompt: str) -> Optional[str]:
            calls.append(prompt)
            # Profile-batch prompts mention "describe who this user is"
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            # Triage prompts mention "EXTRACT" + "SKIP" choices
            if "EXTRACT:" in prompt or "classifying conversations" in prompt:
                return _make_triage_response(3, skip_indices={1})
            return "{}"

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=chunks,
            llm_completion=llm,
            base_extraction_prompt="BASE EXTRACTION PROMPT",
        ))

        assert ctx is not None
        # 3 chunks ≤ PROFILE_BATCH_SIZE (50) ⇒ 1 profile batch + 1 triage batch.
        assert len(calls) == 2
        assert ctx.extract_count == 2
        assert ctx.skip_count == 1
        # Chunk index 1 should be marked SKIP.
        skipped, _ = is_chunk_skipped(1, ctx.decisions)
        assert skipped is True
        # The enriched prompt must include profile context AND the base.
        assert "BASE EXTRACTION PROMPT" in ctx.enriched_system_prompt
        assert "Kubernetes" in ctx.enriched_system_prompt
        assert "Backend engineer" in ctx.enriched_system_prompt

    def test_falls_back_when_profile_response_empty(self) -> None:
        """Empty profile responses across all batches ⇒ pipeline aborts."""
        async def llm(prompt: str) -> Optional[str]:
            return ""  # All LLM calls fail.

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=_make_chunks(),
            llm_completion=llm,
            base_extraction_prompt="BASE",
        ))
        assert ctx is None

    def test_triage_empty_response_defaults_to_extract(self) -> None:
        """If triage batch returns empty, those chunks default to EXTRACT."""
        async def llm(prompt: str) -> Optional[str]:
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            return ""  # Triage empty.

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=_make_chunks(3),
            llm_completion=llm,
            base_extraction_prompt="BASE",
        ))
        assert ctx is not None
        # All 3 default to EXTRACT.
        assert ctx.extract_count == 3
        assert ctx.skip_count == 0
        for i in range(3):
            skipped, _ = is_chunk_skipped(i, ctx.decisions)
            assert skipped is False

    def test_swallows_pipeline_exceptions(self) -> None:
        """Any uncaught exception inside the pipeline ⇒ None, no raise."""
        async def llm(prompt: str) -> Optional[str]:
            raise RuntimeError("simulated provider outage")

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=_make_chunks(),
            llm_completion=llm,
            base_extraction_prompt="BASE",
        ))
        assert ctx is None


# ---------------------------------------------------------------------------
# Plugin-parity: same Rust bindings produce the same enriched prompt
# ---------------------------------------------------------------------------


class TestPluginParityViaCoreBindings:
    """The plugin and Python both call ``enrich_extraction_prompt`` on the
    same Rust core (the TS plugin via WASM, the Python pipeline via PyO3).
    This test pins the Python pipeline to the exact byte sequence the Rust
    core would produce given the same profile + base prompt — proving the
    parity contract from the issue's done criteria ("matches plugin")
    without needing a TS-side runner.
    """

    def test_pipeline_enriched_prompt_matches_direct_core_call(self) -> None:
        # Build a canonical profile (no LLM needed for this assertion).
        profile_json = json.dumps({
            "identity": "Pedro — TotalReclaw maintainer",
            "themes": ["memory systems", "AI agents"],
            "projects": ["TotalReclaw"],
            "stack": ["Python", "Rust", "TypeScript"],
            "decisions": [],
            "interests": ["distributed systems"],
            "skip_patterns": ["recipes"],
        })
        base = "BASE_EXTRACTION_PROMPT"

        # Direct call into the same Rust function the plugin invokes.
        expected = totalreclaw_core.enrich_extraction_prompt(profile_json, base)

        # Indirect call via the pipeline.
        async def llm(prompt: str) -> Optional[str]:
            if "describe who this user is" in prompt:
                return profile_json  # Use as the partial profile.
            return _make_triage_response(1, skip_indices=set())

        ctx = asyncio.run(run_smart_import_pipeline(
            chunks=_make_chunks(1),
            llm_completion=llm,
            base_extraction_prompt=base,
        ))
        assert ctx is not None
        assert ctx.enriched_system_prompt == expected

    def test_summaries_passthrough_byte_equivalent_to_core(self) -> None:
        """chunks_to_summaries is the entry point of the pipeline. Verify
        the Python serialization shape passes through Rust unchanged."""
        chunks = _make_chunks(2)
        # Build the same payload the pipeline constructs internally and
        # invoke the core directly for the expected.
        from totalreclaw._smart_import import _chunks_to_core_payload

        payload = json.dumps(_chunks_to_core_payload(chunks))
        summaries = json.loads(totalreclaw_core.chunks_to_summaries(payload))

        # The summary count must equal chunk count — index preservation
        # is what the triage step relies on.
        assert len(summaries) == 2
        assert summaries[0]["title"] == "Kubernetes setup"
        # First-message field should be the first user message, truncated.
        assert "Kubernetes" in summaries[0]["first_message"]


# ---------------------------------------------------------------------------
# ImportEngine integration
# ---------------------------------------------------------------------------


def _fake_client() -> MagicMock:
    client = MagicMock()

    async def _remember(text, **_kwargs):
        return f"fact-{text[:8]}"

    client.remember = AsyncMock(side_effect=_remember)
    return client


class TestImportEngineSmartImportIntegration:
    def test_skipped_chunks_never_reach_extractor(self) -> None:
        """Chunks marked SKIP by triage must not hit ``llm_extract``."""
        chunks = _make_chunks(3)
        extracted_titles: list[str] = []

        async def llm_extract(messages, timestamp, *, enriched_system_prompt=None):
            # First user message text is the chunk's first message.
            extracted_titles.append(messages[0]["content"][:40])
            return [{"text": "found a fact about kubernetes", "type": "fact", "importance": 7}]

        async def llm_completion(prompt: str) -> Optional[str]:
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            return _make_triage_response(3, skip_indices={1})

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=llm_extract,
            llm_completion=llm_completion,
            base_extraction_prompt="BASE_PROMPT",
        )

        # Drive _process_chunk_batch through the public API. Build a fake
        # AdapterParseResult and call the private method directly so we
        # don't have to wire a full adapter for this unit test.
        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=chunks, total_messages=8, warnings=[], errors=[],
        )
        result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=3, start_ms=0))

        assert result.chunks_skipped == 1
        # Two extractor calls (chunk 0 and 2), chunk 1 was SKIP.
        assert len(extracted_titles) == 2
        assert result.smart_import == {
            "extract_count": 2,
            "skip_count": 1,
            "profile_duration_ms": result.smart_import["profile_duration_ms"],
        }
        assert result.facts_extracted == 2

    def test_extractor_receives_enriched_prompt(self) -> None:
        """When extractor accepts the kwarg, the enriched prompt is passed through."""
        received: list[Optional[str]] = []

        async def llm_extract(messages, timestamp, *, enriched_system_prompt=None):
            received.append(enriched_system_prompt)
            return []

        async def llm_completion(prompt: str) -> Optional[str]:
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            return _make_triage_response(1, skip_indices=set())

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=llm_extract,
            llm_completion=llm_completion,
            base_extraction_prompt="BASE_PROMPT",
        )

        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=_make_chunks(1), total_messages=3, warnings=[], errors=[],
        )
        asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))

        assert len(received) == 1
        # The kwarg should be the smart-import enriched prompt — it must
        # include both profile context and base.
        assert received[0] is not None
        assert "BASE_PROMPT" in received[0]

    def test_legacy_extractor_signature_works(self) -> None:
        """An ``llm_extract`` with only ``(messages, timestamp)`` must still
        be called with that arity — no spurious kwarg failures."""
        captured_arg_count: list[int] = []

        async def legacy_extract(messages, timestamp):
            # Will fail if engine tries to pass enriched_system_prompt as kwarg.
            captured_arg_count.append(2)
            return []

        async def llm_completion(prompt: str) -> Optional[str]:
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            return _make_triage_response(1, skip_indices=set())

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=legacy_extract,
            llm_completion=llm_completion,
            base_extraction_prompt="BASE_PROMPT",
        )

        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=_make_chunks(1), total_messages=2, warnings=[], errors=[],
        )
        result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))

        # No exception raised; legacy extractor was called.
        assert captured_arg_count == [2]
        # Smart-import still ran; the prompt just isn't forwarded.
        assert result.smart_import is not None

    def test_no_llm_completion_falls_back_to_blind(self) -> None:
        """Pre-imp-4 behaviour: without ``llm_completion``, smart-import
        never runs and result fields are at their defaults."""
        async def llm_extract(messages, timestamp, *, enriched_system_prompt=None):
            assert enriched_system_prompt is None  # No smart-import context.
            return [{"text": "blind fact about k8s", "type": "fact", "importance": 8}]

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=llm_extract,
            # llm_completion intentionally omitted.
        )

        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=_make_chunks(1), total_messages=3, warnings=[], errors=[],
        )
        result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))

        assert result.smart_import is None
        assert result.chunks_skipped == 0
        assert result.facts_extracted == 1

    def test_enable_smart_import_false_short_circuits(self) -> None:
        """``enable_smart_import=False`` ⇒ no profile/triage LLM calls."""
        llm_completion_calls = 0

        async def llm_completion(prompt: str) -> Optional[str]:
            nonlocal llm_completion_calls
            llm_completion_calls += 1
            return _PROFILE_BATCH_RESPONSE

        async def llm_extract(messages, timestamp, *, enriched_system_prompt=None):
            return []

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=llm_extract,
            llm_completion=llm_completion,
            enable_smart_import=False,
        )

        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=_make_chunks(1), total_messages=3, warnings=[], errors=[],
        )
        result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))

        assert llm_completion_calls == 0
        assert result.smart_import is None

    def test_smart_import_cached_across_batches(self) -> None:
        """A single engine handles multiple batches; smart-import should
        only run once (the plugin rebuilds per batch; Python caches)."""
        llm_completion_calls: list[str] = []

        async def llm_completion(prompt: str) -> Optional[str]:
            llm_completion_calls.append(prompt[:80])
            if "describe who this user is" in prompt:
                return _PROFILE_BATCH_RESPONSE
            return _make_triage_response(3, skip_indices=set())

        async def llm_extract(messages, timestamp, *, enriched_system_prompt=None):
            return []

        engine = ImportEngine(
            client=_fake_client(),
            llm_extract=llm_extract,
            llm_completion=llm_completion,
            base_extraction_prompt="BASE_PROMPT",
        )

        from totalreclaw.import_adapters.types import AdapterParseResult

        parsed = AdapterParseResult(
            facts=[], chunks=_make_chunks(3), total_messages=8, warnings=[], errors=[],
        )

        # Batch 1: chunks [0..1], batch 2: chunk [2].
        asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=2, start_ms=0))
        first_call_count = len(llm_completion_calls)
        asyncio.run(engine._process_chunk_batch(parsed, offset=2, batch_size=1, start_ms=0))
        second_call_count = len(llm_completion_calls)

        # The second batch must not re-run the pipeline.
        assert first_call_count == second_call_count


# ---------------------------------------------------------------------------
# BatchImportResult shape
# ---------------------------------------------------------------------------


class TestBatchImportResultSmartImportFields:
    def test_default_values_for_non_smart_import_paths(self) -> None:
        """Fact-only sources (Mem0, MCP) never run smart-import."""
        from totalreclaw.import_adapters.types import BatchImportResult

        r = BatchImportResult(
            success=True,
            batch_offset=0,
            batch_size=1,
            chunks_processed=0,
            total_chunks=0,
            facts_extracted=1,
            facts_stored=1,
            remaining_chunks=0,
            is_complete=True,
        )
        assert r.chunks_skipped == 0
        assert r.smart_import is None
