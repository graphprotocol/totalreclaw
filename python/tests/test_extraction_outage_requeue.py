"""Extraction outage re-queue (internal #604 / product #553).

An LLM upstream outage (z.ai 1302, 429, 5xx, timeouts) must keep the
conversation window and retry it later, bounded, instead of marking it
processed. Zero-facts answers and non-retryable errors are unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import httpx
import pytest

from .test_defer_extraction_reads_paused import ASSIST4, USER4, _Env, _fact

from totalreclaw.agent import lifecycle, pending_drain
from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.agent.extraction import extract_facts_llm
from totalreclaw.agent.llm_client import LLMConfig

CHAT = LLMConfig(
    api_key="k", base_url="https://api.example.invalid/v1", model="m", api_format="openai"
)
QA_MESSAGES = [
    {"role": "user", "content": "To be clear for the record: I live in Porto and I work at Tidewater."},
    {"role": "assistant", "content": "Noted."},
]
Z1302 = '{"error":{"code":"1302","message":"Rate limit reached for requests"}}'


def _http_error(status, body):
    request = httpx.Request("POST", "https://api.example.invalid/v1/chat/completions")
    response = httpx.Response(status, content=body.encode("utf-8"), request=request)
    return httpx.HTTPStatusError("http error", request=request, response=response)


@pytest.fixture
def pending_path(tmp_path, monkeypatch):
    p = tmp_path / ".pending_extract.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: p)
    return p


# ---- Task 1: pending queue records attempts --------------------------------

def test_enqueue_attempts_roundtrip(pending_path):
    assert pending_drain.enqueue_messages(
        "0xabc", [{"role": "user", "content": "a"}], session_id="s", attempts=3)
    (b,) = pending_drain.drain_pending("0xabc", path=pending_path, with_meta=True)
    assert b["attempts"] == 3 and b["session_id"] == "s"


def test_attempts_absent_or_bad_reads_as_zero(pending_path):
    pending_drain.enqueue_messages("0xabc", [{"role": "user", "content": "a"}])
    assert '"attempts"' not in pending_path.read_text()  # zero is not written
    pending_path.write_text(pending_path.read_text() + json.dumps(
        {"owner": "0xabc", "messages": [{"role": "user", "content": "b"}],
         "attempts": "x"}) + "\n")
    assert [b["attempts"] for b in pending_drain.drain_pending(
        "0xabc", path=pending_path, with_meta=True)] == [0, 0]


# ---- Task 2: extract_facts_llm separates outage from error ------------------

async def _extract_with(status, body):
    async def fake_openai(cfg, sp, up, mt, t):
        raise _http_error(status, body)
    outcome = {}
    with patch("totalreclaw.agent.llm_client._call_openai", new=fake_openai), \
         patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01] * 5), \
         patch.dict(os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}):
        facts = await extract_facts_llm(QA_MESSAGES, llm_config=CHAT, outcome=outcome)
    return facts, outcome


async def test_zai_1302_is_outage():
    assert await _extract_with(429, Z1302) == ([], {"llm": "outage"})


async def test_http_400_is_error_not_outage():
    assert await _extract_with(
        400, '{"error":{"message":"must contain the word json"}}') == ([], {"llm": "error"})


# ---- Task 3: auto_extract keeps, queues or drops a window -------------------

class _OutageEnv(_Env):
    """_Env whose LLM can report an outage (``outage=True``) or an error."""

    def __init__(self):
        super().__init__()
        self.outage = False
        self.error = False

    async def fake_llm(self, messages, mode="turn", existing_memories=None,
                       llm_config=None, dedup_context=None, outcome=None):
        self.llm_calls.append({"messages": list(messages), "mode": mode, "ctx": dedup_context})
        if self.outage:
            outcome["llm"] = "outage"
            return []
        if self.error:
            outcome["llm"] = "error"
            return []
        outcome["llm"] = "ok"
        return list(self.llm_facts)


def _drained(path):
    return pending_drain.drain_pending("0xabc", path=path, with_meta=True)


def test_outage_keeps_window_then_retries_all_in_full_mode(caplog):
    env = _OutageEnv()
    env.outage = True
    env.turn()
    with caplog.at_level(logging.INFO):
        assert env.run() == []
    assert env.state.has_unprocessed_messages()
    assert env.state._extraction_outage_streak == 1
    assert "LLM upstream outage (attempt 1/5, 2 messages kept" in caplog.text
    env.outage = False
    env.llm_facts = [_fact("User lives in Porto and works at Tidewater.")]
    for _ in range(3):
        env.turn()
    assert len(env.run()) == 1
    last = env.llm_calls[-1]
    assert last["mode"] == "full" and any(m["content"] == USER4 for m in last["messages"])
    assert len(last["messages"]) == 8
    assert env.state._extraction_outage_streak == 0
    assert not env.state.has_unprocessed_messages()


def test_zero_facts_marks_processed():
    env = _OutageEnv()
    env.turn()
    assert env.run() == []
    assert not env.state.has_unprocessed_messages()
    assert env.state._extraction_outage_streak == 0


def test_non_outage_error_marks_processed(pending_path):
    env = _OutageEnv()
    env.error = True
    env.turn()
    assert env.run() == []
    assert not env.state.has_unprocessed_messages()
    assert env.state._extraction_outage_streak == 0
    assert not pending_path.exists()


def test_outage_moves_to_queue_after_live_retries(pending_path):
    env = _OutageEnv()
    env.outage = True
    for i in range(3):
        env.turn()
        env.run()
        if i < 2:
            assert not pending_path.exists()
    (b,) = _drained(pending_path)
    assert b["attempts"] == 3 and len(b["messages"]) == 6
    assert not env.state.has_unprocessed_messages()
    assert env.state._extraction_outage_streak == 0
    assert env.state._extraction_requeued is True


def test_finalize_outage_queues_with_attempt_1(pending_path):
    from totalreclaw.hermes import hooks
    env = _OutageEnv()
    env.outage = True
    env.client._relay._session_id = "sess-live"
    env.turn()
    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
         patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        hooks._auto_extract_queueing(env.state, mode="full", llm_config=object())
    (b,) = _drained(pending_path)
    assert b["attempts"] == 1 and b["session_id"] == "sess-live"
    assert not env.state.has_unprocessed_messages()


def test_outage_gives_up_loudly_at_max(pending_path, caplog):
    env = _OutageEnv()
    env.outage = True
    env.state._extraction_outage_streak = 4
    env.turn()
    with caplog.at_level(logging.WARNING):
        env.run()
    assert "gave up" in caplog.text and "5 attempts" in caplog.text
    assert not env.state.has_unprocessed_messages()
    assert not pending_path.exists()
    assert env.state._extraction_outage_streak == 0


def test_outage_over_defer_cap_goes_to_queue(pending_path):
    env = _OutageEnv()
    env.outage = True
    for _ in range(21):
        env.turn()
    env.run()
    (b,) = _drained(pending_path)
    assert b["attempts"] == 1 and len(b["messages"]) == 42


def test_outage_queue_write_failure_drops_with_warning(caplog):
    env = _OutageEnv()
    env.outage = True
    env.turn()
    with patch("totalreclaw.agent.lifecycle.enqueue_messages", return_value=False), \
         lifecycle.on_paused_policy("queue"), caplog.at_level(logging.WARNING):
        env.run()
    assert "queue write failed" in caplog.text
    assert not env.state.has_unprocessed_messages()
    assert env.state._extraction_outage_streak == 0


async def test_zai_1302_keeps_window_end_to_end():
    from .test_no_heuristic_override_h15 import _make_state, _run
    state, _client = _make_state()

    async def fake_openai(cfg, sp, up, mt, t):
        raise _http_error(429, Z1302)

    import asyncio
    with patch("totalreclaw.agent.llm_client._call_openai", new=fake_openai), \
         patch("totalreclaw.agent.llm_client._BACKOFF_DELAYS", [0.01] * 5), \
         patch.dict(os.environ, {"TOTALRECLAW_LLM_RETRY_BUDGET_MS": "60000"}):
        stored, _ = await asyncio.to_thread(_run, state)
    assert stored == []
    assert state.has_unprocessed_messages()
    assert state._extraction_outage_streak == 1


def test_outage_streak_travels_with_its_slot():
    env = _OutageEnv()
    env.outage = True
    env.state.activate_conversation("chat-A")
    env.turn()
    env.run()
    assert env.state._extraction_outage_streak == 1
    env.state.activate_conversation("chat-B")
    assert env.state._extraction_outage_streak == 0
    env.state.activate_conversation("chat-A")
    assert env.state._extraction_outage_streak == 1
    env.state.clear_messages()
    assert env.state._extraction_outage_streak == 0


# ---- Task 4: session-start drain re-queues on an outage, bounded -----------

def _batch(attempts=0, sid="sess-orig"):
    return {"messages": [{"role": "user", "content": USER4},
                         {"role": "assistant", "content": ASSIST4}],
            "session_id": sid, "attempts": attempts}


def _drain(env, batches):
    from totalreclaw.hermes import hooks

    async def passthrough(fs, *a, **k):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle.apply_pin_guard", new=passthrough), \
         patch("totalreclaw.agent.lifecycle.heuristic_fallback_facts", return_value=[]), \
         patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=object()):
        return hooks._drain_into_state(env.state, batches)


def test_drain_outage_requeues_with_next_attempt(pending_path):
    env = _OutageEnv()
    env.outage = True
    assert _drain(env, [_batch(attempts=2)]) == 0
    (b,) = _drained(pending_path)
    assert b["attempts"] == 3 and b["session_id"] == "sess-orig"


def test_drain_gives_up_at_max_attempts(pending_path, caplog):
    env = _OutageEnv()
    env.outage = True
    with caplog.at_level(logging.WARNING):
        assert _drain(env, [_batch(attempts=4)]) == 0
    assert not pending_path.exists()
    assert "gave up" in caplog.text


def test_drain_short_circuits_and_ages_remaining_batches(pending_path, caplog):
    env = _OutageEnv()
    env.outage = True
    with caplog.at_level(logging.WARNING):
        assert _drain(env, [_batch(0), _batch(0), _batch(4)]) == 0
    assert len(env.llm_calls) == 1
    assert [b["attempts"] for b in _drained(pending_path)] == [1, 1]
    assert "gave up" in caplog.text


def test_drain_paused_requeue_keeps_attempts(pending_path):
    env = _OutageEnv()
    env.paused = True
    assert _drain(env, [_batch(attempts=2)]) == 0
    (b,) = _drained(pending_path)
    assert b["attempts"] == 2


def test_drain_success_resets_streak(pending_path):
    env = _OutageEnv()
    env.llm_facts = [_fact("User lives in Porto and works at Tidewater.")]
    assert _drain(env, [_batch(attempts=3)]) == 2
    assert env.state._extraction_outage_streak == 0
    assert not pending_path.exists()
