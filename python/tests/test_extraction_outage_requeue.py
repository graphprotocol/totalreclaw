"""Extraction outage re-queue (internal #604 / product #553).

An LLM upstream outage (z.ai 1302, 429, 5xx, timeouts) must keep the
conversation window and retry it later, bounded, instead of marking it
processed. Zero-facts answers and non-retryable errors are unchanged.
"""
from __future__ import annotations

import json

import pytest

from totalreclaw.agent import pending_drain


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
