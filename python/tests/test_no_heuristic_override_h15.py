"""S39 (internal rc2 QA H15): an empty LLM extraction is never overridden.

The LLM answered ``{"topics": [...], "facts": []}`` because the facts were
already in the vault; the pattern extractor then ran on the same turn and
stored a regex fragment. The pattern extractor now runs only when no LLM
config resolves, and its output goes through the same filters and dedup.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent import extraction, lifecycle
from totalreclaw.agent.extraction import (
    ExtractedFact,
    extract_facts_heuristic,
    extract_facts_llm,
    heuristic_fallback_facts,
)
from totalreclaw.agent.llm_client import LLMConfig

QA_USER = (
    "To be clear for the record: I live in Porto and I work at Tidewater. "
    "Nothing has changed there."
)
QA_MESSAGES = [
    {"role": "user", "content": QA_USER},
    {"role": "assistant", "content": "Noted."},
]
EMPTY_ANSWER = '{"topics": ["Porto residence", "Tidewater employment"], "facts": []}'
CHAT = LLMConfig(
    api_key="k", base_url="https://api.example.invalid/v1", model="m", api_format="openai"
)


def _make_state():
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    c = MagicMock()
    c.read_block = None
    c.eoa_address = "0xabc"
    c.resolved_wallet_address = None
    c.recall = AsyncMock(return_value=[])
    c.fetch_recent_facts = AsyncMock(return_value=[])
    c.forget = AsyncMock(return_value=True)
    c.forget_ledger = None
    c.resolved_chain_id = 100
    c.remember_batch = AsyncMock(
        side_effect=lambda facts, source="": [f"new-{i}" for i in range(len(facts))]
    )
    state._client = c
    state.add_message("user", QA_USER)
    state.add_message("assistant", "Noted.")
    return state, c


def _run(state, *, llm_config=CHAT, llm=None, heuristic=None, fallback_response=None):
    async def passthrough(fs, *a, **k):
        return fs

    heuristic = heuristic if heuristic is not None else MagicMock(return_value=[])
    patches = [
        patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough),
        patch("totalreclaw.agent.lifecycle.apply_pin_guard", new=passthrough),
        patch("totalreclaw.agent.lifecycle.heuristic_fallback_facts", new=heuristic),
        patch("totalreclaw.embedding.is_model_available_locally", return_value=False),
        patch("totalreclaw.embedding.get_embedding", return_value=None),
    ]
    if llm is not None:
        patches.append(patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=llm))
    if fallback_response is not None:
        patches.append(
            patch.object(
                extraction,
                "complete_with_extraction_fallback",
                new=AsyncMock(return_value=(fallback_response, CHAT)),
            )
        )
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stored = lifecycle.auto_extract(state, llm_config=llm_config)
    return stored, heuristic


# 1
def test_empty_llm_answer_is_final(caplog):
    state, client = _make_state()
    with caplog.at_level(logging.INFO):
        stored, heuristic = _run(state, fallback_response=EMPTY_ANSWER)
    assert stored == []
    heuristic.assert_not_called()
    client.remember_batch.assert_not_called()
    assert "0 proposals" in caplog.text


# 2
def test_llm_exception_does_not_fall_back(caplog):
    state, client = _make_state()

    async def boom(*a, **k):
        raise RuntimeError("secret detail")

    with caplog.at_level(logging.INFO):
        stored, heuristic = _run(state, llm=boom)
    assert stored == []
    heuristic.assert_not_called()
    client.remember_batch.assert_not_called()
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "auto-extract: LLM extraction failed (RuntimeError); nothing extracted this cycle" in warns
    assert "secret detail" not in caplog.text


# 3
@pytest.mark.asyncio
async def test_all_candidates_empty_sets_outcome_empty():
    outcome: dict = {}
    with patch.object(
        extraction, "complete_with_extraction_fallback",
        new=AsyncMock(return_value=("", CHAT)),
    ):
        facts = await extract_facts_llm(QA_MESSAGES, llm_config=CHAT, outcome=outcome)
    assert facts == [] and outcome["llm"] == "empty"


def test_all_candidates_empty_stores_nothing_and_skips_heuristic():
    state, client = _make_state()
    stored, heuristic = _run(state, fallback_response="")
    assert stored == []
    heuristic.assert_not_called()
    client.remember_batch.assert_not_called()


# 4
@pytest.mark.asyncio
async def test_no_llm_config_sets_outcome_unavailable(tmp_path):
    outcome: dict = {}
    with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        facts = await extract_facts_llm(QA_MESSAGES, outcome=outcome)
    assert facts == [] and outcome["llm"] == "unavailable"


def test_no_llm_config_runs_pattern_extractor(caplog, tmp_path):
    state, _ = _make_state()
    fact = ExtractedFact(text="prefers dark mode everywhere", type="preference",
                         importance=7, action="ADD", source="user")
    heuristic = MagicMock(return_value=[fact])

    async def unavailable(messages, *a, outcome=None, **k):
        outcome["llm"] = "unavailable"
        return []

    with caplog.at_level(logging.INFO):
        _, heuristic = _run(state, llm=unavailable, heuristic=heuristic, llm_config=None)
    heuristic.assert_called_once()
    assert "auto-extract: no LLM configured; pattern extractor proposed 1 fact(s)" in caplog.text


# 5
def test_pattern_capture_ends_at_first_sentence_end():
    facts = extract_facts_heuristic(QA_MESSAGES, 15)
    texts = [f.text for f in facts]
    assert all("Nothing has changed there" not in t for t in texts)
    assert texts == ["User works at Tidewater"]
    for f in facts:
        assert "." not in f.text


# 6
def test_heuristic_fallback_drops_near_duplicate_of_existing_memory():
    msgs = [{"role": "user", "content": "I prefer dark mode for all my editors"}]
    existing = [{"id": "e1", "text": "User prefers dark mode", "embedding": [1.0, 0.0, 0.0]}]

    def candidate():
        return ExtractedFact(text="User prefers dark mode in every editor.", type="preference",
                             importance=7, action="ADD", source="user")

    with patch.object(extraction, "extract_facts_heuristic", side_effect=lambda m, n: [candidate()]), \
         patch("totalreclaw.embedding.get_embedding", return_value=[1.0, 0.0, 0.0]):
        assert heuristic_fallback_facts(msgs, 15, existing) == []
        assert len(heuristic_fallback_facts(msgs, 15, [])) == 1


# 7
@pytest.mark.asyncio
async def test_outcome_is_optional_and_ok_and_skipped():
    with patch.object(
        extraction, "complete_with_extraction_fallback",
        new=AsyncMock(return_value=(EMPTY_ANSWER, CHAT)),
    ):
        assert await extract_facts_llm(QA_MESSAGES, llm_config=CHAT) == []
        outcome: dict = {}
        assert await extract_facts_llm(QA_MESSAGES, llm_config=CHAT, outcome=outcome) == []
        assert outcome["llm"] == "ok"
    outcome = {}
    await extract_facts_llm([], llm_config=CHAT, outcome=outcome)
    assert outcome["llm"] == "skipped"
    outcome = {}
    await extract_facts_llm([{"role": "user", "content": "hi"}], llm_config=CHAT, outcome=outcome)
    assert outcome["llm"] == "skipped"


@pytest.mark.asyncio
async def test_error_outcome():
    outcome: dict = {}
    with patch.object(
        extraction, "complete_with_extraction_fallback",
        new=AsyncMock(side_effect=RuntimeError("x")),
    ):
        assert await extract_facts_llm(QA_MESSAGES, llm_config=CHAT, outcome=outcome) == []
    assert outcome["llm"] == "error"


# real pattern output through the real filter chain (no mock of the extractor)
@pytest.mark.parametrize(
    "sentence, expected",
    [
        (QA_USER, "User works at Tidewater"),
        ("I prefer dark mode for all my editors", "User prefers dark mode for all my editors"),
        ("I enjoy hiking in the mountains", "User enjoys hiking in the mountains"),
    ],
)
def test_real_pattern_output_survives_filter_chain(sentence, expected):
    msgs = [{"role": "user", "content": sentence}]
    with patch("totalreclaw.embedding.get_embedding", return_value=None):
        facts = heuristic_fallback_facts(msgs, 15, [])
    assert [f.text for f in facts] == [expected]


# round 2 (R78)
def _texts(sentence):
    return [f.text for f in extract_facts_heuristic([{"role": "user", "content": sentence}], 15)]


@pytest.mark.parametrize(
    "sentence",
    ["\u0130 work at Tidewater", "\u0130'm a software engineer at a startup",
     "\u0130 prefer dark mode for editors", "I\u0307 work at Tidewater"],
)
def test_unicode_case_variants_do_not_crash(sentence):
    assert _texts(sentence) == []  # no exception; re.ASCII keeps these out


@pytest.mark.parametrize(
    "sentence",
    [
        "My use of steroids declined last year",
        "My love of loud music is well known",
        "We decided to hire Pedro for the ops role",
        "Decision: postpone the launch by two weeks",
        "Call me old fashioned maybe",
    ],
)
def test_non_first_person_or_loose_lead_ins_give_no_fact(sentence):
    assert _texts(sentence) == []


def test_overlapping_matches_keep_first_pattern_only():
    assert _texts("I love my job at the mill") == ["User loves my job at the mill"]


def test_capture_stops_before_and_i_clause():
    assert _texts("I enjoy hiking in the mountains and I hate crowded trails") == [
        "User enjoys hiking in the mountains",
        "User hates crowded trails",
    ]


@pytest.mark.parametrize(
    "sentence",
    [
        "The tool I use daily just crashed",
        "The database I work on is called Zeus",
        "The people I enjoy working with moved to Berlin",
        "What I want for dinner is pasta",
        "That's the gym I use every weekday morning",
        "The food I love most is ramen",
    ],
)
def test_relative_clauses_give_no_fact(sentence):
    assert _texts(sentence) == []


@pytest.mark.parametrize(
    "sentence, expected",
    [
        ("Honestly, I prefer dark mode over light mode", ["User prefers dark mode over light mode"]),
        ("Fine. I chose the blue theme for everything", ["User chose the blue theme for everything"]),
        ("I decided to move the launch earlier", ["User decided to move the launch earlier"]),
        ("Ok;\nI work at a small studio downtown", ["User works at a small studio downtown"]),
    ],
)
def test_clause_start_lead_ins_still_match(sentence, expected):
    assert _texts(sentence) == expected
