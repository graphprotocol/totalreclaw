"""Per-chunk "0 facts" diagnostics — the deferred follow-up to issue #389.

#373 fixed the headline bug (smart-import re-profiling per batch) and softened
the *aggregate* "0 facts" message. But the message still couldn't tell you
*why* a given chunk produced nothing, or *which* chunks failed — every
non-exception empty extraction collapsed to one vague line.

These tests pin the Option-B contract (engine-only, no ``llm_extract``
contract change):

* ``BatchImportResult.chunk_diagnostics`` — a per-chunk ``{index, title,
  reason}`` list for every chunk that produced 0 facts (excluding triage-skips
  and exceptions, which already have their own reporting).
* ``reason`` ∈ ``{extractor_empty, filtered_importance, filtered_text,
  filtered, malformed}``:
    - ``extractor_empty``     — extractor returned ``[]``/None (LLM empty /
      parse-fail / no-config all collapse here; DEBUG logs sub-split).
    - ``filtered_importance`` — extractor returned ≥1 candidate, all dropped
      for ``importance < 6`` (none too short).
    - ``filtered_text``       — extractor returned ≥1 candidate, all dropped
      for ``text < 5`` chars (none low-importance).
    - ``filtered``            — both filters contributed (mixed), or a mix of
      malformed + filtered candidates.
    - ``malformed``           — extractor returned ≥1 item but ALL were
      non-dict (malformed output, not facts that failed a threshold).
* The aggregate ``errors`` message summarises the counts by reason instead of
  the old generic "(possible LLM failures)" line.

Reason taxonomy is the canonical spec; the implementation must match these
literal strings.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.import_adapters.types import AdapterParseResult, ConversationChunk
from totalreclaw.import_engine import ImportEngine


# Canonical reason strings (the contract the implementation must satisfy).
REASON_EXTRACTOR_EMPTY = "extractor_empty"
REASON_FILTERED_IMPORTANCE = "filtered_importance"
REASON_FILTERED_TEXT = "filtered_text"
REASON_FILTERED = "filtered"
REASON_MALFORMED = "malformed"


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch):
    """Stub the Harrier ONNX model so tests run in milliseconds.

    ``_process_chunk_batch`` always runs semantic-session grouping
    (``_get_session_assignments`` → ``get_embedding``) and the store phase
    embeds each stored fact. Both would otherwise load the 344 MB model.
    A constant vector is sound: these tests assert diagnostics, not embedding
    or session correctness.
    """
    import totalreclaw.embedding as _emb

    monkeypatch.setattr(_emb, "get_embedding", lambda _text: [0.0] * 640)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _pro_client() -> MagicMock:
    """Fake TotalReclaw client on Gnosis (chain 100) with batched remember."""
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


def _chunk(title: str, *, when: str = "2026-01-15T10:00:00Z") -> ConversationChunk:
    return ConversationChunk(
        title=title,
        messages=[
            {"role": "user", "text": f"Tell me about {title} in enough detail to extract a fact."},
            {"role": "assistant", "text": f"Here is a detailed answer about {title}."},
        ],
        timestamp=when,
    )


def _parsed(chunks: list[ConversationChunk]) -> AdapterParseResult:
    return AdapterParseResult(
        facts=[], chunks=chunks, total_messages=len(chunks) * 2, warnings=[], errors=[],
    )


def _run(coro):
    return asyncio.run(coro)


def _diag(result, *, index: int) -> Optional[dict]:
    """Return the diagnostic for ``index``, or None if absent."""
    for d in result.chunk_diagnostics or []:
        if d.get("index") == index:
            return d
    return None


# ---------------------------------------------------------------------------
# Per-chunk reason classification
# ---------------------------------------------------------------------------


def test_extractor_empty_is_classified() -> None:
    """Extractor returns [] → reason extractor_empty, chunk in diagnostics."""
    engine = ImportEngine(client=_pro_client(), llm_extract=_always_empty())

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Alpha")]), 0, 1, 0))

    assert result.chunk_diagnostics is not None
    assert len(result.chunk_diagnostics) == 1
    assert result.chunk_diagnostics[0]["reason"] == REASON_EXTRACTOR_EMPTY


def test_filtered_importance_is_classified() -> None:
    """Candidates exist but all importance < 6 → filtered_importance."""
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([
            {"text": "A fact long enough to clear the text filter", "importance": 3, "type": "fact"},
        ]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Beta")]), 0, 1, 0))

    assert _diag(result, index=0)["reason"] == REASON_FILTERED_IMPORTANCE


def test_filtered_text_is_classified() -> None:
    """Candidates exist but all text < 5 chars → filtered_text."""
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([
            {"text": "hi", "importance": 9, "type": "fact"},
        ]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Gamma")]), 0, 1, 0))

    assert _diag(result, index=0)["reason"] == REASON_FILTERED_TEXT


def test_mixed_filter_is_classified() -> None:
    """Both a text-short and a low-importance candidate → filtered (mixed)."""
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([
            {"text": "hi", "importance": 9, "type": "fact"},          # text < 5
            {"text": "Adequately long fact text here", "importance": 2, "type": "fact"},  # importance < 6
        ]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Delta")]), 0, 1, 0))

    assert _diag(result, index=0)["reason"] == REASON_FILTERED


def test_malformed_non_dict_is_classified() -> None:
    """All-non-dict extractor output is malformed, NOT 'filtered'.

    A non-dict item (int / None / str) is malformed extractor output, not a
    fact that failed a threshold — bucketing it as 'filtered' would be
    misleading. This guards the code-review finding that non-dict items were
    silently ``continue``d without counting.
    """
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([42, None, "not a dict"]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Zeta")]), 0, 1, 0))

    assert _diag(result, index=0)["reason"] == REASON_MALFORMED


def test_successful_chunk_has_no_diagnostic() -> None:
    """A chunk that yields a valid fact must NOT appear in diagnostics."""
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([
            {"text": "User prefers tabs over spaces for Python", "importance": 8, "type": "preference"},
        ]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Epsilon")]), 0, 1, 0))

    assert result.chunk_diagnostics is None or result.chunk_diagnostics == []
    assert result.facts_extracted == 1


def test_valid_fact_plus_filtered_candidates_has_no_diagnostic() -> None:
    """A valid fact alongside filtered candidates still yields NO diagnostic.

    Guards the ``valid_count > 0 → no diagnostic`` invariant in the pure-mixed
    case (one survivor + some dropped). A regression keying off ``raw_count > 0``
    instead of ``valid_count > 0`` would wrongly emit a diagnostic here.
    """
    engine = ImportEngine(
        client=_pro_client(),
        llm_extract=_returns([
            {"text": "A valid survivor fact worth keeping", "importance": 8, "type": "fact"},  # ok
            {"text": "hi", "importance": 9, "type": "fact"},            # text < 5 → dropped
            {"text": "Low value detail not worth storing", "importance": 2, "type": "fact"},  # importance < 6 → dropped
        ]),
    )

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Eta")]), 0, 1, 0))

    assert result.chunk_diagnostics is None or result.chunk_diagnostics == []
    assert result.facts_extracted == 1


def test_diagnostics_carry_global_index_and_title() -> None:
    """Diagnostic index is the GLOBAL chunk index (offset + i), title is the chunk's."""
    chunks = [_chunk("First"), _chunk("Second"), _chunk("Third")]
    engine = ImportEngine(client=_pro_client(), llm_extract=_always_empty())

    # Batch slice [1:3] → chunks "Second" (global 1) and "Third" (global 2).
    # batch-local indices would be 0 and 1; global must be 1 and 2.
    result = _run(engine._process_chunk_batch(_parsed(chunks), offset=1, batch_size=2, start_ms=0))

    assert len(result.chunk_diagnostics) == 2
    by_index = {d["index"]: d["title"] for d in result.chunk_diagnostics}
    assert by_index == {1: "Second", 2: "Third"}


# ---------------------------------------------------------------------------
# Aggregate message summarises reason counts
# ---------------------------------------------------------------------------


def test_partial_message_summarises_reason_counts() -> None:
    """A mix of extractor_empty + filtered_importance names both categories."""
    chunks = [_chunk("Empty"), _chunk("LowImportance"), _chunk("Good")]
    answers = [
        [],  # chunk 0 → extractor_empty
        [{"text": "Long enough but low importance fact", "importance": 2, "type": "fact"}],  # filtered_importance
        [{"text": "A valid high-importance fact to store", "importance": 8, "type": "fact"}],  # ok
    ]
    engine = ImportEngine(client=_pro_client(), llm_extract=_queue(answers))

    result = _run(engine._process_chunk_batch(_parsed(chunks), 0, 3, 0))

    # Per-chunk surface must match the aggregate: exactly the two zero-fact chunks.
    assert len(result.chunk_diagnostics) == 2
    zero_msg = next((e for e in result.errors if "0 facts" in e), None)
    assert zero_msg is not None, f"expected a '0 facts' summary in errors, got {result.errors!r}"
    # Both distinct reason categories must be named.
    assert "extractor returned no facts" in zero_msg
    assert "importance" in zero_msg
    # 2 of 3 attempted chunks produced 0 facts.
    assert "2/3" in zero_msg


def test_all_zero_alarm_summarises_reason_counts() -> None:
    """When ALL attempted chunks produce 0 facts, the alarm summarises reasons."""
    chunks = [_chunk("EmptyOne"), _chunk("LowImportanceTwo")]
    answers = [
        [],
        [{"text": "Long enough but low importance fact", "importance": 1, "type": "fact"}],
    ]
    engine = ImportEngine(client=_pro_client(), llm_extract=_queue(answers))

    result = _run(engine._process_chunk_batch(_parsed(chunks), 0, 2, 0))

    alarm = next((e for e in result.errors if e.startswith("All ")), None)
    assert alarm is not None, f"expected the 'All … 0 facts' alarm, got {result.errors!r}"
    assert "All 2 extracted chunks produced 0 facts" in alarm
    assert "extractor returned no facts" in alarm
    assert "importance" in alarm


# ---------------------------------------------------------------------------
# Regression guards: exception + skip paths must NOT pollute diagnostics
# ---------------------------------------------------------------------------


def test_exception_chunk_excluded_from_diagnostics() -> None:
    """A chunk whose extractor RAISES is an extraction_failure, not a diagnostic."""
    def raise_once(messages, timestamp, **_kw):
        raise RuntimeError("boom")

    engine = ImportEngine(client=_pro_client(), llm_extract=raise_once)

    result = _run(engine._process_chunk_batch(_parsed([_chunk("Raises")]), 0, 1, 0))

    # Existing behaviour: per-chunk error names the chunk + the exception.
    assert any("Extraction failed" in e and "Raises" in e for e in result.errors)
    # New behaviour: the raised chunk is NOT silently bucketed as a 0-fact diag.
    assert result.chunk_diagnostics is None or result.chunk_diagnostics == []


# ---------------------------------------------------------------------------
# Extractor stubs
# ---------------------------------------------------------------------------


def _always_empty():
    async def _extract(messages, timestamp, *, enriched_system_prompt=None):
        return []
    return _extract


def _returns(payload: list[dict]):
    async def _extract(messages, timestamp, *, enriched_system_prompt=None):
        return payload
    return _extract


def _queue(payloads: list):
    """Return an extractor that yields each payload in turn, one per chunk."""
    it = iter(payloads)

    async def _extract(messages, timestamp, *, enriched_system_prompt=None):
        return next(it)
    return _extract
