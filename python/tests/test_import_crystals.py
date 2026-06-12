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


# Two entries in the same 30-min window: the Gemini adapter merges them into
# ONE chunk with 4 messages (2 turns). A 2-turn chunk is NOT a singleton so it
# emits a Crystal. This was updated for feat/semantic-session-grouping: the old
# single-entry fixture produced a 1-turn singleton which no longer emits a Crystal.
_GEMINI_ONE_CONVO = json.dumps([
    {
        "header": "Gemini Apps",
        "title": "Prompted I just moved to Berlin for a new job",
        "time": "2026-05-14T09:21:03.512Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Congrats on the move to Berlin!"}],
    },
    {
        "header": "Gemini Apps",
        "title": "Prompted What are the best neighbourhoods in Berlin?",
        "time": "2026-05-14T09:24:00.000Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Prenzlauer Berg and Mitte are popular."}],
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
        # New schema: {title, summary, key_outcomes, open_threads, topics_discussed}
        # Crystal text = title (the LLM-generated headline), not summary.
        return (
            '{"title": "Moving to Berlin for a new job", '
            '"summary": "User relocated to Berlin for a new job", '
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

    cm = crystals[0]["extra_metadata"]
    assert crystals[0]["fact_type"] == "summary"
    assert crystals[0]["provenance"] == "external"
    # Crystal TEXT = LLM title (the headline the SPA renders as the card title)
    assert crystals[0]["text"] == "Moving to Berlin for a new job"
    # Summary stored in metadata (not as Crystal text)
    assert cm["session_summary"] == "User relocated to Berlin for a new job"
    assert cm["session_title"] == "Moving to Berlin for a new job"
    assert cm["import_source"] == "gemini"
    assert cm["key_outcomes"] == ["moved to Berlin"]
    assert cm["topics_discussed"] == ["relocation", "work"]

    # Crystal + atomic facts share the SAME session_id.
    crystal_sid = cm["session_id"]
    for atom in atomic:
        assert atom["extra_metadata"]["session_id"] == crystal_sid, \
            "All facts must share the Crystal's session_id"
    # Atomic facts carry external provenance + provider.
    assert all(a["provenance"] == "external" for a in atomic)
    assert all(a["extra_metadata"]["import_source"] == "gemini" for a in atomic)


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
    # LLM exists but returned garbage -> still a Crystal.
    # Fallback title is now derived from the highest-importance fact (not
    # the generic "Imported conversation:" string).
    assert len(crystals) == 1
    title = crystals[0]["text"]
    # Should be from the top-importance fact, NOT the generic fallback
    assert "User moved to Berlin" in title or "Berlin" in title, \
        f"Fallback title should reference the top fact, got: {title!r}"
    assert crystals[0]["extra_metadata"]["import_source"] == "gemini"


def test_no_crystal_without_llm_completion(monkeypatch):
    _install_fake_embedding(monkeypatch)
    client = _BatchClient()

    async def fake_extract(messages, timestamp):
        return [{"text": "User moved to Berlin", "type": "fact", "importance": 8}]

    # No llm_completion → no Crystal. Multi-turn sessions still get session_id
    # (the session grouping happens regardless of whether a Crystal is emitted).
    engine = ImportEngine(client=client, llm_extract=fake_extract)
    asyncio.run(engine.process_batch(source="gemini", content=_GEMINI_ONE_CONVO))

    payloads = _payloads(client)
    assert all(
        (p.get("extra_metadata") or {}).get("subtype") != "session_crystal"
        for p in payloads
    )
    assert all(p["provenance"] == "external" for p in payloads)
    # _GEMINI_ONE_CONVO now has 2 entries -> 1 chunk with 2 turns -> multi-turn
    # session -> atomic facts DO get session_id (unlike singletons).
    assert all((p.get("extra_metadata") or {}).get("session_id") for p in payloads), \
        "Multi-turn session facts must have session_id even without llm_completion"


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
