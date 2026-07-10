"""internal#473 — ledger recording is store-result-aware, not extracted-implies-
recorded.

The #466/rc8 fix recorded a conversation at EXTRACTION time, before the on-chain
store. So a chunk whose facts extracted cleanly but whose ``store_fact_batch``
then raised/reverted was STILL recorded → never re-importable → its facts were
silently lost. #473 moves recording AFTER the store and keys it on which
conversation_ids ACTUALLY durably stored (the store now returns a per-conversation
stored map).

Three failure modes rc8 left uncovered (its ``_BatchClient.remember_batch`` always
returned ids — never simulated a store failure):

  (a) extract-succeeds → store-raises  ⇒ conversation NOT recorded (re-import
      re-extracts + retries the store);
  (b) multi-chunk conversation: one sibling chunk's fact stores, a sibling's
      store fails ⇒ recorded (≥1 stored — the #466 dup guard, now store-derived);
  (c) all-duplicates conversation (store reports dups_skipped, 0 new) ⇒ STILL
      recorded (the facts already exist on-chain; withholding would loop forever
      on re-import).

Pure/unit: a recording client + fake adapter; no network, no LLM. Mirrors the
``_BatchClient`` shape from ``test_rc8_ledger_recording.py`` (the autouse
conftest shim routes the engine's ``store_fact_batch`` back through this fake's
``remember_batch`` recorder).
"""
from __future__ import annotations

import asyncio

import pytest

import totalreclaw.import_state as ist
import totalreclaw.import_engine as ie
from totalreclaw.import_engine import ImportEngine
from totalreclaw.import_adapters.types import AdapterParseResult, ConversationChunk


class _BatchClient:
    """Lightweight fake mirroring rc8's ``_BatchClient``.

    ``remember_batch`` raises when ``fail_all`` is set OR any payload text is in
    ``fail_texts`` — modelling a store that extracts cleanly then raises/reverts
    on submit. ``find_duplicate_texts`` flags any text in ``dup_texts`` as an
    on-chain duplicate (the dedup path).
    """

    def __init__(self, *, fail_all=False, fail_texts=(), dup_texts=()):
        self.n = 0
        self.fail_all = fail_all
        self.fail_texts = tuple(fail_texts)
        self.dup_texts = tuple(dup_texts)

    async def _ensure_chain_id(self):
        return 100

    async def remember_batch(self, payloads, source=None):
        if self.fail_all:
            raise RuntimeError("bundler rejected the UserOp (paymaster down)")
        for p in payloads:
            text = (p or {}).get("text", "") if isinstance(p, dict) else ""
            if any(marker in text for marker in self.fail_texts):
                raise RuntimeError(
                    "-32500 reverted during simulation: store rejected this fact"
                )
        ids = [f"f{self.n + i}" for i in range(len(payloads))]
        self.n += len(payloads)
        return ids

    async def find_duplicate_texts(self, texts):
        return [any(d in (t or "") for d in self.dup_texts) for t in texts]


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


def _extractor():
    """One fact per chunk, carrying the chunk's user text so the fake store can
    raise selectively on it."""
    async def _extract(messages, timestamp):
        text = messages[0]["content"]
        return [{"text": f"fact from {text}", "type": "fact", "importance": 8}]
    return _extract


# ── (a) extract-succeeds → store-raises ⇒ NOT recorded; re-import re-extracts ──
def test_store_failure_keeps_conversation_unrecorded_and_re_importable(monkeypatch):
    """The core #473 repro: a chunk extracts a fact, but the store raises. The
    conversation must NOT be recorded — otherwise its fact is silently lost and
    re-import skips it forever."""
    _install_fake_embedding(monkeypatch)
    chunks = [_chunk("X", "xray one")]
    _install_adapter(monkeypatch, chunks)

    # Run 1: store raises for every fact.
    client = _BatchClient(fail_all=True)
    engine = ImportEngine(client=client, llm_extract=_extractor())
    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    assert result.facts_stored == 0          # store raised → nothing durable
    assert result.errors                     # the store failure is surfaced
    recorded = ist.load_imported_conversations("chatgpt")
    assert recorded == set(), recorded       # X NOT recorded → stays re-importable

    # Run 2 (re-import): store now succeeds. X was never recorded, so it is
    # re-processed — its fact is extracted AND durably stored this time, then X
    # is recorded. This is the recovery the bug used to silently block.
    client.fail_all = False
    engine2 = ImportEngine(client=client, llm_extract=_extractor())
    result2 = asyncio.run(engine2.process_batch(source="chatgpt", content="x"))
    assert result2.facts_stored >= 1
    assert "X" in ist.load_imported_conversations("chatgpt")


# ── (b) multi-chunk: one sibling stores, sibling's store fails ⇒ recorded ──────
def test_partial_store_failure_records_conversation_if_sibling_stored(monkeypatch):
    """#466 dup guard, now store-derived: conversation A spans 2 facts. The
    combined batch store raises (one fact rejects), halves, and the GOOD half
    stores while the bad half errors. A stored ≥1 fact ⇒ recorded (so re-import
    skips it) — even though a sibling fact's store failed."""
    _install_fake_embedding(monkeypatch)
    # a0 stores cleanly; a1's fact carries the marker the fake store raises on.
    chunks = [
        _chunk("A", "alpha good"),
        _chunk("A", "alpha kaboom"),
    ]
    _install_adapter(monkeypatch, chunks)
    client = _BatchClient(fail_texts=("kaboom",))
    engine = ImportEngine(client=client, llm_extract=_extractor())
    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    assert result.facts_stored >= 1          # the good sibling durably stored
    assert result.errors                     # the bad sibling's failure surfaced
    # A recorded because ≥1 fact stored — re-import must skip it (no dups).
    assert "A" in ist.load_imported_conversations("chatgpt")


# ── (c) all-duplicates conversation ⇒ STILL recorded ───────────────────────────
def test_all_duplicate_conversation_is_recorded(monkeypatch):
    """The subtlest case: a conversation whose facts are ALL on-chain
    duplicates (store reports dups_skipped, 0 new). The facts already exist, so
    the conversation IS durably stored — it must record, or every re-import
    re-extracts it forever (an infinite loop of zero-fact re-imports)."""
    _install_fake_embedding(monkeypatch)
    chunks = [_chunk("C", "charlie dupconv")]
    _install_adapter(monkeypatch, chunks)
    # find_duplicate_texts flags C's fact as already on-chain.
    client = _BatchClient(dup_texts=("dupconv",))
    engine = ImportEngine(client=client, llm_extract=_extractor())
    result = asyncio.run(engine.process_batch(source="chatgpt", content="x"))

    assert result.facts_stored == 0          # nothing new written
    assert result.dups_skipped >= 1          # deduped client-side
    assert "C" in ist.load_imported_conversations("chatgpt")
