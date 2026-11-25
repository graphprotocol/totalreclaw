"""Tier 2 (#646/#650): the real Python client against a REAL relay process and
a real Postgres, with a stub Pimlico bundler / paymaster / chain node.

Faults the staging bundler cannot be asked to produce on demand (drop, replace
by fee bump, 504 after accept, 429 without a JSON-RPC body, revert, slow
inclusion, mempool AA10, a stuck predecessor) are injected in the stub; the
asserts read the REAL ``subscriptions`` row (and ``write_debits`` on relay PR
#87), not a hand-copied ledger.

Run (see README.md):
    TR_RELAY_DIR=<relay checkout> TR_RELAY_VARIANT=today|pr87|pr87_drops \
    PYTHONPATH=src:. pytest tests/integration_local -q

Phrases: throwaway, generated in memory per account, handed only to the
in-process client, never printed or written (the Account repr hides it).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

RELAY_DIR = os.environ.get("TR_RELAY_DIR")
VARIANT = os.environ.get("TR_RELAY_VARIANT", "today")      # today | pr87 | pr87_drops
pytestmark = pytest.mark.skipif(not RELAY_DIR, reason="set TR_RELAY_DIR (see README.md)")

if RELAY_DIR:
    import totalreclaw.userop as userop
    from totalreclaw import TotalReclaw
    from totalreclaw.client import BatchStoreError
    from tests.integration_local.relay_proc import Relay, psql, reset_db
    from tests.integration_local.stub_chain import StubChain

SWEEP_WAIT_S = 90          # relay PR #87 sweeper: 30 s default; the drop proof needs two sweeps


@pytest.fixture(scope="module")
def env():
    reset_db()
    stub = StubChain()
    extra = {}
    if VARIANT.startswith("pr87"):
        extra["WRITE_REFUND_SWEEP_INTERVAL_MS"] = "2000"
    if VARIANT == "pr87_drops":
        extra["WRITE_REFUND_DROPPED_OPS"] = "true"
    relay = Relay(RELAY_DIR, stub.bundler_url, stub.rpc_url, extra_env=extra)
    yield stub, relay
    relay.close()
    stub.close()


class Acct:
    """One fresh throwaway account. The phrase lives only in the client."""

    def __init__(self, relay_url: str) -> None:
        from mnemonic import Mnemonic

        self.client = TotalReclaw(
            recovery_phrase=Mnemonic("english").generate(128), server_url=relay_url, is_test=True,
        )
        self.sa = ""
        self.ids: list = []

    def __repr__(self) -> str:
        return f"<Acct {self.sa[:10]}>"

    async def prepare(self) -> None:
        await self.client._ensure_address()
        await self.client._ensure_registered()
        self.client._chain_id = 100
        self.client._chain_id_resolved = True
        self.sa = self.client._wallet_address.lower()

    async def write(self, tag: str, **kw) -> str:
        fid = await self.client.remember(f"d23 local {tag} {time.time_ns()}", importance=7, **kw)
        self.ids.append(fid)
        return fid


@pytest.fixture
def stack(env, monkeypatch):
    stub, relay = env
    monkeypatch.setitem(userop._CHAIN_RPCS, 100, stub.rpc_url)
    w = stub.world
    # fresh knobs for every test
    w.mine_delay, w.bundle_delay = 3.0, 0.5
    w.send_faults.clear(); w.sponsor_faults.clear(); w.status_lies.clear()
    w.drop_next = w.revert_next = 0
    w.sim_lag_blocks = 0
    userop._reset_write_state_for_tests()
    userop._reset_sender_locks_for_tests()
    w.executed_payloads.clear()
    w.sponsor_errs.clear(); w.send_errs.clear(); w.status_overrides.clear()
    w.before_send.clear(); w.after_accept.clear()
    stub.requests.clear()
    return stub, relay


async def acct(stack, deployed: bool = True) -> Acct:
    stub, relay = stack
    a = Acct(relay.url)
    await a.prepare()
    if deployed:
        ac = stub.world.acct(a.sa)
        ac.code_history.append((0, True))
        ac.nonce_history.append((0, 5))
    return a


def quota(sa: str) -> int:
    out = psql(f"select coalesce(free_writes_used,0) from subscriptions where wallet_address='{sa}'")
    return int(out) if out else 0


def debits(sa: str) -> str:
    if not VARIANT.startswith("pr87"):
        return "n/a"
    return psql(f"select status, fact_count from write_debits where wallet_address='{sa}' order by created_at")


async def wait_quota(sa: str, expected: int, timeout: float = SWEEP_WAIT_S) -> int:
    end = time.time() + timeout
    while time.time() < end:
        if quota(sa) == expected:
            break
        await asyncio.sleep(2)
    return quota(sa)


def settle(stub, secs=6):
    time.sleep(secs)


# --- L1 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l1_dropped_accepted_op(stack, caplog):
    stub, _ = stack
    caplog.set_level(logging.ERROR, logger="totalreclaw.userop")
    a = await acct(stack)
    stub.world.drop_next = 1
    await a.write("dropped")                       # accepted, then dropped by the bundler
    lost: list = []
    f2 = await a.client.remember(f"d23 local after {time.time_ns()}", importance=7,
                                 on_lost=lambda ids: lost.append(ids))
    await asyncio.sleep(8)
    assert any("dropped" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
    assert len(stub.world.executed_payloads) == 1, "only the second write executed"
    # real quota row: today keeps the dropped op's debit; #87 refunds only reverts; the
    # drop refund needs the nonce to be consumed, which write 2 did (it reused the nonce)
    expected = {"today": 2, "pr87": 2, "pr87_drops": 1}[VARIANT]
    assert await wait_quota(a.sa, expected) == expected, debits(a.sa)


# --- L2 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l2_foreign_op_replaces_ours(stack, caplog):
    stub, _ = stack
    a = await acct(stack)
    await a.write("replaced")
    op = next(o for o in stub.world.ops.values() if o.owner == "ours")
    stub.world.inject_foreign(a.sa, op.nonce, fee_mult=1.5)
    await a.write("next")
    await asyncio.sleep(8)
    mine = [p for p in stub.world.executed_payloads if not p.startswith(b"foreign")]
    assert len(mine) == 1, "ours was replaced; only the next write executed"
    assert len(stub.world.executed_payloads) == 2
    assert all(stub.world.executed_payloads.count(p) == 1 for p in stub.world.executed_payloads)


# --- L3 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l3_504_after_accept_resolved_by_hash(stack):
    stub, _ = stack
    a = await acct(stack)
    stub.world.send_faults = ["http504_accept"]
    await a.write("accepted-then-504")
    await asyncio.sleep(8)
    sends = [r for r in stub.requests if r[0] == "eth_sendUserOperation"]
    assert len(sends) == 1, "no second send: the first one was accepted"
    assert len(stub.world.executed_payloads) == 1
    # the real relay answers the client 5xx-without-body and keeps the debit: right, the op was accepted
    assert await wait_quota(a.sa, 1, 20) == 1


# --- L4 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l4_429_without_body_not_accepted(stack):
    stub, _ = stack
    a = await acct(stack)
    stub.world.send_faults = ["http429"]
    await a.write("after-429")
    await asyncio.sleep(8)
    assert len(stub.world.executed_payloads) == 1, "retried at the same nonce: one execution"
    # Relay Gap A: the relay debited before the forward and does not refund an upstream
    # HTTP error without a JSON-RPC body, so one fact costs 2. Flips when the fix lands.
    q = await wait_quota(a.sa, 1, 12)
    if q != 1:
        pytest.xfail(f"relay gap A (internal follow-up): one fact debited {q} times")


# --- L5 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l5_revert_on_mine(stack):
    stub, _ = stack
    a = await acct(stack)
    stub.world.revert_next = 1
    with pytest.raises(userop.UserOpRevertedError):
        await a.write("reverts", confirm="receipt", profile="interactive")
    expected = {"today": 1, "pr87": 0, "pr87_drops": 0}[VARIANT]
    assert await wait_quota(a.sa, expected) == expected, debits(a.sa)


# --- L6 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l6_slow_inclusion(stack):
    stub, _ = stack
    a = await acct(stack)
    stub.world.mine_delay = 25.0
    t0 = time.monotonic()
    fid = await a.write("slow", confirm="receipt", profile="interactive", deadline_s=8)
    assert fid and time.monotonic() - t0 < 20, "returned at its deadline with the op pending"
    assert len(userop.pending_records(a.sa)) == 1
    stub.world.mine_delay = 3.0
    await a.write("then")                              # the gate waits for the first, no AA25
    await asyncio.sleep(40)
    assert len(stub.world.executed_payloads) == 2
    assert not any("AA25" in m for m in stub.world.sponsor_errs + stub.world.send_errs)


# --- L7 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l7_sponsor_stage_aa25_then_aa20(stack):
    stub, _ = stack
    a = await acct(stack)
    stub.world.sponsor_faults = [
        "UserOperation reverted during simulation with reason: AA25 invalid account nonce",
    ]
    t0 = time.monotonic()
    await a.write("aa25")
    assert time.monotonic() - t0 < 6, "1-2 s back-off, not the old 6 s nonce+1 wait"
    await asyncio.sleep(8)
    assert len(stub.world.executed_payloads) == 1


# --- L8 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l8_mempool_aa10_second_deploy(stack):
    stub, _ = stack
    a = await acct(stack, deployed=False)
    stub.world.inject_foreign(a.sa, 0, factory=True)
    orig = stub.world.simulate

    def sim(op):
        if op.get("factory") and stub.world.chain_nonce(a.sa) == 0:
            return
        return orig(op)

    stub.world.simulate = sim
    stub.world.send_faults = [
        "AA10 sender already constructed: A conflicting userOperation with initCode "
        "for this sender is already in the mempool",
    ]
    try:
        await a.write("after-foreign-deploy")
        await asyncio.sleep(8)
    finally:
        stub.world.simulate = orig
    assert len(stub.world.deploy_events(a.sa)) == 1
    assert stub.world.chain_nonce(a.sa) == 2, "foreign deploy at nonce 0, ours at nonce 1 without a factory"


# --- L9 -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l9_stuck_predecessor(stack, monkeypatch):
    stub, _ = stack
    a = await acct(stack)
    stub.world.mine_delay = 10_000
    monkeypatch.setitem(userop.PROFILES["background"], "gate", 6.0)
    monkeypatch.setattr(userop, "PENDING_MAX_AGE_S", 12.0)
    await a.write("stuck")
    t0 = time.monotonic()
    with pytest.raises(userop.PredecessorPendingError):
        await a.write("blocked")
    assert 5 <= time.monotonic() - t0 < 12, "the first call waits the window"
    t1 = time.monotonic()
    with pytest.raises(userop.PredecessorPendingError):
        await a.write("blocked-again")
    assert time.monotonic() - t1 < 4, "later calls: one check"
    sends = [r for r in stub.requests if r[0] == "eth_sendUserOperation"]
    assert len(sends) == 1, "nothing was sent for the blocked calls"
    await asyncio.sleep(max(0, 14 - (time.monotonic() - t0)))
    stub.world.status_overrides.clear()
    # past max age the stuck op is stale: the account keeps working (the stub still holds it
    # 'submitted' at that nonce, so the new op waits behind it and ends bounded)
    with pytest.raises(Exception):
        await a.write("past-max-age", deadline_s=25)


# --- L10 ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_l10_ambiguous_then_original_mines(stack, tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    from unittest.mock import MagicMock

    from totalreclaw.agent import lifecycle
    from totalreclaw.agent.extraction import ExtractedFact

    stub, _ = stack
    # the in-process predecessor gate would wait up to 90 s for the op: shorten
    # it so the drain meets a still-pending predecessor, as a later session would
    monkeypatch.setitem(userop.PROFILES["background"], "gate", 3.0)
    a = await acct(stack)
    stub.world.mine_delay = 25.0
    text = f"d23 local ambiguous {time.time_ns()}"
    fact = {"text": text, "importance": 7, "fact_type": "claim"}
    with pytest.raises(BatchStoreError) as ei:
        await a.client.remember_batch([fact], deadline_s=6)
    ef = ExtractedFact(text=text, type="claim", importance=7, action="ADD", confidence=0.9)
    lifecycle._queue_unconfirmed_chunk(a.client, [(ef, None, fact)], [None], ei.value)
    a.client.recall = lambda *x, **k: asyncio.sleep(0, result=[])
    a.client.fetch_recent_facts = lambda *x, **k: asyncio.sleep(0, result=[])
    state = MagicMock()
    state.get_client = MagicMock(return_value=a.client)
    loop = asyncio.get_running_loop()
    saved = await loop.run_in_executor(None, lambda: lifecycle.drain_pending_stores(state, a.client))
    assert saved == 0 and a.client.pending_store.count() == 1
    (entry,) = a.client.pending_store._load()
    assert entry["attempts"] == 0
    await asyncio.sleep(30)                           # the original mines
    await loop.run_in_executor(None, lambda: lifecycle.drain_pending_stores(state, a.client))
    assert a.client.pending_store.count() == 0
    sends = [r for r in stub.requests if r[0] == "eth_sendUserOperation"]
    assert len(sends) == 1 and len(stub.world.executed_payloads) == 1
    assert await wait_quota(a.sa, 1, 20) == 1, "one debit"
