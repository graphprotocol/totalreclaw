"""Extraction outage re-queue (internal #604 / product #553).

An LLM upstream outage (z.ai 1302, 429, 5xx, timeouts) must keep the
conversation window and retry it later, bounded, instead of marking it
processed. Zero-facts answers and non-retryable errors are unchanged.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest

from totalreclaw.agent import pending_drain
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
