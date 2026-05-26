"""Tests for Crystal.session_id — memq-2 (memq spec §3.1, §3.2, §6.1).

Covers the two acceptance bullets:

  * ``Crystal.to_metadata()`` includes ``session_id`` when set, omits when empty.
  * Round-trip through ``MemoryClaimV1`` serde preserves ``session_id``.

The session_id is an encrypted-blob-only client-local UUID (no subgraph
schema impact). Pre-fix vault entries lack the field; recall consumers
treat absence as "no sibling Crystal available" — so the empty-case omit
is a load-bearing forward-compat invariant, not cosmetic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from totalreclaw.agent.debrief import Crystal
from totalreclaw.claims_helper import build_canonical_claim_v1


@dataclass
class _Fact:
    """Minimal ExtractedFact-shape used by build_canonical_claim_v1."""
    text: str
    type: str = "summary"
    source: str = "derived"
    scope: str = "unspecified"
    importance: int = 8
    confidence: Optional[float] = None


# ---------------------------------------------------------------------------
# Crystal.to_metadata — conditional inclusion of session_id
# ---------------------------------------------------------------------------


def test_to_metadata_includes_session_id_when_set() -> None:
    sid = "01902d40-7a2b-7f12-9c44-1c5e7d2af6a1"
    c = Crystal(narrative="Pedro and the agent landed memq-2.", session_id=sid)
    meta = c.to_metadata()
    assert meta["session_id"] == sid
    assert meta["subtype"] == "session_crystal"


def test_to_metadata_omits_session_id_when_empty() -> None:
    """Forward-compat invariant: pre-fix Crystals (and old vault entries
    re-serialised through to_metadata) must NOT emit an empty session_id —
    recall consumers key on presence, not on empty string."""
    c = Crystal(narrative="Trivial session, no session id wired.")
    meta = c.to_metadata()
    assert "session_id" not in meta
    assert meta["subtype"] == "session_crystal"


# ---------------------------------------------------------------------------
# Round-trip through MemoryClaimV1 serde — session_id survives end-to-end
# through the Python write path (build_canonical_claim_v1 + core validator).
# ---------------------------------------------------------------------------


def test_session_id_round_trips_through_memory_claim_v1_serde() -> None:
    sid = "01902d40-7a2b-7f12-9c44-1c5e7d2af6a1"
    c = Crystal(
        narrative="Pedro and the agent worked through the Hermes auto-memory refactor.",
        key_outcomes=["Locked Fork C-refined", "session_id field is encrypted-only"],
        open_threads=["Confirm Q4 quota bug fix scope"],
        lessons=["Mem0 v2 single-pass ADD-only is dominant"],
        session_id=sid,
        topics_discussed=["session-end batching", "dedup", "Crystal"],
    )
    fact = _Fact(text=c.narrative)

    blob = build_canonical_claim_v1(
        fact,
        importance=c.importance,
        extra_metadata=c.to_metadata(),
    )
    parsed = json.loads(blob)

    assert parsed["text"] == c.narrative
    assert parsed["type"] == "summary"
    assert parsed["source"] == "derived"
    assert parsed["schema_version"] == "1.0"

    md = parsed["metadata"]
    assert md["session_id"] == sid
    assert md["subtype"] == "session_crystal"
    assert md["key_outcomes"] == c.key_outcomes
    assert md["topics_discussed"] == c.topics_discussed


def test_round_trip_omits_session_id_when_crystal_lacks_one() -> None:
    """Pre-fix vault forward-compat: a Crystal with no session_id produces
    a v1 blob whose metadata also lacks the key. Recall-consumer absence
    semantics (§4.4) depend on this — empty-string presence would mis-route."""
    c = Crystal(narrative="A trivial pre-fix session.")
    fact = _Fact(text=c.narrative)

    blob = build_canonical_claim_v1(
        fact,
        importance=c.importance,
        extra_metadata=c.to_metadata(),
    )
    parsed = json.loads(blob)
    md = parsed["metadata"]
    assert "session_id" not in md
    assert md["subtype"] == "session_crystal"
