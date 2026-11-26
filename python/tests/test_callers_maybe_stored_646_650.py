"""Callers of the userop layer (#646/#650, Task 7): "nothing sent" versus
"maybe stored".

The real ``TotalReclaw`` client, the real encrypted pending queue and the real
write ledger run on the fake ERC-4337 world with a virtual clock. The relay,
the subgraph and the embedding model are not touched: ``recall`` returns
nothing (the subgraph has not indexed anything yet) and embeddings are off.

U36  new error texts are never read as a duplicate rejection
U37  drain + Ambiguous + subgraph lag + the original mines later
U38  PredecessorPendingError in the drain: put back, attempts unchanged
U39  settle before the write gate (mirror path)
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import totalreclaw.userop as userop
from totalreclaw.agent import lifecycle
from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.client import BatchStoreError, TotalReclaw, batch_outcome_flags
from totalreclaw.operations import is_duplicate_rejection
from totalreclaw.userop import UserOpAmbiguousError
from totalreclaw.write_gate import ledger_for
from tests.fakes.fake_erc4337 import VirtualClock, World

SA = "0x2c0cf74b2b76110708ca431796367779e3738250"
# The public BIP-39 test vector every test file in this suite already uses.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)


def _deploy(w: World) -> None:
    a = w.acct(SA)
    a.code_history.append((0, True))
    a.nonce_history.append((0, 5))


@pytest.fixture
def stack(monkeypatch, tmp_path):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    w = World()
    VirtualClock(monkeypatch, w)
    _deploy(w)
    client = TotalReclaw(mnemonic=TEST_MNEMONIC, wallet_address=SA)
    client._ensure_address = AsyncMock()
    client._ensure_registered = AsyncMock()
    client._ensure_chain_id = AsyncMock(return_value=100)
    client._chain_id_resolved = True
    client._chain_id = 100
    client._relay._relay_url = "http://relay.test"
    client.recall = AsyncMock(return_value=[])            # subgraph: nothing indexed yet
    client.fetch_recent_facts = AsyncMock(return_value=[])
    monkeypatch.setattr("totalreclaw.embedding.get_embedding", lambda t: None)
    state = MagicMock()
    state.get_client = MagicMock(return_value=client)
    return w, client, state


def _fact(text: str) -> dict:
    return {"text": text, "importance": 7, "fact_type": "claim"}


# ---------------------------------------------------------------------------
# U36
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_u36_new_error_texts_are_never_swallowed_as_duplicates(stack):
    w, client, _ = stack
    texts: list[str] = []
    # PredecessorPendingError, WriteDeadline, Busy, Ambiguous (single + batch)
    w.mine_delay = 100_000
    await client.remember("first", importance=7)
    for kw in ({}, {"deadline_s": 0.0}):
        try:
            await client.remember("second", importance=7, **kw)
        except Exception as e:  # noqa: BLE001
            texts.append(str(e))
    with pytest.raises(BatchStoreError) as ei:
        await client.remember_batch([_fact("third")], deadline_s=2)
    texts.append(str(ei.value))
    texts += [
        str(userop.UserOpAmbiguousError("x", fact_ids=["a"], hashes=["0x" + "40" * 31 + "09"])),
        str(userop.AccountBusyError("Another write for this account is still running; nothing was sent. Try again shortly.")),
        str(userop.WriteDeadlineError("The write ran out of time before anything was sent.")),
    ]
    assert len(texts) >= 4
    for t in texts:
        assert not is_duplicate_rejection(t), t


# ---------------------------------------------------------------------------
# U37 / U38
# ---------------------------------------------------------------------------


def _chunk(text: str):
    fact = ExtractedFact(text=text, type="claim", importance=7, action="ADD", confidence=0.9)
    return [(fact, None, _fact(text))]


@pytest.mark.asyncio
async def test_u37_ambiguous_then_original_mines_later_stores_once(stack, tmp_path):
    w, client, state = stack
    w.mine_delay = 400                      # outlives the call's deadline, mines later
    text = "Pedro keeps a stack of paper notebooks"

    with pytest.raises(BatchStoreError) as ei:
        await client.remember_batch([_fact(text)], deadline_s=8)
    err = ei.value
    assert err.maybe_ops and err.maybe_ops[0]["indices"] == [0] and not err.unsent
    assert batch_outcome_flags(err)[1] == err.maybe_ops
    assert w.send_count == 1

    # Auto-extraction queues the chunk as "maybe stored" and records it in the ledger.
    chunk = _chunk(text)
    handled = lifecycle._queue_unconfirmed_chunk(client, chunk, [None], err)
    assert handled == {0}
    queue = client.pending_store
    (entry,) = queue._load()
    assert entry["reason"] == "maybe_stored" and entry["attempts"] == 0
    assert entry["maybe_op"]["hashes"] == err.maybe_ops[0]["hashes"]
    assert any(c["text"] == text for c in ledger_for(client).candidates()), "the write gate must see it"

    # Drain while the original is still pending (the subgraph has nothing yet).
    # Run through the sync bridge exactly like the hook does.
    def drain():
        return lifecycle.drain_pending_stores(state, client)

    saved = await asyncio.get_running_loop().run_in_executor(None, drain)
    assert saved == 0
    (entry,) = queue._load()
    assert entry["attempts"] == 0, "no attempt counted while the op is pending"
    assert w.send_count == 1, "never re-sent while its fate is open"

    # The original mines; the next drain resolves it by hash and drops the entry.
    w.advance(500)
    saved = await asyncio.get_running_loop().run_in_executor(None, drain)
    assert queue.count() == 0
    assert w.send_count == 1
    assert w.executed_payloads.count(w.executed_payloads[0]) == 1 and len(w.executed_payloads) == 1
    assert w.quota_used == 1, "one debit"
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u37b_dead_op_is_stored_again_once(stack):
    """The maybe-stored op was replaced by another process: the drain must store
    the fact (it was never stored), exactly once."""
    w, client, state = stack
    w.mine_delay = 6
    w.bundle_delay = 1000
    text = "Pedro prefers window seats"
    with pytest.raises(BatchStoreError) as ei:
        await client.remember_batch([_fact(text)], deadline_s=4)
    op = next(iter(w.ops.values()))
    w.inject_foreign(SA, op.nonce, fee_mult=1.5)        # replaces ours
    w.bundle_delay = 1
    w.advance(30)                                       # foreign mines: our nonce is used
    lifecycle._queue_unconfirmed_chunk(client, _chunk(text), [None], ei.value)

    def drain():
        return lifecycle.drain_pending_stores(state, client)

    saved = await asyncio.get_running_loop().run_in_executor(None, drain)
    w.advance(60)
    assert saved == 1
    assert client.pending_store.count() == 0
    stored = [p for p in w.executed_payloads if not p.startswith(b"foreign")]
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_u38_predecessor_pending_in_drain_puts_back_without_attempt(stack):
    w, client, state = stack
    w.mine_delay = 100_000
    await client.remember("a stuck predecessor", importance=7)      # accepted, never mines
    client.pending_store.add([_fact("queued while blocked")], reason="write_quota")

    def drain():
        return lifecycle.drain_pending_stores(state, client)

    for _ in range(3):
        saved = await asyncio.get_running_loop().run_in_executor(None, drain)
        assert saved == 0
    (entry,) = client.pending_store._load()
    assert entry["attempts"] == 0
    assert w.send_count == 1


@pytest.mark.asyncio
async def test_unsent_chunk_is_queued_not_dropped(stack):
    w, client, _ = stack
    w.mine_delay = 100_000
    await client.remember("blocks the account", importance=7)
    with pytest.raises(BatchStoreError) as ei:
        await client.remember_batch([_fact("never sent 1"), _fact("never sent 2")])
    assert ei.value.unsent == [0, 1] and not ei.value.maybe_ops
    chunk = _chunk("never sent 1") + _chunk("never sent 2")
    handled = lifecycle._queue_unconfirmed_chunk(client, chunk, [None, None], ei.value)
    assert handled == {0, 1}
    assert [e["reason"] for e in client.pending_store._load()] == ["not_sent", "not_sent"]
    assert w.send_count == 1


# ---------------------------------------------------------------------------
# U39: settle before the write gate (mirror path)
# ---------------------------------------------------------------------------


async def _remember_tool(client, text: str, **kw) -> dict:
    from totalreclaw.hermes import tools

    state = MagicMock()
    state.get_client = MagicMock(return_value=client)
    state.manual_remember_is_dup_of_pending = MagicMock(return_value=False)
    with patch.object(tools, "_embed_for_remember", return_value=None):
        return json.loads(await tools.remember({"text": text, "force": True}, state, **kw))


@pytest.mark.asyncio
async def test_u39_dead_pending_write_is_stored_again_by_the_mirror(stack):
    from totalreclaw.write_gate import record_write

    w, client, _ = stack
    w.bundle_delay = 1000
    text = "Pedro lives in a flat above a bakery"
    fid = await client.remember(text, importance=7)             # accepted, pending
    record_write(client, fid, text, None)                       # what the tool records
    op = next(iter(w.ops.values()))
    w.inject_foreign(SA, op.nonce, fee_mult=1.5)                # another process takes the nonce
    w.bundle_delay = 1
    w.advance(30)                                               # ... and it mines: ours is dead

    out = await _remember_tool(client, text, _from_mirror=True, gate_mode="exact_only")
    w.advance(60)
    assert out.get("stored") is True, out
    assert [p for p in w.executed_payloads if not p.startswith(b"foreign")] != []
    assert len(userop.pending_records(SA)) <= 1


@pytest.mark.asyncio
async def test_u39b_executed_pending_write_is_skipped_by_the_mirror(stack):
    from totalreclaw.write_gate import record_write

    w, client, _ = stack
    text = "Pedro drinks green tea in the morning"
    fid = await client.remember(text, importance=7)
    record_write(client, fid, text, None)
    w.advance(30)                                               # it mined

    sent_before = w.send_count
    out = await _remember_tool(client, text, _from_mirror=True, gate_mode="exact_only")
    assert out.get("stored") is not True, out
    assert w.send_count == sent_before, "a stored fact is not written twice"


@pytest.mark.asyncio
async def test_remember_tool_reports_unconfirmed_not_failed(stack, monkeypatch):
    """B-mode tool call that ends 'sent, not confirmed': the result says so and
    the write ledger blocks a repeat."""
    w, client, _ = stack
    err = UserOpAmbiguousError("sent", fact_ids=["fid-1"], hashes=["0xab"], nonce=5, sent_block=1)
    client.remember = AsyncMock(side_effect=err)
    out = await _remember_tool(client, "text that may be stored")
    assert out["stored"] is False and out["unconfirmed"] is True and out["fact_id"] == "fid-1"
    assert any(c["id"] == "fid-1" for c in ledger_for(client).candidates())


@pytest.mark.asyncio
async def test_remember_tool_nothing_sent_is_a_plain_error(stack):
    w, client, _ = stack
    w.mine_delay = 100_000
    await client.remember("blocks", importance=7)
    out = await _remember_tool(client, "another text")
    assert "error" in out and "nothing was sent" in out["error"]
    assert w.send_count == 1


# ---------------------------------------------------------------------------
# import / recrystallize: "unconfirmed" is not "failed", and is not retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_counts_ambiguous_as_unconfirmed_and_does_not_retry_it():
    from totalreclaw.operations import group_and_store_adaptive

    calls: list[int] = []

    async def store(group):
        calls.append(len(group))
        raise UserOpAmbiguousError("sent, outcome open", fact_ids=["a", "b"], hashes=["0x1"])

    ids, errors = await group_and_store_adaptive(store, [{"text": "a"}, {"text": "b"}], 15, 10**6)
    assert calls == [2], "no halving and no second send of the same group"
    assert ids == []
    assert len(errors) == 1 and "unconfirmed" in errors[0] and "may be stored" in errors[0]
    assert not is_duplicate_rejection(errors[0])


@pytest.mark.asyncio
async def test_import_counts_nothing_sent_as_not_sent():
    from totalreclaw.operations import group_and_store_adaptive

    async def store(group):
        raise userop.PredecessorPendingError("An earlier write is still being confirmed; nothing was sent.")

    ids, errors = await group_and_store_adaptive(store, [{"text": "a"}], 15, 10**6)
    assert ids == [] and "not sent" in errors[0]


@pytest.mark.asyncio
async def test_settle_resends_a_write_proven_replaced(stack):
    """client.settle_pending_writes() sends a write again (once) when the gate proves
    its nonce was used by another op."""
    w, client, _ = stack
    w.bundle_delay = 1000
    await client.remember("proven replaced then re-sent", importance=7)
    op = next(iter(w.ops.values()))
    w.inject_foreign(SA, op.nonce, fee_mult=1.5)
    w.bundle_delay = 1
    w.advance(30)                       # the foreign op mined: ours is dead
    await client.settle_pending_writes()
    w.advance(60)
    mine = [p for p in w.executed_payloads if not p.startswith(b"foreign")]
    assert len(mine) == 1 and len(w.executed_payloads) == 2
    w.assert_invariants()


@pytest.mark.asyncio
async def test_drain_does_not_restore_on_a_bare_not_found(stack):
    """The bundler's status index says not_found for an op that is alive. The drain
    must not read that as 'gone': the fact would be written a second time."""
    w, client, state = stack
    w.mine_delay = 400
    text = "alive op behind a lagging status index"
    with pytest.raises(BatchStoreError) as ei:
        await client.remember_batch([_fact(text)], deadline_s=8)
    lifecycle._queue_unconfirmed_chunk(client, _chunk(text), [None], ei.value)
    w.status_lies = {"*": 10**6}                       # not_found forever, while the op lives

    def drain():
        return lifecycle.drain_pending_stores(state, client)

    loop = asyncio.get_running_loop()
    for _ in range(3):
        assert await loop.run_in_executor(None, drain) == 0
        w.advance(30)
    assert w.send_count == 1, "never re-sent on a bare not_found"
    (entry,) = client.pending_store._load()
    assert entry["attempts"] == 0 and entry.get("maybe_op")
    w.advance(600)
    w.status_lies.clear()
    await loop.run_in_executor(None, drain)
    assert client.pending_store.count() == 0
    assert len(w.executed_payloads) == 1 and w.send_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(40))
async def test_randomized_drain_schedule_never_writes_a_fact_twice(stack, seed):
    """Seeded mix of slow mining, lagging status indexes and drops, with the real
    queue and drain: no payload executes twice."""
    import random

    rng = random.Random(seed)
    w, client, state = stack
    w.mine_delay = rng.choice([20, 60, 400])
    w.bundle_delay = rng.choice([0.5, 3])
    if rng.random() < 0.5:
        w.status_lies = {"*": rng.choice([2, 6, 10**6])}
    if rng.random() < 0.3:
        w.drop_next = 1
    texts = [f"randomized fact {seed}-{i}" for i in range(rng.randint(1, 3))]
    failed_plain: list[str] = []         # calls that returned a plain error (caller knows)
    for t in texts:
        try:
            await client.remember_batch([_fact(t)], deadline_s=rng.choice([6, 10]))
        except BatchStoreError as e:
            if not lifecycle._queue_unconfirmed_chunk(client, _chunk(t), [None], e):
                failed_plain.append(t)
        except Exception:
            failed_plain.append(t)
    loop = asyncio.get_running_loop()
    for _ in range(5):
        await loop.run_in_executor(None, lambda: lifecycle.drain_pending_stores(state, client))
        w.advance(rng.choice([5, 40, 200]))
    w.status_lies.clear()
    w.advance(1500)
    for _ in range(2):
        await loop.run_in_executor(None, lambda: lifecycle.drain_pending_stores(state, client))
        w.advance(100)
    # a re-store builds a NEW payload (new fact id), so count facts, not bytes:
    # each fact is on-chain at most once
    assert len(w.executed_payloads) <= len(texts), "a fact was written twice"
    # nothing silently missing either: every fact either executed, or is still queued
    # (maybe_stored / not_sent), or its op is still live or tracked in the pending registry
    stored_or_open = len(w.executed_payloads)
    queued = client.pending_store.count()
    live = sum(1 for o in w.ops.values() if o.status in ("outstanding", "submitted"))
    tracked = len(userop.pending_records(SA))
    if stored_or_open + queued + live + tracked < len(texts):
        # the only other legitimate end: the batch call itself returned a plain error
        assert len(failed_plain) >= len(texts) - (stored_or_open + queued + live + tracked), (
            "a fact is neither executed, queued nor tracked"
        )
