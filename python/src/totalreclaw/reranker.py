"""TotalReclaw Hermes (Python client) -- reranker wrapper.

As of rc.22 the Python client no longer ships its own BM25 / RRF / source-weight
implementation. All ranking decisions are delegated to the canonical Rust
core via `totalreclaw_core.rerank_with_config`, `totalreclaw_core.cosine_similarity`,
`totalreclaw_core.source_weight`, and `totalreclaw_core.legacy_claim_fallback_weight`.
This guarantees plugin / Hermes / MCP runtimes share one ranker source of truth
and cannot drift again (rc.18 cosine-gate divergence root cause).

The previous Python pipeline added importance, recency, and MMR signals on
top of BM25 + cosine. Those signals are dropped here: core's intent-weighted
RRF + source-weighted final score is the canonical Pipeline G + Tier 1 mix
that benchmark E13 calibrated. The extra Python-side passes were not part
of the validated baseline and were a source of cross-client divergence.

Public surface kept for callers:
  - ``rerank(query, query_embedding, candidates, top_k, apply_source_weights)``
    -- called by ``operations.py``
  - ``cosine_similarity(a, b)`` -- called by ``agent/lifecycle.py``
  - ``source_weight(source)`` / ``LEGACY_CLAIM_FALLBACK_WEIGHT``
  - Dataclasses: ``RerankerCandidate``, ``RerankerResult`` -- ``RerankerResult``
    is re-exported by ``client.py`` and read by ``agent/contradiction.py``

Core owns intent-weighting now (per-candidate BM25/cosine ratio), so the old
client-side ``detect_query_intent`` / ``INTENT_WEIGHTS`` / ``RankingWeights``
compat surface has been removed -- no internal caller used it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import totalreclaw_core


# ---------------------------------------------------------------------------
# Cosine Similarity (delegated to core)
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity by delegating to ``totalreclaw_core``.

    Returns 0 if either vector is empty. Equal-length vectors are required;
    we truncate to ``min(len(a), len(b))`` when lengths differ to match the
    historical Python behaviour.
    """
    if not a or not b:
        return 0.0
    length = min(len(a), len(b))
    a_trunc = [float(x) for x in a[:length]]
    b_trunc = [float(x) for x in b[:length]]
    return totalreclaw_core.cosine_similarity(a_trunc, b_trunc)


# ---------------------------------------------------------------------------
# Source weight (delegated to core)
# ---------------------------------------------------------------------------

LEGACY_CLAIM_FALLBACK_WEIGHT: float = float(totalreclaw_core.legacy_claim_fallback_weight())


def source_weight(source: Optional[str]) -> float:
    """Return the v1 source weight from core.

    ``None`` (or empty string) returns the legacy-claim fallback weight.
    Unknown source strings are routed through core's ``MemorySource::from_str_lossy``
    which lands them at ``user-inferred`` (0.9) -- the canonical safe default.
    """
    if not source:
        return LEGACY_CLAIM_FALLBACK_WEIGHT
    return float(totalreclaw_core.source_weight(source))


# ---------------------------------------------------------------------------
# Data classes (cross-runtime stable surface)
# ---------------------------------------------------------------------------

@dataclass
class RerankerCandidate:
    id: str
    text: str
    embedding: Optional[list[float]] = None
    importance: Optional[float] = None
    created_at: Optional[float] = None  # Unix timestamp (seconds)
    category: str = "fact"
    #: v1 provenance source ("user", "user-inferred", "assistant",
    #: "external", "derived"). When None, the candidate is treated as a
    #: pre-v1 legacy blob and receives ``LEGACY_CLAIM_FALLBACK_WEIGHT``.
    source: Optional[str] = None
    #: Filtered claim-metadata passthrough (#425) — e.g. import_source /
    #: session_id / subtype. Never sent to core; rejoined by id after
    #: ``rerank_with_config`` so read surfaces can expose provenance.
    metadata: Optional[dict] = None


@dataclass
class RerankerResult:
    """Result from the reranker -- mirrors RerankerCandidate fields plus scores."""
    id: str
    text: str
    embedding: Optional[list[float]] = None
    importance: Optional[float] = None
    created_at: Optional[float] = None
    category: str = "fact"
    rrf_score: float = 0.0
    cosine_sim: Optional[float] = None
    source: Optional[str] = None
    source_weight: Optional[float] = None
    metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# Re-ranker (delegates to core::reranker)
# ---------------------------------------------------------------------------

def rerank(
    query: str,
    query_embedding: Optional[list[float]],
    candidates: list[RerankerCandidate],
    top_k: int = 8,
    apply_source_weights: bool = False,
) -> list[RerankerResult]:
    """Re-rank candidates by routing through ``totalreclaw_core.rerank_with_config``.

    Core handles intent-weighting itself based on the per-candidate cosine
    score. Importance / recency / MMR signals are no longer applied
    client-side; if you need those, contribute them to core::reranker first.
    """
    if not candidates:
        return []

    # Build the core::Candidate JSON shape:
    #   { id, text, embedding, timestamp, source? }
    # ``embedding`` defaults to [] for candidates without an embedding;
    # core's cosine_similarity_f32 returns 0 for that case.
    core_candidates: list[dict] = []
    for c in candidates:
        core_obj: dict = {
            "id": c.id,
            "text": c.text,
            "embedding": list(c.embedding) if c.embedding else [],
            "timestamp": str(c.created_at) if c.created_at is not None else "",
        }
        if c.source:
            core_obj["source"] = c.source
        core_candidates.append(core_obj)

    candidates_json = json.dumps(core_candidates)
    query_vec = [float(x) for x in (query_embedding or [])]

    raw = totalreclaw_core.rerank_with_config(
        query,
        query_vec,
        candidates_json,
        top_k,
        apply_source_weights,
    )
    # rerank_with_config returns a JSON string of RankedResult objects.
    ranked: list[dict] = json.loads(raw)

    # Restore the optional metadata (importance / created_at / category /
    # embedding / source) so consumers like agent/contradiction.py keep
    # working without changes.
    by_id = {c.id: c for c in candidates}
    out: list[RerankerResult] = []
    for r in ranked:
        orig = by_id.get(r["id"])
        out.append(
            RerankerResult(
                id=r["id"],
                text=r["text"],
                embedding=orig.embedding if orig else None,
                importance=orig.importance if orig else None,
                created_at=orig.created_at if orig else None,
                category=orig.category if orig else "fact",
                rrf_score=float(r.get("score", 0.0)),
                cosine_sim=float(r.get("cosine_score", 0.0)),
                source=orig.source if orig else None,
                source_weight=float(r["source_weight"])
                if apply_source_weights and "source_weight" in r
                else (1.0 if apply_source_weights else None),
                metadata=orig.metadata if orig else None,
            )
        )

    return out
