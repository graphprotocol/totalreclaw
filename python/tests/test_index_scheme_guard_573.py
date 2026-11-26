"""Pin / unpin / retype / set_scope index-scheme guard (internal#573, spec 4.4/5.3)."""
from __future__ import annotations

import base64
import inspect
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw import operations, retype_setscope
from totalreclaw.claims_helper import build_canonical_claim_v1
from totalreclaw.client import TotalReclaw
from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt
from totalreclaw.operations import (
    FACT_INDEX_SCHEME_QUERY,
    INDEX_SCHEME_UNCONFIRMED_MSG,
    IndexSchemeUnconfirmedError,
)
from totalreclaw.relay import RelayClient, RelayReadBlocked
from totalreclaw.write_gate import KeyedWriteRecord, keyed_writes_for

MN = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)
SA = "0x2c0cf74b2b76110708ca431796367779e3738250"
KEYS = derive_keys_from_mnemonic(MN)
OLD_SUBGRAPH_ERROR = {"errors": [{"message": "Type `Fact` has no field `indexScheme`"}]}

from eth_account import Account as _Account

_Account.enable_unaudited_hdwallet_features()
_EOA = _Account.from_mnemonic(MN, account_path="m/44'/60'/0'/0/0")
EOA_ADDR, EOA_KEY = _EOA.address, bytes(_EOA.key)


def _blob_hex(text="Prefers dark mode") -> str:
    class F:
        pass

    f = F()
    f.text, f.type, f.source, f.scope = text, "preference", "user", "unspecified"
    f.reasoning = f.entities = f.volatility = None
    f.confidence = 0.9
    claim = build_canonical_claim_v1(f, importance=7, created_at="2026-11-01T00:00:00.000Z", claim_id="x")
    return "0x" + base64.b64decode(encrypt(claim, KEYS.encryption_key)).hex()


def _fact_body(fid, scheme_mode, owner=SA):
    f = {
        "id": fid, "owner": owner, "encryptedBlob": _blob_hex(), "encryptedEmbedding": None,
        "decayScore": "1", "timestamp": "1", "createdAt": "1", "isActive": True, "contentFp": "c",
    }
    return f


class FakeRelay:
    """Relay stand-in. ``mode`` drives the answer to the guard query."""

    def __init__(self, mode="ok", owner=SA):
        self.mode = mode
        self.owner = owner
        self.guard_queries = 0
        self.other_queries = 0
        self.guard_bodies = []
        self.relay_url = "https://relay.example"
        self._relay_url = self.relay_url
        self.auth_key_hex = "aa"
        self._auth_key_hex = "aa"
        self.client_id = "t"
        self._client_id = "t"
        self.session_id = None
        self._session_id = None

    async def query_subgraph(self, query, variables, chain=None):
        if "FactIndexScheme" in query:
            self.guard_queries += 1
            self.guard_bodies.append(query)
            if self.mode == "blocked":
                raise RelayReadBlocked(MagicMock(status_code=429, headers={}, json=lambda: {}))
            if self.mode == "boom":
                raise RuntimeError("relay down")
            if self.mode == "old_subgraph":
                return OLD_SUBGRAPH_ERROR
            if self.mode == "null":
                return {"data": {"fact": None}}
            if self.mode == "malformed":
                return {"data": "nope"}
            scheme = {"ok": 1, "zero": 0, "missing": None, "owner_mismatch": 1}[self.mode]
            owner = "0x" + "99" * 20 if self.mode == "owner_mismatch" else self.owner
            fact = {"id": variables["id"], "owner": owner}
            if scheme is not None:
                fact["indexScheme"] = scheme
            return {"data": {"fact": fact}}
        self.other_queries += 1
        assert "indexScheme" not in query
        if "fact(id" in query:
            return {"data": {"fact": _fact_body(variables["id"], None, self.owner)}}
        return {"data": {}}


PIN = lambda relay, **kw: operations.pin_fact(  # noqa: E731
    fact_id="f1", keys=KEYS, owner=SA, relay=relay, eoa_private_key=EOA_KEY,
    eoa_address=EOA_ADDR, sender=SA, **kw)
UNPIN_SETUP = None


async def _unpin(relay, **kw):
    return await operations.unpin_fact(
        fact_id="f1", keys=KEYS, owner=SA, relay=relay, eoa_private_key=EOA_KEY,
        eoa_address=EOA_ADDR, sender=SA, **kw)


async def _retype(relay, **kw):
    return await retype_setscope.execute_retype(
        fact_id="f1", new_type="claim", keys=KEYS, owner=SA, relay=relay,
        eoa_private_key=EOA_KEY, eoa_address=EOA_ADDR, sender=SA, **kw)


async def _scope(relay, **kw):
    return await retype_setscope.execute_set_scope(
        fact_id="f1", new_scope="health", keys=KEYS, owner=SA, relay=relay,
        eoa_private_key=EOA_KEY, eoa_address=EOA_ADDR, sender=SA, **kw)


async def _pin(relay, **kw):
    return await PIN(relay, **kw)


OPS = {"pin": _pin, "retype": _retype, "set_scope": _scope}
# unpin needs a pinned source fact; covered separately below
REFUSE_MODES = ["old_subgraph", "null", "zero", "missing", "owner_mismatch", "boom", "malformed"]


@pytest.fixture
def sends():
    """Patch every UserOp sender and the feedback writer; record calls."""
    calls = {"userop": [], "feedback": []}

    async def _send(**kw):
        calls["userop"].append(kw)
        return "0xhash"

    with patch.object(operations, "build_and_send_userop_batch", side_effect=_send), \
         patch.object(retype_setscope, "build_and_send_userop_batch", side_effect=_send), \
         patch.object(operations, "maybe_write_feedback_for_pin",
                      side_effect=lambda *a, **k: calls["feedback"].append(1)), \
         patch("totalreclaw.confirm_indexed.confirm_indexed", new=AsyncMock(return_value=True)):
        yield calls


def _result_refused(op, result_or_exc):
    if op == "pin":
        return isinstance(result_or_exc, IndexSchemeUnconfirmedError)
    return (
        isinstance(result_or_exc, dict)
        and result_or_exc.get("success") is False
        and result_or_exc.get("error_code") == "index_scheme_unconfirmed"
        and result_or_exc.get("error") == INDEX_SCHEME_UNCONFIRMED_MSG
        and result_or_exc.get("fact_id") == "f1"
    )


async def _run(op, relay, **kw):
    try:
        return await OPS[op](relay, **kw)
    except IndexSchemeUnconfirmedError as e:
        return e


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_keyed_fact_proceeds_with_one_guard_query_before_userop(op, sends):
    relay = FakeRelay("ok")
    order = []
    orig = relay.query_subgraph

    async def spy(q, v, chain=None):
        if "FactIndexScheme" in q:
            order.append(("guard", len(sends["userop"])))
        return await orig(q, v, chain)

    relay.query_subgraph = spy
    result = await _run(op, relay)
    assert not isinstance(result, Exception)
    assert result["success"] is True
    assert relay.guard_queries == 1
    assert order == [("guard", 0)]  # sent before any UserOp
    assert len(sends["userop"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
@pytest.mark.parametrize("mode", REFUSE_MODES)
async def test_refusals_send_nothing(op, mode, sends):
    relay = FakeRelay(mode)
    result = await _run(op, relay)
    assert _result_refused(op, result), mode
    assert sends["userop"] == [] and sends["feedback"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_read_block_propagates(op, sends):
    relay = FakeRelay("blocked")
    with pytest.raises(RelayReadBlocked):
        await OPS[op](relay)
    assert sends["userop"] == []


@pytest.mark.asyncio
async def test_unpin_guarded_and_refused_without_feedback(sends):
    """Unpin: same guard. Source fact is pinned so the idempotent no-op does not apply."""
    pinned_blob = _pinned_blob_hex()

    class R(FakeRelay):
        async def query_subgraph(self, query, variables, chain=None):
            if "FactIndexScheme" not in query and "fact(id" in query:
                self.other_queries += 1
                f = _fact_body(variables["id"], None)
                f["encryptedBlob"] = pinned_blob
                return {"data": {"fact": f}}
            return await super().query_subgraph(query, variables, chain)

    relay = R("old_subgraph")
    with pytest.raises(IndexSchemeUnconfirmedError) as ei:
        await _unpin(relay)
    assert str(ei.value) == INDEX_SCHEME_UNCONFIRMED_MSG
    assert sends["userop"] == [] and sends["feedback"] == []
    ok = R("ok")
    res = await _unpin(ok)
    assert res["success"] is True and ok.guard_queries == 1 and len(sends["userop"]) == 1


def _pinned_blob_hex():
    class F:
        pass

    f = F()
    f.text, f.type, f.source, f.scope = "Prefers dark mode", "preference", "user", "unspecified"
    f.reasoning = f.entities = f.volatility = None
    f.confidence = 0.9
    claim = build_canonical_claim_v1(
        f, importance=7, created_at="2026-11-01T00:00:00.000Z", claim_id="x", pin_status="pinned")
    return "0x" + base64.b64decode(encrypt(claim, KEYS.encryption_key)).hex()


@pytest.mark.asyncio
async def test_idempotent_noop_makes_no_guard_query(sends):
    """Pinning an already pinned fact: no guard query, no write."""
    class R(FakeRelay):
        async def query_subgraph(self, query, variables, chain=None):
            if "FactIndexScheme" not in query and "fact(id" in query:
                self.other_queries += 1
                f = _fact_body(variables["id"], None)
                f["encryptedBlob"] = _pinned_blob_hex()
                return {"data": {"fact": f}}
            return await super().query_subgraph(query, variables, chain)

    relay = R("ok")
    res = await _pin(relay)
    assert res.get("idempotent") is True
    assert relay.guard_queries == 0 and sends["userop"] == []


def test_index_scheme_in_exactly_one_query_constant():
    """``indexScheme`` is in the guard query only (old subgraph has no such field)."""
    for mod in (operations, retype_setscope):
        hits = [n for n, v in vars(mod).items() if isinstance(v, str) and "indexScheme" in v]
        assert hits == (["FACT_INDEX_SCHEME_QUERY"] if mod is operations else []), mod.__name__
    # and no other query string anywhere in the package selects it
    import importlib
    import pkgutil

    import totalreclaw

    for info in pkgutil.walk_packages(totalreclaw.__path__, "totalreclaw."):
        try:
            m = importlib.import_module(info.name)
        except Exception:
            continue
        for n, v in vars(m).items():
            if isinstance(v, str) and "indexScheme" in v:
                assert (m.__name__, n) == ("totalreclaw.operations", "FACT_INDEX_SCHEME_QUERY")
    assert "indexScheme" not in inspect.getsource(retype_setscope)


# --------------------------------------------------------------------------
# KeyedWriteRecord
# --------------------------------------------------------------------------


def test_record_ttl_cap_discard_and_ids_only():
    t = [0.0]
    rec = KeyedWriteRecord(ttl_seconds=10, max_entries=3, clock=lambda: t[0])
    rec.add("a")
    assert rec.contains("a") and not rec.contains("b")
    t[0] = 9.9
    assert rec.contains("a")
    t[0] = 10.1
    assert not rec.contains("a")
    for i in "wxyz":
        rec.add(i)
    assert not rec.contains("w") and rec.contains("x") and rec.contains("z")  # cap 3
    rec.discard("z")
    assert not rec.contains("z")
    rec.add(None)
    rec.add("")
    assert not rec.contains(None)


def test_record_default_ttl_and_cap_match_ledger():
    from totalreclaw import write_gate

    rec = KeyedWriteRecord()
    assert rec._ttl == write_gate.LEDGER_TTL_SECONDS == 15 * 60
    assert rec._max == write_gate.LEDGER_MAX_ENTRIES == 256


def test_record_thread_safe():
    rec = KeyedWriteRecord(max_entries=256)

    def work(k):
        for i in range(500):
            rec.add(f"{k}-{i}")
            rec.contains(f"{k}-{i}")
            if i % 7 == 0:
                rec.discard(f"{k}-{i}")

    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(rec._ids) <= 256


def test_one_record_per_client_object():
    class C:
        pass

    a, b = C(), C()
    assert keyed_writes_for(a) is keyed_writes_for(a)
    assert keyed_writes_for(a) is not keyed_writes_for(b)


# --------------------------------------------------------------------------
# Pin right after remember (client level)
# --------------------------------------------------------------------------


def _client(relay):
    c = TotalReclaw(mnemonic=MN, relay_url="https://relay.example", wallet_address=SA)
    c._relay = relay
    c._ensure_registered = AsyncMock()
    c._ensure_chain_id = AsyncMock(return_value=100)
    return c


@pytest.fixture
def client_env(sends):
    with patch.object(operations, "build_and_send_userop", new=AsyncMock(return_value="0xh")):
        yield sends


@pytest.mark.asyncio
async def test_pin_right_after_remember(client_env):
    sends = client_env
    relay = FakeRelay("null")  # lagging indexer: guard query says fact: null
    c = _client(relay)
    fid = await c.remember("Prefers dark mode", importance=7)
    # (a) same client: no guard query, one UserOp
    res = await c.pin_fact(fid)
    assert res["success"] is True
    assert relay.guard_queries == 0
    assert len(sends["userop"]) == 1
    new_id = res["new_fact_id"]
    # (e) retype right after pin skips the query for the new id
    res2 = await c.retype(new_id, "claim")
    assert res2["success"] is True and relay.guard_queries == 0

    # (b) fresh client object, same keys: refused, no UserOp
    sends["userop"].clear()
    c2 = _client(FakeRelay("null"))
    with pytest.raises(IndexSchemeUnconfirmedError) as ei:
        await c2.pin_fact(fid)
    assert str(ei.value) == INDEX_SCHEME_UNCONFIRMED_MSG
    assert c2._relay.guard_queries == 1 and sends["userop"] == []

    # (c) same client, clock past 15 min: guard query runs and refuses
    rec = keyed_writes_for(c)
    now = [1000.0]
    rec._ids.clear()  # entries from the real clock would break the prune order
    rec._clock = lambda: now[0]
    fid2 = await c.remember("Likes green tea", importance=6)
    now[0] += 15 * 60 + 1
    with pytest.raises(IndexSchemeUnconfirmedError):
        await c.pin_fact(fid2)
    assert relay.guard_queries == 1


@pytest.mark.asyncio
async def test_forget_and_lost_write_drop_the_record(client_env):
    relay = FakeRelay("null")
    c = _client(relay)
    fid = await c.remember("Prefers dark mode", importance=7)
    assert keyed_writes_for(c).contains(fid)
    c._release_lost_writes([fid])
    assert not keyed_writes_for(c).contains(fid)
    with pytest.raises(IndexSchemeUnconfirmedError):
        await c.pin_fact(fid)
    assert relay.guard_queries == 1

    fid2 = await c.remember("Likes green tea", importance=6)
    assert keyed_writes_for(c).contains(fid2)
    with patch("totalreclaw.client.forget_fact", new=AsyncMock(return_value=True)):
        await c.forget(fid2)
    assert not keyed_writes_for(c).contains(fid2)


@pytest.mark.asyncio
async def test_remember_batch_records_ids(client_env):
    relay = FakeRelay("null")
    c = _client(relay)
    with patch("totalreclaw.client.store_fact_batch", new=AsyncMock(return_value=["b1", "b2"])):
        ids = await c.remember_batch([{"text": "one fact"}, {"text": "two fact"}])
    assert list(ids) == ["b1", "b2"]
    assert keyed_writes_for(c).contains("b1") and keyed_writes_for(c).contains("b2")


# --------------------------------------------------------------------------
# Hermes tools: refusal text, and no confirmation asked before the guard
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_pin_and_retype_return_error_msg(client_env):
    from totalreclaw.hermes import tools

    c = _client(FakeRelay("old_subgraph"))
    state = MagicMock()
    state.get_client.return_value = c
    out = json.loads(await tools.pin({"fact_id": "f1"}, state))
    assert out == {"error": INDEX_SCHEME_UNCONFIRMED_MSG}
    out = json.loads(await tools.retype({"fact_id": "f1", "new_type": "claim"}, state))
    assert out == {"error": INDEX_SCHEME_UNCONFIRMED_MSG}
    out = json.loads(await tools.set_scope({"fact_id": "f1", "new_scope": "health"}, state))
    assert out == {"error": INDEX_SCHEME_UNCONFIRMED_MSG}
    assert client_env["userop"] == []


@pytest.mark.asyncio
async def test_hermes_unpin_of_pinned_fact_refuses_before_confirmation(client_env):
    """On a pinned fact in an old subgraph the user is not asked anything."""
    from totalreclaw.hermes import tools

    c = _client(FakeRelay("old_subgraph"))
    c.get_fact_pin_status = AsyncMock(return_value=True)
    state = MagicMock()
    state.get_client.return_value = c
    out = json.loads(await tools.unpin({"fact_id": "f1"}, state))
    assert out == {"error": INDEX_SCHEME_UNCONFIRMED_MSG}
    assert "confirm_token" not in out and client_env["userop"] == []
