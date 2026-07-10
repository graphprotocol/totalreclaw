"""internal#448 review-revision regression: the import store path must be
SINGLE-PASS — byte-capped grouping + halve-on-simfail happens in EXACTLY ONE
``group_and_store_adaptive`` call over the REAL ``store_fact_batch``.

PR #448's first cut hoisted the adaptive helper into ``client.remember_batch``
BUT left the import engine ALSO wrapping ``remember_batch`` in an outer
``group_and_store_adaptive``. Because ``remember_batch`` itself runs the same
helper internally and RE-RAISES a ``RuntimeError`` wrapping the inner floor-1
``-32500 "… reverted during simulation …"``, the OUTER ``_store_group_adaptive``
mis-detected the wrapped message as a fresh sim-revert → re-halved → each half
re-ran the FULL inner cascade → facts already stored by the inner pass got
RE-STORED (on-chain duplicates) and ``facts_stored`` mis-counted.

This test pins the single-pass contract: an import group containing ONE
genuinely-unstorable (persistent floor-1 ``-32500``) fact must

  * submit every DISTINCT fact to the store AT MOST ONCE (no duplicate
    on-chain writes), and
  * credit exactly the storable facts to ``facts_stored`` (the unstorable one
    surfaces as an error, the rest are stored exactly once).

Reproduction model
------------------
The fake client is a REAL ``TotalReclaw`` instance (built via ``__new__`` so no
mnemonic / relay / crypto is constructed) with its store-setup privates
stubbed. Using the real instance means ``client.remember_batch`` is the REAL
method — which is what the PRE-REVISION engine called per group, so the nested
double-cascade actually reproduces on the un-revised code. The store itself is
intercepted by patching ``store_fact_batch`` at BOTH binding sites:

  * ``totalreclaw.client.store_fact_batch`` — the real ``remember_batch``'s
    inner closure (the PRE-REVISION engine path), and
  * ``totalreclaw.imports.engine.store_fact_batch`` — the revised engine's
    direct single-layer call.

So the SAME test FAILS on the pre-revision code (duplicate writes + wrong
``facts_stored``) and PASSES once the engine calls ``store_fact_batch``
directly in a single adaptive pass.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.imports.engine import ImportEngine


# The one genuinely-unstorable fact: a group containing it sim-reverts down to
# the single-fact floor, where it STILL reverts (persistent -32500). Every
# OTHER fact stores cleanly.
_BAD_FACT = "the one genuinely unstorable fact that always sim-reverts at the floor"


def _facts(n: int) -> list[dict]:
    """``n`` distinct facts; index 7 is the unstorable one."""
    return [
        {
            "text": _BAD_FACT if i == 7 else f"distinct storable fact number {i}",
            "type": "fact",
            "importance": 8,
        }
        for i in range(n)
    ]


def _real_client_with_stubbed_store() -> "MagicMock":
    """A REAL ``TotalReclaw`` (so ``remember_batch`` is the genuine method) with
    every store-setup private stubbed — no mnemonic, no relay, no crypto.

    Mirrors ``test_imp448_shared_batch_sizing._make_client``: the real
    ``remember_batch`` runs against these stubs and reaches the (patched)
    ``store_fact_batch``. Using the real method is what lets the pre-revision
    nested cascade reproduce.
    """
    from totalreclaw.client import TotalReclaw

    tr = TotalReclaw.__new__(TotalReclaw)
    tr._ensure_address = AsyncMock()
    tr._ensure_registered = AsyncMock()
    tr._ensure_chain_id = AsyncMock(return_value=100)
    tr._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    tr._get_lsh_hasher = MagicMock(return_value=None)
    tr._relay = MagicMock(name="relay")
    tr._data_edge_address = None
    # Pre-write dedup must fail open (nothing is a duplicate) so all 15 facts
    # reach the store — otherwise the cascade scenario doesn't exercise.
    tr.find_duplicate_texts = AsyncMock(side_effect=lambda texts: [False] * len(texts))
    return tr


@pytest.fixture(autouse=True)
def _no_embedding(monkeypatch):
    """Keep payloads light (no embedding) so the 15 facts form ONE group, and
    avoid loading the real 344 MB Harrier model."""
    import totalreclaw.embedding as emb

    monkeypatch.setattr(emb, "get_embedding", lambda _t: None)


def test_single_pass_no_duplicate_on_unstorable_floor1_fact(monkeypatch):
    """A 15-fact group with one persistent floor-1 unstorable fact must produce
    NO duplicate on-chain writes and credit exactly the 14 storable facts."""
    # A fact is "stored" only on a SUCCESSFUL store call. A sim-revert raises
    # and writes NOTHING on-chain — so duplicate-on-chain-write = a fact
    # appearing in ``stored_log`` more than once. (Counting every attempt
    # instead would false-positive on the legitimate [15]-reverts-then-[7]-and-
    # [8]-retry halving, where the reverted [15] op stored nothing.)
    stored_log: list[str] = []
    attempt_log: list[list[str]] = []

    async def _fake_store(facts, *a, **kw):
        attempt_log.append([f["text"] for f in facts])
        if any(f["text"] == _BAD_FACT for f in facts):
            if len(facts) == 1:
                # Floor: can't halve further — persistent, unstorable.
                raise RuntimeError(
                    "UserOperation reverted during simulation with reason: -32500 "
                    "Sender does not implement validateUserOp or factory is not "
                    "deployed (persistent floor failure)"
                )
            # Group of >1 containing the bad fact: oversized-style sim-revert
            # → adaptive halving should split it.
            raise RuntimeError(
                "UserOperation reverted during simulation with reason: -32500 "
                "Sender does not implement validateUserOp or factory is not deployed"
            )
        # SUCCESS: this group is written on-chain. Record every stored text.
        stored_log.extend(f["text"] for f in facts)
        return [f"id-{i}" for i in range(len(facts))]

    # Patch BOTH binding sites so the mock is hit on the pre-revision engine
    # path (real remember_batch's inner closure) AND the revised engine path
    # (direct store_fact_batch call). ``raising=False`` lets this same test run
    # on the PRE-REVISION code, where the engine does not yet import
    # ``store_fact_batch`` (so that binding site does not exist yet); there the
    # engine calls ``remember_batch`` and the client-site patch is what
    # reproduces the nested double-cascade.
    monkeypatch.setattr("totalreclaw.client.store_fact_batch", _fake_store)
    monkeypatch.setattr(
        "totalreclaw.imports.engine.store_fact_batch", _fake_store, raising=False
    )

    client = _real_client_with_stubbed_store()
    engine = ImportEngine(client=client, llm_extract=None)

    stored, errors, _dups, _conv = asyncio.run(engine._store_facts_chunked(_facts(15)))

    # (1) NO duplicate on-chain writes: every distinct STORABLE fact appears in
    # the success log AT MOST ONCE. This is the core regression — the nested
    # double-pass re-stored good facts up to ×5 (each successful store IS an
    # on-chain write).
    dup_writes = {t: c for t, c in Counter(stored_log).items() if c > 1}
    assert not dup_writes, (
        f"single-pass violation — storable facts WRITTEN on-chain >1x: "
        f"{dup_writes} (attempt trace: {attempt_log})"
    )

    # (2) Correct accounting: the 14 storable facts credited exactly once; the
    # unstorable one surfaces as an error (never silently dropped, never
    # inflating the stored count).
    assert stored == 14, (
        f"facts_stored={stored}, expected 14 (attempt trace: {attempt_log})"
    )
    assert errors, "the unstorable floor-1 fact must surface as an error"
    assert any("Batch store failed" in e for e in errors)
