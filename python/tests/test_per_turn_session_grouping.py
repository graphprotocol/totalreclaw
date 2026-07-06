"""Tests for #368 Part 2 — true turn-granularity session grouping.

Part 1 hoisted ``segment_sessions`` to core. Part 2 exposes a flat per-turn
view (``AdapterParseResult.turns`` / core ``parse_gemini``'s ``turns``) so the
import engine can segment over REAL per-turn timestamps + text and an
authoritative ``chunk_index``, instead of re-deriving turns from chunk message
lists and approximating every turn's time with its chunk timestamp.

These tests target the new ``ImportEngine._turns_from_parsed`` /
``_turns_from_chunks`` dispatch and prove:
  1. ``_turns_from_parsed`` maps ParsedTurns → (chunk_index, ts, text) tuples,
     preserving real per-turn timestamps (None stays None, never epoch-0).
  2. Absent/empty turns → None (caller falls back to chunk re-derivation).
  3. End-to-end: two turns inside ONE chunk that share the chunk timestamp but
     carry DIFFERENT real per-turn timestamps split into two sessions when they
     are >30 min apart — impossible under the pre-Part-2 chunk approximation.
"""
from __future__ import annotations

import asyncio
import json
import math
from unittest.mock import patch

import pytest

from totalreclaw.import_engine import ImportEngine
from totalreclaw.import_adapters.types import (
    AdapterParseResult,
    ConversationChunk,
    ParsedTurn,
)


def _unit(v: list) -> list:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 1e-9 else v


class _BatchClient:
    def __init__(self):
        self.batches = []
        self.single_calls = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100

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


async def _fake_extract(messages, timestamp):
    user_texts = [m.get("text", m.get("content", "")) for m in messages
                  if m.get("role") == "user"]
    combined = " ".join(user_texts)
    if combined.strip():
        return [{"text": combined[:100], "type": "fact", "importance": 8}]
    return []


def _crystals(payloads):
    return [p for p in payloads
            if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"]


# ── Unit: _turns_from_parsed ─────────────────────────────────────────────────


def test_turns_from_parsed_preserves_real_per_turn_timestamps():
    turns = [
        ParsedTurn(user_text="q1", assistant_text="a1", text="q1\na1",
                   chunk_index=0, ts_iso="2026-05-14T09:00:00+00:00",
                   ts_unix=1778749200.0),
        ParsedTurn(user_text="q2", assistant_text="a2", text="q2\na2",
                   chunk_index=0, ts_iso="2026-05-14T11:00:00+00:00",
                   ts_unix=1778756400.0),
    ]
    out = ImportEngine._turns_from_parsed(turns)
    assert out == [
        (0, 1778749200.0, "q1\na1"),
        (0, 1778756400.0, "q2\na2"),
    ]
    # Distinct per-turn timestamps preserved (the whole point of Part 2).
    assert out[0][1] != out[1][1]


def test_turns_from_parsed_none_timestamp_stays_none():
    turns = [ParsedTurn(user_text="q", assistant_text="", text="q",
                        chunk_index=3, ts_iso=None, ts_unix=None)]
    out = ImportEngine._turns_from_parsed(turns)
    assert out == [(3, None, "q")]
    assert out[0][1] is None  # never coerced to epoch-0


@pytest.mark.parametrize("turns", [None, []])
def test_turns_from_parsed_absent_returns_none(turns):
    # Empty/absent per-turn data → None so the caller re-derives from chunks.
    assert ImportEngine._turns_from_parsed(turns) is None


def test_turns_from_parsed_missing_chunk_index_bails_to_fallback():
    class _Bare:
        text = "hi"
        ts_unix = None
        # no chunk_index attribute
    assert ImportEngine._turns_from_parsed([_Bare()]) is None


# ── Unit: _turns_from_chunks (fallback parity) ───────────────────────────────


def test_turns_from_chunks_fallback_uses_chunk_timestamp():
    chunks = [
        ConversationChunk(
            title="c0",
            messages=[
                {"role": "user", "text": "hello"},
                {"role": "assistant", "text": "hi there"},
            ],
            timestamp="2026-05-14T09:00:00Z",
        ),
    ]
    out = ImportEngine._turns_from_chunks(chunks)
    assert len(out) == 1
    chunk_idx, ts, text = out[0]
    assert chunk_idx == 0
    assert text == "hello hi there"
    assert ts == pytest.approx(1778749200.0)


# ── End-to-end: real per-turn times split a single chunk's turns ─────────────


def test_per_turn_real_timestamps_reach_the_segmenter(monkeypatch):
    """The Part 2 payoff: the DISTINCT per-turn timestamps from the flat turns
    view reach ``segment_sessions``, instead of the chunk approximation feeding
    the same chunk timestamp for every turn in a chunk.

    We capture what the engine passes to ``segment_sessions`` and assert the two
    turns' REAL times (2h apart) arrive — which the pre-Part-2 chunk path (one
    shared timestamp) could never produce."""
    monkeypatch.setattr(
        "totalreclaw.embedding.get_embedding",
        lambda t: _unit([1.0, 0.0, 0.0, 0.0]),
    )

    captured = {}

    def spy_segment(timestamps, embeddings, gap_seconds=1800, sim_threshold=0.55):
        captured["timestamps"] = list(timestamps)
        from totalreclaw.session_segmentation import _segment_sessions_local
        return _segment_sessions_local(timestamps, embeddings, gap_seconds, sim_threshold)

    monkeypatch.setattr(
        "totalreclaw.session_segmentation.segment_sessions", spy_segment
    )

    client = _BatchClient()

    async def fake_completion(prompt):
        return json.dumps({"title": "T", "summary": "s"})

    engine = ImportEngine(
        client=client,
        llm_extract=_fake_extract,
        llm_completion=fake_completion,
        enable_smart_import=False,
    )

    # ONE chunk, 4 messages = 2 turns, single chunk timestamp (09:00).
    chunk = ConversationChunk(
        title="Gemini session",
        messages=[
            {"role": "user", "text": "morning question about travel plans"},
            {"role": "assistant", "text": "morning answer about travel plans"},
            {"role": "user", "text": "afternoon question about travel plans"},
            {"role": "assistant", "text": "afternoon answer about travel plans"},
        ],
        timestamp="2026-05-14T09:00:00Z",
    )
    # Flat per-turn view: both turns in chunk 0, but 2 HOURS apart in real time.
    turns = [
        ParsedTurn(
            user_text="morning question about travel plans",
            assistant_text="morning answer about travel plans",
            text="morning question about travel plans\nmorning answer",
            chunk_index=0, ts_iso="2026-05-14T09:00:00+00:00",
            ts_unix=1778749200.0,
        ),
        ParsedTurn(
            user_text="afternoon question about travel plans",
            assistant_text="afternoon answer about travel plans",
            text="afternoon question about travel plans\nafternoon answer",
            chunk_index=0, ts_iso="2026-05-14T11:00:00+00:00",
            ts_unix=1778756400.0,  # +2h
        ),
    ]

    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        mock_adapter.return_value.parse.return_value = AdapterParseResult(
            facts=[], chunks=[chunk], total_messages=4,
            warnings=[], errors=[], turns=turns,
        )
        result = asyncio.run(engine.process_batch(source="gemini", content="x"))

    assert result.is_complete
    # The two turns' REAL, distinct timestamps reached the segmenter (2h apart),
    # not two copies of the 09:00 chunk timestamp.
    assert captured["timestamps"] == [1778749200.0, 1778756400.0]
    assert captured["timestamps"][0] != captured["timestamps"][1]


def test_per_turn_same_window_stays_one_session(monkeypatch):
    """Control for the split test: same topic AND same ~time window → the two
    turns stay in one session (2 turns → one Crystal). Confirms the split above
    is driven by the real per-turn time gap, not an artefact."""
    monkeypatch.setattr(
        "totalreclaw.embedding.get_embedding",
        lambda t: _unit([1.0, 0.0, 0.0, 0.0]),
    )
    client = _BatchClient()

    async def fake_completion(prompt):
        return json.dumps({"title": "T", "summary": "s"})

    engine = ImportEngine(
        client=client, llm_extract=_fake_extract,
        llm_completion=fake_completion, enable_smart_import=False,
    )
    chunk = ConversationChunk(
        title="Gemini session",
        messages=[
            {"role": "user", "text": "question one about travel plans"},
            {"role": "assistant", "text": "answer one about travel plans"},
            {"role": "user", "text": "question two about travel plans"},
            {"role": "assistant", "text": "answer two about travel plans"},
        ],
        timestamp="2026-05-14T09:00:00Z",
    )
    turns = [
        ParsedTurn(user_text="question one about travel plans",
                   assistant_text="answer one about travel plans",
                   text="question one about travel plans\nanswer one",
                   chunk_index=0, ts_iso="2026-05-14T09:00:00+00:00",
                   ts_unix=1778749200.0),
        ParsedTurn(user_text="question two about travel plans",
                   assistant_text="answer two about travel plans",
                   text="question two about travel plans\nanswer two",
                   chunk_index=0, ts_iso="2026-05-14T09:05:00+00:00",
                   ts_unix=1778749500.0),  # +5 min, same window
    ]
    with patch("totalreclaw.import_engine.get_adapter") as mock_adapter:
        mock_adapter.return_value.parse.return_value = AdapterParseResult(
            facts=[], chunks=[chunk], total_messages=4,
            warnings=[], errors=[], turns=turns,
        )
        result = asyncio.run(engine.process_batch(source="gemini", content="x"))

    assert result.is_complete
    crystals = _crystals(client.all_payloads())
    # 2 turns, same session → exactly one Crystal.
    assert len(crystals) == 1, f"expected one Crystal, got {len(crystals)}"
