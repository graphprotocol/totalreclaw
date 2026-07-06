"""Tests for the imp-11 import-engine chunked-batch helper.

Acceptance criteria (imp-11 decomposition): the helper must:

* On Gnosis (chain 100), buffer facts into groups bounded by BOTH the count
  ceiling ``IMPORT_MAX_BATCH_SIZE`` (15 — restored from 30 in rc4/internal#435)
  AND the estimated calldata-byte cap ``_MAX_BATCH_BYTES`` (32KB), submitting
  each group via ``client.remember_batch`` (one UserOp per group). With
  realistic embedded payloads the byte cap governs, so these tests assert the
  dual-cap INVARIANT (every group ≤ both caps, all facts stored) rather than a
  hardcoded group size. The exact byte-driven split is covered in
  ``test_batch_sizing_rc4.py``.
* On free-tier / non-Gnosis chains, fall back to per-fact
  ``client.remember`` calls (current behaviour).

Spec: ``docs/specs/imp/281-gnosis-batching-chain-gate.md`` §5;
rc4 byte cap: internal#435.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.import_engine import (
    ImportEngine,
    IMPORT_MAX_BATCH_SIZE,
    _MAX_BATCH_BYTES,
    _estimate_payload_bytes,
)


def _assert_groups_within_caps(client) -> list[int]:
    """Every submitted group must respect BOTH the count and byte caps.
    Returns the list of group sizes for callers that want to inspect them."""
    sizes = []
    for call in client.remember_batch.await_args_list:
        group = call.args[0]
        assert len(group) <= IMPORT_MAX_BATCH_SIZE, (
            f"group of {len(group)} exceeds count cap {IMPORT_MAX_BATCH_SIZE}"
        )
        est = sum(_estimate_payload_bytes(p) for p in group)
        assert est <= _MAX_BATCH_BYTES, (
            f"group of {len(group)} facts is ~{est}B > {_MAX_BATCH_BYTES}B cap"
        )
        sizes.append(len(group))
    return sizes


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


def test_gnosis_14_facts_all_stored_within_caps() -> None:
    """14 facts on Gnosis → all stored, every group within both caps, 0
    per-fact remember calls."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(14)))

    assert errors == []
    assert facts_stored == 14
    sizes = _assert_groups_within_caps(client)
    assert sum(sizes) == 14  # no fact dropped or duplicated
    assert client.remember.await_count == 0


def test_gnosis_15_facts_all_stored_within_caps() -> None:
    """15 facts (= count cap) → all stored, groups within both caps."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 15
    sizes = _assert_groups_within_caps(client)
    assert sum(sizes) == 15
    assert client.remember.await_count == 0


def test_gnosis_30_facts_all_stored_within_caps() -> None:
    """30 facts → all stored, no single group exceeds the count or byte cap."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(30)))

    assert errors == []
    assert facts_stored == 30
    sizes = _assert_groups_within_caps(client)
    assert sum(sizes) == 30
    # More than one group: 30 realistic embedded facts cannot fit one ≤32KB op.
    assert client.remember_batch.await_count >= 2
    assert client.remember.await_count == 0


def test_gnosis_45_facts_split_within_caps() -> None:
    """45 facts → split into multiple groups, each within both caps, all
    stored."""
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(45)))

    assert errors == []
    assert facts_stored == 45
    sizes = _assert_groups_within_caps(client)
    assert sum(sizes) == 45
    assert client.remember_batch.await_count >= 2
    assert client.remember.await_count == 0


def test_gnosis_count_cap_binds_without_embedding(monkeypatch) -> None:
    """With NO embedding (small payloads) the byte cap is loose, so the count
    ceiling of 15 governs: 20 tiny facts → groups of 15 + 5."""
    import totalreclaw.embedding as _emb
    monkeypatch.setattr(_emb, "get_embedding", lambda _t: None)
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(20)))

    assert errors == []
    assert facts_stored == 20
    sizes = _assert_groups_within_caps(client)
    assert sizes == [15, 5]


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

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(15)))

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

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(3)))

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

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(15)))

    assert errors == []
    assert facts_stored == 0


def test_gnosis_batch_partial_id_list_counts_only_returned_ids(monkeypatch) -> None:
    """If remember_batch returns fewer ids than facts submitted (likely
    fingerprint dedup of a subset), only the returned count is credited.

    Uses 5 no-embedding facts so they form a single group (well under both
    caps) and the ``len(ids) - 2`` short return maps to a deterministic stored
    count. (With embeddings, 5 realistic facts exceed the 32KB byte cap and
    would split.)"""
    import totalreclaw.embedding as _emb
    monkeypatch.setattr(_emb, "get_embedding", lambda _t: None)
    client = _make_pro_client()

    async def _short_batch(facts, source="python-client"):
        return [f"id-{i}" for i in range(len(facts) - 2)]

    client.remember_batch = AsyncMock(side_effect=_short_batch)
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(5)))

    assert errors == []
    assert client.remember_batch.await_count == 1
    assert facts_stored == 3


def test_gnosis_batch_non_dedup_error_surfaced() -> None:
    """A non-409, non-sim-revert error (e.g. AA25) propagates one error per
    group with no halving. One error per remember_batch call."""
    client = _make_pro_client()
    client.remember_batch = AsyncMock(
        side_effect=RuntimeError("AA25 nonce zombie")
    )
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked(_make_facts(45)))

    assert facts_stored == 0
    # AA25 is not a sim-size revert → no halving → exactly one error per group.
    assert len(errors) == client.remember_batch.await_count
    assert all("AA25 nonce zombie" in e for e in errors)


def test_empty_input_returns_zero_no_calls() -> None:
    client = _make_pro_client()
    engine = ImportEngine(client=client, llm_extract=None)

    facts_stored, errors, _dups = _run(engine._store_facts_chunked([]))

    assert facts_stored == 0
    assert errors == []
    assert client.remember_batch.await_count == 0
    assert client.remember.await_count == 0
