"""#368 Part 2 — straddle-splitting: per-session fact attribution.

#466 exposed a flat per-turn view (`ParsedTurn` with real per-turn timestamps +
`chunk_index`) so `segment_sessions` runs at true turn granularity. But it still
mapped a chunk whose turns straddle a session boundary WHOLESALE to its first
turn's session — so all of a straddling chunk's facts landed on one session_id.

This module pins the straddle-splitting refinement: a chunk whose turns span more
than one session is split into per-session sub-chunks for extraction (using the
per-turn message ranges `ParsedTurn.chunk_msg_start/end`, or the equivalent ranges
the `_turns_from_chunks` fallback derives), so each fact lands in the session its
turn actually belongs to.

  1. STRADDLE: a 20-message chunk (first turns topic A, rest topic B) splits into
     2 sessions with facts partitioned by turn — where the whole-chunk mapping put
     them all on one session_id.
  2. NO-STRADDLE: a single-topic chunk is NOT recorded as straddling, extracted
     once, one session — behaviour-preserving.
  3. Per-turn time-gap straddle within a chunk.
  4/5. #367 invariants (multi-topic 30-min split; long same-topic intact).
  6. One Crystal per session for a straddling chunk.

The embedding stubs mirror Harrier's L2-normalised geometry (orthogonal =
different topic). Embedding is local-only — never routed through an LLM.
"""
from __future__ import annotations

import asyncio
import json
import math

import pytest

from totalreclaw.import_engine import ImportEngine
from totalreclaw.imports.adapters.types import ConversationChunk, AdapterParseResult


# ── helpers ───────────────────────────────────────────────────────────────────


def _unit(v: list) -> list:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 1e-9 else v


_TOPIC_A = _unit([1.0, 0.0, 0.0, 0.0])
_TOPIC_B = _unit([0.0, 1.0, 0.0, 0.0])


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


def _atomic(payloads):
    return [p for p in payloads
            if (p.get("extra_metadata") or {}).get("subtype") != "session_crystal"]


def _crystals(payloads):
    return [p for p in payloads
            if (p.get("extra_metadata") or {}).get("subtype") == "session_crystal"]


def _session_ids_of(payloads):
    return {(p.get("extra_metadata") or {}).get("session_id")
            for p in payloads if (p.get("extra_metadata") or {}).get("session_id")}


async def _extract_echo(messages, timestamp):
    """One fact per extraction unit, text = user messages joined — lets a test
    assert which topic a fact came from (i.e. which session it should be in)."""
    user_texts = [m.get("text", m.get("content", "")) for m in messages
                  if m.get("role") == "user"]
    combined = " ".join(user_texts).strip()
    return [{"text": combined[:200], "type": "fact", "importance": 8}] if combined else []


def _make_engine(client, embed_fn, monkeypatch, llm_completion=None):
    import totalreclaw.embedding as emb_mod
    monkeypatch.setattr(emb_mod, "get_embedding", embed_fn)
    return ImportEngine(client=client, llm_extract=_extract_echo,
                        llm_completion=llm_completion, enable_smart_import=False)


def _topic_embed(text: str) -> list:
    if "berlin" in text.lower():
        return _TOPIC_A
    if "python" in text.lower():
        return _TOPIC_B
    return _TOPIC_A


def _straddling_chunk() -> ConversationChunk:
    """A 20-message chunk (10 turns): first 4 turns topic A, next 6 topic B."""
    msgs = []
    base = "2026-05-14T09:{:02d}:00.000Z"
    for i in range(4):
        msgs.append({"role": "user", "text": f"berlin question {i}", "timestamp": base.format(i)})
        msgs.append({"role": "assistant", "text": f"berlin answer {i}", "timestamp": base.format(i)})
    for i in range(6):
        msgs.append({"role": "user", "text": f"python question {i}", "timestamp": base.format(4 + i)})
        msgs.append({"role": "assistant", "text": f"python answer {i}", "timestamp": base.format(4 + i)})
    assert len(msgs) == 20
    return ConversationChunk(title="Mixed session", messages=msgs,
                             timestamp="2026-05-14T09:00:00.000Z")


# ── Test 1: STRADDLE — mid-chunk topic boundary splits into 2 sessions ────────


def test_straddle_splits_one_chunk_into_two_sessions(monkeypatch):
    client = _BatchClient()
    engine = _make_engine(client, _topic_embed, monkeypatch)

    parsed = AdapterParseResult(
        facts=[], chunks=[_straddling_chunk()], total_messages=20,
        warnings=[], errors=[],
    )
    result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))
    assert result.is_complete

    assert len(engine._session_assignments) == 2, \
        f"expected 2 sessions, got {engine._session_assignments}"
    assert sorted(engine._session_turn_counts) == [4, 6]

    assert 0 in engine._chunk_straddle_slices
    slices = engine._chunk_straddle_slices[0]
    assert len(slices) == 2
    assert slices[0][1] == list(range(0, 8))     # topic A messages
    assert slices[1][1] == list(range(8, 20))    # topic B messages

    atoms = _atomic(client.all_payloads())
    assert len(_session_ids_of(atoms)) == 2

    berlin_sids = {(p.get("extra_metadata") or {}).get("session_id")
                   for p in atoms if "berlin" in p["text"].lower()}
    python_sids = {(p.get("extra_metadata") or {}).get("session_id")
                   for p in atoms if "python" in p["text"].lower()}
    assert len(berlin_sids) == 1 and len(python_sids) == 1
    assert berlin_sids.isdisjoint(python_sids), \
        "berlin and python facts must land in different sessions"


def test_whole_chunk_mapping_would_have_collapsed_the_straddle(monkeypatch):
    """Red/green proof: with per-turn message ranges absent (emulating the #466
    whole-chunk mapping — a core wheel without the range fields), the straddling
    chunk's facts collapse to ONE session_id. WITH ranges (the other tests) they
    split into two. This is the exact behaviour Part 2 straddle-splitting adds."""
    client = _BatchClient()
    engine = _make_engine(client, _topic_embed, monkeypatch)

    # Strip the per-turn message range from both turn sources so straddle-slicing
    # is impossible — reproducing the pre-straddle-fidelity behaviour.
    real_from_chunks = ImportEngine._turns_from_chunks

    def _no_range_chunks(chunks):
        return [(ci, ts, text, None) for (ci, ts, text, _r) in real_from_chunks(chunks)]

    monkeypatch.setattr(ImportEngine, "_turns_from_parsed", staticmethod(lambda turns: None))
    monkeypatch.setattr(ImportEngine, "_turns_from_chunks", staticmethod(_no_range_chunks))

    parsed = AdapterParseResult(
        facts=[], chunks=[_straddling_chunk()], total_messages=20, warnings=[], errors=[],
    )
    asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))
    assert engine._chunk_straddle_slices == {}, "no ranges ⇒ no straddle-splitting"
    atoms = _atomic(client.all_payloads())
    assert len(_session_ids_of(atoms)) == 1, \
        "whole-chunk mapping collapses the straddle to one session"


# ── Test 2: NO-STRADDLE — behaviour-preserving ────────────────────────────────


def test_no_straddle_single_topic_chunk_one_session(monkeypatch):
    client = _BatchClient()
    engine = _make_engine(client, lambda t: _TOPIC_A, monkeypatch)

    msgs = []
    for i in range(10):
        ts = f"2026-05-14T09:{i:02d}:00.000Z"
        msgs.append({"role": "user", "text": f"berlin question {i}", "timestamp": ts})
        msgs.append({"role": "assistant", "text": f"berlin answer {i}", "timestamp": ts})
    chunk = ConversationChunk(title="Berlin session", messages=msgs,
                              timestamp="2026-05-14T09:00:00.000Z")

    parsed = AdapterParseResult(
        facts=[], chunks=[chunk], total_messages=20, warnings=[], errors=[],
    )
    result = asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))
    assert result.is_complete

    assert len(engine._session_assignments) == 1
    assert engine._session_turn_counts == [10]
    assert engine._chunk_straddle_slices == {}
    atoms = _atomic(client.all_payloads())
    assert len(atoms) == 1
    assert len(_session_ids_of(atoms)) == 1


# ── Test 3: per-message timestamp time-gap straddle ───────────────────────────


def test_time_gap_straddle_within_chunk(monkeypatch):
    client = _BatchClient()
    engine = _make_engine(client, lambda t: _TOPIC_A, monkeypatch)

    msgs = [
        {"role": "user", "text": "first block a", "timestamp": "2026-05-14T09:00:00.000Z"},
        {"role": "assistant", "text": "ok a", "timestamp": "2026-05-14T09:00:00.000Z"},
        {"role": "user", "text": "first block b", "timestamp": "2026-05-14T09:05:00.000Z"},
        {"role": "assistant", "text": "ok b", "timestamp": "2026-05-14T09:05:00.000Z"},
        {"role": "user", "text": "second block c", "timestamp": "2026-05-14T10:35:00.000Z"},  # +90m
        {"role": "assistant", "text": "ok c", "timestamp": "2026-05-14T10:35:00.000Z"},
    ]
    chunk = ConversationChunk(title="Two-window session", messages=msgs,
                              timestamp="2026-05-14T09:00:00.000Z")
    asyncio.run(engine._get_session_assignments([chunk]))

    assert len(engine._session_assignments) == 2, \
        f"internal 90-min gap must split, got {engine._session_assignments}"
    assert sorted(engine._session_turn_counts) == [1, 2]
    assert 0 in engine._chunk_straddle_slices


# ── Test 4/5: #367 invariants ─────────────────────────────────────────────────


def test_multi_topic_window_splits_invariant(monkeypatch):
    client = _BatchClient()
    engine = _make_engine(client, _topic_embed, monkeypatch)
    msgs = []
    for i in range(4):
        ts = f"2026-05-14T09:{i:02d}:00.000Z"
        msgs.append({"role": "user", "text": f"berlin {i}", "timestamp": ts})
        msgs.append({"role": "assistant", "text": f"a {i}", "timestamp": ts})
    for i in range(4):
        ts = f"2026-05-14T09:{10 + i:02d}:00.000Z"
        msgs.append({"role": "user", "text": f"python {i}", "timestamp": ts})
        msgs.append({"role": "assistant", "text": f"a {i}", "timestamp": ts})
    chunk = ConversationChunk(title="w", messages=msgs, timestamp="2026-05-14T09:00:00.000Z")
    asyncio.run(engine._get_session_assignments([chunk]))
    assert len(engine._session_assignments) == 2
    assert sorted(engine._session_turn_counts) == [4, 4]


def test_long_same_topic_convo_intact_invariant(monkeypatch):
    client = _BatchClient()
    engine = _make_engine(client, lambda t: _TOPIC_A, monkeypatch)
    msgs = []
    for i in range(20):
        ts = f"2026-05-14T09:{i:02d}:00.000Z"
        msgs.append({"role": "user", "text": f"berlin {i}", "timestamp": ts})
        msgs.append({"role": "assistant", "text": f"a {i}", "timestamp": ts})
    chunk = ConversationChunk(title="long", messages=msgs, timestamp="2026-05-14T09:00:00.000Z")
    asyncio.run(engine._get_session_assignments([chunk]))
    assert len(engine._session_assignments) == 1
    assert engine._session_turn_counts == [20]
    assert engine._chunk_straddle_slices == {}


# ── Test 6: one Crystal per session for a straddling chunk ─────────────────────


def test_straddle_emits_one_crystal_per_session(monkeypatch):
    client = _BatchClient()

    async def completion(prompt):
        return json.dumps({"title": "S", "summary": "sum"})

    engine = _make_engine(client, _topic_embed, monkeypatch, llm_completion=completion)
    parsed = AdapterParseResult(
        facts=[], chunks=[_straddling_chunk()], total_messages=20, warnings=[], errors=[],
    )
    asyncio.run(engine._process_chunk_batch(parsed, offset=0, batch_size=1, start_ms=0))

    payloads = client.all_payloads()
    crystals = _crystals(payloads)
    assert len(crystals) == 2, f"expected one Crystal per session, got {len(crystals)}"
    crystal_sids = {(c.get("extra_metadata") or {}).get("session_id") for c in crystals}
    assert crystal_sids == _session_ids_of(_atomic(payloads))
