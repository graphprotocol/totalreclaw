"""PRD-04 F1 / DEP-5 -- recall pin boost through the Python reranker wrapper.

Fixture mirrors rust/totalreclaw-core/src/reranker.rs (``pin_fixture``) and
mcp/tests/reranker-pin-boost.test.ts. Change all three together.
"""
from __future__ import annotations

import json

import totalreclaw_core

from totalreclaw import reranker as rr
from totalreclaw.reranker import RerankerCandidate, rerank

QUERY = "zulu"
QUERY_EMBEDDING = [1.0, 0.0, 0.0, 0.0]


def _fixture(p_pinned: bool) -> list[RerankerCandidate]:
    return [
        RerankerCandidate(id="a", text="alpha note", embedding=[1.0, 0.0, 0.0, 0.0]),
        RerankerCandidate(id="b", text="bravo note", embedding=[0.9, 0.1, 0.0, 0.0]),
        RerankerCandidate(id="c", text="charlie note", embedding=[0.8, 0.2, 0.0, 0.0]),
        RerankerCandidate(id="p", text="papa note", embedding=[0.7, 0.3, 0.0, 0.0], pinned=p_pinned),
        RerankerCandidate(id="e", text="echo note", embedding=[0.6, 0.4, 0.0, 0.0]),
        RerankerCandidate(id="f", text="foxtrot note", embedding=[0.5, 0.5, 0.0, 0.0]),
    ]


def test_core_default_pin_boost_is_1_5() -> None:
    assert rr.default_pin_boost() == 1.5


def test_pinned_candidate_just_outside_top_k_enters_top_k() -> None:
    without = rerank(QUERY, QUERY_EMBEDDING, _fixture(True), top_k=3)
    assert [r.id for r in without] == ["a", "b", "c"]
    boosted = rerank(QUERY, QUERY_EMBEDDING, _fixture(True), top_k=3, pin_boost=rr.default_pin_boost())
    assert [r.id for r in boosted] == ["p", "a", "b"]
    assert boosted[0].pinned is True
    assert boosted[1].pinned is False


def test_pin_boost_with_nothing_pinned_changes_nothing() -> None:
    base = rerank(QUERY, QUERY_EMBEDDING, _fixture(False), top_k=6)
    boosted = rerank(QUERY, QUERY_EMBEDDING, _fixture(False), top_k=6, pin_boost=1.5)
    assert [r.id for r in base] == [r.id for r in boosted]
    assert [r.rrf_score for r in base] == [r.rrf_score for r in boosted]


def test_entities_round_trip_through_rerank() -> None:
    cands = _fixture(True)
    cands[0].entities = [{"n": "Pedro", "tp": "person"}]
    out = {r.id: r for r in rerank(QUERY, QUERY_EMBEDDING, cands, top_k=6)}
    assert out["a"].entities == [{"n": "Pedro", "tp": "person"}]
    assert out["b"].entities is None


def test_old_core_without_pin_support_ignores_pin_boost(monkeypatch) -> None:
    calls: list[tuple] = []
    real = totalreclaw_core.rerank_with_config

    def spy(*args):
        calls.append(args)
        return real(*args[:5])

    monkeypatch.setattr(rr, "_CORE_SUPPORTS_PIN_BOOST", False)
    monkeypatch.setattr(totalreclaw_core, "rerank_with_config", spy)
    assert rr.default_pin_boost() is None
    out = rerank(QUERY, QUERY_EMBEDDING, _fixture(True), top_k=3, pin_boost=1.5)
    assert [r.id for r in out] == ["a", "b", "c"]
    assert calls and all(len(c) == 5 for c in calls), "pin_boost must not reach an old core"
    sent = json.loads(calls[0][2])
    assert sent[3]["pinned"] is True
    assert "pinned" not in sent[0]
