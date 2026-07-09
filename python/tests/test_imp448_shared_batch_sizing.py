"""internal#448 — byte-capped batching + halve-on-simfail hoisted into the
SHARED write path (``client.remember_batch``).

rc4 (internal#435) hardened the IMPORT path with a dual cap (count <=15 AND
estimated calldata bytes <=32KB) plus adaptive halve-on-simfail. That logic
lived ONLY in ``imports/engine.py``. #448 moves it DOWN into
``client.remember_batch`` (via a shared ``operations.group_and_store_adaptive``
helper) so EVERY on-chain write caller — auto-extraction, recrystallize, the
import engine — inherits the same size protection.

These tests pin the hoist:
  * the shared helper + estimator are importable from ``totalreclaw.operations``;
  * ``remember_batch`` accepts ANY count and groups internally;
  * BEHAVIOR-PRESERVING for the common case: <=15 light facts -> ONE group,
    ONE store call (identical to today);
  * oversized heavy batches split into groups each <=15 AND <=32KB est;
  * a -32500 sim-revert triggers adaptive halving -> all stored, no error;
  * a floor-1 group that still fails surfaces its error (never silently dropped);
  * the import engine still delegates and its BatchImportResult counts reconcile;
  * the recrystallize + auto-extraction callers flow through the grouping.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# RED until impl: the sizing helpers live in the shared operations layer.
from totalreclaw.operations import (
    group_and_store_adaptive,
    estimate_payload_bytes,
    group_payloads_by_size,
    MAX_BATCH_BYTES,
    MAX_BATCH_GROUP_COUNT,
)


# ── shared client builder (no mnemonic / no relay / no real crypto) ─────────
def _make_client():
    """A TotalReclaw shell with the ensure_* + context hooks stubbed so
    ``remember_batch`` reaches the (mocked) store layer without touching keys,
    the relay, or the bundler."""
    from totalreclaw.client import TotalReclaw

    tr = TotalReclaw.__new__(TotalReclaw)
    tr._ensure_address = AsyncMock()
    tr._ensure_registered = AsyncMock()
    tr._ensure_chain_id = AsyncMock(return_value=100)
    tr._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    tr._get_lsh_hasher = MagicMock(return_value=None)
    tr._relay = MagicMock(name="relay")
    tr._data_edge_address = None
    return tr


def _light(n):
    """Auto-extraction-shape facts: short text, no embedding."""
    return [{"text": f"short fact number {i}"} for i in range(n)]


def _heavy(n):
    """Oversized facts: realistic prose + a 640-dim embedding (~KBs each)."""
    base = (
        "The user moved to Berlin in May 2026 for a new engineering role at a "
        "startup. They are looking for an apartment in Prenzlauer Berg or Mitte "
        "with a budget around 1500 euros per month and want good public transport."
    )
    return [
        {"text": (base * 4)[:800], "embedding": [0.01 * i for i in range(640)]}
        for i in range(n)
    ]


# ── (1) shared helper is importable + count cap is 15 ──────────────────────
def test_shared_sizing_constants_exposed():
    assert MAX_BATCH_GROUP_COUNT == 15
    assert MAX_BATCH_BYTES == 32_000
    # the calibrated estimator + grouper + adaptive orchestrator all live in
    # the shared operations layer now (not just the import engine).
    assert callable(estimate_payload_bytes)
    assert callable(group_payloads_by_size)
    assert callable(group_and_store_adaptive)


# ── (2) BEHAVIOR-PRESERVING: <=15 light facts -> 1 group, 1 store call ─────
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_light_facts_form_one_group_one_store_call(mock_store):
    """The common auto-extraction shape (<=15 light facts that fit well under
    32KB) MUST produce exactly ONE group and ONE store call — byte-identical
    to today. Byte-capping only splits OVERSIZED batches."""
    mock_store.return_value = [f"id-{i}" for i in range(15)]
    client = _make_client()

    ids = await client.remember_batch(_light(15), source="hermes-auto")

    assert mock_store.await_count == 1  # ONE group, ONE store call
    assert len(ids) == 15
    submitted = mock_store.await_args.kwargs.get("facts") or mock_store.await_args.args[0]
    assert len(submitted) == 15


# ── (3) oversized heavy batch splits, every group within BOTH caps ─────────
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_oversized_heavy_facts_split_within_both_caps(mock_store):
    async def _store(facts, *a, **kw):
        return [f"id-{i}" for i in range(len(facts))]

    mock_store.side_effect = _store
    client = _make_client()

    facts = _heavy(40)
    ids = await client.remember_batch(facts, source="python-client")

    assert len(ids) == 40  # nothing dropped or duplicated
    assert mock_store.await_count >= 2  # the byte cap forces a split
    seen = 0
    for call in mock_store.await_args_list:
        group = call.kwargs.get("facts") or call.args[0]
        # count cap (belt-and-braces ceiling)
        assert len(group) <= MAX_BATCH_GROUP_COUNT
        # byte cap (the real governor)
        est = sum(estimate_payload_bytes(p) for p in group)
        assert est <= MAX_BATCH_BYTES, (
            f"group of {len(group)} is ~{est}B > {MAX_BATCH_BYTES}B cap"
        )
        seen += len(group)
    assert seen == 40


# ── (4) -32500 sim-revert -> adaptive halve -> all stored, no error ─────────
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_sim_revert_halves_and_stores_all(mock_store):
    """A 4-light-fact group (ONE group) sim-reverts; halves 2+2 which both
    succeed -> all 4 stored, no error surfaced. Store sees sizes [4, 2, 2]."""
    seen = []

    async def _store(facts, *a, **kw):
        seen.append(len(facts))
        if len(facts) > 2:
            raise RuntimeError(
                "UserOperation reverted during simulation with reason: -32500 "
                "Sender does not implement validateUserOp or factory is not deployed"
            )
        return [f"id-{i}" for i in range(len(facts))]

    mock_store.side_effect = _store
    client = _make_client()

    ids = await client.remember_batch(_light(4), source="python-client")

    assert len(ids) == 4
    assert seen == [4, 2, 2]  # halved once, both halves succeeded


# ── (5) floor-1 group that still fails surfaces its error (no silent drop) ──
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_floor1_failure_surfaces_error(mock_store):
    """A lone fact that sim-reverts at the floor (can't halve further) must
    surface — remember_batch raises rather than silently dropping it."""
    mock_store.side_effect = RuntimeError(
        "UserOperation reverted during simulation with reason: -32500 "
        "Sender does not implement validateUserOp"
    )
    client = _make_client()

    with pytest.raises(RuntimeError):
        await client.remember_batch([{"text": "one lonely fact"}], source="python-client")


# ── (6) duplicate rejection is swallowed (not surfaced as a hard error) ─────
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_duplicate_rejection_is_silent(mock_store):
    """A 409/duplicate rejection from the store is swallowed (0 stored, no
    raise) — same contract as the import path today."""
    mock_store.side_effect = RuntimeError("409 fingerprint duplicate")
    client = _make_client()

    # No raise — duplicates are not hard failures.
    ids = await client.remember_batch(_light(3), source="python-client")
    assert ids == []


# ── (7) import engine delegates; BatchImportResult counts reconcile ─────────
def test_engine_delegation_reconciles_counts(monkeypatch):
    """The import engine runs grouping+adaptive ONCE over the real store
    (store_fact_batch — NOT client.remember_batch; wrapping remember_batch
    would nest two halving cascades and re-store already-stored facts). Its
    BatchImportResult accounting (facts_stored / errors / dups_skipped) must
    still reconcile."""
    # Stub the embedding model so the 10 facts stay LIGHT → one group of 10
    # (mirrors test_batch_sizing_rc4's _no_embedding fixture). Otherwise
    # _prepare_fact_payload generates real ~4.5KB embeddings and the byte cap
    # splits the group before the halving-on-sim-revert is observable.
    import totalreclaw.embedding as emb

    monkeypatch.setattr(emb, "get_embedding", lambda t: None)

    from totalreclaw.import_engine import ImportEngine

    client = _make_client()
    # No pre-write dedup (all facts are fresh).
    client.find_duplicate_texts = AsyncMock(return_value=None)

    calls: list[int] = []

    async def _store(facts, *a, **kw):
        calls.append(len(facts))
        if len(facts) > 5:
            raise RuntimeError(
                "UserOperation reverted during simulation with reason: -32500 "
                "Sender does not implement validateUserOp"
            )
        return [f"id{i}" for i in range(len(facts))]

    monkeypatch.setattr("totalreclaw.import_engine.store_fact_batch", _store)

    engine = ImportEngine(client=client, llm_extract=None)
    facts = [
        {"text": f"distinct fact number {i} about something", "type": "fact", "importance": 8}
        for i in range(10)
    ]
    stored, errors, dups = asyncio.run(engine._store_facts_chunked(facts))
    # 10 facts -> one group of 10 -> sim-revert -> halves 5+5 -> all stored.
    assert stored == 10
    assert errors == []
    assert dups == 0
    assert calls == [10, 5, 5]


# ── (8) recrystallize caller flows through the grouping ────────────────────
@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_recrystallize_shaped_caller_flows_through_grouping(mock_store):
    """recrystallize.py pre-batches by MAX_BATCH_SIZE (30) then calls
    remember_batch. With the hoist, a 30-fact call groups into <=15-fact
    groups internally and stores them all."""
    async def _store(facts, *a, **kw):
        return [f"id-{i}" for i in range(len(facts))]

    mock_store.side_effect = _store
    client = _make_client()

    # recrystallize passes up to 30 facts in one remember_batch call.
    ids = await client.remember_batch(_light(30), source="recrystallize")

    assert len(ids) == 30
    # 30 light facts -> the count cap (15) binds -> exactly two groups.
    sizes = []
    for call in mock_store.await_args_list:
        group = call.kwargs.get("facts") or call.args[0]
        sizes.append(len(group))
    assert sizes == [15, 15]
