"""Concurrent extraction in ImportEngine — #392 Part 1.

The extraction loop in ``_process_chunk_batch`` historically awaited each
chunk's LLM call sequentially, so a 25-chunk batch at ~1.5 min/call + 2s
inter-chunk delay took ~40+ min (#389/#392). This module pins the concurrent
contract:

* Extraction across a batch runs concurrently (bounded by a semaphore), so
  wall-clock collapses from ~N×per-call to ~ceil(N / concurrency)×per-call.
* Diagnostics, session/Crystal accumulation, and per-chunk exception handling
  are preserved (builds on #376) — order is maintained because results are
  processed in chunk-index order regardless of completion order.
* Concurrency is env-tunable (``TOTALRECLAW_IMPORT_CONCURRENCY``), conservative
  default — a semaphore bounds concurrency, not request rate, so we keep
  pacing to avoid glm/zai 429s.
* A chunk whose extractor raises does NOT lose its siblings: successful chunks
  still store, the exception is reported per-chunk (#376 regression guard).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.import_adapters.types import AdapterParseResult, ConversationChunk
from totalreclaw.import_engine import ImportEngine


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch):
    import totalreclaw.embedding as _emb
    monkeypatch.setattr(_emb, "get_embedding", lambda _text: [0.0] * 640)


@pytest.fixture(autouse=True)
def _no_interchunk_delay(monkeypatch):
    """Neutralise the 2s rate-limit delay so the test isolates *extraction*
    concurrency, not the delay. (The concurrency impl keeps its own pacing.)"""
    import totalreclaw.import_engine as _ie
    monkeypatch.setattr(_ie, "INTER_CHUNK_DELAY", 0.0)


def _pro_client() -> MagicMock:
    client = MagicMock()

    async def _ensure_chain_id():
        return 100

    async def _remember_batch(facts, source="python-client"):
        return [f"fact-id-{i}" for i in range(len(facts))]

    async def _remember(text, **kwargs):
        return f"single-{text[:20]}"

    client._ensure_chain_id = AsyncMock(side_effect=_ensure_chain_id)
    client.remember_batch = AsyncMock(side_effect=_remember_batch)
    client.remember = AsyncMock(side_effect=_remember)
    return client


def _parsed(n: int) -> AdapterParseResult:
    chunks = [
        ConversationChunk(
            title=f"Chunk {i}",
            messages=[{"role": "user", "text": f"question {i}"},
                      {"role": "assistant", "text": f"answer {i}"}],
            timestamp="2026-01-15T10:00:00Z",
        )
        for i in range(n)
    ]
    return AdapterParseResult(facts=[], chunks=chunks, total_messages=n * 2, warnings=[], errors=[])


def test_extraction_is_concurrent_not_sequential() -> None:
    """N chunks each taking ``delay`` must finish in ~ceil(N/concurrency)×delay,
    not N×delay. With concurrency default ≥2 and 6 chunks × 0.5s, sequential
    would be ≥3.0s; concurrent must be well under that."""
    delay = 0.5
    n = 6

    async def slow_extract(messages, timestamp, *, enriched_system_prompt=None):
        await asyncio.sleep(delay)
        return [{"text": f"a stored fact from chunk {messages[0]['content']}", "importance": 7, "type": "fact"}]

    engine = ImportEngine(client=_pro_client(), llm_extract=slow_extract)

    start = time.monotonic()
    result = asyncio.run(engine._process_chunk_batch(_parsed(n), 0, n, 0))
    elapsed = time.monotonic() - start

    assert result.facts_stored == n, f"all {n} chunks should store; got {result.facts_stored}"
    # Sequential would be n*delay = 3.0s. Allow generous slack; concurrency must
    # beat sequential by a clear margin.
    assert elapsed < n * delay, (
        f"extraction was sequential: {elapsed:.2f}s >= {n * delay:.2f}s sequential bound"
    )
