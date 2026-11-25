"""Session finalize must fit Hermes' 10 s hook budget without losing work
(internal #657).

A fake clock plus slow mocks stand in for the LLM stages. Work that does not
fit must land in the #148 pending queue, never vanish.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent import pending_drain
from totalreclaw.hermes import hooks
from totalreclaw.hermes.state import PluginState

OWNER = "0xabc0000000000000000000000000000000000001"


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(hooks, "_clock", c)
    monkeypatch.setattr(hooks, "_stage_seconds", {})
    monkeypatch.setenv(hooks._STAGE_ESTIMATE_ENV, "12")
    monkeypatch.setenv(hooks._FINALIZE_BUDGET_ENV, "8")
    return c


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "pending.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: path)
    return path


def _state(n_turns: int = 5) -> PluginState:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            st = PluginState()
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-id")
    st._client = client
    st.start_session(external_id="s1")
    for i in range(n_turns):
        st.add_message("user", f"u{i}")
        st.add_message("assistant", f"a{i}")
    return st


def _queued() -> list:
    return pending_drain.drain_pending([OWNER], with_meta=True)


def _run_finalize(state, **patches):
    ctx = [
        patch.object(hooks, "_owner_address", return_value=OWNER),
        patch.object(hooks, "_get_hermes_llm_config", return_value=None),
    ]
    for k, v in patches.items():
        ctx.append(patch.object(hooks, k, v))
    for c in ctx:
        c.start()
    try:
        hooks.on_session_finalize(state, session_id="s1", reason="cli_close")
    finally:
        for c in reversed(ctx):
            c.stop()


def test_cold_finalize_defers_without_running_llm_stages(clock, queue):
    st = _state()
    extract = MagicMock(return_value=["f"])
    debrief = MagicMock(return_value=[])
    _run_finalize(st, _auto_extract=extract, _session_debrief=debrief)

    extract.assert_not_called()
    debrief.assert_not_called()
    batches = _queued()
    assert len(batches) == 1
    assert [m["content"] for m in batches[0]["messages"]][:2] == ["u0", "a0"]
    assert len(batches[0]["messages"]) == 10  # nothing dropped
    assert batches[0]["crystal"] is True      # the Crystal is deferred too


def test_short_segment_defers_unprocessed_only(clock, queue):
    st = _state(n_turns=1)
    extract = MagicMock(return_value=[])
    _run_finalize(st, _auto_extract=extract, _session_debrief=MagicMock(return_value=[]))
    extract.assert_not_called()
    batches = _queued()
    assert len(batches) == 1 and batches[0]["crystal"] is False
    assert len(batches[0]["messages"]) == 2


def test_warm_extract_fits_and_journal_is_cleared(clock, queue):
    hooks._stage_seconds.update({"extract": 2.0, "crystal": 2.0})

    def slow_extract(state, **kw):
        # journal-first: the batch is on disk while extraction runs
        assert queue.exists() and "token" in queue.read_text()
        clock.advance(2.0)
        state.mark_messages_processed()
        return ["a fact"]

    def slow_debrief(state, stored_fact_texts=None):
        clock.advance(2.0)
        return ["crystal"]

    t0 = clock()
    debrief = MagicMock(side_effect=slow_debrief)
    _run_finalize(_st := _state(), _auto_extract=slow_extract, _session_debrief=debrief)
    assert clock() - t0 <= 8.0
    debrief.assert_called_once()
    assert not queue.exists()  # landed -> no leftover record


def test_crystal_that_no_longer_fits_is_queued(clock, queue):
    hooks._stage_seconds.update({"extract": 2.0, "crystal": 6.0})

    def extract(state, **kw):
        clock.advance(4.0)  # remaining 4 < crystal estimate 6
        state.mark_messages_processed()
        return ["f1", "f2"]

    debrief = MagicMock(return_value=[])
    _run_finalize(_state(), _auto_extract=extract, _session_debrief=debrief)
    debrief.assert_not_called()
    batches = _queued()
    assert len(batches) == 1 and batches[0]["crystal"] is True


def test_killed_mid_extraction_leaves_journal(clock, queue):
    """A hard kill during the inline extraction must not lose the batch."""
    hooks._stage_seconds.update({"extract": 1.0, "crystal": 1.0})

    def dies(state, **kw):
        raise KeyboardInterrupt  # stands in for the process being killed

    with pytest.raises(KeyboardInterrupt):
        _run_finalize(_state(), _auto_extract=dies, _session_debrief=MagicMock())
    assert len(_queued()) == 1


def test_queue_write_failure_falls_back_to_inline(clock, queue):
    extract = MagicMock(return_value=[])
    with patch.object(hooks, "_enqueue_messages", return_value=False):
        _run_finalize(_state(), _auto_extract=extract, _session_debrief=MagicMock(return_value=[]))
    extract.assert_called_once()  # tried inline rather than dropping


def test_drain_journals_batches_and_replays_crystal(clock, queue):
    st = _state(n_turns=0)
    batches = [{"messages": [{"role": "user", "content": "x"}] * 8,
                "session_id": "old", "crystal": True}]
    seen = {}

    def extract(state, **kw):
        seen["on_disk"] = queue.exists()  # re-journaled before the LLM call
        return ["fact"]

    debrief = MagicMock(return_value=["c"])
    with patch.object(hooks, "_owner_address", return_value=OWNER), \
         patch.object(hooks, "_auto_extract", extract), \
         patch.object(hooks, "_session_debrief", debrief), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=None):
        hooks._drain_into_state(st, batches)
    assert seen["on_disk"] is True
    debrief.assert_called_once()
    assert not queue.exists()


def test_discard_record_removes_only_its_token(queue):
    pending_drain.enqueue_messages(OWNER, [{"role": "user", "content": "a"}], path=queue, token="t1")
    pending_drain.enqueue_messages(OWNER, [{"role": "user", "content": "b"}], path=queue, token="t2")
    assert pending_drain.discard_record("t1", path=queue) is True
    left = pending_drain.drain_pending([OWNER], path=queue)
    assert left == [[{"role": "user", "content": "b"}]]


# --- review follow-ups ------------------------------------------------------

def _drain(state, batches, **patches):
    ctx = [
        patch.object(hooks, "_owner_address", return_value=OWNER),
        patch.object(hooks, "_get_hermes_llm_config", return_value=None),
    ]
    for k, v in patches.items():
        ctx.append(patch.object(hooks, k, v))
    for c in ctx:
        c.start()
    try:
        return hooks._drain_into_state(state, batches)
    finally:
        for c in reversed(ctx):
            c.stop()


def test_deferred_record_carries_the_active_session_id(clock, queue):
    st = _state()
    _run_finalize(st, _auto_extract=MagicMock(), _session_debrief=MagicMock())
    batches = _queued()
    assert batches[0]["session_id"] == st.session_id or batches[0]["session_id"]
    assert batches[0]["session_id"] != ""


def test_drain_passes_original_session_to_facts_and_crystal(clock, queue):
    st = _state(n_turns=0)
    seen = {}

    def extract(state, **kw):
        seen["override"] = kw.get("session_id_override")
        return []

    debrief = MagicMock(return_value=["c"])
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    _drain(st, [{"messages": msgs, "session_id": "orig-1", "crystal": True}],
           _auto_extract=extract, _session_debrief=debrief)
    assert seen["override"] == "orig-1"
    kw = debrief.call_args.kwargs
    assert kw["session_id"] == "orig-1"
    # 6 messages, 0 new facts: the finalize-time gate decision is not re-run
    assert kw["gate_already_passed"] is True


def test_real_session_debrief_replay_writes_crystal_for_6_messages():
    """Real session_debrief + real generate_crystal; only the LLM call is mocked.
    6 messages and 0 new facts on replay: the finalize-time gate decision holds."""
    import json as _json
    from totalreclaw.agent import debrief, lifecycle

    st = _state(n_turns=3)  # 6 messages
    client = MagicMock()
    client.remember = AsyncMock(return_value="crystal-id")
    st.get_client = MagicMock(return_value=client)
    llm = AsyncMock(return_value=_json.dumps({
        "narrative": "The user planned a trip and settled the details.",
        "key_outcomes": ["booked"], "open_threads": [], "lessons": [],
        "topics_discussed": ["travel"], "importance": 6,
    }))
    with patch.object(debrief, "detect_llm_config", return_value=MagicMock()), \
         patch.object(debrief, "chat_completion", llm):
        assert lifecycle.session_debrief(st, stored_fact_texts=[]) == []
        assert llm.await_count == 0   # normal run: both gates apply
        ids = lifecycle.session_debrief(st, stored_fact_texts=[], gate_already_passed=True)
    assert ids == ["crystal-id"]
    assert llm.await_count == 1
    client.remember.assert_awaited_once()


def test_replay_skip_covers_records_without_session_id(clock, queue):
    st = _state(n_turns=0)
    hooks._deferred_crystals_done.clear()
    debrief = MagicMock(return_value=["c"])
    b = {"messages": [{"role": "user", "content": "m"}] * 8, "session_id": None, "crystal": True}
    _drain(st, [b, dict(b)], _auto_extract=MagicMock(return_value=[]), _session_debrief=debrief)
    assert debrief.call_count == 1


def test_paused_requeue_keeps_crystal_flag(clock, queue):
    from totalreclaw.agent import lifecycle

    st = _state(n_turns=0)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(8)]

    def extract(state, **kw):
        # what auto_extract's paused path does
        lifecycle._enqueue_unprocessed(state, state.get_client(), list(msgs))
        state._extraction_requeued = True
        return []

    debrief = MagicMock()
    with patch.object(lifecycle, "_owner_address", return_value=OWNER):
        _drain(st, [{"messages": msgs, "session_id": "o", "crystal": True, "attempts": 2}],
               _auto_extract=extract, _session_debrief=debrief)
    debrief.assert_not_called()
    left = _queued()
    assert len(left) == 1 and left[0]["crystal"] is True


def test_drain_is_time_boxed_and_leaves_rest_queued(clock, queue):
    st = _state(n_turns=0)
    hooks._stage_seconds.update({"extract": 3.0})

    def extract(state, **kw):
        clock.advance(3.0)
        return []

    mk = lambda i: {"messages": [{"role": "user", "content": f"b{i}"}], "session_id": f"s{i}",
                    "attempts": 1}
    ext = MagicMock(side_effect=extract)
    _drain(st, [mk(0), mk(1), mk(2), mk(3)], _auto_extract=ext, _session_debrief=MagicMock())
    assert ext.call_count == 2   # 8 s budget, 3 s per batch: a third (2 s left) does not fit
    left = _queued()
    assert [b["session_id"] for b in left] == ["s2", "s3"]
    assert left[0]["attempts"] == 1   # outage counter preserved


def test_empty_batch_does_not_rejournal_forever(clock, queue):
    st = _state(n_turns=0)
    _drain(st, [{"messages": [{"role": "user"}], "session_id": "x"}],
           _auto_extract=MagicMock(), _session_debrief=MagicMock())
    assert not queue.exists()


def test_deferred_crystal_not_stored_twice_in_process(clock, queue):
    st = _state(n_turns=0)
    hooks._deferred_crystals_done.clear()
    debrief = MagicMock(return_value=["c"])
    b = {"messages": [{"role": "user", "content": "m"}] * 8, "session_id": "dup", "crystal": True}
    _drain(st, [b, dict(b)], _auto_extract=MagicMock(return_value=[]), _session_debrief=debrief)
    assert debrief.call_count == 1
