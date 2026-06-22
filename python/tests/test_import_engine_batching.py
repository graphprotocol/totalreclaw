"""Tests for the imp-11 import-engine chunked-batch helper.

Acceptance criteria (imp-11 decomposition): the helper must:

* On Gnosis (chain 100), buffer facts into groups of ≤IMPORT_MAX_BATCH_SIZE
  (30 since #392 Part 2 — was 15) and submit each group via
  ``client.remember_batch`` (one UserOp per group).
* On free-tier / non-Gnosis chains, fall back to per-fact
  ``client.remember`` calls (current behaviour).
* Cover 14-, 30-, and 45-fact cases (sub-cap, at-cap, over-cap → split).

Spec: ``docs/specs/imp/281-gnosis-batching-chain-gate.md`` §5.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.import_engine import (
    ImportEngine,
    IMPORT_MAX_BATCH_SIZE,
)


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch):
    """Stub the embedding model for this module.

    These tests assert *batching* behaviour (one ``remember_batch`` per
    ≤15-fact group), not embedding correctness. ``_store_facts_chunked`` calls
    ``_prepare_fact_payload`` → real ``get_embedding`` (the 344 MB Harrier ONNX
    model) for every fact, which costs ~23 s/fact on CI — so the 14- and
    15-fact cases ran ~5-6 min EACH and blew the python-tests ``timeout-minutes``
    (the operation-cancelled failure on doc-only PR #302). The payload attaches
    the embedding best-effort and the assertions never inspect the vector, so a
    constant stub is sound and keeps the batching logic running in milliseconds.
    """
    import totalreclaw.embedding as _emb

    monkeypatch.setattr(_emb, "get_embedding", lambda _text: [0.0] * 640)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_facts(n: int) -> list[dict]:
    """Build ``n`` distinct fact dicts in the engine's internal shape."""
    return [
        {
            "text": f"Fact number {i}: some unique user detail to avoid dedup",
            "type": "fact",
            "importance": 7,
        }
        for i in range(n)
    ]


def _make_pro_client() -> MagicMock:
    """Build a fake TotalReclaw client resolved on Gnosis (chain 100).

    ``remember_batch`` returns one UUID-shaped string per fact so the
    engine's ``len(ids)`` accounting reflects ``len(input)``.
    ``remember`` is wired but should not be invoked on the batched path.
    """
    client = MagicMock()

    async def _ensure_chain_id():
        return 100

    async def _remember_batch(facts, source="python-client"):
        return [f"fact-id-{i}" for i in range(len(facts))]

    async def _remember(text, **kwargs):
        return f"single-{text[:20]}"

    client._ensure_chain_id = AsyncMock(side_effect=_ensure_chain_id)
    client.remember_batch = AsyncMock(side_effect=_remember_batch)
    client.remember = AsyncMock(side_effect=_remember)
    return client


def _make_free_client() -> MagicMock:
    """Build a fake client resolved on free-tier (Base Sepolia, 84532)."""
    client = MagicMock()

    async def _ensure_chain_id():
        return 84532

    async def _remember(text, **kwargs):
        return f"single-{text[:20]}"

    async def _remember_batch(facts, source="python-client"):
        raise AssertionError("remember_batch must not be called on free-tier chain")

    client._ensure_chain_id = AsyncMock(side_effect=_ensure_chain_id)
    client.remember = AsyncMock(side_effect=_remember)
    client.remember_batch = AsyncMock(side_effect=_remember_batch)
    return client


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Chain-gate predicate
# ---------------------------------------------------------------------------


def test_gnosis_14_facts_submits_one_batch() -> None:
    """14 facts on Gnosis → 1 remember_batch call of 14, 0 remember calls."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(14)))

    assert errors == []
    assert facts_stored == 14
    assert client.remember_batch.await_count == 1
    submitted = client.remember_batch.await_args_list[0].args[0]
    assert len(submitted) == 14
    assert client.remember.await_count == 0


def test_gnosis_15_facts_submits_one_batch() -> None:
    """15 facts (sub-cap under IMPORT_MAX_BATCH_SIZE=30) → 1 batch of 15."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 15
    assert client.remember_batch.await_count == 1
    submitted = client.remember_batch.await_args_list[0].args[0]
    assert len(submitted) == 15
    assert client.remember.await_count == 0


def test_gnosis_30_facts_submits_one_batch() -> None:
    """30 facts (= IMPORT_MAX_BATCH_SIZE) → 1 remember_batch call of 30."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(30)))

    assert errors == []
    assert facts_stored == 30
    assert client.remember_batch.await_count == 1
    sizes = [len(c.args[0]) for c in client.remember_batch.await_args_list]
    assert sizes == [30]
    assert client.remember.await_count == 0


def test_gnosis_45_facts_splits_at_cap() -> None:
    """45 facts (> IMPORT_MAX_BATCH_SIZE=30) → 2 batches of 30 + 15."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(45)))

    assert errors == []
    assert facts_stored == 45
    assert client.remember_batch.await_count == 2
    sizes = [len(c.args[0]) for c in client.remember_batch.await_args_list]
    assert sizes == [30, 15]
    assert client.remember.await_count == 0


def test_gnosis_uneven_sub_cap_single_batch() -> None:
    """20 facts (sub-cap under 30) → 1 batch of 20."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(20)))

    assert errors == []
    assert facts_stored == 20
    assert client.remember_batch.await_count == 1
    sizes = [len(c.args[0]) for c in client.remember_batch.await_args_list]
    assert sizes == [20]


# ---------------------------------------------------------------------------
# Source tag + payload shape
# ---------------------------------------------------------------------------


def test_gnosis_batch_uses_import_source_tag() -> None:
    """remember_batch must be called with source="import" to match the
    pre-batching per-fact behaviour."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    _run(engine._store_facts_chunked(_make_facts(3)))

    call = client.remember_batch.await_args_list[0]
    assert call.kwargs.get("source") == "import"


def test_gnosis_batch_normalises_importance_to_unit_range() -> None:
    """The 1-10 importance scale must be normalised to 0.0-1.0 before
    submission, matching the existing _store_fact contract."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    _run(engine._store_facts_chunked([
        {"text": "max importance fact", "type": "fact", "importance": 10},
        {"text": "mid importance fact", "type": "fact", "importance": 5},
    ]))

    submitted = client.remember_batch.await_args_list[0].args[0]
    assert submitted[0]["importance"] == pytest.approx(1.0)
    assert submitted[1]["importance"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Free-tier fallback
# ---------------------------------------------------------------------------


def test_free_tier_falls_back_to_per_fact_remember() -> None:
    """On Base Sepolia (84532) the helper must use per-fact remember() and
    never call remember_batch — current behaviour is preserved."""
    client = _make_free_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 15
    assert client.remember.await_count == 15
    assert client.remember_batch.await_count == 0


def test_unresolvable_chain_falls_back_to_per_fact() -> None:
    """If ``_ensure_chain_id`` raises (e.g. test client without an awaitable
    stub), the helper must fall back to per-fact remember() rather than
    crash. This preserves backwards compat with existing tests."""
    client = MagicMock()
    # MagicMock auto-creates _ensure_chain_id as a non-awaitable Mock so
    # ``await self._client._ensure_chain_id()`` raises TypeError.
    async def _remember(text, **kwargs):
        return f"single-{text[:20]}"
    client.remember = AsyncMock(side_effect=_remember)
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(3)))

    assert errors == []
    assert facts_stored == 3
    assert client.remember.await_count == 3


# ---------------------------------------------------------------------------
# Error / dedup handling
# ---------------------------------------------------------------------------


def test_gnosis_batch_409_dedup_is_silent() -> None:
    """A 409/duplicate failure from remember_batch must not surface as an
    error — matches the per-fact loop's contract."""
    client = _make_pro_client()
    client.remember_batch = AsyncMock(
        side_effect=RuntimeError("409 fingerprint duplicate")
    )
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 0


def test_gnosis_batch_partial_id_list_counts_only_returned_ids() -> None:
    """If remember_batch returns fewer ids than facts submitted (likely
    fingerprint dedup of a subset), only the returned count is credited."""
    client = _make_pro_client()

    async def _short_batch(facts, source="python-client"):
        return [f"id-{i}" for i in range(len(facts) - 2)]

    client.remember_batch = AsyncMock(side_effect=_short_batch)
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 13


def test_gnosis_batch_non_dedup_error_surfaced() -> None:
    """Non-409 errors propagate to the returned error list (capped at 20)."""
    client = _make_pro_client()
    client.remember_batch = AsyncMock(
        side_effect=RuntimeError("AA25 nonce zombie")
    )
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked(_make_facts(45)))

    assert facts_stored == 0
    # Two chunks attempted (30 + 15); two errors collected.
    assert len(errors) == 2
    assert all("AA25 nonce zombie" in e for e in errors)


def test_empty_input_returns_zero_no_calls() -> None:
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors = _run(engine._store_facts_chunked([]))

    assert facts_stored == 0
    assert errors == []
    assert client.remember_batch.await_count == 0
    assert client.remember.await_count == 0
