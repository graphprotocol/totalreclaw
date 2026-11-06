"""Staging E2E for PRD-04 F1 / DEP-5: pin contract + recall pin state.

STAGING ONLY. Skipped unless TOTALRECLAW_RECOVERY_PHRASE is set to a throwaway
staging vault supplied through the environment (never written to any file).
Asserts the client resolved the STAGING DataEdge before anything is written.

Run:
  TOTALRECLAW_RECOVERY_PHRASE=... python -m pytest tests/test_staging_pin_contract.py -v -s
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.agent.pin_guard import apply_pin_guard
from totalreclaw.client import TotalReclaw

MNEMONIC = os.environ.get("TOTALRECLAW_RECOVERY_PHRASE", "")
STAGING_URL = "https://api-staging.totalreclaw.xyz"
STAGING_DATA_EDGE = "0xe7a4d2677b686e13775ba9092631089e35f0bb91"
PROD_DATA_EDGE = "0xc445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca"
INDEX_WAIT_S = 30

pytestmark = pytest.mark.skipif(not MNEMONIC, reason="TOTALRECLAW_RECOVERY_PHRASE not set")


@pytest.fixture
async def client():
    c = TotalReclaw(mnemonic=MNEMONIC, relay_url=STAGING_URL, is_test=True)
    await c.resolve_address()
    await c.resolve_chain_id()
    data_edge = (c._data_edge_address or "").lower()
    assert data_edge == STAGING_DATA_EDGE, f"refusing to run: DataEdge {data_edge!r} is not staging"
    assert data_edge != PROD_DATA_EDGE
    yield c
    await c.close()


async def test_pin_contract_on_staging(client) -> None:
    unique = f"dep5pin{int(time.time())}"
    original_id = await client.remember(f"Pedro {unique} home city is Lisbon", importance=0.9)
    await asyncio.sleep(INDEX_WAIT_S)

    pin = await client.pin_fact(original_id)
    assert pin["success"] is True
    pinned_id = pin["new_fact_id"]
    await asyncio.sleep(INDEX_WAIT_S)
    assert await client.get_fact_pin_status(pinned_id) is True

    contradictions = [
        ExtractedFact(text=f"Pedro {unique} home city is Porto", type="claim", importance=8,
                      action="UPDATE", existing_fact_id=pinned_id, source="user"),
        ExtractedFact(text=f"Pedro {unique} home city is Madrid", type="claim", importance=8,
                      action="UPDATE", existing_fact_id=pinned_id, source="user"),
        ExtractedFact(text=f"Pedro {unique} home city is Lisbon", type="claim", importance=8,
                      action="DELETE", existing_fact_id=pinned_id, source="user"),
    ]
    assert await apply_pin_guard(contradictions, client) == []

    # Still pinned and active on the staging subgraph.
    assert await client.get_fact_pin_status(pinned_id) is True

    # Recall (with a query embedding, so the core pin boost path runs) returns
    # the fact flagged as pinned.
    from totalreclaw.embedding import get_embedding

    query = f"Pedro {unique} home city"
    results = await client.recall(query, query_embedding=get_embedding(query), top_k=8)
    hits = [r for r in results if r.id == pinned_id]
    assert hits and hits[0].pinned is True

    # Tidy the throwaway vault (explicit forget is user-initiated, not guarded).
    await client.forget(pinned_id)
