"""Integration test: Gemini MyActivity.json -> ImportEngine -> store pipeline.

Proves the full client-side path for the new JSON adapter WITHOUT touching the
network or any real keys:

    MyActivity.json  --adapter-->  ConversationChunks
                     --llm_extract (mock)-->  facts
                     --get_embedding (mock)-->  embedded
                     --client.remember (mock)-->  stored, source="import"

It also asserts the engine treats a store-time duplicate (relay 409) as a skip,
not an error -- the hook the E2EE store-dedup relies on.

Everything here is mocked: the relay (client.remember), the embedder, and the
LLM extractor. No XChaCha20, no UserOps, no staging calls.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

from totalreclaw.import_engine import ImportEngine


def _install_fake_embedding(monkeypatch, vector):
    """Inject a fake ``totalreclaw.embedding`` module so _store_fact embeds."""
    mod = types.ModuleType("totalreclaw.embedding")
    mod.get_embedding = lambda text: list(vector)  # noqa: E731
    monkeypatch.setitem(sys.modules, "totalreclaw.embedding", mod)


class _RecordingClient:
    """Stand-in TotalReclaw client: records remember() calls, returns fake ids."""

    def __init__(self, fail_texts=None):
        self.calls = []
        self._fail_texts = set(fail_texts or [])
        self._n = 0

    async def remember(self, text, embedding=None, importance=None, source=None,
                       provenance=None, fact_type=None, extra_metadata=None, **kw):
        self.calls.append({
            "text": text, "embedding": embedding,
            "importance": importance, "source": source,
            "provenance": provenance, "fact_type": fact_type,
            "extra_metadata": extra_metadata,
        })
        if text in self._fail_texts:
            # Mimic the relay's content-fingerprint dedup response.
            raise RuntimeError("HTTP 409: duplicate fingerprint")
        self._n += 1
        return f"fact-{self._n}"


_GEMINI_JSON = json.dumps([
    {
        "header": "Gemini Apps",
        "title": "Prompted I just moved to Berlin for a new job",
        "time": "2026-05-14T09:21:03.512Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Congrats on the move to Berlin!"}],
    },
    {
        "header": "Gemini Apps",
        "title": "Prompted Remind me I am allergic to peanuts",
        "time": "2026-05-14T09:24:00.000Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Noted, peanut allergy."}],
    },
])


def test_gemini_json_flows_through_engine_to_store(monkeypatch) -> None:
    _install_fake_embedding(monkeypatch, vector=[0.1, 0.2, 0.3])
    client = _RecordingClient()

    async def fake_extract(messages, timestamp):
        # Both turns land in one 30-min session -> one chunk -> one extract call.
        # A real extractor returns multiple facts; importance >= 6 so none filter.
        joined = " ".join(m["content"] for m in messages if m.get("role") == "user")
        out = []
        if "Berlin" in joined:
            out.append({"text": "User moved to Berlin for a new job", "type": "fact", "importance": 8})
        if "peanut" in joined:
            out.append({"text": "User is allergic to peanuts", "type": "fact", "importance": 9})
        return out

    engine = ImportEngine(client=client, llm_extract=fake_extract)
    result = asyncio.run(engine.process_batch(source="gemini", content=_GEMINI_JSON))

    assert result.success
    assert result.is_complete
    assert result.facts_stored == 2
    assert result.errors == []

    # Every store carried an embedding and the import provenance tag.
    # No llm_completion → no Crystal; just the 2 atomic facts.
    assert len(client.calls) == 2
    assert all(c["embedding"] == [0.1, 0.2, 0.3] for c in client.calls)
    assert all(c["source"] == "import" for c in client.calls)
    stored = {c["text"] for c in client.calls}
    assert "User moved to Berlin for a new job" in stored
    assert "User is allergic to peanuts" in stored

    # #356 — every imported fact has v1 external provenance + provider +
    # a session_id; both facts of one conversation share that session_id.
    assert all(c["provenance"] == "external" for c in client.calls)
    metas = [c["extra_metadata"] or {} for c in client.calls]
    assert all(m.get("import_source") == "gemini" for m in metas)
    session_ids = {m.get("session_id") for m in metas}
    assert len(session_ids) == 1 and None not in session_ids  # one shared session


def test_gemini_json_duplicate_is_skipped_not_errored(monkeypatch) -> None:
    _install_fake_embedding(monkeypatch, vector=[0.0, 1.0])
    # The Berlin fact already exists -> relay returns 409 on store.
    client = _RecordingClient(fail_texts={"User moved to Berlin for a new job"})

    async def fake_extract(messages, timestamp):
        joined = " ".join(m["content"] for m in messages if m.get("role") == "user")
        out = []
        if "Berlin" in joined:
            out.append({"text": "User moved to Berlin for a new job", "type": "fact", "importance": 8})
        if "peanut" in joined:
            out.append({"text": "User is allergic to peanuts", "type": "fact", "importance": 9})
        return out

    engine = ImportEngine(client=client, llm_extract=fake_extract)
    result = asyncio.run(engine.process_batch(source="gemini", content=_GEMINI_JSON))

    # Duplicate skipped silently; the non-duplicate still stored.
    assert result.facts_extracted == 2
    assert result.facts_stored == 1
    assert result.errors == []
