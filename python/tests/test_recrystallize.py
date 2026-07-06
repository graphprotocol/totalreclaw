"""Unit tests for the re-crystallize backfill.

Covers the dry-run planner / cost estimator (pure) AND the on-chain write path
(:func:`execute_recrystallize`) with a mocked client — mint ordering,
tombstone-after-mint, resume-after-interrupt, the plan-required guard, and
quota-estimator agreement. No network, no crypto, no core wheel required (the
segmenter falls back to the local Python impl).
"""
from __future__ import annotations

import math

import pytest

from totalreclaw.recrystallize import (
    METADATA_SUBTYPE_SESSION_CRYSTAL,
    CorrectedSession,
    DecryptedFact,
    QuotaEstimate,
    RecrystallizeCheckpoint,
    SessionCheckpoint,
    QuotaPaused,
    build_corrected_sessions,
    build_plan,
    estimate_quota_cost,
    execute_recrystallize,
    fetch_and_decrypt_vault,
    split_facts,
    _ceil_div,
    _decode_raw_blob,
    _is_quota_exhausted_error,
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


# ── Execute guards (refuse to write without all three preconditions) ──────────


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
async def test_execute_refuses_without_plan():
    with pytest.raises(RuntimeError, match="plan"):
        await execute_recrystallize(
            client=None, plan=None, write_side_fix_confirmed=True, confirm=True
        )


# ── Checkpoint persistence (round-trip) ───────────────────────────────────────


def test_checkpoint_fingerprint_is_case_insensitive():
    cp = RecrystallizeCheckpoint(owner="0xABC", started_at="t0", last_updated="t0")
    assert cp.fingerprint("0xABC") == cp.fingerprint("0xabc")
    assert str(cp.path()).endswith(".json")


def test_checkpoint_save_load_round_trip(monkeypatch, tmp_path):
    # Redirect the state dir into a tmp path so the test never touches ~.
    import totalreclaw.recrystallize as rec

    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)
    cp = RecrystallizeCheckpoint(
        owner="0xABC", started_at="t0", last_updated="t0", status="running"
    )
    cp.sessions["sess-1"] = SessionCheckpoint(
        phase="written",
        old_fact_ids=["old-a", "old-b"],
        new_fact_ids=["new-a", "new-b"],
        crystal_written=True,
    )
    cp.save()

    loaded = RecrystallizeCheckpoint.load("0xabc")  # case-insensitive fingerprint
    assert loaded is not None
    assert loaded.owner == "0xABC"
    assert loaded.status == "running"
    sc = loaded.sessions["sess-1"]
    assert isinstance(sc, SessionCheckpoint)
    assert sc.phase == "written"
    assert sc.old_fact_ids == ["old-a", "old-b"]
    assert sc.new_fact_ids == ["new-a", "new-b"]
    assert sc.crystal_written is True


def test_checkpoint_load_missing_returns_none(monkeypatch, tmp_path):
    import totalreclaw.recrystallize as rec

    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)
    assert RecrystallizeCheckpoint.load("0xnever") is None


def test_checkpoint_load_tolerates_unknown_keys(monkeypatch, tmp_path):
    import json
    import totalreclaw.recrystallize as rec

    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)
    fp = rec.RecrystallizeCheckpoint.fingerprint("0xabc")
    (tmp_path / f"{fp}.json").write_text(
        json.dumps(
            {
                "owner": "0xabc",
                "started_at": "t0",
                "last_updated": "t0",
                "status": "running",
                "sessions": {"s": {"phase": "done", "future_field": 42}},
                "some_future_top_level_key": "ignored",
            }
        ),
        encoding="utf-8",
    )
    loaded = RecrystallizeCheckpoint.load("0xabc")
    assert loaded is not None
    assert loaded.sessions["s"].phase == "done"


# ── Write-path with a mocked client (mint → confirm → tombstone) ──────────────


class _FakeRelay:
    """Stand-in relay; only used as an opaque handle passed to confirm_indexed
    (which the tests monkeypatch to a no-op)."""


class _FakeClient:
    """Records every write/tombstone in call order for ordering assertions.

    ``remember_batch`` returns synthetic new-fact ids; ``forget`` records the
    tombstoned id. ``fail_after`` (if set) makes the Nth mutating call raise —
    used to simulate a mid-run interrupt for the resume test. ``quota_after``
    makes the Nth call raise a 403-shaped error for the quota-pause test.
    """

    def __init__(self, *, fail_after=None, quota_after=None):
        self.calls: list[tuple[str, object]] = []
        self._relay = _FakeRelay()
        self._n = 0
        self._fail_after = fail_after
        self._quota_after = quota_after
        self._new_counter = 0

    def _tick(self):
        self._n += 1
        if self._quota_after is not None and self._n >= self._quota_after:
            err = RuntimeError("quota_exceeded")

            class _Resp:
                status_code = 403

            err.response = _Resp()  # type: ignore[attr-defined]
            raise err
        if self._fail_after is not None and self._n >= self._fail_after:
            raise RuntimeError("simulated interrupt")

    async def remember_batch(self, facts, source="python-client"):
        self._tick()
        ids = []
        for _ in facts:
            self._new_counter += 1
            ids.append(f"new-{self._new_counter}")
        self.calls.append(("remember_batch", [f["text"] for f in facts]))
        return ids

    async def remember(self, text, **kwargs):
        self._tick()
        self._new_counter += 1
        nid = f"crystal-{self._new_counter}"
        self.calls.append(("remember_crystal", text))
        return nid

    async def forget(self, fact_id):
        self._tick()
        self.calls.append(("forget", fact_id))
        return True


@pytest.fixture
def _no_confirm(monkeypatch):
    """Patch confirm_indexed to a no-op so the write path never hits the net."""
    import totalreclaw.confirm_indexed as ci

    async def _fake_confirm(fact_id, relay, **kwargs):
        return True

    monkeypatch.setattr(ci, "confirm_indexed", _fake_confirm)
    return _fake_confirm


def _sample_plan():
    # 1 multi-fact session (2 facts) + 1 singleton + 1 old crystal.
    decrypted = [
        _fact("a1", embedding=TOPIC_A, created_at=0, metadata={"session_id": "old-giant"}),
        _fact("a2", embedding=TOPIC_A, created_at=10, metadata={"session_id": "old-giant"}),
        _fact("b1", embedding=TOPIC_B, created_at=9000, metadata={"session_id": "old-giant"}),
        _fact("cry", embedding=None, created_at=1,
              metadata={"subtype": METADATA_SUBTYPE_SESSION_CRYSTAL, "session_id": "old-giant"}),
    ]
    return build_plan("0xabc", decrypted, session_id_factory=_counting_id_factory())


@pytest.mark.asyncio
async def test_execute_writes_before_tombstones(monkeypatch, tmp_path, _no_confirm):
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    client = _FakeClient()
    plan = _sample_plan()
    cp = await execute_recrystallize(
        client, plan, write_side_fix_confirmed=True, confirm=True,
    )
    assert cp.status == "completed"

    kinds = [c[0] for c in client.calls]
    # Every old-fact/crystal tombstone (forget) must come AFTER at least one
    # write. Concretely: the first call is never a forget.
    assert kinds[0] in ("remember_batch", "remember_crystal")
    # For the multi-fact session, its facts are written before they are
    # tombstoned: index of the a1/a2 remember_batch < index of forget('a1').
    first_write = kinds.index("remember_batch")
    forget_a1 = next(i for i, c in enumerate(client.calls) if c == ("forget", "a1"))
    assert first_write < forget_a1

    # A fresh Crystal is written for the multi-fact session (only).
    assert kinds.count("remember_crystal") == 1
    # All 3 atomic facts + the old crystal are tombstoned.
    tombstoned = {c[1] for c in client.calls if c[0] == "forget"}
    assert tombstoned == {"a1", "a2", "b1", "cry"}


@pytest.mark.asyncio
async def test_execute_no_crystal_for_singletons(monkeypatch, tmp_path, _no_confirm):
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    # Two singleton sessions (well-separated in time + topic) → no Crystal.
    decrypted = [
        _fact("s1", embedding=TOPIC_A, created_at=0),
        _fact("s2", embedding=TOPIC_B, created_at=99999),
    ]
    plan = build_plan("0xabc", decrypted, session_id_factory=_counting_id_factory())
    client = _FakeClient()
    await execute_recrystallize(
        client, plan, write_side_fix_confirmed=True, confirm=True,
    )
    assert [c[0] for c in client.calls].count("remember_crystal") == 0


@pytest.mark.asyncio
async def test_execute_resume_skips_completed(monkeypatch, tmp_path, _no_confirm):
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    plan = _sample_plan()

    # First run: crash partway (after the 3rd mutating call).
    client1 = _FakeClient(fail_after=3)
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        await execute_recrystallize(
            client1, plan, write_side_fix_confirmed=True, confirm=True,
        )
    cp = RecrystallizeCheckpoint.load("0xabc")
    assert cp is not None
    assert cp.status == "failed"

    # Re-derive the SAME plan (deterministic segmentation) and resume.
    plan2 = _sample_plan()
    client2 = _FakeClient()
    cp2 = await execute_recrystallize(
        client2, plan2, write_side_fix_confirmed=True, confirm=True, checkpoint=cp,
    )
    assert cp2.status == "completed"

    # Across BOTH runs, each old id is tombstoned exactly once (no double
    # tombstone) — the resume skipped already-completed work.
    all_forgets = [c[1] for c in client1.calls + client2.calls if c[0] == "forget"]
    assert sorted(all_forgets) == sorted(set(all_forgets))
    assert set(all_forgets) == {"a1", "a2", "b1", "cry"}


@pytest.mark.asyncio
async def test_execute_resume_never_double_mints(monkeypatch, tmp_path, _no_confirm):
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    plan = _sample_plan()
    # Crash right after the first write batch (before its tombstones).
    client1 = _FakeClient(fail_after=2)
    with pytest.raises(RuntimeError):
        await execute_recrystallize(
            client1, plan, write_side_fix_confirmed=True, confirm=True,
        )
    cp = RecrystallizeCheckpoint.load("0xabc")

    plan2 = _sample_plan()
    client2 = _FakeClient()
    await execute_recrystallize(
        client2, plan2, write_side_fix_confirmed=True, confirm=True, checkpoint=cp,
    )
    # The multi-fact session's facts are written on run 1 (phase→written) and
    # NOT re-written on run 2 — so run 2 issues no remember_batch for that
    # session's already-written facts. Count total writes: 1 batch (run1
    # multi) + 1 crystal (run1) may or may not have landed depending on where
    # the crash hit; the resume must not RE-issue a write for a 'written'
    # session. Assert the multi-fact text isn't written twice.
    writes_run1 = [c for c in client1.calls if c[0] == "remember_batch"]
    writes_run2 = [c for c in client2.calls if c[0] == "remember_batch"]
    multi_texts = {"text-a1", "text-a2"}
    wrote_multi_run1 = any(multi_texts & set(c[1]) for c in writes_run1)
    wrote_multi_run2 = any(multi_texts & set(c[1]) for c in writes_run2)
    assert wrote_multi_run1
    assert not wrote_multi_run2  # never re-minted


@pytest.mark.asyncio
async def test_execute_quota_pause_is_clean(monkeypatch, tmp_path, _no_confirm):
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    plan = _sample_plan()
    client = _FakeClient(quota_after=2)  # 403 on the 2nd mutating call
    with pytest.raises(QuotaPaused):
        await execute_recrystallize(
            client, plan, write_side_fix_confirmed=True, confirm=True,
        )
    cp = RecrystallizeCheckpoint.load("0xabc")
    assert cp is not None
    assert cp.status == "paused_quota"
    assert cp.quota_exhausted_at is not None


def test_is_quota_exhausted_error_detects_403():
    class _Resp:
        status_code = 403

    err = RuntimeError("nope")
    err.response = _Resp()  # type: ignore[attr-defined]
    assert _is_quota_exhausted_error(err) is True
    assert _is_quota_exhausted_error(RuntimeError("quota_exceeded 403")) is True
    assert _is_quota_exhausted_error(RuntimeError("AA25 nonce")) is False


@pytest.mark.asyncio
async def test_execute_quota_agreement_with_estimator(monkeypatch, tmp_path, _no_confirm):
    """The number of writes + tombstones the write path actually issues must
    match the dry-run estimator's writes_new / tombstones (design §5)."""
    import totalreclaw.recrystallize as rec
    monkeypatch.setattr(rec, "RECRYSTALLIZE_STATE_DIR", tmp_path)

    plan = _sample_plan()
    client = _FakeClient()
    await execute_recrystallize(
        client, plan, write_side_fix_confirmed=True, confirm=True,
    )
    # writes_new = F (3) + S_multi (1 crystal) = 4 new memories.
    # The write path batches the 3 facts into one remember_batch (all in one
    # session? no — a1/a2 in the multi session, b1 in the singleton) → 2
    # remember_batch calls + 1 crystal. Count MEMORIES not calls:
    minted_facts = sum(len(c[1]) for c in client.calls if c[0] == "remember_batch")
    minted_crystals = sum(1 for c in client.calls if c[0] == "remember_crystal")
    tombstones = sum(1 for c in client.calls if c[0] == "forget")
    assert minted_facts == plan.estimate.atomic_facts  # 3
    assert minted_facts + minted_crystals == plan.estimate.writes_new  # 4
    assert tombstones == plan.estimate.tombstones  # F + C_old = 3 + 1 = 4
