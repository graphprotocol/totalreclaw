"""S37 (internal H15): auto-extraction waits while relay reads are paused.

Hermes QA of 2.6.0rc1 stored two restatements of an existing fact because
extraction ran with no vault view while the relay had paused reads. These
tests pin: deferral (nothing stored, messages kept), catch-up with the vault
view after the pause, ``mode="full"`` for a long deferred buffer, the
finalize/drain queue path, the safety valve, and the audit log lines.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from totalreclaw.agent import lifecycle, pending_drain
from totalreclaw.agent.extraction import TURN_MODE_WINDOW_MESSAGES, ExtractedFact
from totalreclaw.relay import RelayRateLimited, ephemeral_read_block_state

ORIG_ID = "1bc87008-4a34-489f-bbb6-75fcf9e4df1a"
ORIG = (
    "User relocated from Lisbon to Porto in October 2026 to join Tidewater, a small "
    "climate-tech startup, where they are building the company's carbon-accounting dashboard."
)
USER4 = "Please remember that I live in Porto and work at Tidewater."
ASSIST4 = "That one's already saved, so I skipped writing a duplicate."


def _paused_error() -> RelayRateLimited:
    req = httpx.Request("POST", "https://relay.invalid/v1/subgraph")
    return RelayRateLimited(httpx.Response(429, json={}, request=req), retry_after_s=2100)


class _R:
    def __init__(self, id, text):
        self.id, self.text, self.embedding, self.pinned = id, text, None, False


class _Env:
    """A PluginState with a fake client whose reads can be paused / resumed."""

    def __init__(self):
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                self.state = PluginState()
        self.paused = False
        c = MagicMock()
        c.read_block = None  # real clients return None or a ReadBlockState
        c.eoa_address = "0xabc"
        c.resolved_wallet_address = None
        c.recall = AsyncMock(side_effect=self._recall)
        c.fetch_recent_facts = AsyncMock(side_effect=self._recent)
        c.forget = AsyncMock(return_value=True)
        c.forget_ledger = None
        c.resolved_chain_id = 100
        c.remember_batch = AsyncMock(
            side_effect=lambda facts, source="": [f"new-{i}" for i in range(len(facts))]
        )
        self.client = c
        self.state._client = c
        self.llm_calls: list[dict] = []
        self.llm_facts: list[ExtractedFact] = []

    async def _recall(self, *a, **k):
        if self.paused:
            raise _paused_error()
        return [_R(ORIG_ID, ORIG)]

    async def _recent(self, *a, **k):
        if self.paused:
            raise _paused_error()
        return [{"id": ORIG_ID, "text": ORIG, "embedding": None}]

    async def fake_llm(self, messages, mode="turn", existing_memories=None, llm_config=None, dedup_context=None, outcome=None):
        self.llm_calls.append({"messages": list(messages), "mode": mode, "ctx": dedup_context})
        return list(self.llm_facts)

    def run(self, **kw):
        async def passthrough(fs, *a, **k):
            return fs

        with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=self.fake_llm), \
             patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
             patch("totalreclaw.agent.lifecycle.apply_pin_guard", new=passthrough), \
             patch("totalreclaw.agent.lifecycle.heuristic_fallback_facts", return_value=[]), \
             patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
             patch("totalreclaw.embedding.get_embedding", return_value=None):
            return lifecycle.auto_extract(self.state, llm_config=object(), **kw)

    def turn(self, user=USER4, assistant=ASSIST4):
        self.state.add_message("user", user)
        self.state.add_message("assistant", assistant)


def _fact(text, action="ADD"):
    return ExtractedFact(text=text, type="claim", importance=8, action=action,
                         confidence=0.9, source="user", scope="unspecified")


@pytest.fixture
def pending_path(tmp_path, monkeypatch):
    p = tmp_path / ".pending_extract.jsonl"
    monkeypatch.setattr(pending_drain, "_pending_path", lambda: p)
    return p


# (a)
def test_auto_extract_defers_when_reads_paused(caplog):
    env = _Env()
    env.paused = True
    env.llm_facts = [_fact("User lives in Porto.")]
    env.turn()
    with caplog.at_level(logging.INFO, logger="totalreclaw"):
        stored = env.run()
    assert stored == []
    assert env.llm_calls == []
    env.client.remember_batch.assert_not_called()
    assert env.state.has_unprocessed_messages()
    assert any("auto-extraction deferred" in r.getMessage() and r.levelno == logging.INFO
               and "2 messages kept" in r.getMessage() for r in caplog.records)


def test_auto_extract_defers_on_client_read_block_signal(caplog):
    env = _Env()
    env.client.read_block = ephemeral_read_block_state(_paused_error())
    env.turn()
    with caplog.at_level(logging.INFO, logger="totalreclaw"):
        assert env.run() == []
    assert env.llm_calls == []
    env.client.recall.assert_not_called()  # signal (a): no pointless fetch
    assert env.state.has_unprocessed_messages()
    assert any("auto-extraction deferred: reads paused until " in r.getMessage()
               and "unknown" not in r.getMessage() for r in caplog.records)


def test_magicmock_client_does_not_look_paused():
    env = _Env()
    env.client.read_block = MagicMock()  # truthy, not a ReadBlockState
    env.turn()
    env.run()
    assert len(env.llm_calls) == 1


# (b)
def test_deferred_turn_extracts_with_vault_after_pause():
    env = _Env()
    env.paused = True
    env.turn()
    assert env.run() == []
    assert env.llm_calls == []

    env.paused = False
    env.llm_facts = []
    assert env.run() == []
    (call,) = env.llm_calls
    assert f"[ID: {ORIG_ID}]" in call["ctx"]
    assert any(m["content"] == USER4 for m in call["messages"])
    env.client.remember_batch.assert_not_called()
    assert not env.state.has_unprocessed_messages()


# (c)
def test_long_deferred_buffer_runs_in_full_mode():
    env = _Env()
    env.paused = True
    for i in range(TURN_MODE_WINDOW_MESSAGES):  # 12 messages > window
        env.turn(f"user {i}", f"assistant {i}")
    env.run()  # deferred (default mode is "turn")
    assert env.llm_calls == []
    env.paused = False
    env.run()
    (call,) = env.llm_calls
    assert call["mode"] == "full"


def test_short_buffer_without_deferral_stays_turn_mode():
    env = _Env()
    for i in range(TURN_MODE_WINDOW_MESSAGES):
        env.turn(f"user {i}", f"assistant {i}")
    env.run()
    assert env.llm_calls[0]["mode"] == "turn"


# (d)
def test_finalize_while_paused_enqueues_buffer(pending_path):
    from totalreclaw.hermes import hooks

    env = _Env()
    env.paused = True
    env.llm_facts = [_fact("User lives in Porto.")]
    env.turn()
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=object()), \
         patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm):
        stored = hooks._auto_extract_queueing(env.state, mode="full", llm_config=object())
    assert stored == []
    assert env.llm_calls == []
    env.client.remember_batch.assert_not_called()
    batches = pending_drain.drain_pending("0xabc", path=pending_path, with_meta=True)
    assert len(batches) == 1
    assert [m["content"] for m in batches[0]["messages"]] == [USER4, ASSIST4]
    assert not env.state.has_unprocessed_messages()


def test_drain_while_paused_requeues_not_drops(pending_path):
    from totalreclaw.hermes import hooks

    env = _Env()
    env.paused = True
    batch = {"messages": [{"role": "user", "content": USER4}, {"role": "assistant", "content": ASSIST4}],
             "session_id": "sess-orig"}
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=object()), \
         patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm):
        # nothing landed: the batch was queued again, so it is not counted
        assert hooks._drain_into_state(env.state, [batch]) == 0
    assert env.llm_calls == []
    env.client.remember_batch.assert_not_called()
    again = pending_drain.drain_pending("0xabc", path=pending_path, with_meta=True)
    assert len(again) == 1  # one re-queued record, no loop
    assert again[0]["session_id"] == "sess-orig"
    assert [m["content"] for m in again[0]["messages"]] == [USER4, ASSIST4]


def test_enqueue_failure_falls_back_to_blind_with_warning(pending_path, caplog):
    from totalreclaw.hermes import hooks

    env = _Env()
    env.paused = True
    env.turn()
    with patch.object(lifecycle, "enqueue_messages", return_value=False), \
         caplog.at_level(logging.WARNING, logger="totalreclaw"):
        with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
             patch("totalreclaw.embedding.is_model_available_locally", return_value=False):
            hooks._auto_extract_queueing(env.state, mode="full", llm_config=object())
    assert len(env.llm_calls) == 1
    assert any("ran without a vault view" in r.getMessage() for r in caplog.records)


def test_on_pre_compress_extracts_blind_with_warning(caplog):
    env = _Env()
    env.paused = True
    env.turn()
    with caplog.at_level(logging.WARNING, logger="totalreclaw"):
        with lifecycle.on_paused_policy("blind"):
            env.run()
    assert len(env.llm_calls) == 1
    assert any("auto-extraction ran without a vault view: reads paused and 2 messages waiting"
               in r.getMessage() for r in caplog.records)
    assert caplog.text.count("ran without a vault view") == 1  # no double warning


# (e)
def test_over_defer_cap_extracts_blind_and_warns(caplog):
    env = _Env()
    env.paused = True
    for i in range(lifecycle.DEFER_MAX_MESSAGES // 2 + 1):  # 42 messages
        env.turn(f"user {i}", f"assistant {i}")
    with caplog.at_level(logging.WARNING, logger="totalreclaw"):
        env.run()
    assert len(env.llm_calls) == 1
    assert any(r.levelno == logging.WARNING and
               "auto-extraction ran without a vault view: reads paused and 42 messages waiting"
               in r.getMessage() for r in caplog.records)


# (f)
def test_degraded_vault_view_token_is_info(caplog):
    env = _Env()
    env.paused = True
    env.turn()
    with caplog.at_level(logging.INFO, logger="totalreclaw"):
        env.run()
    assert any(r.levelno == logging.INFO and r.getMessage().startswith("vault view degraded: reads paused")
               for r in caplog.records)


def test_per_cycle_summary_counts(caplog):
    env = _Env()
    env.llm_facts = [_fact("User adopted a cat named Miso."), _fact("Already known thing", action="NOOP")]
    env.turn("I adopted a cat named Miso", "Nice")
    with caplog.at_level(logging.INFO, logger="totalreclaw"):
        stored = env.run()
    assert stored == ["User adopted a cat named Miso."]
    assert any(r.getMessage() == "auto-extract: 2 proposals, 0 pii-dropped, 1 noop, 0 gate-skipped, 0 forget-skipped, 1 stored, 0 other"
               for r in caplog.records), caplog.text
    assert "Miso" not in "".join(r.getMessage() for r in caplog.records if "auto-extract:" in r.getMessage())


def test_summary_adds_up_with_pii_and_other(caplog):
    env = _Env()
    env.llm_facts = [_fact("User adopted a cat named Miso."), _fact("secret thing")]
    env.turn("I adopted a cat named Miso", "Nice")
    real = lifecycle.drop_sensitive_facts
    with patch.object(lifecycle, "drop_sensitive_facts", side_effect=lambda fs: [f for f in fs if "secret" not in f.text]), \
         caplog.at_level(logging.INFO, logger="totalreclaw"):
        env.run()
    assert any(r.getMessage() == "auto-extract: 2 proposals, 1 pii-dropped, 0 noop, 0 gate-skipped, "
                                 "0 forget-skipped, 1 stored, 0 other" for r in caplog.records), caplog.text


# --- review round (R72) -------------------------------------------------


def test_deferral_flag_travels_with_its_conversation_slot():
    env = _Env()
    st = env.state
    st.activate_conversation("chat-A")
    env.paused = True
    for i in range(TURN_MODE_WINDOW_MESSAGES):  # 12 messages in A
        env.turn(f"A-user {i}", f"A-assistant {i}")
    assert env.run() == []
    assert env.llm_calls == []

    st.activate_conversation("chat-B")
    env.paused = False
    env.turn("B-user", "B-assistant")
    env.run()  # B extracts (its own, short buffer, turn mode)
    assert env.llm_calls[-1]["mode"] == "turn"

    st.activate_conversation("chat-A")
    env.llm_calls.clear()
    env.run()
    (call,) = env.llm_calls
    assert call["mode"] == "full"
    contents = [m["content"] for m in call["messages"]]
    assert "A-user 0" in contents and "A-assistant 5" in contents
    assert len(contents) == 2 * TURN_MODE_WINDOW_MESSAGES


def test_drain_queues_live_deferred_buffer_instead_of_consuming_it(pending_path):
    from totalreclaw.hermes import hooks

    env = _Env()
    env.turn("live deferred user", "live deferred assistant")
    batch = {"messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "ok"}],
             "session_id": "sess-orig"}
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=object()), \
         patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
         patch("totalreclaw.agent.lifecycle.heuristic_fallback_facts", return_value=[]):
        assert hooks._drain_into_state(env.state, [batch]) == 2
    # the drained batch was extracted on its own ...
    assert [m["content"] for m in env.llm_calls[0]["messages"]] == ["old", "ok"]
    # ... and the live messages were queued, not lost
    queued = pending_drain.drain_pending("0xabc", path=pending_path, with_meta=True)
    assert [m["content"] for b in queued for m in b["messages"]] == [
        "live deferred user", "live deferred assistant"]


def test_ingest_turn_cadence_defers_when_reads_paused():
    from totalreclaw.hermes import hooks

    env = _Env()
    env.paused = True
    with patch.object(env.state, "get_extraction_interval", return_value=1), \
         patch.object(env.state, "is_configured", return_value=True), \
         patch.object(hooks, "_resolve_extraction_llm_config", return_value=object()), \
         patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
         patch("totalreclaw.embedding.is_model_available_locally", return_value=False):
        hooks.ingest_turn(env.state, USER4, ASSIST4)
    assert env.llm_calls == []
    env.client.remember_batch.assert_not_called()
    assert env.state.has_unprocessed_messages()


def test_provider_on_pre_compress_extracts_blind_and_warns(caplog):
    from totalreclaw.hermes import hooks
    from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider

    env = _Env()
    env.paused = True
    env.turn()
    provider = TotalReclawMemoryProvider(env.state)
    with patch.object(hooks, "_get_hermes_llm_config", return_value=object()), \
         patch.object(env.state, "is_configured", return_value=True), \
         patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=env.fake_llm), \
         patch("totalreclaw.agent.lifecycle.heuristic_fallback_facts", return_value=[]), \
         patch("totalreclaw.embedding.is_model_available_locally", return_value=False), \
         caplog.at_level(logging.WARNING, logger="totalreclaw"):
        provider.on_pre_compress(messages=[])
    assert len(env.llm_calls) == 1
    assert any("auto-extraction ran without a vault view: reads paused and 2 messages waiting"
               in r.getMessage() for r in caplog.records)
