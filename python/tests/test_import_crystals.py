"""#356 — per-conversation Crystals + import provenance.

Each imported conversation = one session: a Crystal (type=summary,
subtype=session_crystal) + its atomic facts sharing a UUIDv7 session_id, every
claim tagged source=external + metadata.import_source=<provider>.

No network: a recording client captures the batched payloads; embedding +
LLM are stubbed.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from totalreclaw.import_engine import (
    ImportEngine,
    _uuid7,
    _extract_json_object,
    _as_str_list,
)


class _BatchClient:
    """Records remember_batch payloads; pretends to be on Gnosis (chain 100)."""

    def __init__(self):
        self.batches = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100

    async def remember_batch(self, payloads, source=None):
        self.batches.append({"payloads": list(payloads), "source": source})
        ids = [f"f{self._n + i}" for i in range(len(payloads))]
        self._n += len(payloads)
        return ids


def _install_fake_embedding(monkeypatch):
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: [0.1, 0.2, 0.3])


_GEMINI_ONE_CONVO = json.dumps([
    {
        "header": "Gemini Apps",
        "title": "Prompted I just moved to Berlin for a new job",
        "time": "2026-05-14T09:21:03.512Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Congrats on the move to Berlin!"}],
    },
])


def _payloads(client):
    return [p for b in client.batches for p in b["payloads"]]


# ── helpers ──────────────────────────────────────────────────────────────


def test_uuid7_is_valid_version_7():
    s = _uuid7()
    u = uuid.UUID(s)
    assert u.version == 7
    # variant nibble is RFC-4122 (10xx)
    assert (u.int >> 62) & 0b11 == 0b10


def test_uuid7_is_time_ordered():
    a, b = _uuid7(), _uuid7()
    # First 48 bits (ms timestamp) are non-decreasing.
    assert uuid.UUID(a).int >> 80 <= uuid.UUID(b).int >> 80


def test_extract_json_object_from_fenced_prose():
    raw = "Here you go:\n```json\n{\"summary\": \"x\", \"key_outcomes\": [\"a\"]}\n```\nDone"
    obj = _extract_json_object(raw)
    assert obj == {"summary": "x", "key_outcomes": ["a"]}


def test_as_str_list_coerces_and_caps():
    assert _as_str_list(["a", "", "  b ", 3]) == ["a", "b", "3"]
    assert _as_str_list("nope") == []
    assert len(_as_str_list([str(i) for i in range(20)])) == 8


# ── Crystal emission ─────────────────────────────────────────────────────


def test_crystal_emitted_with_llm(monkeypatch):
    _install_fake_embedding(monkeypatch)
    client = _BatchClient()

    async def fake_extract(messages, timestamp):
        return [{"text": "User moved to Berlin", "type": "fact", "importance": 8}]

    async def fake_completion(prompt):
        return (
            '{"summary": "User relocated to Berlin for a new job", '
            '"key_outcomes": ["moved to Berlin"], "open_threads": [], '
            '"topics_discussed": ["relocation", "work"]}'
        )

    engine = ImportEngine(
        client=client, llm_extract=fake_extract, llm_completion=fake_completion
    )
    result = asyncio.run(
        engine.process_batch(source="gemini", content=_GEMINI_ONE_CONVO)
    )
    assert result.is_complete

    payloads = _payloads(client)
    crystals = [
        p for p in payloads
        if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"
    ]
    atomic = [
        p for p in payloads
        if (p.get("extra_metadata") or {}).get("subtype") != "session_crystal"
    ]
    assert len(crystals) == 1, "exactly one Crystal per conversation"
    assert len(atomic) == 1

    cm = crystals[0]["extra_metadata"]
    assert crystals[0]["fact_type"] == "summary"
    assert crystals[0]["provenance"] == "external"
    assert crystals[0]["text"] == "User relocated to Berlin for a new job"
    assert cm["import_source"] == "gemini"
    assert cm["key_outcomes"] == ["moved to Berlin"]
    assert cm["topics_discussed"] == ["relocation", "work"]

    # Crystal + atomic fact share the SAME session_id.
    assert cm["session_id"] == atomic[0]["extra_metadata"]["session_id"]
    # Atomic fact carries external provenance + provider.
    assert atomic[0]["provenance"] == "external"
    assert atomic[0]["extra_metadata"]["import_source"] == "gemini"


def test_crystal_synthetic_fallback_on_bad_llm(monkeypatch):
    _install_fake_embedding(monkeypatch)
    client = _BatchClient()

    async def fake_extract(messages, timestamp):
        return [{"text": "User moved to Berlin", "type": "fact", "importance": 8}]

    async def bad_completion(prompt):
        return "sorry, I can't help with that"  # no JSON

    engine = ImportEngine(
        client=client, llm_extract=fake_extract, llm_completion=bad_completion
    )
    asyncio.run(engine.process_batch(source="gemini", content=_GEMINI_ONE_CONVO))

    crystals = [
        p for p in _payloads(client)
        if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"
    ]
    # LLM exists but returned garbage → still a Crystal, synthetic headline.
    assert len(crystals) == 1
    assert crystals[0]["text"].startswith("Imported conversation:")
    assert crystals[0]["extra_metadata"]["import_source"] == "gemini"


def test_no_crystal_without_llm_completion(monkeypatch):
    _install_fake_embedding(monkeypatch)
    client = _BatchClient()

    async def fake_extract(messages, timestamp):
        return [{"text": "User moved to Berlin", "type": "fact", "importance": 8}]

    # No llm_completion → no Crystal, but atomic facts still get session_id.
    engine = ImportEngine(client=client, llm_extract=fake_extract)
    asyncio.run(engine.process_batch(source="gemini", content=_GEMINI_ONE_CONVO))

    payloads = _payloads(client)
    assert all(
        (p.get("extra_metadata") or {}).get("subtype") != "session_crystal"
        for p in payloads
    )
    assert all(p["provenance"] == "external" for p in payloads)
    assert all(p["extra_metadata"]["session_id"] for p in payloads)


# ── operations: metadata threads into the canonical claim ────────────────


def test_build_canonical_claim_carries_import_source():
    from totalreclaw.claims_helper import build_canonical_claim_v1

    blob = build_canonical_claim_v1(
        {"text": "User moved to Berlin", "type": "claim", "source": "external"},
        importance=8,
        created_at="2026-05-14T09:21:03Z",
        claim_id="01902d40-7a2b-7f12-9c44-1c5e7d2af6a1",
        extra_metadata={"session_id": "s1", "import_source": "gemini"},
    )
    doc = json.loads(blob)
    assert doc["metadata"]["import_source"] == "gemini"
    assert doc["metadata"]["session_id"] == "s1"
    assert doc["source"] == "external"
