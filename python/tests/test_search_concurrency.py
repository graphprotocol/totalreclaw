"""Trapdoor batches are queried concurrently (#647).

A typical chat message turns into ~11 trapdoor batches. Awaiting them one by
one took 6-12 s against production, past Hermes' 8 s prefetch budget, so most
Telegram turns lost auto-recall. The first batch still goes alone, so a read
denial stops the fan-out after one request (#662, covered in
test_search_read_short_circuit.py).
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt, generate_blind_indices
from totalreclaw.operations import (
    BROADENED_SEARCH_QUERY,
    DEFAULT_TRAPDOOR_BATCH_SIZE,
    BROADENED_SEARCH_QUERY_NO_EMBEDDING,
    FACTS_BY_IDS_QUERY,
    FACTS_BY_IDS_QUERY_NO_EMBEDDING,
    SEARCH_QUERY_CONCURRENCY,
    search_facts,
)

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
LONG_QUERY = (
    "Can you check whether the restaurant we talked about yesterday near the "
    "hotel takes reservations for Saturday lunch, and remind me what I said "
    "about the distance limit?"
)


def _fact(keys, fact_id: str, text: str) -> dict:
    blob = "0x" + base64.b64decode(encrypt(text, keys.encryption_key)).hex()
    return {
        "id": fact_id,
        "encryptedBlob": blob,
        "encryptedEmbedding": None,
        "decayScore": "0.5",
        "timestamp": "2026-11-01T10:00:00.000Z",
        "isActive": True,
        "contentFp": fact_id,
    }


class _SlowRelay:
    """Answers every query after a delay and records peak concurrency."""

    def __init__(self, keys, delay: float = 0.05) -> None:
        self.keys = keys
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def query_subgraph(self, query: str, variables: dict) -> dict:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        if query == BROADENED_SEARCH_QUERY:
            return {"data": {"facts": [_fact(self.keys, "broad-1", "User likes hiking")]}}
        first = variables["trapdoors"][0]
        return {
            "data": {
                "blindIndexes": [
                    {"id": f"idx-{first}", "fact": _fact(self.keys, f"fact-{first[:8]}", "User restaurant lunch")}
                ]
            }
        }


@pytest.fixture
def keys():
    return derive_keys_from_mnemonic(TEST_MNEMONIC)


def _chunk_count(query: str) -> int:
    return -(-len(generate_blind_indices(query)) // DEFAULT_TRAPDOOR_BATCH_SIZE)


@pytest.mark.asyncio
async def test_batches_run_concurrently_and_bounded(keys):
    n_chunks = _chunk_count(LONG_QUERY)
    assert n_chunks >= 8  # precondition: a realistic multi-batch query
    relay = _SlowRelay(keys)

    loop = asyncio.get_running_loop()
    started = loop.time()
    results = await search_facts(query=LONG_QUERY, keys=keys, owner="0xABC", relay=relay, top_k=50)
    elapsed = loop.time() - started

    # Every batch (+ entity trapdoors) and the broadened query still ran.
    assert relay.calls >= n_chunks + 1
    assert 1 < relay.peak <= SEARCH_QUERY_CONCURRENCY
    # Sequential would take calls * delay; concurrent is a few rounds.
    assert elapsed < relay.calls * relay.delay / 2
    ids = {r.id for r in results}
    assert "broad-1" in ids
    assert len(ids) >= n_chunks


@pytest.mark.asyncio
async def test_failed_batch_degrades_while_others_succeed(keys):
    relay = _SlowRelay(keys, delay=0.0)
    real = relay.query_subgraph
    seen = {"n": 0}

    async def flaky(query, variables):
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("502 from gateway")
        return await real(query, variables)

    relay.query_subgraph = flaky
    results = await search_facts(query=LONG_QUERY, keys=keys, owner="0xabc", relay=relay, top_k=50)
    assert results
    assert "broad-1" in {r.id for r in results}


class _IdsOnlyRelay:
    """Production shape: batch queries return fact ids only; payloads come
    from FACTS_BY_IDS_QUERY. Every batch matches the same 30 facts (a common
    word), and the broadened query returns 10 of them with payloads."""

    def __init__(self, keys) -> None:
        self.facts = {f"f{i:02d}": _fact(keys, f"f{i:02d}", f"User fact number {i}") for i in range(30)}
        self.fetched: list[str] = []
        self.calls = {"batch": 0, "broadened": 0, "by_ids": 0}
        self.queries: list[str] = []

    async def query_subgraph(self, query: str, variables: dict) -> dict:
        await asyncio.sleep(0)
        if query in (BROADENED_SEARCH_QUERY, BROADENED_SEARCH_QUERY_NO_EMBEDDING):
            self.calls["broadened"] += 1
            self.queries.append(query)
            return {"data": {"facts": [self.facts[f"f{i:02d}"] for i in range(10)]}}
        if query in (FACTS_BY_IDS_QUERY, FACTS_BY_IDS_QUERY_NO_EMBEDDING):
            self.calls["by_ids"] += 1
            self.queries.append(query)
            self.fetched += variables["ids"]
            return {"data": {"facts": [self.facts[i] for i in variables["ids"]]}}
        self.calls["batch"] += 1
        return {"data": {"blindIndexes": [{"id": f"idx-{fid}", "fact": {"id": fid}} for fid in self.facts]}}


@pytest.mark.asyncio
async def test_payloads_fetched_once_per_unique_fact(keys):
    relay = _IdsOnlyRelay(keys)
    results = await search_facts(query=LONG_QUERY, keys=keys, owner="0xabc", relay=relay, top_k=50)

    assert relay.calls["batch"] >= 8
    # Only the 20 facts the broadened query didn't already carry are fetched,
    # each exactly once, however many batches matched them.
    assert sorted(relay.fetched) == [f"f{i:02d}" for i in range(10, 30)]
    assert relay.calls["by_ids"] == 1
    assert {r.id for r in results} == set(relay.facts)


@pytest.mark.asyncio
async def test_without_embeddings_ranks_identically(keys):
    with_emb = _IdsOnlyRelay(keys)
    without = _IdsOnlyRelay(keys)
    a = await search_facts(query=LONG_QUERY, keys=keys, owner="0xabc", relay=with_emb, top_k=50)
    b = await search_facts(
        query=LONG_QUERY, keys=keys, owner="0xabc", relay=without, top_k=50,
        include_embeddings=False,
    )
    assert set(with_emb.queries) <= {BROADENED_SEARCH_QUERY, FACTS_BY_IDS_QUERY}
    assert set(without.queries) == {BROADENED_SEARCH_QUERY_NO_EMBEDDING, FACTS_BY_IDS_QUERY_NO_EMBEDDING}
    assert [(r.id, r.rrf_score) for r in a] == [(r.id, r.rrf_score) for r in b]


@pytest.mark.asyncio
async def test_hits_with_failed_payload_fetch_raise_not_empty(keys):
    """Ids found but every payload fetch failed: an outage, not "no memories"."""
    relay = _IdsOnlyRelay(keys)
    real = relay.query_subgraph

    async def payloads_down(query, variables):
        if query in (BROADENED_SEARCH_QUERY, FACTS_BY_IDS_QUERY):
            raise RuntimeError("502 from gateway")
        return await real(query, variables)

    relay.query_subgraph = payloads_down
    with pytest.raises(RuntimeError, match="502"):
        await search_facts(query=LONG_QUERY, keys=keys, owner="0xabc", relay=relay)
