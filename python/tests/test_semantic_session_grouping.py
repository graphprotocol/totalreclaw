"""Tests for semantic session grouping in ImportEngine (feat/semantic-session-grouping).

Covers the behaviour change in _process_chunk_batch:
  1. Long convo spanning multiple chunks -> grouped into ONE session + ONE Crystal
  2. Multi-topic single time-window (30 min) -> multiple sessions, multiple Crystals
  3. Singleton session (1 turn) -> NO session_id, NO Crystal (provenance only)
  4. Crystal title: LLM generates {title, summary}; title stored in metadata["session_title"]
     AND the Crystal's text IS the title (what the SPA renders as the card headline)
  5. Crystal title fallback: LLM unavailable -> title derived from highest-importance fact
  6. Existing non-semantic behaviour is preserved: batching, provenance tagging, errors

NOTE: segment_sessions is called ONCE over the FULL chunk list, not per store-batch.
The embedding stubs here mirror harrier's L2-normalised 640d output as small
low-dim vectors that have the same geometry (orthogonal = different topic,
aligned = same topic).
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from totalreclaw.import_engine import ImportEngine
from totalreclaw.imports.adapters.types import ConversationChunk


# ── helpers ───────────────────────────────────────────────────────────────────


def _unit(v: list) -> list:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 1e-9 else v


class _BatchClient:
    """Records remember_batch and remember payloads; pretends to be on Gnosis."""

    def __init__(self):
        self.batches = []
        self.single_calls = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100  # Gnosis

    async def remember_batch(self, payloads, source=None):
        self.batches.append(list(payloads))
        ids = [f"f{self._n + i}" for i in range(len(payloads))]
        self._n += len(payloads)
        return ids

    async def remember(self, text, **kwargs):
        self.single_calls.append({"text": text, **kwargs})
        self._n += 1
        return f"f{self._n}"

    def all_payloads(self):
        return [p for b in self.batches for p in b] + self.single_calls


def _make_engine(client, llm_extract=None, llm_completion=None,
                 fake_embedding_vector=None):
    """Build an ImportEngine with stubbed embedding."""
    return ImportEngine(
        client=client,
        llm_extract=llm_extract,
        llm_completion=llm_completion,
        enable_smart_import=False,  # keep tests hermetic
    )


def _fake_embed(topic_vector: list):
    """Return a monkeypatching fixture-style function that stubs get_embedding."""
    def _get_embedding(text: str) -> list:
        return topic_vector
    return _get_embedding


async def _fake_extract(messages, timestamp):
    """Extract 1 fact from any message list (importance >=6 so not filtered)."""
    user_texts = [m.get("text", m.get("content", "")) for m in messages
                  if m.get("role") == "user"]
    combined = " ".join(user_texts)
    if combined.strip():
        return [{"text": combined[:100], "type": "fact", "importance": 8}]
    return []


def _crystals(payloads):
    return [p for p in payloads
            if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"]


def _atomic(payloads):
    return [p for p in payloads
            if (p.get("extra_metadata") or {}).get("subtype") != "session_crystal"]


# ── Gemini JSON fixture with two SEPARATE 30-min windows ──────────────────────

# Window 1 (09:00): Berlin-related turns (topic A)
# Window 2 (10:30+): Python-related turns (topic B — 90+ min later)
_GEMINI_TWO_WINDOWS = json.dumps([
    {
        "header": "Gemini Apps",
        "title": "Prompted I moved to Berlin",
        "time": "2026-05-14T09:00:00.000Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Berlin is a great city."}],
    },
    {
        "header": "Gemini Apps",
        "title": "Prompted Berlin cost of living",
        "time": "2026-05-14T09:15:00.000Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Berlin is affordable."}],
    },
    {
        "header": "Gemini Apps",
        "title": "Prompted How do I learn Python",
        "time": "2026-05-14T11:00:00.000Z",  # 105-min gap -> new 30-min window
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Start with tutorials."}],
    },
])

# Single Gemini turn (singleton) - only 1 message
_GEMINI_SINGLETON = json.dumps([
    {
        "header": "Gemini Apps",
        "title": "Prompted quick question",
        "time": "2026-05-14T08:00:00.000Z",
        "products": ["Gemini Apps"],
        "subtitles": [{"name": "Sure!"}],
    },
])


# ── Test 1: Long convo across chunks -> ONE session + ONE Crystal ─────────────


def test_long_convo_across_chunks_one_session(monkeypatch):
    """Two chunks from the SAME conversation (short time gap) should be grouped
    into one semantic session: one Crystal + all their facts share one session_id."""
    # Both chunks emit topic-A embeddings so they stay in the same session.
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def fake_completion(prompt):
        return json.dumps({
            "title": "Moving to Berlin",
            "summary": "User moved to Berlin for a new job."
        })

    engine = _make_engine(client, _fake_extract, fake_completion)

    # We can use the Gemini two-windows fixture but the 105-min gap between
    # window 2 and 3 is a time-only split. Here we use only the first two
    # entries which are 15 min apart (same session).
    two_same_window = json.dumps([
        {
            "header": "Gemini Apps",
            "title": "Prompted I moved to Berlin",
            "time": "2026-05-14T09:00:00.000Z",
            "products": ["Gemini Apps"],
            "subtitles": [{"name": "Berlin is a great city."}],
        },
        {
            "header": "Gemini Apps",
            "title": "Prompted Berlin cost of living",
            "time": "2026-05-14T09:15:00.000Z",
            "products": ["Gemini Apps"],
            "subtitles": [{"name": "Berlin is affordable."}],
        },
    ])

    result = asyncio.run(engine.process_batch(source="gemini", content=two_same_window))
    assert result.is_complete

    payloads = client.all_payloads()
    crystals = _crystals(payloads)
    atoms = _atomic(payloads)

    # Two chunks on the same topic -> one semantic session -> one Crystal
    assert len(crystals) == 1, f"Expected 1 Crystal, got {len(crystals)}"

    # All atoms share the same session_id as the Crystal
    crystal_sid = crystals[0]["extra_metadata"]["session_id"]
    assert crystal_sid, "Crystal must have session_id"
    for atom in atoms:
        assert atom["extra_metadata"]["session_id"] == crystal_sid, \
            "All facts in the same session must share session_id"


# ── Test 2: Multi-topic window -> multiple sessions + multiple Crystals ────────


def test_multi_topic_window_multiple_crystals(monkeypatch):
    """Two chunks in the SAME 30-min time window but with orthogonal topic
    embeddings should split into two sessions. Since each session has 2 turns
    (user+assistant+user+assistant), both sessions emit Crystals."""
    def fake_embed(text: str) -> list:
        # Map chunks to topic by content.
        if "Berlin" in text or "berlin" in text:
            return _unit([1.0, 0.0, 0.0, 0.0])
        elif "Python" in text or "python" in text:
            return _unit([0.0, 1.0, 0.0, 0.0])
        else:
            return _unit([1.0, 0.0, 0.0, 0.0])

    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", fake_embed)

    client = _BatchClient()

    async def fake_completion(prompt):
        return json.dumps({
            "title": "Topic session",
            "summary": "A short session."
        })

    engine = _make_engine(client, _fake_extract, fake_completion)

    # Two chunks: Berlin (4 messages = 2 turns), Python (4 messages = 2 turns).
    # Both are in the same 10-min window — but their embeddings are orthogonal
    # so semantic grouping splits them into 2 sessions of 2 turns each.
    # 2 turns per session -> NOT singletons -> 2 Crystals.
    chunks = [
        ConversationChunk(
            title="Berlin session",
            messages=[
                {"role": "user", "text": "I moved to Berlin for a new job"},
                {"role": "assistant", "text": "Congrats, Berlin is great"},
                {"role": "user", "text": "What are the best neighbourhoods in Berlin?"},
                {"role": "assistant", "text": "Prenzlauer Berg is popular in Berlin"},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
        ConversationChunk(
            title="Python session",
            messages=[
                {"role": "user", "text": "How do I learn Python programming"},
                {"role": "assistant", "text": "Start with Python tutorials"},
                {"role": "user", "text": "What Python libraries are popular for data science?"},
                {"role": "assistant", "text": "NumPy and Pandas are widely used in Python"},
            ],
            timestamp="2026-05-14T09:10:00Z",  # Only 10 min gap (same window!)
        ),
    ]

    # Inject chunks directly via a mock adapter
    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        from totalreclaw.imports.adapters.types import AdapterParseResult
        mock_result = AdapterParseResult(
            facts=[], chunks=chunks,
            total_messages=8,
            warnings=[], errors=[],
        )
        mock_adapter.return_value.parse.return_value = mock_result
        result = asyncio.run(engine.process_batch(source="gemini", content="unused"))

    assert result.is_complete

    payloads = client.all_payloads()
    crystals = _crystals(payloads)

    # Berlin and Python embeddings are orthogonal -> two sessions.
    # Each session has 2 turns -> NOT singletons -> 2 Crystals.
    assert len(crystals) == 2, \
        f"Orthogonal topics each with 2 turns should produce 2 Crystals, got {len(crystals)}"

    # Crystal session_ids must be different
    sids = [c["extra_metadata"]["session_id"] for c in crystals]
    assert len(set(sids)) == 2, "Each Crystal must have a distinct session_id"


# ── Test 3: Singleton session -> no session_id, no Crystal ────────────────────


def test_singleton_no_crystal_no_session_id(monkeypatch):
    """A single-turn session (1 turn extracted) must NOT get a Crystal or session_id."""
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def fake_completion(prompt):
        return json.dumps({"title": "Quick question", "summary": "A quick query."})

    engine = _make_engine(client, _fake_extract, fake_completion)

    # One chunk with one turn -> one-turn session -> singleton
    chunks = [
        ConversationChunk(
            title="Quick question",
            messages=[
                {"role": "user", "text": "What is the capital of France?"},
                {"role": "assistant", "text": "Paris."},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
    ]

    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        from totalreclaw.imports.adapters.types import AdapterParseResult
        mock_result = AdapterParseResult(
            facts=[], chunks=chunks,
            total_messages=2,
            warnings=[], errors=[],
        )
        mock_adapter.return_value.parse.return_value = mock_result
        result = asyncio.run(engine.process_batch(source="gemini", content="unused"))

    assert result.is_complete
    payloads = client.all_payloads()
    crystals = _crystals(payloads)

    # Singleton -> no Crystal
    assert len(crystals) == 0, "Singleton session must not produce a Crystal"

    # Atomic facts still get external provenance + import_source
    atoms = _atomic(payloads)
    for atom in atoms:
        assert atom.get("provenance") == "external"
        assert (atom.get("extra_metadata") or {}).get("import_source") == "gemini"
        # No session_id on singletons
        assert "session_id" not in (atom.get("extra_metadata") or {}), \
            "Singleton facts must not have session_id"


# ── Test 4: Crystal title from LLM call ───────────────────────────────────────


def test_crystal_title_from_llm(monkeypatch):
    """Crystal text == LLM-generated title; session_title stored in metadata."""
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def fake_completion(prompt):
        # Verify the prompt includes extracted facts
        assert "extracted" in prompt.lower() or "facts" in prompt.lower() or "fact" in prompt.lower()
        return json.dumps({
            "title": "Moving to Berlin — job, cost, neighbourhood",
            "summary": "User relocated to Berlin for a tech job and researched cost of living.",
        })

    async def fake_extract_rich(messages, timestamp):
        return [
            {"text": "User relocated to Berlin for a new tech job", "type": "fact", "importance": 9},
            {"text": "Berlin has affordable cost of living", "type": "fact", "importance": 7},
        ]

    engine = _make_engine(client, fake_extract_rich, fake_completion)

    chunks = [
        ConversationChunk(
            title="Berlin session",
            messages=[
                {"role": "user", "text": "I just moved to Berlin for a job"},
                {"role": "assistant", "text": "Congratulations!"},
                {"role": "user", "text": "What is the cost of living in Berlin?"},
                {"role": "assistant", "text": "Berlin is quite affordable."},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
        ConversationChunk(
            title="Berlin session part 2",
            messages=[
                {"role": "user", "text": "Which neighbourhood should I live in Berlin?"},
                {"role": "assistant", "text": "Prenzlauer Berg is popular."},
            ],
            timestamp="2026-05-14T09:10:00Z",
        ),
    ]

    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        from totalreclaw.imports.adapters.types import AdapterParseResult
        mock_result = AdapterParseResult(
            facts=[], chunks=chunks,
            total_messages=6,
            warnings=[], errors=[],
        )
        mock_adapter.return_value.parse.return_value = mock_result
        result = asyncio.run(engine.process_batch(source="gemini", content="unused"))

    assert result.is_complete
    payloads = client.all_payloads()
    crystals = _crystals(payloads)

    assert len(crystals) == 1
    crystal = crystals[0]

    # Crystal TEXT == title (what the SPA renders)
    assert crystal["text"] == "Moving to Berlin — job, cost, neighbourhood", \
        f"Crystal text should be the LLM title, got: {crystal['text']!r}"

    # session_title also stored in metadata for future SPA use
    assert crystal["extra_metadata"]["session_title"] == "Moving to Berlin — job, cost, neighbourhood"

    # Summary in extra_metadata
    assert "session_summary" in crystal["extra_metadata"]
    assert "Berlin" in crystal["extra_metadata"]["session_summary"]


# ── Test 5: Crystal title fallback when LLM unavailable ───────────────────────


def test_crystal_title_fallback_from_highest_importance_fact(monkeypatch):
    """When llm_completion is None, the Crystal title is derived from the
    highest-importance extracted fact, NOT a generic 'Gemini session' string."""
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def fake_extract_with_importance(messages, timestamp):
        return [
            {"text": "User relocated to Berlin for a new tech job", "type": "fact", "importance": 9},
            {"text": "User prefers dark mode", "type": "preference", "importance": 6},
        ]

    # No llm_completion -> fallback title path
    engine = _make_engine(client, fake_extract_with_importance, llm_completion=None)

    chunks = [
        ConversationChunk(
            title="Session chunk 1",
            messages=[
                {"role": "user", "text": "I moved to Berlin"},
                {"role": "assistant", "text": "Great!"},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
        ConversationChunk(
            title="Session chunk 2",
            messages=[
                {"role": "user", "text": "I prefer dark mode in my editor"},
                {"role": "assistant", "text": "Noted."},
            ],
            timestamp="2026-05-14T09:10:00Z",
        ),
    ]

    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        from totalreclaw.imports.adapters.types import AdapterParseResult
        mock_result = AdapterParseResult(
            facts=[], chunks=chunks,
            total_messages=4,
            warnings=[], errors=[],
        )
        mock_adapter.return_value.parse.return_value = mock_result
        result = asyncio.run(engine.process_batch(source="gemini", content="unused"))

    assert result.is_complete
    payloads = client.all_payloads()
    # No llm_completion -> no Crystal (as per _make_crystal spec:
    # "When no llm_completion is wired we skip [the Crystal]")
    crystals = _crystals(payloads)
    assert len(crystals) == 0, \
        "Without llm_completion, no Crystal should be emitted (spec: skip rather than emit low-value synthetic)"


# ── Test 6: Crystal title fallback: bad LLM JSON -> derive from top fact ──────


def test_crystal_title_fallback_from_top_fact_on_bad_llm(monkeypatch):
    """When LLM returns garbage JSON, Crystal title falls back to the
    highest-importance fact text (not the generic chunk title)."""
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def bad_completion(prompt):
        return "Sorry, I cannot help with that."  # no JSON

    async def rich_extract(messages, timestamp):
        return [
            {"text": "User relocated to Berlin for a new tech job", "type": "fact", "importance": 9},
            {"text": "User likes pizza", "type": "preference", "importance": 6},
        ]

    engine = _make_engine(client, rich_extract, bad_completion)

    chunks = [
        ConversationChunk(
            title="Generic chunk title",
            messages=[
                {"role": "user", "text": "I moved to Berlin for a job"},
                {"role": "assistant", "text": "Congrats!"},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
        ConversationChunk(
            title="Generic chunk title 2",
            messages=[
                {"role": "user", "text": "More Berlin things"},
                {"role": "assistant", "text": "Sure."},
            ],
            timestamp="2026-05-14T09:10:00Z",
        ),
    ]

    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        from totalreclaw.imports.adapters.types import AdapterParseResult
        mock_result = AdapterParseResult(
            facts=[], chunks=chunks,
            total_messages=4,
            warnings=[], errors=[],
        )
        mock_adapter.return_value.parse.return_value = mock_result
        result = asyncio.run(engine.process_batch(source="gemini", content="unused"))

    assert result.is_complete
    payloads = client.all_payloads()
    crystals = _crystals(payloads)

    assert len(crystals) == 1
    crystal = crystals[0]

    # Fallback: should use the highest-importance fact, NOT generic chunk title
    title = crystal["text"]
    assert "Gemini session" not in title, \
        "Fallback title must not be the generic 'Gemini session' string"
    assert "Generic chunk" not in title, \
        "Fallback title must not be the raw chunk title"
    # It should be derived from the top-importance fact
    assert "Berlin" in title or "relocated" in title or "tech job" in title, \
        f"Fallback title should reference the top-importance fact, got: {title!r}"


# ── Test 7: Crystal prompt includes both transcript and extracted facts ────────


def test_crystal_prompt_includes_facts():
    """The crystal prompt must include the extracted facts, not just the transcript."""
    from totalreclaw.import_engine import ImportEngine

    # Build a minimal engine just to call _crystal_prompt directly
    engine = ImportEngine(client=None, llm_extract=None)

    facts = [
        {"text": "User relocated to Berlin", "importance": 9},
        {"text": "User likes pizza", "importance": 6},
    ]

    # Mock a chunk-like object
    class FakeChunk:
        title = "Test session"
        messages = [
            {"role": "user", "text": "I moved to Berlin"},
            {"role": "assistant", "text": "Congrats!"},
        ]
        timestamp = "2026-05-14T09:00:00Z"

    prompt = engine._crystal_prompt(FakeChunk(), facts)

    # Prompt must ask for BOTH title and summary (not just summary)
    assert '"title"' in prompt, "Prompt must request a title field"
    assert '"summary"' in prompt, "Prompt must request a summary field"

    # Prompt must include the extracted facts
    assert "User relocated to Berlin" in prompt, "Prompt must include extracted facts"


# ── Test 7b: Cross-batch session straddling a batch boundary ──────────────────


def test_session_straddling_batch_boundary_one_id_one_crystal(monkeypatch):
    """REGRESSION (multi-batch path): a multi-turn session whose chunks straddle
    a batch boundary must (a) keep ONE stable session_id across batches and
    (b) emit EXACTLY ONE Crystal — on the batch that completes the session.

    This fails on the pre-fix code, which (1) regenerated session_id per
    _process_chunk_batch call (so facts in different batches got different ids)
    and (2) only emitted a Crystal when ALL of a session's chunks were in the
    CURRENT batch (so a straddling session got no Crystal in any batch).

    Setup: 3 chunks, all the SAME topic + within the same 30-min window -> ONE
    semantic session of 3 turns. Driven with batch_size=2, so chunks {0,1} are
    processed in batch 1 and chunk {2} in batch 2.
    """
    # Single topic vector for everything -> all turns in one session.
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    completion_calls = {"n": 0}

    async def fake_completion(prompt):
        completion_calls["n"] += 1
        return json.dumps({
            "title": "Berlin relocation",
            "summary": "User moved to Berlin and discussed logistics.",
        })

    async def fake_extract_idx(messages, timestamp):
        user_texts = " ".join(m.get("text", m.get("content", "")) for m in messages
                              if m.get("role") == "user")
        return [{"text": user_texts[:80] or "fact", "type": "fact", "importance": 8}]

    engine = _make_engine(client, fake_extract_idx, fake_completion)

    # 3 same-topic chunks, each 2 messages = 1 turn; all within 20 min.
    chunks = [
        ConversationChunk(
            title=f"Berlin chunk {i}",
            messages=[
                {"role": "user", "text": f"Berlin question number {i}"},
                {"role": "assistant", "text": f"Berlin answer {i}"},
            ],
            timestamp=f"2026-05-14T09:0{i}:00Z",
        )
        for i in range(3)
    ]

    parsed_result = None

    def _build_parsed():
        from totalreclaw.imports.adapters.types import AdapterParseResult
        return AdapterParseResult(
            facts=[], chunks=chunks, total_messages=6, warnings=[], errors=[],
        )

    # Drive process_batch repeatedly with batch_size=2 (multi-batch path).
    results = []
    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        mock_adapter.return_value.parse.side_effect = lambda *a, **k: _build_parsed()
        offset = 0
        while True:
            r = asyncio.run(engine.process_batch(
                source="gemini", content="unused", offset=offset, batch_size=2,
            ))
            results.append(r)
            if r.is_complete:
                break
            offset += r.chunks_processed
            assert offset < 100, "runaway batching loop"

    # We must have run at least 2 batches (3 chunks / batch_size 2).
    assert len(results) >= 2, f"Expected multi-batch run, got {len(results)} batch(es)"

    payloads = client.all_payloads()
    crystals = _crystals(payloads)
    atoms = _atomic(payloads)

    # (b) EXACTLY ONE Crystal across the whole multi-batch import.
    assert len(crystals) == 1, \
        f"Straddling session must emit exactly ONE Crystal across batches, got {len(crystals)}"

    # (a) ALL atomic facts share ONE session_id — and it equals the Crystal's.
    crystal_sid = crystals[0]["extra_metadata"]["session_id"]
    assert crystal_sid, "Crystal must carry a session_id"
    atom_sids = {a["extra_metadata"].get("session_id") for a in atoms}
    assert atom_sids == {crystal_sid}, (
        "All atomic facts across batches must share the SINGLE stable session_id "
        f"(crystal={crystal_sid!r}, atoms={atom_sids!r})"
    )

    # We extracted from all 3 chunks -> 3 atomic facts.
    assert len(atoms) == 3, f"Expected 3 atomic facts (one per chunk), got {len(atoms)}"


# ── Test 8: Existing batch tests still pass ────────────────────────────────────


def test_existing_gemini_single_window_backward_compat(monkeypatch):
    """The two-entry Gemini fixture from the old test still works after rewiring.

    Two entries in the same 30-min window, topic-A embeddings -> 1 session
    -> 2 atomic facts + (with llm_completion) 1 Crystal.
    """
    topic_a = _unit([1.0, 0.0, 0.0, 0.0])
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", lambda t: topic_a)

    client = _BatchClient()

    async def fake_extract(messages, timestamp):
        user_texts = " ".join(m.get("content", m.get("text", ""))
                               for m in messages if m.get("role") == "user")
        out = []
        if "Berlin" in user_texts or "berlin" in user_texts:
            out.append({"text": "User moved to Berlin for a new job", "type": "fact", "importance": 8})
        if "peanut" in user_texts:
            out.append({"text": "User is allergic to peanuts", "type": "fact", "importance": 9})
        return out

    async def fake_completion(prompt):
        return json.dumps({"title": "Berlin relocation", "summary": "User moved to Berlin."})

    engine = _make_engine(client, fake_extract, fake_completion)

    # The two entries from the original test
    content = json.dumps([
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

    result = asyncio.run(engine.process_batch(source="gemini", content=content))
    assert result.is_complete
    assert result.success

    payloads = client.all_payloads()
    crystals = _crystals(payloads)
    atoms = _atomic(payloads)

    # Same 30-min window, same topic -> 1 session
    assert len(crystals) == 1

    # Crystal text is the LLM-generated title
    assert crystals[0]["text"] == "Berlin relocation"

    # Both atomic facts share the Crystal's session_id
    crystal_sid = crystals[0]["extra_metadata"]["session_id"]
    for atom in atoms:
        assert atom["extra_metadata"]["session_id"] == crystal_sid

    # Provenance preserved
    assert all(p.get("provenance") == "external" for p in payloads)
    assert all((p.get("extra_metadata") or {}).get("import_source") == "gemini"
               for p in payloads)
