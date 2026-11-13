"""Batch fact ids stay with their own facts when a group is skipped (#751).

``remember_batch`` returns the ids of the facts it stored. A group skipped as
a "duplicate" contributes none, so the list is shorter than the input and
callers that paired it with the input by position moved every later id onto
the wrong fact. In auto-extraction that tombstoned the old fact of an UPDATE
whose replacement was never written; in the #747 drain, skipped queued facts
were lost.

On the managed relay the skip only fires by accident: the duplicate check
matched ``409`` anywhere in an error message, e.g. inside a hex hash.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.agent.extraction import ExtractedFact

_SIM_REVERT = "UserOperation reverted during simulation with reason: -32500 too big"
_DUPLICATE = "HTTP 409: duplicate fingerprint"
OLD_ID = "33333333-3333-4333-8333-333333333333"


def _client_shell():
    """A TotalReclaw with keys, relay and chain stubbed, so the real
    ``remember_batch`` runs down to a patched ``store_fact_batch``."""
    from totalreclaw.client import TotalReclaw

    tr = TotalReclaw.__new__(TotalReclaw)
    tr._ensure_address = AsyncMock()
    tr._ensure_registered = AsyncMock()
    tr._ensure_chain_id = AsyncMock(return_value=100)
    tr._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    tr._get_lsh_hasher = MagicMock(return_value=None)
    tr._relay = MagicMock(name="relay")
    tr._data_edge_address = None
    return tr


def _store_first_half_skipped():
    """``store_fact_batch`` stand-in: a group of more than one fact reverts
    for size (so it is halved), a group holding the first fact is rejected
    as a duplicate, anything else is stored with an id named after its text."""
    calls: list[list[str]] = []

    async def _store(facts, *a, **kw):
        texts = [f["text"] for f in facts]
        calls.append(texts)
        if len(facts) > 1:
            raise RuntimeError(_SIM_REVERT)
        if texts[0].startswith("first"):
            raise RuntimeError(_DUPLICATE)
        return [f"id:{t}" for t in texts]

    return _store, calls


# ---------------------------------------------------------------------------
# 1. remember_batch reports one outcome per input fact
# ---------------------------------------------------------------------------


class TestRememberBatchResults:
    @pytest.mark.asyncio
    async def test_a_skipped_group_keeps_later_ids_with_their_facts(self):
        store, calls = _store_first_half_skipped()
        client = _client_shell()
        with patch("totalreclaw.client.store_fact_batch", new=store):
            ids = await client.remember_batch(
                [{"text": "first fact"}, {"text": "second fact"}], source="t"
            )
        assert calls == [["first fact", "second fact"], ["first fact"], ["second fact"]]
        # Unchanged contract: the ids of the stored facts.
        assert list(ids) == ["id:second fact"]
        # New: which input fact each id belongs to.
        assert ids.batch_results == [None, "id:second fact"]

    @pytest.mark.asyncio
    async def test_full_success_lines_up_one_to_one(self):
        async def store(facts, *a, **kw):
            return [f"id:{f['text']}" for f in facts]

        client = _client_shell()
        with patch("totalreclaw.client.store_fact_batch", new=store):
            ids = await client.remember_batch([{"text": "a"}, {"text": "b"}], source="t")
        assert ids == ["id:a", "id:b"]
        assert ids.batch_results == ["id:a", "id:b"]

    @pytest.mark.asyncio
    async def test_all_skipped_gives_no_ids_and_all_none(self):
        async def store(facts, *a, **kw):
            raise RuntimeError(_DUPLICATE)

        client = _client_shell()
        with patch("totalreclaw.client.store_fact_batch", new=store):
            ids = await client.remember_batch([{"text": "a"}, {"text": "b"}], source="t")
        assert ids == []
        assert ids.batch_results == [None, None]


# ---------------------------------------------------------------------------
# 2. A 409 inside a hex string is not a duplicate rejection
# ---------------------------------------------------------------------------


class TestDuplicateClassifier:
    @pytest.mark.parametrize(
        "msg",
        [
            "HTTP 409: duplicate fingerprint",
            "409 fingerprint duplicate",
            "Client error '409 Conflict' for url 'https://relay.invalid/v1/store'",
        ],
    )
    def test_real_duplicate_rejections_match(self, msg):
        from totalreclaw.operations import is_duplicate_rejection

        assert is_duplicate_rejection(msg)

    @pytest.mark.parametrize(
        "msg",
        [
            "AA21 didn't pay prefund sender 0x5409ab00c0ffee0000000000000000000000beef",
            "Bundler error: userOpHash 0xdead4091beef failed",
            "paymaster deposit too low: 14090 wei",
        ],
    )
    def test_409_inside_other_text_does_not_match(self, msg):
        from totalreclaw.operations import is_duplicate_rejection

        assert not is_duplicate_rejection(msg)

    @pytest.mark.asyncio
    async def test_hex_409_error_is_surfaced_not_swallowed(self):
        """Before #751 this error was taken for a duplicate: no ids, no error,
        and the facts silently dropped."""

        async def store(facts, *a, **kw):
            raise RuntimeError("AA21 didn't pay prefund sender 0x5409ab00c0ffee")

        client = _client_shell()
        with patch("totalreclaw.client.store_fact_batch", new=store):
            with pytest.raises(RuntimeError, match="AA21"):
                await client.remember_batch([{"text": "a"}], source="t")


# ---------------------------------------------------------------------------
# 3. Auto-extraction: an UPDATE whose replacement was skipped tombstones nothing
# ---------------------------------------------------------------------------


def _plugin_state(client):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client
    return state


def _run_extract(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*a, **kw):
        return facts

    async def passthrough(fs, *a, **kw):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        state.add_message("user", "My home city changed")
        state.add_message("assistant", "Noted")
        return auto_extract(state)


class TestAutoExtractAlignment:
    def test_skipped_update_does_not_tombstone_and_ids_stay_put(self):
        shell = _client_shell()
        client = MagicMock()
        client.recall = AsyncMock(return_value=[])
        client.forget = AsyncMock(return_value=True)
        client.get_fact_pin_status = AsyncMock(return_value=False)
        client.remember_batch = shell.remember_batch  # the real method
        state = _plugin_state(client)
        facts = [
            ExtractedFact(text="first: home city is Porto", type="claim", importance=8,
                          action="UPDATE", existing_fact_id=OLD_ID, source="user"),
            ExtractedFact(text="second: likes espresso", type="preference", importance=7,
                          action="ADD", source="user"),
        ]
        store, _ = _store_first_half_skipped()
        with patch("totalreclaw.client.store_fact_batch", new=store), \
             patch("totalreclaw.agent.lifecycle.record_write") as rec:
            stored = _run_extract(state, facts)

        # The UPDATE's replacement was not written: its old fact must stay.
        client.forget.assert_not_awaited()
        assert stored == ["second: likes espresso"]
        assert [(c.args[1], c.args[2]) for c in rec.call_args_list] == [
            ("id:second: likes espresso", "second: likes espresso")
        ]


# ---------------------------------------------------------------------------
# 4. The #747 drain: a skipped queued fact goes back to the queue
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    return tmp_path


class _DrainClient:
    resolved_chain_id = 100
    eoa_address = "0x000000000000000000000000000000000000cafe"

    def __init__(self):
        from totalreclaw.agent.pending_store import PendingStoreQueue

        self._queue = PendingStoreQueue.for_owner(self.eoa_address, secrets.token_bytes(32))
        shell = _client_shell()
        self.remember_batch = shell.remember_batch  # the real method

    @property
    def pending_store(self):
        return self._queue

    async def fetch_recent_facts(self, limit=10):
        return []

    async def recall(self, *a, **kw):
        return []


class TestDrainAlignment:
    def _drain(self, client, store):
        from totalreclaw.agent.lifecycle import drain_pending_stores

        state = MagicMock()
        with patch("totalreclaw.client.store_fact_batch", new=store), \
             patch("totalreclaw.embedding.get_embedding", return_value=None), \
             patch("totalreclaw.agent.lifecycle.record_write") as rec:
            saved = drain_pending_stores(state, client)
        return saved, rec

    def test_skipped_fact_is_kept_and_ids_stay_with_their_facts(self, state_dir):
        client = _DrainClient()
        client.pending_store.add(
            [{"text": "first queued fact about trains"}, {"text": "second queued fact about boats"}],
            reason="write_quota",
        )
        store, _ = _store_first_half_skipped()
        saved, rec = self._drain(client, store)

        assert saved == 1
        assert [(c.args[1], c.args[2]) for c in rec.call_args_list] == [
            ("id:second queued fact about boats", "second queued fact about boats")
        ]
        left = client.pending_store.take(10)
        assert [e["fact"]["text"] for e in left] == ["first queued fact about trains"]
        assert left[0]["attempts"] == 1

    def test_skipped_fact_is_dropped_after_the_attempt_limit(self, state_dir):
        from totalreclaw.agent.lifecycle import _DRAIN_MAX_ATTEMPTS

        client = _DrainClient()
        client.pending_store.add([{"text": "first queued fact about trains"}], reason="write_quota")
        entries = client.pending_store.take(10)
        entries[0]["attempts"] = _DRAIN_MAX_ATTEMPTS - 1
        client.pending_store.put_back(entries)

        async def store(facts, *a, **kw):
            raise RuntimeError(_DUPLICATE)

        saved, _ = self._drain(client, store)
        assert saved == 0
        assert client.pending_store.count() == 0
