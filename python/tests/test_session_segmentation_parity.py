"""Cross-language parity tests for session segmentation (totalreclaw#368).

The whole point of the #368 hoist is that the shared Rust core and the local
Python fallback produce *identical* groupings. These tests run BOTH
implementations on the same fixtures and assert byte-identical output:

  - the local Python impl: ``session_segmentation._segment_sessions_local``
  - the hoisted core impl:  ``totalreclaw_core.segment_sessions``

Fixtures cover the algorithm's decision surface:
  - a >30-min time-gap split
  - a same-window semantic split right at the 0.55 threshold boundary
  - a singleton-gate case (all-perpendicular embeddings → all singletons)
  - a long intact conversation (one session)
  - randomized-embedding cases with a fixed seed (deterministic across runs)

If the installed ``totalreclaw_core`` wheel predates #368 (no
``segment_sessions`` symbol — e.g. PyPI ``totalreclaw-core<=2.5.5``), the
core-vs-python parity tests SKIP with an explicit message telling you how to
run them (build the local core wheel). This is the exact case the runtime
fallback handles, and the Rust-side mirrored unit tests
(``rust/totalreclaw-core/src/session_segmentation.rs``) independently prove
algorithm correctness. The local-impl-only sanity tests always run.
"""
from __future__ import annotations

import math
import random

import pytest

from totalreclaw.session_segmentation import (
    _has_core_segment_sessions,
    _segment_sessions_local,
    segment_sessions,
)

_SKIP_REASON = (
    "installed totalreclaw_core lacks the segment_sessions symbol "
    "(pre-#368 wheel, e.g. PyPI totalreclaw-core<=2.5.5). To run this test, "
    "build the local core wheel: "
    "`cd rust/totalreclaw-core && maturin build --release "
    "--features python-extension --out dist -i <venv-python>` then "
    "`pip install --force-reinstall rust/totalreclaw-core/dist/totalreclaw_core-*.whl`. "
    "Rust-side mirrored unit tests still cover algorithm correctness."
)

_HAS_CORE = _has_core_segment_sessions()

requires_core = pytest.mark.skipif(not _HAS_CORE, reason=_SKIP_REASON)


# ── helpers ─────────────────────────────────────────────────────────────────


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-9:
        return list(v)
    return [x / norm for x in v]


def _core_seg(timestamps, embeddings, gap_seconds=1800, sim_threshold=0.55):
    """Invoke the core binding directly (bypassing the dispatcher)."""
    import totalreclaw_core

    return totalreclaw_core.segment_sessions(
        timestamps, embeddings, float(gap_seconds), float(sim_threshold)
    )


def _normalize(sessions) -> list[list[int]]:
    """Coerce to a plain list[list[int]] so tuple/list differences don't matter."""
    return [[int(i) for i in s] for s in sessions]


# ── fixtures: (name, timestamps, embeddings, gap_seconds, sim_threshold) ─────


def _fixtures():
    fixtures: list[tuple] = []

    # 1. >30-min time-gap split (same topic, but 2000s > 1800s gap).
    fixtures.append(
        (
            "time_gap_split",
            [0.0, 2000.0],
            [_unit([1.0, 0.0, 0.0, 0.0]), _unit([1.0, 0.0, 0.0, 0.0])],
            1800,
            0.55,
        )
    )

    # 2a. Same-window semantic split AT the 0.55 boundary → stays (>= threshold).
    x = 0.55
    y = math.sqrt(1.0 - x * x)
    fixtures.append(
        (
            "semantic_at_boundary_stays",
            [0.0, 100.0],
            [_unit([1.0, 0.0, 0.0, 0.0]), _unit([x, y, 0.0, 0.0])],
            1800,
            0.55,
        )
    )

    # 2b. Same-window semantic split JUST below the 0.55 boundary → splits.
    xb = 0.549
    yb = math.sqrt(max(0.0, 1.0 - xb * xb))
    fixtures.append(
        (
            "semantic_just_below_boundary_splits",
            [0.0, 100.0],
            [_unit([1.0, 0.0, 0.0, 0.0]), _unit([xb, yb, 0.0, 0.0])],
            1800,
            0.55,
        )
    )

    # 3. Singleton-gate: all-perpendicular embeddings → every turn a singleton.
    fixtures.append(
        (
            "all_singletons",
            [0.0, 100.0, 200.0, 300.0],
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
            1800,
            0.55,
        )
    )

    # 4. Long intact conversation (tight cluster) → one session.
    rng = random.Random(42)
    n = 56
    long_ts = [i * 30.0 for i in range(n)]
    long_embs = []
    for _ in range(n):
        v = [
            0.8 + rng.gauss(0, 0.05),
            rng.gauss(0, 0.1),
            rng.gauss(0, 0.05),
            rng.gauss(0, 0.05),
        ]
        long_embs.append(_unit(v))
    fixtures.append(("long_intact_conversation", long_ts, long_embs, 1800, 0.55))

    # 5. None-timestamp handling (None → 0-gap).
    fixtures.append(
        (
            "none_timestamps",
            [0.0, None, 5000.0],
            [
                _unit([1.0, 0.0, 0.0, 0.0]),
                _unit([1.0, 0.0, 0.0, 0.0]),
                _unit([1.0, 0.0, 0.0, 0.0]),
            ],
            1800,
            0.55,
        )
    )

    # 6. Multi-topic window → three sessions.
    fixtures.append(
        (
            "multi_topic_window",
            [0.0, 60.0, 120.0, 600.0, 660.0],
            [
                _unit([1.0, 0.0, 0.0, 0.0]),
                _unit([1.0, 0.0, 0.0, 0.0]),
                _unit([0.0, 1.0, 0.0, 0.0]),
                _unit([0.0, 0.0, 1.0, 0.0]),
                _unit([0.0, 0.0, 1.0, 0.0]),
            ],
            1800,
            0.55,
        )
    )

    # 7. Randomized-embedding cases, fixed seeds → deterministic across runs.
    for seed in (1, 7, 13, 99, 2024):
        rng = random.Random(seed)
        m = 40
        ts = [i * 45.0 for i in range(m)]
        embs = [
            _unit([rng.gauss(0, 1) for _ in range(8)])
            for _ in range(m)
        ]
        fixtures.append((f"random_seed_{seed}", ts, embs, 1800, 0.55))

    # 8. Empty input.
    fixtures.append(("empty", [], [], 1800, 0.55))

    return fixtures


_FIXTURES = _fixtures()
_FIXTURE_IDS = [f[0] for f in _FIXTURES]


# ── local-impl sanity (always runs) ─────────────────────────────────────────


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
def test_local_impl_partitions_all_turns(fixture):
    """The local impl must cover every turn exactly once, contiguously."""
    _name, ts, embs, gap, thr = fixture
    sessions = _normalize(_segment_sessions_local(ts, embs, gap, thr))
    all_indices = sorted(idx for s in sessions for idx in s)
    assert all_indices == list(range(len(ts)))
    # Contiguous + ordered + non-overlapping.
    prev_last = -1
    for s in sessions:
        assert s == list(range(s[0], s[-1] + 1)), f"{_name}: non-contiguous {s}"
        assert s[0] > prev_last, f"{_name}: overlapping sessions"
        prev_last = s[-1]


# ── the actual parity assertion (core vs python) ────────────────────────────


@requires_core
@pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
def test_core_matches_local(fixture):
    """Core and local Python impl produce IDENTICAL groupings."""
    _name, ts, embs, gap, thr = fixture
    local = _normalize(_segment_sessions_local(ts, embs, gap, thr))
    core = _normalize(_core_seg(ts, embs, gap, thr))
    assert core == local, (
        f"{_name}: core/python divergence\n  core:  {core}\n  local: {local}"
    )


@requires_core
@pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
def test_dispatcher_matches_local(fixture):
    """The public dispatcher (core-preferring) matches the local impl too.

    When core is present the dispatcher routes to core; this proves the
    dispatcher wiring itself doesn't perturb the result.
    """
    _name, ts, embs, gap, thr = fixture
    local = _normalize(_segment_sessions_local(ts, embs, gap, thr))
    public = _normalize(segment_sessions(ts, embs, gap, thr))
    assert public == local, f"{_name}: dispatcher/local divergence"


# ── dispatcher always agrees with local regardless of core presence ─────────


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_FIXTURE_IDS)
def test_public_dispatcher_never_diverges_from_local(fixture):
    """Whether core is present or not, the public API must equal the local impl.

    (When core is absent, the dispatcher IS the local impl, so this is trivially
    true; when core is present, it asserts real cross-impl parity. Either way
    the public contract holds — this test always runs.)
    """
    _name, ts, embs, gap, thr = fixture
    local = _normalize(_segment_sessions_local(ts, embs, gap, thr))
    public = _normalize(segment_sessions(ts, embs, gap, thr))
    assert public == local, f"{_name}: public API diverged from local impl"
