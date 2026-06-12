"""Centroid-walk session segmentation for conversation imports.

Pure computation — no I/O, no LLM calls, no network. Designed to be hoistable
to the shared Rust core (totalreclaw-core) in a future pass for cross-client
parity. For now it lives Python-only and is used by import_engine.py.

Algorithm (from the validated harrier_prototype.py):
  - Walk turns in chronological order.
  - Maintain a running centroid = unnormalized sum of member embeddings.
  - For each new turn i (i >= 1), compare its embedding to the *normalised*
    running centroid of the current session.
  - Start a new session if EITHER:
      1. time gap > gap_seconds  (hard boundary)
      2. cosine(turn_emb, centroid_normalised) < sim_threshold  (semantic shift)
  - When a turn joins the current session, accumulate: centroid += turn_emb.
    Re-normalise the centroid before each comparison.

Embeddings are assumed L2-normalised on input (Harrier output already is), so
the dot product between any embedding and the normalised centroid is numerically
stable and equals the cosine similarity.

Reference: /tmp/harrier_prototype.py `segment()` function, validated on
Pedro's real Gemini Takeout (3942 turns):
  - thr=0.55: 1739 sessions, 56% singletons, 56-turn conversation preserved.
  - thr=0.60: similar stats. 0.55 chosen as default.
"""
from __future__ import annotations

import math
from typing import Optional


def segment_sessions(
    timestamps: list[Optional[float]],
    embeddings: list[list[float]],
    gap_seconds: int = 1800,
    sim_threshold: float = 0.55,
) -> list[list[int]]:
    """Centroid-walk segmentation over time-ordered turns.

    Parameters
    ----------
    timestamps:
        Unix timestamps (float seconds) for each turn, in chronological order.
        ``None`` entries are treated as a 0-gap to the previous turn (no time
        split triggered); if the very first entry is None it is treated as 0.
    embeddings:
        L2-normalised embedding vectors for each turn (same length as
        *timestamps*). Harrier 640d output is already normalised.
    gap_seconds:
        Minimum time gap (strict: ``>``, not ``>=``) that forces a new session.
        Defaults to 1800 s (30 minutes), matching the Gemini adapter chunk
        boundary.
    sim_threshold:
        Cosine similarity threshold (strict: ``<`` triggers split). Turns with
        ``cosine(turn_emb, centroid) >= sim_threshold`` join the current
        session; turns below start a new one. Default 0.55 is validated on
        real Gemini data.

    Returns
    -------
    list[list[int]]
        Ordered list of sessions; each session is a list of turn indices
        (into the input lists), contiguous and ascending.  Empty input
        returns ``[]``.
    """
    n = len(timestamps)
    if n == 0:
        return []

    # Initialise first session with turn 0.
    sessions: list[list[int]] = [[0]]

    # Running centroid: unnormalised sum of member embeddings.
    # Copy so we don't mutate the input vector.
    cen: list[float] = list(embeddings[0])

    # Precompute the normalised centroid (used for dot-product comparison).
    cen_norm = _normalise(cen)

    for i in range(1, n):
        # ── Time-gap check ───────────────────────────────────────────────────
        ts_prev = timestamps[i - 1]
        ts_curr = timestamps[i]
        if ts_prev is None or ts_curr is None:
            gap = 0.0
        else:
            gap = float(ts_curr) - float(ts_prev)

        # ── Semantic similarity check ────────────────────────────────────────
        emb_i = embeddings[i]
        sim = _dot(emb_i, cen_norm)

        # ── Decision ─────────────────────────────────────────────────────────
        if gap > gap_seconds or sim < sim_threshold:
            # Start a new session; reset centroid.
            sessions.append([i])
            cen = list(emb_i)
        else:
            # Extend the current session; accumulate centroid.
            sessions[-1].append(i)
            cen = [c + e for c, e in zip(cen, emb_i)]

        # Always renormalise after each step (whether we reset or accumulated).
        cen_norm = _normalise(cen)

    return sessions


# ── Internal helpers ──────────────────────────────────────────────────────────


def _normalise(v: list[float]) -> list[float]:
    """Return an L2-normalised copy of *v*.  Returns *v* unchanged if norm < eps."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-9:
        return list(v)
    inv = 1.0 / norm
    return [x * inv for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two same-length vectors."""
    return sum(x * y for x, y in zip(a, b))
