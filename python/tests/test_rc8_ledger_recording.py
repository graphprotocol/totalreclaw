"""rc8 — #466: the #436 conversation ledger must satisfy stored-implies-recorded.

rc7 pre-stable QA: after two completed imports the conversation ledger was NEVER
written, so a re-import stored 10 duplicates. Root cause: the recording filter
excluded a conversation if ANY of its chunks failed extraction (#436-review
Finding-2), but under heavy z.ai 429s a MULTI-CHUNK conversation can have one
chunk transiently fail while a SIBLING chunk stores facts — so the conversation
had stored facts yet was withheld from the ledger, and the re-import re-extracted
it → dups.

The invariant that matters: any conversation whose facts were STORED must be
recorded (so a re-import skips it). A conversation that produced NO stored facts
AND had a failed chunk stays unrecorded, so a genuine transient failure is
retried on re-import (Finding-2).

Pure/unit: a recording client + fake adapter; no network, no LLM.
"""
from __future__ import annotations

import asyncio

import pytest

import totalreclaw.import_state as ist
import totalreclaw.import_engine as ie
from totalreclaw.import_engine import ImportEngine
from totalreclaw.import_adapters.types import AdapterParseResult, ConversationChunk


class _BatchClient:
    def __init__(self):
        self.n = 0

    async def _ensure_chain_id(self):
        return 100

    async def remember_batch(self, payloads, source=None):
        ids = [f"f{self.n + i}" for i in range(len(payloads))]
        self.n += len(payloads)
        return ids


def _install_fake_embedding(monkeypatch):
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: [0.1, 0.2, 0.3])


def _chunk(conv_id, text):
    return ConversationChunk(
        title=f"{conv_id}:{text[:12]}",
        messages=[{"role": "user", "text": text},
                  {"role": "assistant", "text": f"re: {text}"}],
        timestamp="2026-05-14T09:00:00Z",
        conversation_id=conv_id,
    )


def _install_adapter(monkeypatch, chunks):
    class _Adapter:
        def parse(self, content=None, file_path=None):
            return AdapterParseResult(
                facts=[], chunks=list(chunks), total_messages=len(chunks) * 2,
                warnings=[], errors=[])
    monkeypatch.setattr(ie, "get_adapter", lambda source: _Adapter())


@pytest.fixture(autouse=True)
def _tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")
    yield


def _flaky_extractor(fail_texts):
    """Extractor that RAISES for chunks whose text is in fail_texts (simulating
    a 429 that exhausts the retry budget), else returns one fact."""
    async def _extract(messages, timestamp):
        text = messages[0]["content"]
        if any(f in text for f in fail_texts):
            raise RuntimeError("LLM upstream outage (z.ai 429 exhausted)")
        return [{"text": f"fact from {text}", "type": "fact", "importance": 8}]
    return _extract


# ── the exact #466 repro: multi-chunk conversation, one sibling 429-fails ──
def test_conversation_with_stored_facts_is_recorded_despite_sibling_failure(monkeypatch):
    _install_fake_embedding(monkeypatch)
    # Conversation A spans 2 chunks: a0 stores facts, a1 transiently fails.
    # Conversation B likewise. Single process_batch call completes everything.
    chunks = [
        _chunk("A", "alpha one stored"),
        _chunk("A", "alpha two boom429"),
        _chunk("B", "bravo one stored"),
        _chunk("B", "bravo two boom429"),
    ]
    _install_adapter(monkeypatch, chunks)
    engine = ImportEngine(
        client=_BatchClient(),
        llm_extract=_flaky_extractor(fail_texts=["boom429"]),
    )

    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    # Facts WERE stored (from the surviving sibling of each conversation).
    assert result.facts_stored >= 2
    # stored-implies-recorded: both conversations must be in the ledger so a
    # re-import skips them — this is the regression that shipped 10 dups.
    recorded = ist.load_imported_conversations("chatgpt")
    assert recorded == {"A", "B"}


def test_conversation_with_no_stored_facts_and_failure_stays_unrecorded(monkeypatch):
    """Finding-2 preserved: a conversation that stored NOTHING and had a failed
    chunk is NOT recorded, so a re-import retries it."""
    _install_fake_embedding(monkeypatch)
    chunks = [
        _chunk("C", "charlie only boom429"),   # single chunk, fails → 0 facts
        _chunk("D", "delta one stored"),        # clean conversation
    ]
    _install_adapter(monkeypatch, chunks)
    engine = ImportEngine(
        client=_BatchClient(),
        llm_extract=_flaky_extractor(fail_texts=["boom429"]),
    )

    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))
    recorded = ist.load_imported_conversations("chatgpt")
    assert "D" in recorded          # clean → recorded
    assert "C" not in recorded      # failed + no facts → stays re-importable


def test_single_call_complete_records_in_one_process_batch(monkeypatch):
    """Hypothesis (a): recording must fire on a single is_complete call, not
    only on a later batch."""
    _install_fake_embedding(monkeypatch)
    chunks = [_chunk("E", "echo stored"), _chunk("F", "foxtrot stored")]
    _install_adapter(monkeypatch, chunks)
    engine = ImportEngine(client=_BatchClient(),
                          llm_extract=_flaky_extractor(fail_texts=[]))
    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))
    assert result.is_complete
    assert ist.load_imported_conversations("chatgpt") == {"E", "F"}


def test_triage_skipped_chunk_does_not_block_recording(monkeypatch):
    """Hypothesis (b): a conversation whose extra chunk is triage-skipped (not
    failed) but whose other chunk stores facts is still recorded."""
    _install_fake_embedding(monkeypatch)
    # G has 2 chunks; we simulate a skip by having the engine's smart-import
    # skip decisions mark chunk index 1. Simplest: no smart_ctx, so instead we
    # cover the "skipped == not failed" path via an empty (0-fact) sibling,
    # which is the same recording-relevant state (processed, not failed).
    chunks = [_chunk("G", "golf one stored"), _chunk("G", "golf two empty")]
    _install_adapter(monkeypatch, chunks)

    async def _extract(messages, timestamp):
        text = messages[0]["content"]
        if "empty" in text:
            return []  # 0 facts, NOT a failure
        return [{"text": f"fact from {text}", "type": "fact", "importance": 8}]

    engine = ImportEngine(client=_BatchClient(), llm_extract=_extract)
    asyncio.run(engine.process_batch(source="chatgpt", content="x"))
    assert "G" in ist.load_imported_conversations("chatgpt")
