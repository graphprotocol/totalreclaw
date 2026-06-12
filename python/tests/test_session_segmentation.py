"""Unit tests for session_segmentation.segment_sessions.

TDD-first: these tests were written BEFORE the implementation.

The algorithm:
  - New session starts when: time gap > gap_seconds, OR
    cosine(turn_embedding, running_session_centroid) < sim_threshold.
  - Running centroid = sum of member embeddings, renormalized each step.
  - Embeddings are assumed L2-normalized (Harrier output already is).

Key behaviour verified:
  - Time-gap split (gap > 1800 s -> new session)
  - Semantic split (low cosine sim to running centroid -> new session)
  - Referential follow-up stays in session via CENTROID
  - Single-turn sessions (singletons) are returned as single-element lists
  - Empty input -> empty output
  - Threshold edge: sim exactly at threshold -> stays in session
  - None timestamps don't crash
"""
from __future__ import annotations

import math


def _seg():
    from totalreclaw.session_segmentation import segment_sessions
    return segment_sessions


def _unit(v: list) -> list:
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-9:
        return v
    return [x / norm for x in v]


def _topic_a():
    return _unit([1.0, 0.0, 0.0, 0.0])


def _topic_b():
    return _unit([0.0, 1.0, 0.0, 0.0])


def _topic_c():
    return _unit([0.0, 0.0, 1.0, 0.0])


# ── basic API ─────────────────────────────────────────────────────────────────


def test_empty_input():
    sessions = _seg()([], [], gap_seconds=1800, sim_threshold=0.55)
    assert sessions == []


def test_single_turn():
    sessions = _seg()([0.0], [_topic_a()], gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0]]


def test_two_turns_same_topic_no_gap():
    ts = [0.0, 100.0]
    embs = [_topic_a(), _unit([0.9, 0.1, 0.0, 0.0])]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1]]


# ── time-gap split ────────────────────────────────────────────────────────────


def test_time_gap_splits_session():
    ts = [0.0, 2000.0]
    embs = [_topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 2
    assert sessions[0] == [0]
    assert sessions[1] == [1]


def test_time_gap_exactly_at_boundary_stays():
    ts = [0.0, 1800.0]
    embs = [_topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1]]


def test_time_gap_just_above_boundary_splits():
    ts = [0.0, 1801.0]
    embs = [_topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 2


def test_multiple_time_gaps():
    ts = [0.0, 2000.0, 4000.0]
    embs = [_topic_a(), _topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0], [1], [2]]


# ── semantic split ────────────────────────────────────────────────────────────


def test_semantic_split_perpendicular_topics():
    ts = [0.0, 100.0]
    embs = [_topic_a(), _topic_b()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 2
    assert sessions[0] == [0]
    assert sessions[1] == [1]


def test_semantic_threshold_stays_in_session():
    x = 0.55
    y = math.sqrt(1.0 - x * x)
    ts = [0.0, 100.0]
    embs = [_unit([1.0, 0.0, 0.0, 0.0]), _unit([x, y, 0.0, 0.0])]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1]]


def test_semantic_just_below_threshold_splits():
    x = 0.549
    y = math.sqrt(max(0.0, 1.0 - x * x))
    ts = [0.0, 100.0]
    embs = [_unit([1.0, 0.0, 0.0, 0.0]), _unit([x, y, 0.0, 0.0])]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 2


# ── centroid accumulation ────────────────────────────────────────────────────


def test_referential_followup_stays_via_centroid():
    e0 = _unit([1.0, 0.0, 0.0, 0.0])
    e1 = _unit([0.9, 0.1, 0.0, 0.0])
    e2 = _unit([0.6, 0.4, 0.0, 0.0])  # cos vs e0 = 0.6 > 0.55, stays

    ts = [0.0, 100.0, 200.0]
    embs = [e0, e1, e2]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1, 2]]


def test_centroid_based_not_prev_turn():
    e_a = _unit([1.0, 0.0, 0.0, 0.0])
    e_drift = _unit([0.99, 0.14, 0.0, 0.0])
    ts = [0.0, 10.0, 20.0, 30.0]
    embs = [e_a, e_a, e_drift, e_a]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1, 2, 3]]


def test_long_conversation_stays_in_one_session():
    import random
    rng = random.Random(42)
    n = 56
    ts = [i * 30.0 for i in range(n)]
    embs = []
    for _ in range(n):
        v = [0.8 + rng.gauss(0, 0.05), rng.gauss(0, 0.1), rng.gauss(0, 0.05), rng.gauss(0, 0.05)]
        embs.append(_unit(v))

    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 1
    assert sessions[0] == list(range(n))


def test_multi_topic_window_splits():
    ts = [0.0, 60.0, 120.0, 600.0, 660.0]
    embs = [
        _topic_a(),
        _topic_a(),
        _topic_b(),
        _topic_c(),
        _topic_c(),
    ]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == 3
    assert sessions[0] == [0, 1]
    assert sessions[1] == [2]
    assert sessions[2] == [3, 4]


# ── None timestamps ───────────────────────────────────────────────────────────


def test_none_timestamps_treated_as_no_gap():
    ts = [None, None, None]
    embs = [_topic_a(), _topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert sessions == [[0, 1, 2]]


def test_mixed_none_and_real_timestamps():
    ts = [0.0, None, 5000.0]
    embs = [_topic_a(), _topic_a(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert isinstance(sessions, list)
    assert all(isinstance(s, list) for s in sessions)
    total_turns = sum(len(s) for s in sessions)
    assert total_turns == 3


# ── return type invariants ────────────────────────────────────────────────────


def test_all_turns_covered():
    ts = [0.0, 100.0, 2000.0, 2100.0, 5000.0]
    embs = [_topic_a(), _topic_b(), _topic_a(), _topic_c(), _topic_a()]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    all_indices = sorted(idx for s in sessions for idx in s)
    assert all_indices == [0, 1, 2, 3, 4]


def test_sessions_are_contiguous_ordered():
    ts = [i * 30.0 for i in range(10)]
    import random
    rng = random.Random(7)
    embs = [_unit([rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)]) for _ in range(10)]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    prev_last = -1
    for s in sessions:
        assert s == list(range(s[0], s[-1] + 1)), "session indices must be contiguous"
        assert s[0] > prev_last, "sessions must not overlap"
        prev_last = s[-1]


# ── singletons ────────────────────────────────────────────────────────────────


def test_all_perpendicular_embeddings_all_singletons():
    n = 4
    ts = [i * 100.0 for i in range(n)]
    embs = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    sessions = _seg()(ts, embs, gap_seconds=1800, sim_threshold=0.55)
    assert len(sessions) == n
    assert all(len(s) == 1 for s in sessions)
