"""PRD-04 G2, end to end below the client: a pinned fact survives three
contradicting extractions.

``test_pin_contract_integration.py`` stubs ``recall`` and
``get_fact_pin_status`` on the client. This test does not: pin state is read
the way production reads it, by decrypting the on-chain blob.

Real code on the read path:
  * ``operations.search_facts`` (ids-first blind-index search, payload fetch,
    decrypt, ``totalreclaw_core.is_pinned_claim``, entity refs, rerank with
    core's pin boost),
  * ``operations.get_fact_pin_status`` (strict fact-by-id fetch, decrypt),
  * ``detect_and_resolve_contradictions`` + ``totalreclaw_core.resolve_with_candidates``,
  * ``apply_pin_guard`` and the ``auto_extract`` action loop.

Faked: the subgraph (an in-memory store of AES-GCM-encrypted blobs and
embeddings behind ``query_subgraph``), chain writes (``remember_batch`` /
``forget`` mutate that store), the extraction LLM (one scripted fact per
round) and the embedder (fixed vectors). The encryption key is random bytes;
no recovery phrase is involved.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest

from totalreclaw import operations
from totalreclaw.agent.extraction import ExtractedEntity, ExtractedFact
from totalreclaw.crypto import decrypt, encrypt, encrypt_embedding

OWNER = "0x" + "ab" * 20
PINNED_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PINNED_TEXT = "Pedro's home city is Lisbon"
PINNED_EMBEDDING = [1.0, 0.0, 0.0, 0.0]
# cosine 0.6 with PINNED_EMBEDDING: inside core's contradiction band [0.3, 0.85)
# and below the 0.85 store-time near-duplicate threshold.
CONTRADICTION_EMBEDDING = [0.6, 0.8, 0.0, 0.0]
DIMS = len(PINNED_EMBEDDING)


def _blob(fact_id: str, text: str, *, pinned: bool, entities: list[dict]) -> str:
    claim = {
        "id": fact_id, "text": text, "type": "claim", "source": "user",
        "created_at": "2026-09-01T00:00:00Z", "schema_version": "1.0", "importance": 8,
        "entities": entities,
    }
    if pinned:
        claim["pin_status"] = "pinned"
    return json.dumps(claim)


class FakeChain:
    """In-memory subgraph + chain for one Smart Account."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.facts: dict[str, dict] = {}
        self.forgotten: list[str] = []
        self.stored: list[str] = []
        self.fail_fact_by_id = False
        self._n = 0

    def put(self, fact_id: str, plaintext: str, embedding: list[float]) -> None:
        blob_hex = "0x" + base64.b64decode(encrypt(plaintext, self.key)).hex()
        self.facts[fact_id] = {
            "id": fact_id, "owner": OWNER, "encryptedBlob": blob_hex,
            "encryptedEmbedding": encrypt_embedding(embedding, self.key),
            "decayScore": "0.9", "timestamp": "", "createdAt": "0",
            "isActive": True, "contentFp": "",
        }

    def plaintext(self, fact_id: str) -> str:
        blob_hex = self.facts[fact_id]["encryptedBlob"][2:]
        return decrypt(base64.b64encode(bytes.fromhex(blob_hex)).decode("ascii"), self.key)

    def _active(self) -> list[dict]:
        return [dict(f) for f in self.facts.values() if f["isActive"]]

    # -- relay surface -------------------------------------------------------
    async def query_subgraph(self, gql: str, variables: dict) -> dict:
        if "SearchByBlindIndex" in gql:
            # Every active fact matches; the reranker does the ranking.
            return {"data": {"blindIndexes": [
                {"id": f"bi-{f['id']}", "fact": {"id": f["id"]}} for f in self._active()
            ]}}
        if "PaginateBlindIndex" in gql:
            return {"data": {"blindIndexes": []}}
        if "BroadenedSearch" in gql:
            return {"data": {"facts": self._active()}}
        if "FactsByIds" in gql:
            ids = set(variables["ids"])
            return {"data": {"facts": [f for f in self._active() if f["id"] in ids]}}
        if "FactById" in gql:
            if self.fail_fact_by_id:
                # Indexer error on HTTP 2xx: no ``data`` key.
                return {"errors": [{"message": "indexer unavailable"}]}
            fact = self.facts.get(variables["id"])
            return {"data": {"fact": dict(fact) if fact else None}}
        raise AssertionError(f"unexpected subgraph query: {gql.split('(')[0].strip()}")


class ChainBackedClient:
    """The ``TotalReclaw`` surface ``auto_extract`` uses. Reads go through
    the real ``operations`` functions; writes mutate ``FakeChain``."""

    def __init__(self, chain: FakeChain) -> None:
        self.chain = chain
        self._keys = mock.Mock(encryption_key=chain.key)
        self._relay = chain
        self.resolved_chain_id = 84532

    async def recall(self, query, query_embedding=None, top_k=8, max_candidates=250, **_):
        return await operations.search_facts(
            query=query, keys=self._keys, owner=OWNER, relay=self.chain,
            query_embedding=query_embedding, max_candidates=max_candidates, top_k=top_k,
        )

    async def get_fact_pin_status(self, fact_id):
        return await operations.get_fact_pin_status(
            fact_id=fact_id, keys=self._keys, owner=OWNER, relay=self.chain,
        )

    async def forget(self, fact_id):
        self.chain.forgotten.append(fact_id)
        if fact_id in self.chain.facts:
            self.chain.facts[fact_id]["isActive"] = False
        return True

    async def remember_batch(self, facts, source="python-client"):
        ids = []
        for d in facts:
            self.chain._n += 1
            fact_id = f"bbbbbbbb-bbbb-4bbb-8bbb-{self.chain._n:012d}"
            ents = [
                {"name": e.name, "type": e.type} for e in (d.get("entities") or [])
            ]
            self.chain.put(
                fact_id, _blob(fact_id, d["text"], pinned=False, entities=ents),
                list(d.get("embedding") or CONTRADICTION_EMBEDDING),
            )
            self.chain.stored.append(d["text"])
            ids.append(fact_id)
        return ids


def _vault(pinned: bool) -> tuple[FakeChain, ChainBackedClient]:
    chain = FakeChain(secrets.token_bytes(32))
    chain.put(
        PINNED_ID,
        _blob(PINNED_ID, PINNED_TEXT, pinned=pinned, entities=[{"name": "Pedro", "type": "person"}]),
        PINNED_EMBEDDING,
    )
    return chain, ChainBackedClient(chain)


def _state_with(client: ChainBackedClient):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client
    return state


def _embed(text: str) -> list[float]:
    return list(PINNED_EMBEDDING) if text == PINNED_TEXT else list(CONTRADICTION_EMBEDDING)


def _extract_round(state, fact: ExtractedFact) -> list[str]:
    from totalreclaw.agent.lifecycle import auto_extract

    async def scripted_llm(*args, **kwargs):
        return [fact]

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=scripted_llm), \
         patch("totalreclaw.embedding.get_embedding", side_effect=_embed), \
         patch.object(operations, "get_embedding", side_effect=_embed), \
         patch.object(operations, "get_embedding_dims", return_value=DIMS):
        state.add_message("user", "Actually, about where I live...")
        state.add_message("assistant", "Noted.")
        return auto_extract(state)


PEDRO = [ExtractedEntity(name="Pedro", type="person")]

# Three contradictions, one per destructive path auto-extraction has, each
# stopped by a different mechanism (asserted from the log line it emits).
GUARD_UPDATE = "pin_guard: skipped UPDATE on a pinned fact"
RESOLVER_SKIP = "it contradicts a pinned fact"
GUARD_DELETE = "pin_guard: skipped DELETE on a pinned fact"
CONTRADICTIONS = [
    # 1. The LLM rewrites the pinned fact. No entities, so the resolver does
    #    not run; the pin guard (pin state read via fact-by-id) drops it.
    (ExtractedFact(text="Pedro's home city is Porto", type="claim", importance=8,
                   action="UPDATE", existing_fact_id=PINNED_ID, source="user"),
     GUARD_UPDATE),
    # 2. The LLM adds a conflicting fact sharing the "Pedro" entity. The
    #    contradiction resolver sees the pin (read from the decrypted blob by
    #    search_facts) and core returns SkipNew { ExistingPinned }.
    (ExtractedFact(text="Pedro's home city is Madrid", type="claim", importance=8,
                   action="ADD", source="user", entities=PEDRO),
     RESOLVER_SKIP),
    # 3. The LLM deletes the pinned fact. Pin guard: the DELETE is dropped.
    (ExtractedFact(text=PINNED_TEXT, type="claim", importance=8,
                   action="DELETE", existing_fact_id=PINNED_ID, source="user"),
     GUARD_DELETE),
]


async def _recall_pinned(client: ChainBackedClient):
    with patch.object(operations, "get_embedding_dims", return_value=DIMS):
        results = await client.recall(
            "Pedro home city", query_embedding=list(CONTRADICTION_EMBEDDING), top_k=8,
        )
    return {r.id: r for r in results}


@pytest.mark.parametrize("round_order", [(0, 1, 2), (2, 1, 0)])
async def test_pinned_fact_survives_three_contradicting_extractions(round_order, caplog) -> None:
    chain, client = _vault(pinned=True)
    state = _state_with(client)
    caplog.set_level(logging.INFO, logger="totalreclaw")
    for i in round_order:
        fact, expected_log = CONTRADICTIONS[i]
        caplog.clear()
        _extract_round(state, fact)
        assert expected_log in caplog.text, f"round {i}: {expected_log!r} not logged"

    assert chain.forgotten == []
    assert chain.stored == []
    assert chain.facts[PINNED_ID]["isActive"] is True
    assert json.loads(chain.plaintext(PINNED_ID))["pin_status"] == "pinned"
    recalled = await _recall_pinned(client)
    assert recalled[PINNED_ID].pinned is True
    assert recalled[PINNED_ID].text == PINNED_TEXT


async def test_control_unpinned_update_is_applied() -> None:
    """Without the pin the same UPDATE tombstones and replaces the fact, so
    the pinned test cannot pass because the harness never writes."""
    chain, client = _vault(pinned=False)
    _extract_round(_state_with(client), CONTRADICTIONS[0][0])
    assert chain.forgotten == [PINNED_ID]
    assert chain.stored == ["Pedro's home city is Porto"]


async def test_control_unpinned_contradicting_add_is_stored() -> None:
    chain, client = _vault(pinned=False)
    _extract_round(_state_with(client), CONTRADICTIONS[1][0])
    assert chain.forgotten == []
    assert chain.stored == ["Pedro's home city is Madrid"]


async def test_unreadable_pin_state_never_tombstones() -> None:
    """Subgraph returns a GraphQL error body for the fact-by-id read: pin
    state is unknown, so the UPDATE is stored as a plain ADD and the DELETE
    is dropped. The pinned fact stays active."""
    chain, client = _vault(pinned=True)
    chain.fail_fact_by_id = True
    state = _state_with(client)
    _extract_round(state, CONTRADICTIONS[0][0])
    _extract_round(state, CONTRADICTIONS[2][0])
    assert chain.forgotten == []
    assert chain.stored == ["Pedro's home city is Porto"]
    assert chain.facts[PINNED_ID]["isActive"] is True
