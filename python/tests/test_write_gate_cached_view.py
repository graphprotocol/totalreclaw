"""Write gate keeps a cached vault view while relay reads are paused.

Internal rc2 QA Finding #2: during a read pause the gate decided on the
recent-write ledger alone, so an explicit remember of a restated fact was
stored twice. The gate now also checks the facts this process saw (topical
gate fetches, dedup-context fetches) in the last 6 hours, in memory only.
"""
from __future__ import annotations

import asyncio
import logging
import math
from unittest.mock import patch

import pytest

from totalreclaw.agent import lifecycle
from totalreclaw.relay import RelayReadBlocked
from totalreclaw.reranker import RerankerResult
from totalreclaw import write_gate
from totalreclaw.write_gate import (
    RecentWriteLedger,
    record_write,
    VIEW_CACHE_MAX_ENTRIES,
    VIEW_CACHE_TTL_SECONDS,
    VaultViewCache,
    check_store,
    ledger_for,
    view_cache_for,
)

VEG = [1.0, 0.0, 0.0, 0.0]
VEG_PARA = [1.0, 0.25, 0.0, 0.0]
TEA = [0.0, 0.0, 1.0, 0.0]


def _blocked() -> RelayReadBlocked:
    return RelayReadBlocked.__new__(RelayReadBlocked)


class _Client:
    def __init__(self, results=None, exc=None, recent=None):
        self.results = results or []
        self.exc = exc
        self.recent = recent or []

    async def recall(self, query, **kw):
        if self.exc:
            raise self.exc
        return self.results

    async def fetch_recent_facts(self, limit=10):
        if self.exc:
            raise self.exc
        return self.recent


def _res(fid, text, emb):
    return RerankerResult(id=fid, text=text, embedding=list(emb), pinned=False)


class _State:
    def __init__(self, client):
        self._client = client

    def get_client(self):
        return self._client

    def get_max_candidate_pool(self):
        return 250


def _check(client, text, emb, mode="explicit"):
    return asyncio.run(check_store(client, text, emb, mode=mode))


@pytest.fixture(autouse=True)
def _no_model():
    with patch("totalreclaw.embedding.is_model_available_locally", return_value=False):
        yield


# 1. fed by a successful check_store fetch
def test_cache_fed_by_gate_fetch_then_used_when_reads_blocked(caplog):
    client = _Client(results=[_res("f1", "User is vegetarian", VEG)])
    assert _check(client, "User likes tea", TEA).store
    client.exc = _blocked()
    with caplog.at_level(logging.INFO, logger="totalreclaw.write_gate"):
        d = _check(client, "The user follows a vegetarian diet", VEG_PARA)
    assert d.action == "confirm"
    assert d.match.fact_id == "f1"
    assert "vault view degraded: reads paused (write gate: ledger + 1 cached)" in caplog.text


# 2. fed by the dedup-context fetch
def test_cache_fed_by_fetch_recent_memories(caplog):
    client = _Client(
        results=[_res("f1", "User is vegetarian", VEG)],
        recent=[{"id": "f2", "text": "User likes tea", "embedding": TEA, "pinned": False}],
    )
    msgs = [{"role": "user", "content": "I eat no meat"}]
    lifecycle._fetch_recent_memories(_State(client), msgs, "turn")
    assert {c["id"] for c in view_cache_for(client).candidates()} == {"f1", "f2"}
    client.exc = _blocked()
    with caplog.at_level(logging.INFO, logger="totalreclaw.write_gate"):
        d = _check(client, "The user follows a vegetarian diet", VEG_PARA)
    assert d.action == "confirm" and d.match.fact_id == "f1"
    assert "ledger + 2 cached" in caplog.text


# 3. forgotten ids are not candidates
def test_forgotten_id_not_a_candidate_in_degraded_mode():
    client = _Client(results=[_res("f1", "User is vegetarian", VEG)])
    _check(client, "User likes tea", TEA)
    view_cache_for(client).forget("f1")
    client.exc = _blocked()
    assert _check(client, "The user follows a vegetarian diet", VEG_PARA).store


def test_ledger_forgotten_id_is_excluded_even_if_cached():
    client = _Client(results=[_res("f1", "User is vegetarian", VEG)])
    _check(client, "User likes tea", TEA)
    ledger_for(client).forget("f1")
    client.exc = _blocked()
    assert _check(client, "The user follows a vegetarian diet", VEG_PARA).store


async def test_client_forget_and_rename_invalidate_cache():
    from totalreclaw.client import TotalReclaw

    class Stub:
        _wallet_address = "0x" + "ab" * 20
        _relay = None
        _signing_priv_key = b"\x00" * 32
        _eoa_address = "0x" + "cd" * 20
        _data_edge_address = None
        _keys = None

        async def _ensure_address(self):
            pass

        async def _ensure_registered(self):
            pass

        async def _ensure_chain_id(self):
            return 84532

    stub = Stub()
    cache = view_cache_for(stub)
    cache.update([{"id": "f1", "text": "a", "embedding": VEG}, {"id": "g1", "text": "b", "embedding": TEA}])
    with patch("totalreclaw.client.forget_fact", return_value=True):
        await TotalReclaw.forget(stub, "f1")
    assert [c["id"] for c in cache.candidates()] == ["g1"]
    result = {"success": True, "fact_id": "g1", "new_fact_id": "g2"}
    with patch("totalreclaw.client.pin_fact", return_value=result):
        await TotalReclaw.pin_fact(stub, "g1")
    (entry,) = cache.candidates()
    assert entry["id"] == "g2" and entry["pinned"] is True and entry["text"] == "b"


# 4. TTL, size, embeddings
def test_ttl_and_size_pruning_and_embedding_filter():
    now = [0.0]
    cache = VaultViewCache(ttl_seconds=100, max_entries=3, clock=lambda: now[0])
    cache.update([
        {"id": "a", "text": "a", "embedding": [1.0]},
        {"id": "noemb", "text": "x", "embedding": None},
        {"id": "empty", "text": "x", "embedding": []},
        {"id": "", "text": "x", "embedding": [1.0]},
        {"id": "notext", "text": "", "embedding": [1.0]},
        "junk",
    ])
    assert [c["id"] for c in cache.candidates()] == ["a"]
    now[0] = 50
    cache.update([{"id": "b", "text": "b", "embedding": [0.5]}])
    now[0] = 120  # a (seen at 0) expired, b (50) alive
    assert [c["id"] for c in cache.candidates()] == ["b"]
    now[0] = 121
    cache.update([{"id": i, "text": i, "embedding": [1.0]} for i in "cdef"])
    assert [c["id"] for c in cache.candidates()] == ["d", "e", "f"]
    # refresh moves to end
    cache.update([{"id": "d", "text": "d", "embedding": [1.0]}])
    cache.update([{"id": "g", "text": "g", "embedding": [1.0]}])
    assert [c["id"] for c in cache.candidates()] == ["f", "d", "g"]
    now[0] = 1000
    assert cache.candidates() == []


def test_embedding_round_trip_floats_and_section_tag():
    cache = VaultViewCache()
    cache.update([{"id": "a", "text": "a", "embedding": [0.1, -0.5, 1.0], "pinned": True}])
    (c,) = cache.candidates()
    assert c["embedding"] == pytest.approx([0.1, -0.5, 1.0], abs=1e-7)
    assert c["embedding"][0] != 0.1  # float32 by design
    assert type(c["embedding"]) is list and all(type(x) is float for x in c["embedding"])
    assert c["section"] == "cached" and c["pinned"] is True


def test_constants():
    assert VIEW_CACHE_TTL_SECONDS == 6 * 3600
    assert VIEW_CACHE_MAX_ENTRIES == 300


# 5. empty cache == old behaviour
def test_degraded_with_empty_cache_is_ledger_only_and_allows_write(caplog):
    client = _Client(exc=_blocked())
    with caplog.at_level(logging.INFO, logger="totalreclaw.write_gate"):
        assert _check(client, "User is vegetarian", VEG).store
    assert "vault view degraded: reads paused (write gate: ledger + 0 cached)" in caplog.text


# 6. per-client
def test_two_clients_have_separate_caches():
    c1, c2 = _Client(), _Client()
    view_cache_for(c1).update([{"id": "a", "text": "a", "embedding": VEG}])
    assert view_cache_for(c1) is view_cache_for(c1)
    assert view_cache_for(c2).candidates() == []


def test_rename_moves_entry_and_sets_pin():
    cache = VaultViewCache()
    cache.update([{"id": "a", "text": "t", "embedding": [1.0]}])
    cache.rename("a", "b", pinned=True)
    (c,) = cache.candidates()
    assert (c["id"], c["text"], c["pinned"]) == ("b", "t", True)
    cache.rename("missing", "z")
    assert [c["id"] for c in cache.candidates()] == ["b"]
    cache.rename("b", "c")
    assert cache.candidates()[0]["pinned"] is True


def test_write_during_pause_still_caught_after_ledger_ttl():
    now = [0.0]
    client = _Client(exc=_blocked())
    write_gate._LEDGERS[client] = RecentWriteLedger(clock=lambda: now[0])
    write_gate._VIEW_CACHES[client] = VaultViewCache(clock=lambda: now[0])
    record_write(client, "f1", "User is vegetarian", VEG)
    now[0] = 20 * 60  # past the 15 min ledger TTL
    assert ledger_for(client).candidates() == []
    d = _check(client, "The user follows a vegetarian diet", VEG_PARA)
    assert d.action == "confirm" and d.match.fact_id == "f1"


def test_record_write_without_embedding_skips_cache():
    client = _Client()
    record_write(client, "f1", "text", None)
    assert view_cache_for(client).candidates() == []


def test_generic_exception_uses_cache_and_logs_count(caplog):
    client = _Client(results=[_res("f1", "User is vegetarian", VEG)])
    _check(client, "User likes tea", TEA)
    client.exc = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG, logger="totalreclaw.write_gate"):
        d = _check(client, "The user follows a vegetarian diet", VEG_PARA)
    assert d.action == "confirm" and d.match.fact_id == "f1"
    assert "ledger + 1 cached" in caplog.text
    assert "RuntimeError" in caplog.text
