"""Regression tests for issue #148.

CLI one-shot mode (``hermes chat -q "<msg>"``) runs each turn in its own
process. The Hermes plugin's ``on_session_finalize`` hook fires from
``hermes_cli``'s atexit chain. By that point Python's
``concurrent.futures.thread`` module has already set its process-global
``_shutdown`` flag from a ``threading._register_atexit`` callback (which
runs even before atexit-module callbacks), and any subsequent
``ThreadPoolExecutor.submit`` raises::

    RuntimeError: cannot schedule new futures after interpreter shutdown

``httpx``'s anyio backend internally calls ``loop.run_in_executor(None,
...)`` for DNS / SSL, so the persistent sync-loop runner can't drive any
HTTP work during atexit. Auto-extract / debrief / on_session_finalize
all silently swallowed the failure pre-rc.23, dropping every CLI-turn
fact on the floor (auto-QA umbrella #147 / sub-issue #148).

The fix:

* ``loop_runner.InterpreterShutdownError`` — typed subclass so callers
  can distinguish "interpreter is gone" from generic ``RuntimeError``.
* ``pending_drain`` — owner-keyed JSONL queue at
  ``~/.totalreclaw/.pending_extract.jsonl``.
* ``lifecycle.auto_extract`` — catches ``InterpreterShutdownError`` and
  enqueues the unprocessed message buffer instead of dropping it.
* ``hermes.hooks.on_session_start`` — drains the queue on a healthy
  interpreter and runs auto-extract on the recovered messages.

These tests pin the contract:

1. ``InterpreterShutdownError`` is detected via both ``isinstance`` and
   the original CPython error message.
2. ``pending_drain`` enqueue + drain round-trips messages atomically and
   filters by owner.
3. ``auto_extract`` catches ``InterpreterShutdownError`` from
   ``_fetch_recent_memories`` AND from ``remember_batch`` and queues the
   unprocessed buffer in both cases.
4. ``on_session_start`` drains the queue and re-runs ``auto_extract``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pytest

from totalreclaw.agent.loop_runner import (
    InterpreterShutdownError,
    is_interpreter_shutdown_error,
)
from totalreclaw.agent import pending_drain
from totalreclaw.agent.pending_drain import (
    drain_pending,
    enqueue_messages,
    has_pending,
)


# ---------------------------------------------------------------------------
# 1. Error detection
# ---------------------------------------------------------------------------


def test_interpreter_shutdown_error_subclass_detected():
    err = InterpreterShutdownError("foo")
    assert is_interpreter_shutdown_error(err)
    assert isinstance(err, RuntimeError)


def test_cpython_message_detected_as_shutdown():
    # Exact message raised by concurrent/futures/thread.py:172.
    err = RuntimeError("cannot schedule new futures after interpreter shutdown")
    assert is_interpreter_shutdown_error(err)


def test_unrelated_runtime_error_not_detected():
    assert not is_interpreter_shutdown_error(RuntimeError("Event loop is closed"))
    assert not is_interpreter_shutdown_error(ValueError("nope"))


# ---------------------------------------------------------------------------
# 2. pending_drain round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def pending_path(tmp_path, monkeypatch):
    p = tmp_path / ".pending_extract.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: p)
    return p


def test_enqueue_then_drain_round_trips(pending_path):
    owner = "0xowner"
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert enqueue_messages(owner, messages) is True
    assert pending_path.exists()
    assert has_pending(owner) is True

    batches = drain_pending(owner)
    assert len(batches) == 1
    assert batches[0] == messages
    # File removed after full drain.
    assert not pending_path.exists()
    assert has_pending(owner) is False


def test_drain_filters_by_owner(pending_path):
    enqueue_messages("0xa", [{"role": "user", "content": "a-1"}])
    enqueue_messages("0xb", [{"role": "user", "content": "b-1"}])
    enqueue_messages("0xa", [{"role": "user", "content": "a-2"}])

    drained_a = drain_pending("0xa")
    assert len(drained_a) == 2
    assert drained_a[0][0]["content"] == "a-1"
    assert drained_a[1][0]["content"] == "a-2"

    # 0xb's batch survives.
    assert has_pending("0xb") is True
    drained_b = drain_pending("0xb")
    assert len(drained_b) == 1
    assert drained_b[0][0]["content"] == "b-1"


def test_enqueue_empty_messages_is_noop(pending_path):
    assert enqueue_messages("0xowner", []) is True
    assert not pending_path.exists()


def test_drain_on_missing_file_is_empty(pending_path):
    assert pending_path.exists() is False
    assert drain_pending("0xowner") == []


def test_drain_skips_malformed_lines(pending_path, caplog):
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps({"owner": "0xowner", "messages": [{"role": "user", "content": "ok"}]})
        + "\n"
        + "not-json\n"
        + json.dumps({"owner": "0xowner", "messages": [{"role": "user", "content": "ok2"}]})
        + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.DEBUG, logger="totalreclaw.agent.pending_drain"):
        batches = drain_pending("0xowner")
    assert len(batches) == 2
    assert batches[0][0]["content"] == "ok"
    assert batches[1][0]["content"] == "ok2"


# ---------------------------------------------------------------------------
# 3. auto_extract catches shutdown -> enqueues
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, *, raise_on: str):
        self._eoa_address = "0xdeadbeef00000000000000000000000000000001"
        self._sa_address = None
        self._raise_on = raise_on

    async def recall(self, *_a, **_kw):
        if self._raise_on == "recall":
            raise InterpreterShutdownError("simulated")
        return []

    async def remember_batch(self, *_a, **_kw):
        if self._raise_on == "remember_batch":
            raise InterpreterShutdownError("simulated")
        return []

    async def forget(self, *_a, **_kw):
        return None


class _FakeState:
    def __init__(self, client: _FakeClient, messages: list[dict]):
        self._client = client
        self._messages = list(messages)
        self._last_processed_idx = 0
        self.quota_warning: Optional[str] = None

    def is_configured(self) -> bool:
        return True

    def get_client(self):
        return self._client

    def get_unprocessed_messages(self):
        return self._messages[self._last_processed_idx :]

    def has_unprocessed_messages(self):
        return self._last_processed_idx < len(self._messages)

    def mark_messages_processed(self):
        self._last_processed_idx = len(self._messages)

    def get_max_facts_per_extraction(self):
        return 5

    def set_quota_warning(self, w: str):
        self.quota_warning = w


def test_auto_extract_catches_recall_shutdown_and_enqueues(pending_path):
    from totalreclaw.agent.lifecycle import auto_extract

    msgs = [
        {"role": "user", "content": "Hi I'm Pedro from Porto."},
        {"role": "assistant", "content": "Got it."},
    ]
    state = _FakeState(_FakeClient(raise_on="recall"), msgs)

    result = auto_extract(state, mode="turn")

    assert result == []
    # Queue file should hold the buffer.
    assert pending_path.exists()
    drained = drain_pending(state._client._eoa_address.lower())
    assert len(drained) == 1
    assert drained[0] == msgs
    # Quota warning surfaced for next-session display.
    assert state.quota_warning is not None
    assert "deferred" in state.quota_warning
    # Messages NOT marked processed — the on_session_finalize / next-run
    # drain path is responsible.
    assert state.has_unprocessed_messages()


def test_auto_extract_with_clean_recall_does_not_touch_queue(pending_path):
    from totalreclaw.agent.lifecycle import auto_extract

    state = _FakeState(
        _FakeClient(raise_on="never"),
        [{"role": "user", "content": "hello"}],
    )

    # Heuristic fallback may yield nothing — that's fine. We only assert
    # the queue stays empty and no quota warning fires for the shutdown
    # path. Other warnings (e.g. LLM-config-missing) are unrelated.
    auto_extract(state, mode="turn")
    assert not pending_path.exists()
    assert state.quota_warning is None or "deferred" not in state.quota_warning


# ---------------------------------------------------------------------------
# 4. hooks.on_session_start drains pending queue
# ---------------------------------------------------------------------------


def test_on_session_start_drains_pending_into_state(pending_path, monkeypatch):
    from totalreclaw.hermes import hooks

    drained_calls: list = []

    def fake_auto_extract(state, mode="turn", llm_config=None):
        drained_calls.append({"mode": mode, "msg_count": len(state._messages)})
        return []

    monkeypatch.setattr(hooks, "_auto_extract", fake_auto_extract)
    monkeypatch.setattr(hooks, "_get_hermes_llm_config", lambda: None)

    owner = "0xdeadbeef00000000000000000000000000000001"
    enqueue_messages(
        owner,
        [
            {"role": "user", "content": "queued from prior session"},
            {"role": "assistant", "content": "queued reply"},
        ],
    )

    state = _FakeState(_FakeClient(raise_on="never"), [])
    # PluginState in production exposes ``add_message`` + ``reset_turn_counter``;
    # FakeState already has add_message-equivalent (we use it via append).
    state.add_message = lambda role, content: state._messages.append(
        {"role": role, "content": content}
    )
    state.reset_turn_counter = lambda: None
    state.get_cached_billing = lambda: None

    hooks.on_session_start(state, session_id="test-session")

    assert len(drained_calls) == 1
    assert drained_calls[0]["mode"] == "full"
    assert drained_calls[0]["msg_count"] == 2
    # Queue consumed.
    assert not pending_path.exists()
    # Quota warning announces the catch-up.
    assert state.quota_warning and "caught up" in state.quota_warning


# ---------------------------------------------------------------------------
# 5. Issue #165 (umbrella #163, F2): pre_llm_call must also drain so the
#    --resume case (where Hermes does NOT fire on_session_start) recovers
#    the queue. Without this, drain is gated to fresh-session boundaries
#    only and ``hermes chat -q --resume <sid>`` users accumulate pending
#    entries forever.
# ---------------------------------------------------------------------------


def test_issue_165_pre_llm_call_drains_pending_for_resume_case(
    pending_path, monkeypatch
):
    from totalreclaw.hermes import hooks

    drained_calls: list = []

    def fake_auto_extract(state, mode="turn", llm_config=None):
        drained_calls.append({"mode": mode, "msg_count": len(state._messages)})
        return []

    def fake_auto_recall(*_a, **_kw):
        return ""

    monkeypatch.setattr(hooks, "_auto_extract", fake_auto_extract)
    monkeypatch.setattr(hooks, "_get_hermes_llm_config", lambda: None)
    monkeypatch.setattr(hooks, "auto_recall", fake_auto_recall)

    owner = "0xdeadbeef00000000000000000000000000000001"
    enqueue_messages(
        owner,
        [
            {"role": "user", "content": "queued during --resume turn N-1"},
            {"role": "assistant", "content": "queued reply"},
        ],
    )

    state = _FakeState(_FakeClient(raise_on="never"), [])
    state.add_message = lambda role, content: state._messages.append(
        {"role": role, "content": content}
    )
    # PluginState surface used by pre_llm_call.
    state.get_quota_warning = lambda: None
    state.clear_quota_warning = lambda: None

    # Simulate ``hermes chat -q --resume <sid> --query "..."`` — Hermes
    # treats this as a continuing session and skips on_session_start, but
    # pre_llm_call still fires.
    hooks.pre_llm_call(state, user_message="continue", is_first_turn=False)

    assert len(drained_calls) == 1, (
        "drain should have run from pre_llm_call even though "
        "on_session_start was never called for the --resume turn"
    )
    assert drained_calls[0]["mode"] == "full"
    assert drained_calls[0]["msg_count"] == 2
    assert not pending_path.exists(), "queue should be consumed after drain"
    assert state.quota_warning and "caught up" in state.quota_warning


def test_issue_165_pre_llm_call_drain_is_noop_when_queue_empty(
    pending_path, monkeypatch
):
    """The drain hook must be cheap on the hot path. With no queue entry,
    no extraction should fire and no quota warning should be set."""
    from totalreclaw.hermes import hooks

    drained_calls: list = []

    def fake_auto_extract(state, mode="turn", llm_config=None):
        drained_calls.append({"mode": mode})
        return []

    def fake_auto_recall(*_a, **_kw):
        return ""

    monkeypatch.setattr(hooks, "_auto_extract", fake_auto_extract)
    monkeypatch.setattr(hooks, "_get_hermes_llm_config", lambda: None)
    monkeypatch.setattr(hooks, "auto_recall", fake_auto_recall)

    state = _FakeState(_FakeClient(raise_on="never"), [])
    state.add_message = lambda role, content: state._messages.append(
        {"role": role, "content": content}
    )
    state.get_quota_warning = lambda: None
    state.clear_quota_warning = lambda: None

    hooks.pre_llm_call(state, user_message="hi", is_first_turn=False)

    # Queue empty → no auto_extract, no quota warning, no on-disk file.
    assert drained_calls == []
    assert not pending_path.exists()
    assert state.quota_warning is None
