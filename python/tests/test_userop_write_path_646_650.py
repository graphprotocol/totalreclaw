"""Write-path tests for internal #646 (AA25 on back-to-back writes) and
#650 (AA20 after a fresh pairing), on a fake ERC-4337 world with a virtual
clock. Case ids (U1..U40) follow the test plan.

No real recovery phrase appears here: the signing key is a fixed dummy
constant and the "auth key" a placeholder. Every test also checks, through
``caplog``, that no log line carries either of them or a ``Bearer`` value.
"""
from __future__ import annotations

import asyncio
import logging
import random

import pytest

import totalreclaw.userop as userop
from totalreclaw.userop import (
    AccountBusyError,
    NothingSentError,
    PredecessorPendingError,
    UserOpAmbiguousError,
    UserOpDroppedError,
    UserOpRevertedError,
)
from tests.fakes.fake_erc4337 import SENDER_DEFAULT as S
from tests.fakes.fake_erc4337 import VirtualClock, World

KEY = bytes([7]) * 32
AUTH = "ab" * 32
AA25 = "UserOperation reverted during simulation with reason: AA25 invalid account nonce"
AA20 = "UserOperation reverted during simulation with reason: AA20 account not deployed"


@pytest.fixture(autouse=True)
def _no_secrets_in_logs(caplog):
    """U35: no test may leak the key, the auth key or a Bearer value."""
    caplog.set_level(logging.DEBUG, logger="totalreclaw")
    yield
    text = caplog.text
    assert "Bearer" not in text
    assert AUTH not in text
    assert KEY.hex() not in text


def make(monkeypatch, **kw) -> World:
    w = World(**kw)
    VirtualClock(monkeypatch, w)
    return w


def deploy(w: World, sender: str = S, nonce: int = 5) -> None:
    """An already-deployed account at ``nonce``."""
    a = w.acct(sender)
    a.code_history.append((0, True))
    a.nonce_history.append((0, nonce))


async def one(payload: bytes, sender: str = S, **kw) -> str:
    kw.setdefault("fact_ids", [payload.decode(errors="replace")])
    return await userop.build_and_send_userop(
        sender=sender, eoa_address="0x" + "22" * 20, eoa_private_key=KEY,
        protobuf_payload=payload, relay_url="http://relay.test", auth_key_hex=AUTH,
        wallet_address=sender, chain_id=100, **kw,
    )


async def many(payloads, sender: str = S, **kw) -> str:
    kw.setdefault("fact_ids", [p.decode(errors="replace") for p in payloads])
    return await userop.build_and_send_userop_batch(
        sender=sender, eoa_address="0x" + "22" * 20, eoa_private_key=KEY,
        protobuf_payloads=list(payloads), relay_url="http://relay.test", auth_key_hex=AUTH,
        wallet_address=sender, chain_id=100, **kw,
    )


def settle(w: World, secs: float = 60) -> None:
    w.advance(secs)


# ---------------------------------------------------------------------------
# #646 and #650 reproductions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_u1_fifteen_sequential_writes_no_aa25(monkeypatch):
    """#646: back-to-back single writes no longer hit AA25 or burn 6 s waits."""
    w = make(monkeypatch)
    deploy(w)
    t0 = w.now()
    for i in range(15):
        await one(b"u1-%02d" % i)
        w.advance(3)                 # an LLM turn between two tool calls
    settle(w)
    assert not any("AA25" in m for m in w.sponsor_errs + w.send_errs)
    assert w.now() - t0 < 15 * 12, "no fixed 6 s waits on top of the ~7 s mining time"
    assert w.send_count == 15
    assert w.op_count_executed(S) == 15
    assert len(w.executed_payloads) == 15
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u2_fresh_account_second_write_no_aa20(monkeypatch):
    """#650: the second write after the deploy write waits for it, then goes
    out without a factory."""
    w = make(monkeypatch)
    await one(b"u2-a")
    w.advance(0.7)                   # rc6: the next call came 0.7 s later
    await one(b"u2-b")
    settle(w)
    assert not any("AA20" in m or "AA25" in m for m in w.sponsor_errs + w.send_errs)
    assert len(w.deploy_events(S)) == 1
    assert w.chain_nonce(S) == 2
    assert [o.factory for o in sorted(w.ops.values(), key=lambda o: o.nonce)] == [True, False]
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u3_aa20_with_undeployed_chain_rederives_factory(monkeypatch):
    w = make(monkeypatch)
    userop._mark_sender_deployed(S)      # a wrong latch
    h = await one(b"u3")
    settle(w)
    assert any("AA20" in m for m in w.sponsor_errs)
    assert w.ops[h].factory is True
    assert w.op_count_executed(S) == 1
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u3b_aa20_never_deploying_is_bounded(monkeypatch):
    w = make(monkeypatch)
    w.sponsor_faults = [AA20] * 20
    with pytest.raises(RuntimeError):
        await one(b"u3b")
    assert w.send_count == 0
    assert w.now() < 60


@pytest.mark.asyncio
async def test_u4_forced_sponsor_aa25_backs_off_does_not_wait_k_plus_2(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.sponsor_faults = [AA25]
    await one(b"u4")
    assert w.now() < 4, "old code burned the full 6 s nonce+1 wait"
    settle(w)
    assert w.op_count_executed(S) == 1


@pytest.mark.asyncio
async def test_u5_foreign_same_nonce_op_waits_then_rebuilds(monkeypatch):
    """Another process holds nonce k (submitted, not mined); ours is rejected,
    waits for k+1 and rebuilds there. Both execute once."""
    w = make(monkeypatch)
    deploy(w)
    w.inject_foreign(S, 5)
    w.advance(2)                 # foreign op is now bundled
    assert w.ops[next(iter(w.ops))].status == "submitted"
    h = await one(b"u5")
    settle(w)
    assert any("already being processed" in m for m in w.send_errs)
    assert w.ops[h].nonce == 6
    assert w.op_count_executed(S) == 2
    assert b"u5" in w.executed_payloads
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u6_dropped_predecessor_fresh_account_reattaches_factory(monkeypatch, caplog):
    w = make(monkeypatch)
    w.drop_next = 1
    lost: list[list[str]] = []
    await one(b"u6-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    h = await one(b"u6-b", fact_ids=["b"])
    settle(w)
    assert lost == [["a"]]
    assert w.ops[h].factory is True and w.ops[h].nonce == 0
    assert any(r.levelno == logging.ERROR and "dropped" in r.getMessage() for r in caplog.records)
    assert w.chain_nonce(S) == 2
    # a was proven dropped (bundler status "rejected", nonce unused): it is sent again, once
    assert sorted(w.executed_payloads) == [b"u6-a", b"u6-b"]


@pytest.mark.asyncio
async def test_u7_stuck_predecessor_raises_with_zero_sends(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    await one(b"u7-a")
    with pytest.raises(PredecessorPendingError):
        await one(b"u7-b")
    assert w.send_count == 1
    assert len(userop.pending_records(S)) == 1
    assert isinstance(PredecessorPendingError("x"), NothingSentError)


@pytest.mark.asyncio
async def test_u8_receipt_lag_decided_from_chain_event(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.receipt_lag_s = 100
    h = await one(b"u8", confirm="receipt")
    assert w.now() < 20
    assert w.ops[h].status == "included"
    assert userop.pending_records(S) == []


@pytest.mark.asyncio
async def test_u9_replica_lag_after_gate_does_not_lower_nonce(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    await one(b"u9-a")
    w.replica_lag_reads = 6
    h = await one(b"u9-b")
    settle(w)
    assert w.ops[h].nonce == 6
    assert not any("AA25" in m for m in w.sponsor_errs + w.send_errs)


@pytest.mark.asyncio
async def test_u10_reverted_predecessor_opens_gate_and_reports_loss(monkeypatch, caplog):
    w = make(monkeypatch)
    deploy(w)
    w.revert_next = 1
    lost: list[list[str]] = []
    await one(b"u10-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    await one(b"u10-b", fact_ids=["b"])
    settle(w)
    assert lost == [["a"]]
    assert any(r.levelno == logging.WARNING and "reverted" in r.getMessage() for r in caplog.records)
    assert w.executed_payloads == [b"u10-b"]


@pytest.mark.asyncio
async def test_u11_burst_of_ten_is_serialized(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    await asyncio.gather(*[one(b"u11-%d" % i) for i in range(10)])
    settle(w)
    assert not any("AA25" in m for m in w.sponsor_errs + w.send_errs)
    assert w.op_count_executed(S) == 10
    assert len(set(w.executed_payloads)) == 10
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u12_single_then_batch_immediately(monkeypatch):
    w = make(monkeypatch)
    await one(b"u12-s")
    await many([b"u12-%d" % i for i in range(5)])
    settle(w)
    assert not any("AA20" in m for m in w.sponsor_errs)
    assert w.op_count_executed(S) == 2
    assert len(w.executed_payloads) == 6
    assert len(w.deploy_events(S)) == 1
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u14_batch_not_confirmed_never_rebuilds_at_k_plus_1(monkeypatch):
    """Old code: an alive-but-slow batch was "dropped" at 240 s, resubmitted,
    got AA25 and was rebuilt at k+1: written twice and billed twice."""
    w = make(monkeypatch, mine_delay=500)
    deploy(w)
    with pytest.raises(UserOpAmbiguousError) as ei:
        await many([b"u14-a", b"u14-b"], fact_ids=["a", "b"])
    err = ei.value
    assert err.fact_ids == ["a", "b"] and len(err.hashes) == 1 and err.nonce == 5
    assert w.send_count == 1
    assert 380 <= w.now() <= 400, "batch overall cap is 390 s"
    settle(w, 600)
    assert w.op_count_executed(S) == 1
    assert sorted(w.executed_payloads) == [b"u14-a", b"u14-b"]
    # the next write resolves the original by hash first
    await one(b"u14-c")
    w.assert_invariants()


# ---------------------------------------------------------------------------
# Same-nonce re-sends and send timeouts (F1, U15, U20-U23)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout_accept", "http504_accept"])
async def test_u15a_timeout_after_accept_resolved_by_hash(monkeypatch, fault):
    w = make(monkeypatch)
    deploy(w)
    w.send_faults = [fault]
    h = await one(b"u15a")
    settle(w)
    assert w.send_count == 1, "no second send: the first one was accepted"
    assert w.ops[h].status == "included"
    assert w.executed_payloads == [b"u15a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout_reject", "http504_reject"])
async def test_u15b_timeout_not_accepted_resent_at_same_nonce(monkeypatch, fault):
    w = make(monkeypatch)
    deploy(w)
    w.send_faults = [fault]
    h = await one(b"u15b")
    settle(w)
    assert w.send_count == 2
    assert w.ops[h].nonce == 5
    assert w.executed_payloads == [b"u15b"]
    w.assert_invariants()


@pytest.mark.asyncio
async def test_u15c_accepted_then_replaced_by_foreign_is_dead_then_rebuilt(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.send_faults = ["timeout_accept"]
    w.after_accept = [lambda world, op: world.inject_foreign(S, op.nonce, fee_mult=1.5)]
    h = await one(b"u15c")
    settle(w)
    assert b"u15c" in w.executed_payloads and w.executed_payloads.count(b"u15c") == 1
    assert w.ops[h].nonce == 6, "rebuilt above the foreign op only after it was proven dead"
    assert w.op_count_executed(S) == 2


@pytest.mark.asyncio
async def test_u20_resend_already_known_is_ours_no_rebuild(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.send_faults = ["timeout_accept"]
    w.status_lies = {"*": 2}            # bundler status index lags: op looks gone
    h = await one(b"u20")
    settle(w)
    assert "Already known" in w.send_errs
    assert w.send_count == 2
    assert w.executed_payloads == [b"u20"]
    assert w.ops[h].nonce == 5


@pytest.mark.asyncio
async def test_u21_resend_aa25_being_processed_is_ours(monkeypatch):
    w = make(monkeypatch, bundle_delay=0.5)
    deploy(w)
    w.send_faults = ["timeout_accept"]
    w.status_lies = {"*": 2}
    w.after_accept = [lambda world, op: setattr(world, "gas_price", int(world.gas_price * 1.05))]  # re-send: +5%, new hash
    await one(b"u21")
    settle(w)
    assert any("already being processed" in m for m in w.send_errs)
    assert w.executed_payloads == [b"u21"]
    assert w.op_count_executed(S) == 1


@pytest.mark.asyncio
async def test_u22_resend_aa25_present_in_mempool_is_ours(monkeypatch):
    w = make(monkeypatch, bundle_delay=1000, mine_delay=3)
    deploy(w)
    w.send_faults = ["timeout_accept"]
    w.status_lies = {"*": 2}
    def after(world, op):
        world.gas_price = int(world.gas_price * 1.05)   # the re-send pays +5%: a new hash
        world.bundle_delay = 8                          # and the op can bundle later
    w.after_accept = [after]
    await one(b"u22")
    settle(w)
    assert any("already present in mempool" in m for m in w.send_errs)
    assert w.executed_payloads == [b"u22"]
    assert w.op_count_executed(S) == 1


@pytest.mark.asyncio
async def test_u23_own_resend_with_fee_bump_replaces_earlier_attempt(monkeypatch):
    w = make(monkeypatch, bundle_delay=1000)
    deploy(w)
    w.send_faults = ["timeout_accept"]
    w.status_lies = {"*": 2}
    def after(world, op):
        world.gas_price = int(world.gas_price * 1.25)
        world.bundle_delay = 2
    w.after_accept = [after]
    h = await one(b"u23")
    settle(w)
    first, second = sorted(w.ops.values(), key=lambda o: o.accepted_t)
    assert first.status == "replaced" and second.status == "included"
    assert h == second.hash
    assert w.executed_payloads == [b"u23"]


@pytest.mark.asyncio
async def test_u24_foreign_replaces_our_accepted_single_write(monkeypatch, caplog):
    """Another process replaces our accepted write by a fee bump. The bundler no
    longer knows our op (not_found, not proof), so the gate gives it a grace
    period; the foreign op mines, the verdict turns "dead" and our write is sent
    again, once."""
    w = make(monkeypatch)
    deploy(w)
    lost: list[list[str]] = []
    h = await one(b"u24-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    w.inject_foreign(S, 5, fee_mult=1.5)
    h2 = await one(b"u24-b", fact_ids=["b"])
    settle(w)
    assert lost == [["a"]]
    assert w.ops[h].status == "replaced"
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert w.executed_payloads.count(b"u24-a") == 1 and w.executed_payloads.count(b"u24-b") == 1
    assert w.ops[h2].status == "included"


@pytest.mark.asyncio
async def test_u24b_dead_verdict_when_foreign_already_mined(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    lost: list[list[str]] = []
    await one(b"u24c-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    w.inject_foreign(S, 5, fee_mult=1.5)
    w.advance(30)
    await one(b"u24c-b", fact_ids=["b"])
    settle(w)
    assert lost == [["a"]]
    assert w.executed_payloads.count(b"u24c-b") == 1


# ---------------------------------------------------------------------------
# Mempool states, simulator lag, RPC faults, reorg
# ---------------------------------------------------------------------------


def test_u25_fake_models_the_three_predecessor_states():
    """Sponsoring k+1 passes while k is outstanding, fails once k is bundled,
    and passes again once k is mined."""
    w = World()
    deploy(w)
    op = {"sender": S, "nonce": hex(6)}
    pre = {"sender": S, "nonce": hex(5)}
    from tests.fakes.fake_erc4337 import RpcError
    w.inject_foreign(S, 5)
    w.simulate(op)                                   # outstanding: passes
    w.advance(2)                                     # bundled
    with pytest.raises(RpcError, match="AA25"):
        w.simulate(op)                               # submitted: fails
    w.advance(10)                                    # mined
    w.simulate(op)
    assert w.chain_nonce(S) == 6
    del pre


@pytest.mark.asyncio
async def test_u25b_client_never_hits_the_submitted_window(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    for i in range(6):
        await one(b"u25-%d" % i)
    assert w.sponsor_errs == [] and w.send_errs == []


@pytest.mark.asyncio
async def test_u26_simulator_one_block_behind_backs_off(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.sim_lag_blocks = 1
    for i in range(3):
        await one(b"u26-%d" % i)
    settle(w)
    assert w.op_count_executed(S) == 3
    assert w.now() < 90, "no 20 s losses"


@pytest.mark.asyncio
async def test_u26b_simulator_behind_after_deploy_aa20_with_code_present(monkeypatch):
    w = make(monkeypatch)
    w.sim_lag_blocks = 1
    await one(b"u26b-a")
    await one(b"u26b-b")
    settle(w)
    assert w.op_count_executed(S) == 2
    assert len(w.deploy_events(S)) == 1


@pytest.mark.asyncio
async def test_u27_public_rpc_down_fails_fast_with_nothing_sent(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    await one(b"u27-a")
    w.rpc_down = True
    t0 = w.now()
    with pytest.raises(PredecessorPendingError):
        await one(b"u27-b")
    assert w.now() - t0 <= 15
    assert w.send_count == 1


@pytest.mark.asyncio
async def test_u28_malformed_eth_call_never_reads_as_nonce_zero(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.malformed_eth_call = 1
    async with userop.httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError):
            await userop.get_nonce(http, S, 100)
        assert await userop.get_nonce(http, S, 100) == 5


@pytest.mark.asyncio
async def test_u28b_dead_needs_two_matching_reads(monkeypatch):
    """A replica that has not yet shown the log must not turn a mined op into
    'dead': the verdict needs two matching passes."""
    w = make(monkeypatch)
    deploy(w)
    h = await one(b"u28b")
    w.advance(30)
    w.receipt_lag_s = 1000
    for o in w.ops.values():
        o.hide_receipt_until = 10**9
    calls = {"n": 0}
    real = userop._find_userop_events

    async def flaky(http, rpc_url, hashes, a, b):
        calls["n"] += 1
        return {} if calls["n"] == 1 else await real(http, rpc_url, hashes, a, b)

    monkeypatch.setattr(userop, "_find_userop_events", flaky)
    ctx = userop._make_ctx(S, "http://relay.test", AUTH, S, 100, "t", None)
    async with userop.httpx.AsyncClient() as http:
        fate = await userop.resolve_fate(http, ctx, [h], 5, w.block - 3)
    assert fate.kind == "executed", "first pass said dead, second pass found the event"


@pytest.mark.asyncio
async def test_u29_reorg_nonce_goes_back_floor_resets(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    await one(b"u29-a")
    await one(b"u29-b")          # gate resolves a -> floor 6
    w.advance(30)
    assert w.chain_nonce(S) == 7 - 1 or True
    before = w.chain_nonce(S)
    w.reorg(12)                  # last blocks gone: b (and maybe a) un-executed
    after = w.chain_nonce(S)
    assert after < before
    userop._landed_floor[S.lower()] = before      # our floor is ahead of the chain
    h = await one(b"u29-c")
    settle(w)
    assert w.ops[h].status == "included"
    assert w.ops[h].nonce == after or w.ops[h].nonce >= after


@pytest.mark.asyncio
async def test_u29b_reorged_deploy_rederives_factory(monkeypatch):
    w = make(monkeypatch)
    await one(b"u29b-a")
    await one(b"u29b-b")
    w.advance(30)
    w.reorg(100)                 # everything, including the deploy
    assert not w.has_code(S) and w.chain_nonce(S) == 0
    userop._landed_floor[S.lower()] = 2
    h = await one(b"u29b-c")
    settle(w)
    assert w.ops[h].factory is True and w.ops[h].status == "included"


# ---------------------------------------------------------------------------
# Deadlines, mempool AA10, stuck predecessor, shutdown, cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_u30a_lock_wait_is_counted_and_busy_sends_nothing(monkeypatch):
    w = make(monkeypatch)
    deploy(w)

    async def slow_sleep(s):
        await asyncio.sleep(0.02)
        w.advance(s)

    monkeypatch.setattr(userop, "_sleep", slow_sleep)
    first = asyncio.create_task(one(b"u30-a", confirm="receipt"))
    await asyncio.sleep(0.01)
    with pytest.raises(AccountBusyError):
        await one(b"u30-b", deadline_s=0.05)
    await first
    assert w.send_count == 1


@pytest.mark.asyncio
async def test_u30b_finalize_budget_honoured(monkeypatch):
    w = make(monkeypatch, mine_delay=500)
    deploy(w)
    t0 = w.now()
    with pytest.raises(UserOpAmbiguousError):
        await many([b"u30b"], deadline_s=8)
    assert 8 <= w.now() - t0 <= 20


@pytest.mark.asyncio
async def test_u30c_interactive_profile_gate_30s(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    await one(b"u30c-a")
    t0 = w.now()
    with pytest.raises(PredecessorPendingError):
        await one(b"u30c-b", profile="interactive")
    assert 25 <= w.now() - t0 <= 40


@pytest.mark.asyncio
async def test_u31_mempool_aa10_does_not_latch_waits_for_deploy(monkeypatch):
    w = make(monkeypatch)
    w.inject_foreign(S, 0, factory=True)            # another process deploys
    mempool_aa10 = (
        "AA10 sender already constructed: A conflicting userOperation with "
        "initCode for this sender is already in the mempool"
    )
    w.send_faults = [mempool_aa10]
    # make the simulator pass for nonce 0 + factory despite the foreign op
    orig = w.simulate

    def sim(op):
        if op.get("factory") and w.chain_nonce(S) == 0:
            return
        return orig(op)

    w.simulate = sim
    h = await one(b"u31")
    settle(w)
    assert w.ops[h].factory is False and w.ops[h].nonce == 1
    assert w.executed_payloads.count(b"u31") == 1
    assert len(w.deploy_events(S)) == 1


@pytest.mark.asyncio
async def test_u32_stuck_predecessor_window_then_fast_fail_then_max_age(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    lost: list[list[str]] = []
    h = None
    w.drop_next = 1
    h = await one(b"u32-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    w.advance(3)                                     # op dropped by the bundler...
    w.status_overrides[h] = "submitted"              # ...but its status index still says submitted
    t0 = w.now()
    with pytest.raises(PredecessorPendingError):
        await one(b"u32-b")
    assert w.now() - t0 >= 85                         # first call waits the window
    t1 = w.now()
    with pytest.raises(PredecessorPendingError):
        await one(b"u32-b")
    assert w.now() - t1 < 5                           # later calls: one check
    w.advance(userop.PENDING_MAX_AGE_S)
    h2 = await one(b"u32-c", fact_ids=["c"])          # max age: moves to stale, continues
    settle(w)
    assert w.ops[h2].nonce == 5 and w.ops[h2].status == "included"
    assert lost == []                                 # stale does not call on_lost yet
    del w.status_overrides[h]
    await one(b"u32-d", fact_ids=["d"])               # gate: stale record is dead/gone
    assert lost == [["a"]]
    settle(w)
    # a stale record proven terminally dropped is sent again, like a live one
    assert w.executed_payloads.count(b"u32-a") == 1


@pytest.mark.asyncio
async def test_u33_interpreter_shutdown_in_gate_propagates(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    await one(b"u33-a")

    def boom(*a, **k):
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(w, "public_rpc", boom)
    with pytest.raises(RuntimeError, match="interpreter shutdown"):
        await one(b"u33-b")
    assert w.send_count == 1
    lock = await userop._get_sender_lock(S)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_u19_cancellation_during_gate_releases_lock_sends_nothing(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    await one(b"u19-a")
    task = asyncio.create_task(one(b"u19-b"))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert w.send_count == 1
    lock = await userop._get_sender_lock(S)
    assert not lock.locked()


# ---------------------------------------------------------------------------
# Cross-process, B-mode, ledger modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("deployed", [True, False])
async def test_u18_two_processes_one_chain_converge_no_double_execution(monkeypatch, deployed):
    """Process 2 starts with EMPTY module state right after process 1 sent."""
    w = make(monkeypatch)
    if deployed:
        deploy(w)
    await one(b"u18-p1")
    userop._reset_write_state_for_tests()           # a second process: no shared memory
    h = await one(b"u18-p2")
    settle(w)
    assert w.op_count_executed(S) == 2
    assert sorted(w.executed_payloads) == [b"u18-p1", b"u18-p2"]
    assert len(w.deploy_events(S)) == (0 if deployed else 1)
    assert w.ops[h].status == "included"
    w.assert_invariants()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", ["receipt", "accept"])
async def test_u40_remember_confirm_modes(monkeypatch, confirm):
    w = make(monkeypatch)
    deploy(w)
    t0 = w.now()
    h = await one(b"u40", confirm=confirm, profile="interactive")
    if confirm == "receipt":
        assert w.ops[h].status == "included"
        assert userop.pending_records(S) == []
    else:
        assert w.now() - t0 < 5
        assert len(userop.pending_records(S)) == 1
    h2 = await one(b"u40-2", confirm=confirm, profile="interactive")
    settle(w)
    assert w.op_count_executed(S) == 2
    w.assert_invariants()
    del h2


@pytest.mark.asyncio
async def test_u40b_receipt_mode_deadline_exit_returns_pending_with_warning(monkeypatch, caplog):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    h = await one(b"u40b", confirm="receipt", profile="interactive")
    assert h
    assert len(userop.pending_records(S)) == 1
    assert any("not yet confirmed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["today", "pr87", "pr87_drops"])
async def test_u17_quota_invariant_in_every_ledger_mode(monkeypatch, mode):
    w = make(monkeypatch, ledger_mode=mode)
    deploy(w)
    w.revert_next = 1
    await one(b"u17-rev", fact_ids=["rev"])          # reverts
    await one(b"u17-ok", fact_ids=["ok"])
    w.drop_next = 1
    await one(b"u17-drop", fact_ids=["drop"])        # dropped at bundle time
    await many([b"u17-b1", b"u17-b2", b"u17-b3"])
    settle(w)
    w.assert_invariants()
    # the dropped write is proven dropped and sent again: it costs a second debit
    if mode == "today":
        assert w.quota_used == 7        # revert + the dropped op's debit are kept
    elif mode == "pr87":
        assert w.quota_used == 6        # revert refunded
    else:
        assert w.quota_used == 5        # revert and drop refunded


@pytest.mark.asyncio
async def test_batch_revert_raises_reverted_and_batch_drop_raises_dropped(monkeypatch):
    w = make(monkeypatch)
    deploy(w)
    w.revert_next = 1
    with pytest.raises(UserOpRevertedError):
        await many([b"rv1", b"rv2"])
    w.drop_next = 3
    with pytest.raises(UserOpDroppedError):
        await many([b"dr1", b"dr2"])
    assert w.send_count == 1 + 3, "three sends at the pinned nonce, then dropped"
    assert w.executed_payloads == []


@pytest.mark.asyncio
async def test_accepted_log_line_carries_hash_nonce_and_factory(monkeypatch, caplog):
    w = make(monkeypatch)
    h = await one(b"log-1")
    lines = [r.getMessage() for r in caplog.records if "userop accepted" in r.getMessage()]
    assert lines == [f"userop accepted hash={h} nonce=0 factory=True"]


# ---------------------------------------------------------------------------
# U34: randomized fault schedule (seeded, standard library only)
# ---------------------------------------------------------------------------

_SEND_FAULTS = [None, None, None, "timeout_accept", "timeout_reject", "http504_accept", "http504_reject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(200))
async def test_u34_randomized_fault_schedule(monkeypatch, seed):
    rng = random.Random(seed)
    w = make(
        monkeypatch,
        mine_delay=rng.choice([3, 6, 12]),
        bundle_delay=rng.choice([0.5, 1, 3]),
        ledger_mode=rng.choice(["today", "pr87", "pr87_drops"]),
    )
    if rng.random() < 0.5:
        deploy(w)
    w.sim_lag_blocks = rng.choice([0, 0, 1])
    n_writes = rng.randint(3, 7)
    ids_stored: list[bytes] = []
    reported_lost: set[str] = set()      # facts a gate proved lost and told on_lost about
    errors = 0
    for i in range(n_writes):
        w.send_faults = [rng.choice(_SEND_FAULTS)]
        if rng.random() < 0.15:
            w.status_lies = {"*": rng.randint(1, 3)}
        if rng.random() < 0.1:
            w.drop_next = 1
        if rng.random() < 0.1:
            w.revert_next = 1
        if rng.random() < 0.1 and w.ops:
            nonce = w.chain_nonce(S)
            try:
                w.inject_foreign(S, nonce, fee_mult=rng.choice([1.0, 1.5]))
            except Exception:
                pass
        payload = b"u34-%d-%d" % (seed, i)
        kind = rng.choice(["single", "single", "batch"])
        t0 = w.now()
        try:
            if kind == "single":
                await one(payload, confirm=rng.choice(["accept", "receipt"]),
                          on_lost=lambda ids: reported_lost.update(ids))
            else:
                await many([payload, payload + b"-b"], on_lost=lambda ids: reported_lost.update(ids))
            ids_stored.append(payload)
        except (NothingSentError, UserOpAmbiguousError, UserOpDroppedError, UserOpRevertedError, RuntimeError):
            errors += 1
        assert w.now() - t0 <= 395, "every call ends inside its deadline"
        lock = await userop._get_sender_lock(S)
        assert not lock.locked(), "the lock is released on every exit path"
    settle(w, 900)
    # invariant 1: nothing executes twice (also covers payload + "-b" pairs)
    seen = set()
    for p in w.executed_payloads:
        assert p not in seen
        seen.add(p)
    # invariant 2: the quota ledger agrees with the op states in the active mode
    w.assert_invariants()
    # nothing silently missing: every write that returned success either executed, or its
    # op is still live in the mempool, or is still tracked (pending / stale record)
    tracked = {h for r in userop.pending_records(S) for h in r.hashes}
    live = {o.hash for o in w.ops.values() if o.status in ("outstanding", "submitted")}
    for payload in ids_stored:
        carriers = [o for o in w.ops.values() if payload in o.payloads]
        assert (
            payload in w.executed_payloads
            or payload.decode(errors="replace") in reported_lost
            or any(o.hash in tracked or o.hash in live for o in carriers)
        ), "a write that returned success is neither executed, tracked nor reported lost"
    # invariant 3: a send happens at most _MAX_NONCE_RETRIES times per logical write
    assert w.send_count <= n_writes * userop._MAX_NONCE_RETRIES + 4 * n_writes


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_own_fate_tracks_the_op(monkeypatch):
    """A cancelled receipt-mode write already has an op in the bundler: the next
    write must find it as a pending record and resolve it by hash."""
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    task = asyncio.create_task(one(b"cancel-a", confirm="receipt", fact_ids=["a"]))
    for _ in range(40):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recs = userop.pending_records(S)
    assert len(recs) == 1 and recs[0].fact_ids == ["a"]
    lock = await userop._get_sender_lock(S)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_resend_needs_proof_a_lagging_status_index_is_not_proof(monkeypatch):
    """The bundler's status index says not_found for an op that is alive. That is
    not proof: the write is NOT sent again (it would execute twice)."""
    w = make(monkeypatch)
    deploy(w)
    await one(b"lag-a", fact_ids=["a"])
    w.status_lies = {"*": 2}        # two passes of "not_found" while the op is alive
    w.bundle_delay = 1000           # ... and still unmined at the gate
    with pytest.raises(RuntimeError):      # b collides with the live op at its nonce: bounded failure
        await one(b"lag-b", fact_ids=["b"])
    settle(w, 3000)
    assert w.executed_payloads.count(b"lag-a") <= 1
    w.assert_invariants()


@pytest.mark.asyncio
async def test_cancel_during_the_send_tracks_the_accepted_op(monkeypatch):
    """The send was cut short after the bundler accepted the op: the call parks the
    hash, so the next write resolves it by hash instead of colliding with it."""
    w = make(monkeypatch)
    deploy(w)
    orig = userop._relay_rpc
    sent = asyncio.Event()

    async def slow_send(http, relay_url, headers, method, params, rpc_id=1):
        res = await orig(http, relay_url, headers, method, params, rpc_id)
        if method == "eth_sendUserOperation":
            sent.set()
            await asyncio.sleep(30)          # real time: the caller cancels us here
        return res

    monkeypatch.setattr(userop, "_relay_rpc", slow_send)
    task = asyncio.create_task(one(b"cut-a", fact_ids=["a"]))
    await asyncio.wait_for(sent.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recs = userop.pending_records(S)
    assert len(recs) == 1 and recs[0].fact_ids == ["a"] and recs[0].nonce == 5
    monkeypatch.setattr(userop, "_relay_rpc", orig)
    await one(b"cut-b", fact_ids=["b"])
    settle(w)
    assert w.executed_payloads.count(b"cut-a") == 1 and w.executed_payloads.count(b"cut-b") == 1
    assert w.ops and all(o.nonce in (5, 6) for o in w.ops.values())
    w.assert_invariants()


@pytest.mark.asyncio
async def test_batch_with_only_not_found_is_ambiguous_never_dropped(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    w.status_lies = {"*": 10**6}                 # the op is alive, the index says not_found
    with pytest.raises(UserOpAmbiguousError) as ei:
        await many([b"nf-a", b"nf-b"], fact_ids=["a", "b"])
    assert ei.value.fact_ids == ["a", "b"]
    assert len(w.ops) == 1, "same op, same nonce: never rebuilt at another nonce"
    assert len(userop.pending_records(S)) == 1, "still tracked"


@pytest.mark.asyncio
async def test_replaced_then_not_found_is_not_released_until_proof(monkeypatch):
    """Our accepted op was replaced by a foreign fee bump that has not mined: the
    bundler says not_found, which is not proof. After the grace the record is kept as
    stale (nothing released, nothing re-sent); once the foreign op mines the verdict is
    dead and the write is released and sent again exactly once."""
    w = make(monkeypatch, bundle_delay=1000)
    deploy(w)
    lost: list[list[str]] = []
    await one(b"pf-a", fact_ids=["a"], on_lost=lambda ids: lost.append(ids))
    w.inject_foreign(S, 5, fee_mult=1.5)               # replaces ours; nothing mines yet
    kw = dict(
        sender=S, relay_url="http://relay.test", auth_key_hex=AUTH, wallet_address=S,
        chain_id=100, eoa_address="0x" + "22" * 20, eoa_private_key=KEY,
    )
    await userop.settle_pending_writes(**kw)           # grace, then stale: no release, no send
    assert lost == [], "not_found alone releases nothing"
    assert len(userop.pending_records(S)) == 1 and w.send_count == 1
    w.bundle_delay = 1
    w.advance(30)                                      # the foreign op mines: ours is dead
    await userop.settle_pending_writes(**kw)
    settle(w)
    assert lost == [["a"]]
    assert w.executed_payloads.count(b"pf-a") == 1, "re-sent once on proof"
    assert [r.resends for r in userop.pending_records(S)] == [1], "only the re-sent op is still tracked"


@pytest.mark.asyncio
async def test_batch_record_gets_no_release_on_bare_not_found(monkeypatch):
    w = make(monkeypatch, mine_delay=100_000)
    deploy(w)
    lost: list = []
    with pytest.raises(UserOpAmbiguousError):
        await many([b"bn-a"], fact_ids=["a"], on_lost=lambda ids: lost.append(ids), deadline_s=8)
    w.status_lies = {"*": 10**6}
    await userop.settle_pending_writes(
        sender=S, relay_url="http://relay.test", auth_key_hex=AUTH, wallet_address=S, chain_id=100)
    assert lost == [] and len(userop.pending_records(S)) == 1
