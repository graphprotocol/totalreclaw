"""PRD-04 F1 / DEP-5 -- the contradiction resolver receives entity refs + pin
state, and only a conflict with a pinned fact drops the new fact.

Uses the REAL totalreclaw_core resolver except where noted."""
from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock, patch

import totalreclaw_core

from totalreclaw.agent.contradiction import (
    SKIP_REASON_EXISTING_PINNED,
    _short_key_claim_for_resolver,
    detect_and_resolve_contradictions,
)
from totalreclaw.agent.extraction import ExtractedEntity, ExtractedFact
from totalreclaw.reranker import RerankerResult

NEW_TEXT = "Pedro's home city is Porto"
PINNED_TEXT = "Pedro's home city is Lisbon"
# cosine([0.6, 0.8, 0, 0], [1, 0, 0, 0]) = 0.6 -- inside the [0.3, 0.85) band.
NEW_EMBEDDING = [0.6, 0.8, 0.0, 0.0]
EXISTING_EMBEDDING = [1.0, 0.0, 0.0, 0.0]


def _new_fact() -> ExtractedFact:
    return ExtractedFact(
        text=NEW_TEXT, type="claim", importance=8, action="ADD", source="user",
        confidence=0.9, entities=[ExtractedEntity(name="Pedro", type="person")],
    )


def _existing(pinned: bool, entities=None) -> RerankerResult:
    return RerankerResult(
        id="existing-1", text=PINNED_TEXT, embedding=list(EXISTING_EMBEDDING),
        importance=0.9, created_at=time.time() - 86400, category="claim",
        pinned=pinned,
        entities=[{"n": "Pedro", "tp": "person"}] if entities is None else entities,
    )


def _client(results) -> AsyncMock:
    client = AsyncMock()
    client.recall = AsyncMock(return_value=results)
    return client


def test_short_key_claim_carries_pin_sentinel_and_entities() -> None:
    claim = _short_key_claim_for_resolver(
        text=PINNED_TEXT, fact_type="claim", importance=8, confidence=0.9,
        source_agent="unknown", created_at="2026-09-01T00:00:00.000Z",
        entities=[{"n": "Pedro", "tp": "person"}], pinned=True,
    )
    assert claim["st"] == "p"
    assert claim["e"] == [{"n": "Pedro", "tp": "person"}]
    encoded = json.dumps(claim)
    assert totalreclaw_core.is_pinned_claim(encoded) is True
    totalreclaw_core.canonicalize_claim(encoded)  # raises if core rejects it


def test_short_key_claim_unpinned_omits_st_and_empty_entities() -> None:
    claim = _short_key_claim_for_resolver(
        text="x", fact_type="preference", importance=5, confidence=0.9,
        source_agent="hermes-auto", created_at="2026-09-01T00:00:00.000Z",
    )
    assert "st" not in claim and "e" not in claim
    assert claim["c"] == "pref"


async def test_new_fact_contradicting_a_pinned_fact_is_dropped() -> None:
    with patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_EMBEDDING)):
        kept = await detect_and_resolve_contradictions([_new_fact()], _client([_existing(pinned=True)]))
    assert kept == []


async def test_same_conflict_with_an_unpinned_fact_is_kept() -> None:
    with patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_EMBEDDING)):
        kept = await detect_and_resolve_contradictions([_new_fact()], _client([_existing(pinned=False)]))
    assert [f.text for f in kept] == [NEW_TEXT]


async def test_pinned_fact_without_shared_entity_cannot_block() -> None:
    # Core only compares claims that share an entity.
    with patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_EMBEDDING)):
        kept = await detect_and_resolve_contradictions(
            [_new_fact()], _client([_existing(pinned=True, entities=[])]),
        )
    assert [f.text for f in kept] == [NEW_TEXT]


async def test_existing_wins_is_not_acted_on() -> None:
    fake_actions = json.dumps([
        {"type": "skip_new", "reason": "existing_wins", "existing_id": "existing-1", "new_id": "n"},
    ])
    with patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_EMBEDDING)), \
         patch.object(totalreclaw_core, "resolve_with_candidates", return_value=fake_actions):
        kept = await detect_and_resolve_contradictions([_new_fact()], _client([_existing(pinned=False)]))
    assert [f.text for f in kept] == [NEW_TEXT]


async def test_pinned_skip_log_has_no_fact_text(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="totalreclaw.agent.contradiction"), \
         patch("totalreclaw.embedding.get_embedding", return_value=list(NEW_EMBEDDING)):
        await detect_and_resolve_contradictions([_new_fact()], _client([_existing(pinned=True)]))
    assert f"reason={SKIP_REASON_EXISTING_PINNED}" in caplog.text
    assert NEW_TEXT not in caplog.text
    assert PINNED_TEXT not in caplog.text
    assert "existing-1" not in caplog.text
