"""S41 -- the extraction LLM confirms a pinned-fact conflict before a fact is dropped
(internal Hermes QA of 2.6.0rc2, Finding #4).

Real ``totalreclaw_core`` resolver; the LLM is a stub on ``pin_check.chat_completion``.
Texts and cosines are the measured ones (fixtures/pin_check_cases.json, 2-d vectors
built at the measured cosine to the rc2 pin)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from totalreclaw.agent import pin_check
from totalreclaw.agent.contradiction import detect_and_resolve_contradictions
from totalreclaw.agent.extraction import ExtractedEntity, ExtractedFact
from totalreclaw.agent.llm_client import LLMConfig, LLMUpstreamOutageError
from totalreclaw.agent.pin_check import llm_confirms_pin_conflict
from totalreclaw.reranker import RerankerResult

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "pin_check_cases.json").read_text(encoding="utf-8")
)
PIN_TEXT = _FIXTURE["pinned_text"]
PIN_ID = "5478cf5a-5e95-4f40-9410-9c2aa10eb4d0"
CASES = {c["id"]: c for c in _FIXTURE["cases"]}
CFG = LLMConfig(api_key="test-key", base_url="https://llm.invalid/v1", model="m", api_format="openai")


def _vec(cos: float) -> list:
    return [cos, math.sqrt(1.0 - cos * cos)]


PIN_EMBEDDING = [1.0, 0.0]


class Stub:
    """Stub for ``pin_check.chat_completion``: scripted answers, records calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list = []

    async def __call__(self, config, system_prompt, user_prompt, **kwargs):
        self.calls.append(user_prompt)
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(a, BaseException):
            raise a
        return a


def _fact(case_id: str = None, *, text=None, entities=None) -> ExtractedFact:
    c = CASES.get(case_id) if case_id else None
    ents = entities if entities is not None else (c["entities"] if c else [])
    return ExtractedFact(
        text=text or c["text"], type="claim", importance=8, action="ADD", source="user",
        confidence=0.9, entities=[ExtractedEntity(name=e["n"], type=e["tp"]) for e in ents],
    )


def _pin(pinned=True, entities=None) -> RerankerResult:
    return RerankerResult(
        id=PIN_ID, text=PIN_TEXT, embedding=list(PIN_EMBEDDING), importance=0.9,
        created_at=time.time() - 86400, category="claim", pinned=pinned,
        entities=entities if entities is not None else [],
    )


def _client(*results) -> AsyncMock:
    client = AsyncMock()
    client.recall = AsyncMock(return_value=list(results))
    return client


async def _run(facts, cos_by_text, *, pin=None, answers=("NO",), config=CFG, caplog=None):
    stub = Stub(*answers)
    pin = pin or _pin()
    if caplog is not None:
        caplog.set_level(logging.DEBUG, logger="totalreclaw")
    with patch("totalreclaw.embedding.get_embedding", side_effect=lambda t: _vec(cos_by_text[t])), \
         patch.object(pin_check, "chat_completion", new=stub):
        kept = await detect_and_resolve_contradictions(
            facts, _client(pin), llm_config=config
        )
    return kept, stub


def _one(case_id, **kw):
    f = _fact(case_id)
    return _run([f], {f.text: CASES[case_id]["cosine_to_rc2_pin"]}, **kw)


# 1 ---------------------------------------------------------------------------

@pytest.mark.parametrize("resp,expected", [
    ("YES", True), ("No.", False), (" yes, it reverses", True), ("Maybe", None), (None, None), ("", None),
    ('{ "answer": "NO" }', False), ('{"answer":"yes"}', True), ('{"Answer": "Yes."}', True),
    ('{"verdict": "no"}', False), ('{"answer": "maybe"}', None), ('{"a": "yes", "b": "no"}', None),
    ('"YES"', True), ('```json\n{"answer": "YES"}\n```', True), ("{not json", None),
])
async def test_parsing(resp, expected) -> None:
    stub = Stub(resp)
    with patch.object(pin_check, "chat_completion", new=stub):
        assert await llm_confirms_pin_conflict(CFG, "p", "n") is expected


async def test_upstream_outage_gives_none() -> None:
    with patch.object(pin_check, "chat_completion", new=Stub(LLMUpstreamOutageError("down", 3))):
        assert await llm_confirms_pin_conflict(CFG, "p", "n") is None


async def test_timeout_gives_none() -> None:
    async def slow(*a, **k):
        await asyncio.sleep(1)
        return "YES"

    with patch.object(pin_check, "PIN_CHECK_TIMEOUT_S", 0.05), \
         patch.object(pin_check, "chat_completion", new=slow):
        assert await llm_confirms_pin_conflict(CFG, "p", "n") is None


async def test_no_config_gives_none_without_a_call() -> None:
    stub = Stub("YES")
    with patch.object(pin_check, "chat_completion", new=stub):
        assert await llm_confirms_pin_conflict(None, "p", "n") is None
    assert stub.calls == []


async def test_user_prompt_carries_both_texts() -> None:
    stub = Stub("NO")
    with patch.object(pin_check, "chat_completion", new=stub):
        await llm_confirms_pin_conflict(CFG, "the pin", "the new fact")
    assert stub.calls == ["PINNED: the pin\nNEW: the new fact"]


# 2, 3, 6 legit facts kept -----------------------------------------------------

async def test_d5_kept_when_llm_says_no(caplog) -> None:
    kept, stub = await _one("D5", caplog=caplog)
    assert [f.text for f in kept] == [CASES["D5"]["text"]]
    assert len(stub.calls) == 1
    assert "pinned fact 5478cf5a" in caplog.text
    assert "check=llm_no" in caplog.text
    assert "pinned conflicts 0" in caplog.text
    assert "pinned_llm_no=1" in caplog.text


@pytest.mark.parametrize("case_id", ["D2a", "S8"])
async def test_legit_facts_kept_when_llm_says_no(case_id) -> None:
    kept, _ = await _one(case_id)
    assert len(kept) == 1


# 4, 5 reversals dropped -------------------------------------------------------

async def test_d4_dropped_when_llm_says_yes(caplog) -> None:
    kept, _ = await _one("D4", answers=("YES",), caplog=caplog)
    assert kept == []
    assert "reason=existing_pinned, pinned=5478cf5a, check=llm_yes, link=name, sim=0.776" in caplog.text


@pytest.mark.parametrize("case_id", ["R1", "R17"])
async def test_reversals_dropped_when_llm_says_yes(case_id) -> None:
    kept, _ = await _one(case_id, answers=("YES",))
    assert kept == []


# 7 high band ------------------------------------------------------------------

async def test_high_band_yes_drops(caplog) -> None:
    kept, stub = await _one("R12", answers=("YES",), caplog=caplog)
    assert kept == []
    assert len(stub.calls) == 1
    assert "pinned_high_checked=1" in caplog.text
    assert "check=llm_yes" in caplog.text


@pytest.mark.parametrize("answers", [("NO",), (RuntimeError("boom"),), ("Maybe",)])
async def test_high_band_no_or_failure_keeps(answers) -> None:
    kept, _ = await _one("R12", answers=answers)
    assert len(kept) == 1


# 8 failure in band ------------------------------------------------------------

async def test_in_band_llm_failure_drops_unconfirmed(caplog) -> None:
    kept, _ = await _one("D4", answers=(RuntimeError("boom"),), caplog=caplog)
    assert kept == []
    assert "check=unconfirmed" in caplog.text
    assert "pinned_unconfirmed=1" in caplog.text


# 9 no config keeps rc2 behaviour ---------------------------------------------

async def test_no_llm_config_keeps_rc2_behaviour() -> None:
    kept, stub = await _one("D5", config=None)
    assert kept == []
    assert stub.calls == []


# 10 unpinned control ----------------------------------------------------------

async def test_unpinned_control_kept_without_calls() -> None:
    kept, stub = await _one("D5", pin=_pin(pinned=False))
    assert len(kept) == 1
    assert stub.calls == []


# 11 no link --------------------------------------------------------------------

async def test_no_name_link_kept_without_calls() -> None:
    kept, stub = await _one("C1")
    assert len(kept) == 1
    assert stub.calls == []


# 12 budget ---------------------------------------------------------------------

async def test_budget_limits_calls_and_drops_the_rest_unconfirmed(caplog) -> None:
    facts = [
        _fact(text=f"User works at Tidewater, variant {i}.", entities=CASES["D5"]["entities"])
        for i in range(4)
    ]
    kept, stub = await _run(
        facts, {f.text: 0.598 for f in facts}, answers=("NO",), caplog=caplog,
    )
    assert len(stub.calls) == pin_check.PIN_CHECK_MAX_CALLS_PER_CYCLE == 3
    assert [f.text for f in kept] == [f.text for f in facts[:3]]
    assert "pinned_unconfirmed=1" in caplog.text
    assert "check=unconfirmed" in caplog.text


# 13 entity link ----------------------------------------------------------------

async def test_link_is_entity_when_pin_has_the_entity(caplog) -> None:
    pin = _pin(entities=[{"n": "Tidewater", "tp": "company"}])
    kept, _ = await _one("D5", pin=pin, answers=("YES",), caplog=caplog)
    assert kept == []
    assert "link=entity" in caplog.text


# 14 no fact text in logs -------------------------------------------------------

@pytest.mark.parametrize("case_id,answers", [
    ("D5", ("NO",)), ("D4", ("YES",)), ("D4", (RuntimeError("secret text",),)), ("R12", ("YES",)),
])
async def test_logs_carry_no_fact_text(case_id, answers, caplog) -> None:
    await _one(case_id, answers=answers, caplog=caplog)
    assert PIN_TEXT not in caplog.text
    assert CASES[case_id]["text"] not in caplog.text
    assert "secret text" not in caplog.text
    assert PIN_ID not in caplog.text  # only the 8-char prefix


async def test_json_mode_answers_at_contradiction_level() -> None:
    kept, _ = await _one("D5", answers=('{ "answer": "NO" }',))
    assert len(kept) == 1
    kept, _ = await _one("D4", answers=('{"answer": "YES"}',))
    assert kept == []


# S41 review round (R80) -------------------------------------------------------

async def test_cycle_budget_bounds_slow_checks(caplog) -> None:
    calls = []

    async def slow(*a, **k):
        calls.append(1)
        await asyncio.sleep(2)
        return '{"answer": "NO"}'

    facts = [
        _fact(text=f"User works at Tidewater, variant {i}.", entities=CASES["D5"]["entities"])
        for i in range(2)
    ]
    caplog.set_level(logging.INFO, logger="totalreclaw")
    with patch("totalreclaw.embedding.get_embedding", side_effect=lambda t: _vec(0.598)), \
         patch.object(pin_check, "PIN_CHECK_TIMEOUT_S", 5.0), \
         patch.object(pin_check, "PIN_CHECK_CYCLE_BUDGET_S", 0.6), \
         patch.object(pin_check, "chat_completion", new=slow):
        kept = await detect_and_resolve_contradictions(facts, _client(_pin()), llm_config=CFG)
    assert kept == []                 # both dropped as unconfirmed (rc2 rule)
    assert len(calls) == 1            # the second fact started no call
    assert caplog.text.count("check=unconfirmed") == 2


async def test_interpreter_shutdown_propagates() -> None:
    from totalreclaw.agent.loop_runner import InterpreterShutdownError

    stub = Stub(RuntimeError("cannot schedule new futures after interpreter shutdown"))
    with pytest.raises(InterpreterShutdownError):
        await _run([_fact("D5")], {CASES["D5"]["text"]: 0.598}, answers=stub.answers)


async def test_in_band_no_then_high_band_yes_drops() -> None:
    f = _fact("D5")
    c, s = 0.598, math.sqrt(1 - 0.598 ** 2)
    fv = (c, s)
    perp = (-s, c)
    k = math.sqrt(1 - 0.9 ** 2)
    high_vec = [0.9 * fv[0] + k * perp[0], 0.9 * fv[1] + k * perp[1]]
    high_pin = RerankerResult(
        id="aaaaaaaa-0000-0000-0000-000000000000", text=PIN_TEXT, embedding=high_vec,
        importance=0.9, created_at=time.time() - 86400, category="claim", pinned=True, entities=[],
    )
    stub = Stub("NO", "YES")
    client = AsyncMock()
    client.recall = AsyncMock(return_value=[_pin(), high_pin])
    with patch("totalreclaw.embedding.get_embedding", side_effect=lambda t: _vec(0.598)), \
         patch.object(pin_check, "chat_completion", new=stub):
        kept = await detect_and_resolve_contradictions([f], client, llm_config=CFG)
    assert kept == []
    assert len(stub.calls) == 2


async def test_high_band_hit_with_budget_spent_is_kept() -> None:
    with patch.object(pin_check, "PIN_CHECK_MAX_CALLS_PER_CYCLE", 0):
        kept, stub = await _one("R12", answers=("YES",))
    assert len(kept) == 1
    assert stub.calls == []
