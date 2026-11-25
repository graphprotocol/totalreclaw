"""#646 (Task 9): the first built-in memory reconcile writes its entries with a
batched UserOp instead of one write per entry.

Before: a first run with 15 USER.md / MEMORY.md entries made 15 sequential
single writes (15 UserOps, each waiting for the previous one to land). Now the
real ``tools.remember`` / ``tools.remember_many`` run (PII screen, forget guard,
the store-time gate, entity derivation) and the entries that need a write go
out together through ``client.remember_batch``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.client import BatchIds, BatchStoreError
from totalreclaw.hermes import memory_reconcile as mr
from totalreclaw.hermes.state import PluginState

DELIM = "\n§\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("totalreclaw.embedding.get_embedding", lambda t: None)
    return home


def _state():
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = MagicMock()
    client.eoa_address = "0xAbC"
    client.recall = AsyncMock(return_value=[])
    client.find_duplicate_texts = AsyncMock(side_effect=lambda texts: [False] * len(texts))
    state._client = client
    return state, client


def _files(home: Path, user=(), memory=()) -> None:
    (home / "memories" / "USER.md").write_text(DELIM.join(user), encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text(DELIM.join(memory), encoding="utf-8")


def _entries(n: int) -> list[str]:
    return [f"Entry number {i} about topic {i * 7} is true." for i in range(n)]


def _snapshot(tmp_path: Path) -> dict:
    (f,) = list((tmp_path / "state").glob("hermes-memory-snapshot-*.json"))
    return json.loads(f.read_text(encoding="utf-8"))


def test_fifteen_entry_first_run_is_one_batched_write(env, tmp_path):
    state, client = _state()
    texts = _entries(15)
    _files(env, user=texts[:10], memory=texts[10:])
    sent: list[list[dict]] = []

    async def batch(facts, source="x", **kw):
        sent.append(facts)
        return BatchIds([f"id{len(sent)}-{i}" for i in range(len(facts))])

    client.remember_batch = AsyncMock(side_effect=batch)
    client.remember = AsyncMock(side_effect=AssertionError("no single writes on the batched path"))
    summary = mr.reconcile(state, force=True)

    # USER.md and MEMORY.md are separate targets: one batch each, 2 UserOps for 15 entries.
    assert len(sent) == 2 and [len(g) for g in sent] == [10, 5]
    assert summary["added"] == 15 and summary["deferred"] == 0
    client.remember.assert_not_called()
    snap = _snapshot(tmp_path)
    assert sorted(snap["targets"]["user"].values()) == sorted(texts[:10])
    assert sorted(snap["targets"]["memory"].values()) == sorted(texts[10:])
    # the same write the single tool makes: user provenance, confidence 1.0, importance 8
    for fact in sent[0]:
        assert fact["provenance"] == "user" and fact["confidence"] == 1.0 and fact["importance"] == 8


def test_partial_batch_failure_defers_only_the_failed_entries(env, tmp_path):
    state, client = _state()
    texts = _entries(6)
    _files(env, user=texts)

    async def batch(facts, source="x", **kw):
        results = [f"id{i}" if i % 2 == 0 else None for i in range(len(facts))]
        raise BatchStoreError("some groups failed", results, unsent=[1, 3, 5])

    client.remember_batch = AsyncMock(side_effect=batch)
    summary = mr.reconcile(state, force=True)
    assert summary["added"] == 3 and summary["deferred"] == 3
    snap = _snapshot(tmp_path)
    kept = set(snap["targets"].get("user", {}).values())
    assert kept == {texts[0], texts[2], texts[4]}, "the snapshot only moves on for stored entries"


def test_failed_batch_is_retried_whole_next_pass(env, tmp_path):
    state, client = _state()
    texts = _entries(3)
    _files(env, user=texts)
    client.remember_batch = AsyncMock(side_effect=RuntimeError("relay down"))
    first = mr.reconcile(state, force=True)
    assert first["added"] == 0 and first["deferred"] == 3

    async def ok(facts, source="x", **kw):
        return BatchIds([f"n{i}" for i in range(len(facts))])

    client.remember_batch = AsyncMock(side_effect=ok)
    second = mr.reconcile(state, force=True)
    assert second["added"] == 3 and second["deferred"] == 0


def test_maybe_stored_entries_are_deferred_and_recorded_in_the_ledger(env):
    from totalreclaw.write_gate import ledger_for

    state, client = _state()
    texts = _entries(2)
    _files(env, user=texts)

    async def batch(facts, source="x", **kw):
        raise BatchStoreError(
            "unconfirmed", [None, None],
            maybe_ops=[{"hashes": ["0xab"], "nonce": 5, "sent_block": 1,
                        "fact_ids": ["fa", "fb"], "indices": [0, 1]}],
        )

    client.remember_batch = AsyncMock(side_effect=batch)
    summary = mr.reconcile(state, force=True)
    assert summary["added"] == 0 and summary["deferred"] == 2
    ids = {c["id"] for c in ledger_for(client).candidates()}
    assert ids == {"fa", "fb"}, "the next pass's gate sees them, so they are not written twice"


def test_gate_still_skips_a_flagged_or_exact_duplicate_entry(env):
    state, client = _state()
    _files(env, user=["Likes tea.", "Has a dog."])
    client.find_duplicate_texts = AsyncMock(side_effect=lambda texts: [t == "Likes tea." for t in texts])

    async def batch(facts, source="x", **kw):
        assert [f["text"] for f in facts] == ["Has a dog."]
        return BatchIds(["x1"])

    client.remember_batch = AsyncMock(side_effect=batch)
    summary = mr.reconcile(state, force=True)
    assert summary["added"] == 1 and summary["skipped"] == 1


def test_changed_entries_are_still_written_one_at_a_time(env):
    """A changed pair forgets the old facts after the new write, so it keeps the
    single-entry path."""
    state, client = _state()
    _files(env, user=["Lives in Porto."])
    client.remember_batch = AsyncMock(return_value=BatchIds(["a"]))
    mr.reconcile(state, force=True)                        # first run, batched
    _files(env, user=["Lives in Lisbon."])
    client.remember = AsyncMock(return_value="chg1")
    client.remember_batch = AsyncMock(side_effect=AssertionError("a change is not batched"))
    client.recall = AsyncMock(return_value=[])
    mr.reconcile(state, force=True)
    client.remember.assert_awaited()


def test_gate_stopped_and_blocked_entries_are_counted_skipped_not_added(env, tmp_path):
    from totalreclaw.write_gate import ledger_for

    state, client = _state()
    _files(env, user=["Likes tea.", "Has a dog."])
    ledger_for(client).record("old-1", "Likes tea.", None)     # the gate sees an exact copy

    async def batch(facts, source="x", **kw):
        assert [f["text"] for f in facts] == ["Has a dog."]
        return BatchIds(["x1"])

    client.remember_batch = AsyncMock(side_effect=batch)
    summary = mr.reconcile(state, force=True)
    assert summary["added"] == 1 and summary["skipped"] == 1 and summary["deferred"] == 0
    kept = set(_snapshot(tmp_path)["targets"]["user"].values())
    assert kept == {"Likes tea.", "Has a dog."}, "the snapshot still moves on for the skipped entry"
