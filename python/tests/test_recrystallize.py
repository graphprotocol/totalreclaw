"""Unit tests for the re-crystallize backfill *pure* logic.

Covers the dry-run planner / cost estimator only — the on-chain write path is a
scaffold stub (``execute_recrystallize`` / ``fetch_and_decrypt_vault`` /
checkpoint persistence all raise ``NotImplementedError``), which we assert. No
network, no crypto, no core wheel required (the segmenter falls back to the
local Python impl).
"""
from __future__ import annotations

import math

import pytest

from totalreclaw.recrystallize import (
    METADATA_SUBTYPE_SESSION_CRYSTAL,
    CorrectedSession,
    DecryptedFact,
    QuotaEstimate,
    build_corrected_sessions,
    build_plan,
    estimate_quota_cost,
    execute_recrystallize,
    fetch_and_decrypt_vault,
    split_facts,
    _ceil_div,
    _decode_raw_blob,
    RecrystallizeCheckpoint,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _fact(
    fact_id: str,
    *,
    embedding: list[float] | None,
    created_at: float,
    metadata: dict | None = None,
    fact_type: str = "claim",
) -> DecryptedFact:
    return DecryptedFact(
        fact_id=fact_id,
        text=f"text-{fact_id}",
        embedding=embedding,
        created_at=created_at,
        importance=5.0,
        fact_type=fact_type,
        provenance="user",
        metadata=metadata or {},
    )


def _counting_id_factory():
    """Deterministic session-id factory for reproducible tests."""
    counter = {"n": 0}

    def mint() -> str:
        counter["n"] += 1
        return f"sess-{counter['n']}"

    return mint


# Two well-separated topics (orthogonal vectors), split in time.
TOPIC_A = _norm([1.0, 0.0, 0.0])
TOPIC_B = _norm([0.0, 1.0, 0.0])


# ── split_facts ───────────────────────────────────────────────────────────────


def test_split_facts_separates_crystals():
    facts = [
        _fact("a", embedding=TOPIC_A, created_at=0),
        _fact(
            "cry",
            embedding=TOPIC_A,
            created_at=1,
            metadata={"subtype": METADATA_SUBTYPE_SESSION_CRYSTAL},
        ),
        _fact("b", embedding=TOPIC_B, created_at=2),
    ]
    atomic, crystals = split_facts(facts)
    assert [f.fact_id for f in atomic] == ["a", "b"]
    assert [f.fact_id for f in crystals] == ["cry"]


def test_decrypted_fact_properties():
    f = _fact(
        "x",
        embedding=None,
        created_at=0,
        metadata={"session_id": "old-1", "subtype": METADATA_SUBTYPE_SESSION_CRYSTAL},
    )
    assert f.old_session_id == "old-1"
    assert f.is_crystal is True
    g = _fact("y", embedding=None, created_at=0, metadata={"session_id": "old-2"})
    assert g.old_session_id == "old-2"
    assert g.is_crystal is False


# ── build_corrected_sessions (segmentation) ───────────────────────────────────


def test_segmentation_splits_two_topics_by_time_and_semantics():
    facts = [
        _fact("a1", embedding=TOPIC_A, created_at=0),
        _fact("a2", embedding=TOPIC_A, created_at=10),
        _fact("a3", embedding=TOPIC_A, created_at=20),
        _fact("b1", embedding=TOPIC_B, created_at=5000),  # big time gap + topic shift
        _fact("b2", embedding=TOPIC_B, created_at=5010),
    ]
    sessions = build_corrected_sessions(
        facts, session_id_factory=_counting_id_factory()
    )
    assert len(sessions) == 2
    assert [f.fact_id for f in sessions[0].facts] == ["a1", "a2", "a3"]
    assert [f.fact_id for f in sessions[1].facts] == ["b1", "b2"]
    # Fresh, distinct session ids minted.
    assert sessions[0].fresh_session_id == "sess-1"
    assert sessions[1].fresh_session_id == "sess-2"


def test_segmentation_sorts_by_time_first():
    # Provide out-of-order; expect chronological grouping.
    facts = [
        _fact("late", embedding=TOPIC_B, created_at=9000),
        _fact("early", embedding=TOPIC_A, created_at=0),
    ]
    sessions = build_corrected_sessions(
        facts, session_id_factory=_counting_id_factory()
    )
    # Two separate sessions, earliest first.
    assert sessions[0].facts[0].fact_id == "early"
    assert sessions[-1].facts[-1].fact_id == "late"


def test_segmentation_empty_input():
    assert build_corrected_sessions([]) == []


def test_needs_crystal_threshold():
    multi = CorrectedSession("s", [_fact("a", embedding=TOPIC_A, created_at=0),
                                    _fact("b", embedding=TOPIC_A, created_at=1)])
    single = CorrectedSession("s", [_fact("a", embedding=TOPIC_A, created_at=0)])
    assert multi.needs_crystal is True
    assert single.needs_crystal is False


# ── estimate_quota_cost (the formula: 2·F + S_multi + C_old) ──────────────────


def test_quota_formula_basic():
    # 1 multi-fact session (3 facts) + 1 singleton = 4 atomic facts.
    sessions = [
        CorrectedSession(
            "s1",
            [
                _fact("a", embedding=TOPIC_A, created_at=0),
                _fact("b", embedding=TOPIC_A, created_at=1),
                _fact("c", embedding=TOPIC_A, created_at=2),
            ],
        ),
        CorrectedSession("s2", [_fact("d", embedding=TOPIC_B, created_at=9000)]),
    ]
    old_crystals = [
        _fact("cry1", embedding=None, created_at=0,
              metadata={"subtype": METADATA_SUBTYPE_SESSION_CRYSTAL}),
        _fact("cry2", embedding=None, created_at=0,
              metadata={"subtype": METADATA_SUBTYPE_SESSION_CRYSTAL}),
    ]
    e = estimate_quota_cost(sessions, old_crystals)
    assert e.atomic_facts == 4  # F
    assert e.old_crystals == 2  # C_old
    assert e.multi_fact_sessions == 1  # S_multi
    assert e.singleton_sessions == 1
    # writes_new = F + S_multi = 4 + 1 = 5
    assert e.writes_new == 5
    # tombstones = F + C_old = 4 + 2 = 6
    assert e.tombstones == 6
    # total = 2·F + S_multi + C_old = 8 + 1 + 2 = 11
    assert e.total_quota_cost == 11
    # cross-check the closed form
    assert e.total_quota_cost == 2 * e.atomic_facts + e.multi_fact_sessions + e.old_crystals


def test_quota_worked_example_from_spec():
    # Design §5.2: F=600, C_old=20, S_multi=45  ->  total 1265.
    est = QuotaEstimate(
        atomic_facts=600,
        old_crystals=20,
        multi_fact_sessions=45,
        singleton_sessions=100,
    )
    assert est.writes_new == 645
    assert est.tombstones == 620
    assert est.total_quota_cost == 1265


def test_quota_large_vault_exceeds_one_pro_month():
    # Design §5.2: F=2500, C_old=60, S_multi=180 -> 5240 (> ~3000 Pro/month).
    est = QuotaEstimate(
        atomic_facts=2500,
        old_crystals=60,
        multi_fact_sessions=180,
        singleton_sessions=400,
    )
    assert est.total_quota_cost == 5240
    assert est.total_quota_cost > 3000  # spans >1 Pro month -> needs resumability


def test_userops_estimate_batches_by_30():
    est = QuotaEstimate(
        atomic_facts=600,
        old_crystals=20,
        multi_fact_sessions=45,
        singleton_sessions=0,
    )
    # writes_new=645 -> ceil(645/30)=22 ; tombstones=620 -> ceil(620/30)=21
    assert est.userops_estimate(batch_size=30) == 22 + 21


def test_ceil_div_edges():
    assert _ceil_div(0, 30) == 0
    assert _ceil_div(1, 30) == 1
    assert _ceil_div(30, 30) == 1
    assert _ceil_div(31, 30) == 2
    assert _ceil_div(10, 0) == 0  # defensive


# ── build_plan end-to-end (pure) ──────────────────────────────────────────────


def test_build_plan_integration():
    decrypted = [
        _fact("a1", embedding=TOPIC_A, created_at=0),
        _fact("a2", embedding=TOPIC_A, created_at=10),
        _fact("b1", embedding=TOPIC_B, created_at=9000),
        _fact("cry", embedding=None, created_at=1,
              metadata={"subtype": METADATA_SUBTYPE_SESSION_CRYSTAL,
                        "session_id": "old-giant"}),
    ]
    plan = build_plan(
        "0xabc", decrypted, session_id_factory=_counting_id_factory()
    )
    assert plan.owner == "0xabc"
    # 2 corrected sessions (topic A pair, topic B singleton); 1 old crystal split out.
    assert len(plan.corrected_sessions) == 2
    assert len(plan.old_crystals) == 1
    assert plan.estimate.atomic_facts == 3
    assert plan.estimate.old_crystals == 1
    assert plan.estimate.multi_fact_sessions == 1  # topic A pair
    assert plan.estimate.singleton_sessions == 1  # topic B singleton
    # total = 2·3 + 1 + 1 = 8
    assert plan.estimate.total_quota_cost == 8
    # summary renders without error and mentions the total.
    lines = plan.summary_lines()
    assert any("TOTAL QUOTA COST" in ln for ln in lines)
    assert any("0xabc" in ln for ln in lines)


# ── _decode_raw_blob (recovers metadata read_blob_unified drops) ──────────────


def test_decode_raw_blob_preserves_metadata():
    blob = '{"text":"x","type":"summary","schema_version":"1.0",' \
           '"metadata":{"session_id":"s1","subtype":"session_crystal"}}'
    meta = _decode_raw_blob(blob)
    assert meta["session_id"] == "s1"
    assert meta["subtype"] == "session_crystal"


def test_decode_raw_blob_bad_input():
    assert _decode_raw_blob("not json") == {}
    assert _decode_raw_blob("[1,2,3]") == {}
    assert _decode_raw_blob('{"text":"x"}') == {}  # no metadata key


# ── Guards / stubs (assert the scaffold refuses to write) ─────────────────────


@pytest.mark.asyncio
async def test_execute_refuses_without_write_side_fix():
    plan = build_plan("0xabc", [], session_id_factory=_counting_id_factory())
    with pytest.raises(RuntimeError, match="write_side_fix_confirmed"):
        await execute_recrystallize(
            client=None, plan=plan, write_side_fix_confirmed=False, confirm=True
        )


@pytest.mark.asyncio
async def test_execute_refuses_without_confirm():
    plan = build_plan("0xabc", [], session_id_factory=_counting_id_factory())
    with pytest.raises(RuntimeError, match="confirm"):
        await execute_recrystallize(
            client=None, plan=plan, write_side_fix_confirmed=True, confirm=False
        )


@pytest.mark.asyncio
async def test_execute_is_stubbed_even_when_guards_pass():
    plan = build_plan("0xabc", [], session_id_factory=_counting_id_factory())
    with pytest.raises(NotImplementedError):
        await execute_recrystallize(
            client=None, plan=plan, write_side_fix_confirmed=True, confirm=True
        )


@pytest.mark.asyncio
async def test_fetch_is_stubbed():
    with pytest.raises(NotImplementedError):
        await fetch_and_decrypt_vault(client=None)


def test_checkpoint_persistence_is_stubbed():
    cp = RecrystallizeCheckpoint(owner="0xABC", started_at="t0", last_updated="t0")
    # fingerprint is pure + case-insensitive on the owner address.
    assert cp.fingerprint("0xABC") == cp.fingerprint("0xabc")
    assert str(cp.path()).endswith(".json")
    with pytest.raises(NotImplementedError):
        cp.save()
    with pytest.raises(NotImplementedError):
        RecrystallizeCheckpoint.load("0xabc")
