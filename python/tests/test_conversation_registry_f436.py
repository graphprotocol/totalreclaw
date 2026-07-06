"""F2 / internal#436 — re-imports must not re-store the same conversations.

Text-fingerprint dedup can never catch re-imports: a re-import RE-EXTRACTS via
the LLM and paraphrase yields fresh fingerprints. The fix works at the
CONVERSATION level — a per-source registry of already-imported
``conversation_id``s. On a re-import, chunks whose conversation is in the
registry are dropped BEFORE extraction (the LLM is never called) and counted
into ``BatchImportResult.conversations_skipped``. A conversation is recorded
only once ALL of its chunks have been processed (so a conversation straddling
a batch boundary isn't recorded early).

Gemini (no ``conversation_id``) is unaffected.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import totalreclaw.import_state as ist
import totalreclaw.import_engine as ie
from totalreclaw.import_engine import ImportEngine
from totalreclaw.import_adapters.types import (
    AdapterParseResult,
    ConversationChunk,
)


# ── recording client (Gnosis, records remember_batch payloads) ────────────
class _BatchClient:
    def __init__(self):
        self.batches = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100

    async def remember_batch(self, payloads, source=None):
        self.batches.append(list(payloads))
        ids = [f"f{self._n + i}" for i in range(len(payloads))]
        self._n += len(payloads)
        return ids


def _install_fake_embedding(monkeypatch):
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: [0.1, 0.2, 0.3])


def _chunk(conv_id: str, text: str, ts: str) -> ConversationChunk:
    return ConversationChunk(
        title=f"conv {conv_id}",
        messages=[
            {"role": "user", "text": text},
            {"role": "assistant", "text": f"reply about {text}"},
        ],
        timestamp=ts,
        conversation_id=conv_id,
    )


def _install_fake_adapter(monkeypatch, chunks):
    """Patch get_adapter so parse() returns a fixed chunk list."""
    class _Adapter:
        def parse(self, content=None, file_path=None):
            return AdapterParseResult(
                facts=[], chunks=list(chunks), total_messages=len(chunks) * 2,
                warnings=[], errors=[],
            )

    monkeypatch.setattr(ie, "get_adapter", lambda source: _Adapter())


@pytest.fixture(autouse=True)
def _tmp_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")
    yield


def _extractor():
    calls = {"n": 0}

    async def fake_extract(messages, timestamp):
        calls["n"] += 1
        return [{"text": "an extracted fact worth keeping", "type": "fact", "importance": 8}]

    return fake_extract, calls


# ── registry primitives (import_state) ────────────────────────────────────
def test_registry_roundtrip():
    assert ist.load_imported_conversations("chatgpt") == set()
    ist.record_imported_conversations("chatgpt", ["a", "b"])
    ist.record_imported_conversations("chatgpt", ["b", "c"])
    assert ist.load_imported_conversations("chatgpt") == {"a", "b", "c"}
    # Source-scoped: a different source has its own registry.
    assert ist.load_imported_conversations("claude") == set()


def test_registry_tolerates_corrupt_file(tmp_path, monkeypatch):
    p = ist.IMPORT_STATE_DIR
    p.mkdir(parents=True, exist_ok=True)
    (p / "imported-conversations-chatgpt.json").write_text("{ not json ]")
    # Corrupt file reads as empty, not an exception.
    assert ist.load_imported_conversations("chatgpt") == set()
    # And a subsequent record still works (overwrites the corrupt file).
    ist.record_imported_conversations("chatgpt", ["x"])
    assert ist.load_imported_conversations("chatgpt") == {"x"}


# ── engine behaviour ──────────────────────────────────────────────────────
def test_reimport_skips_all_conversations_pre_extraction(monkeypatch):
    _install_fake_embedding(monkeypatch)
    chunks = [
        _chunk("c1", "first topic", "2026-05-14T09:00:00Z"),
        _chunk("c2", "second topic", "2026-05-14T10:00:00Z"),
    ]
    _install_fake_adapter(monkeypatch, chunks)

    # Pre-seed the registry: both conversations already imported.
    ist.record_imported_conversations("chatgpt", ["c1", "c2"])

    fake_extract, calls = _extractor()
    client = _BatchClient()
    engine = ImportEngine(client=client, llm_extract=fake_extract)

    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    assert result.conversations_skipped == 2
    assert calls["n"] == 0  # extractor never called for skipped conversations
    assert result.facts_stored == 0
    assert client.batches == []  # nothing written


def test_fresh_import_records_completed_conversations(monkeypatch):
    _install_fake_embedding(monkeypatch)
    chunks = [
        _chunk("c1", "first topic", "2026-05-14T09:00:00Z"),
        _chunk("c2", "second topic", "2026-05-14T10:00:00Z"),
    ]
    _install_fake_adapter(monkeypatch, chunks)

    fake_extract, calls = _extractor()
    engine = ImportEngine(client=_BatchClient(), llm_extract=fake_extract)

    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))
    assert result.conversations_skipped == 0
    assert calls["n"] == 2
    # Both conversations fully processed → both recorded.
    assert ist.load_imported_conversations("chatgpt") == {"c1", "c2"}


def test_extraction_failure_does_not_record_conversation(monkeypatch):
    """Review Finding 2: a conversation whose chunk FAILS extraction must NOT
    be recorded as imported — otherwise the natural re-import recovery skips
    it forever. Other (successful) conversations in the same batch are still
    recorded."""
    _install_fake_embedding(monkeypatch)
    chunks = [
        _chunk("c1", "good topic", "2026-05-14T09:00:00Z"),
        _chunk("c2", "boom topic", "2026-05-14T10:00:00Z"),
    ]
    _install_fake_adapter(monkeypatch, chunks)

    async def flaky_extract(messages, timestamp):
        # c2's chunk text is "boom topic ..." — raise for it, succeed otherwise.
        if any("boom" in (m.get("content") or "") for m in messages):
            raise RuntimeError("transient LLM failure")
        return [{"text": "an extracted fact worth keeping", "type": "fact", "importance": 8}]

    engine = ImportEngine(client=_BatchClient(), llm_extract=flaky_extract)
    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    # Batch completed (c1 stored a fact); c2 failed extraction.
    assert result.facts_stored >= 1
    recorded = ist.load_imported_conversations("chatgpt")
    assert "c1" in recorded          # succeeded → recorded
    assert "c2" not in recorded      # failed → NOT recorded, stays re-importable


def test_partial_import_only_records_fully_processed_conversations(monkeypatch):
    _install_fake_embedding(monkeypatch)
    # c1 fits in batch 1; c2's two chunks straddle the batch boundary.
    chunks = [
        _chunk("c1", "topic one", "2026-05-14T09:00:00Z"),
        _chunk("c2", "topic two part a", "2026-05-14T10:00:00Z"),
        _chunk("c2", "topic two part b", "2026-05-14T10:05:00Z"),
    ]
    _install_fake_adapter(monkeypatch, chunks)

    fake_extract, _ = _extractor()
    engine = ImportEngine(client=_BatchClient(), llm_extract=fake_extract)

    # Batch 1: chunks [0, 1] → c1 complete, c2 still has chunk 2 outstanding.
    asyncio.run(engine.process_batch(source="chatgpt", content="x", offset=0, batch_size=2))
    assert ist.load_imported_conversations("chatgpt") == {"c1"}

    # Batch 2: chunk [2] → c2 now complete.
    asyncio.run(engine.process_batch(source="chatgpt", content="x", offset=2, batch_size=2))
    assert ist.load_imported_conversations("chatgpt") == {"c1", "c2"}
