"""
TotalReclaw Python Client — Client-Side Re-Ranker

Replaces naive word-overlap scoring with a proper ranking pipeline:
  1. Okapi BM25 — term frequency / inverse document frequency
  2. Cosine similarity — between query and fact embeddings
  3. Importance — normalized importance score (0-1)
  4. Recency — time-decay with 1-week half-life
  5. Weighted RRF (Reciprocal Rank Fusion) — combines all ranking lists
  6. MMR (Maximal Marginal Relevance) — promotes diversity in results

Cosine similarity delegates to the totalreclaw_core Rust/PyO3 module.
The full rerank pipeline remains in Python because the Rust core's rerank()
uses a simpler 2-signal model (BM25 + Cosine), while the Python client uses
a richer 4-signal pipeline (BM25 + Cosine + Importance + Recency) with
MMR diversity and stemming.

Port of the canonical TypeScript implementation at mcp/src/subgraph/reranker.ts.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import Stemmer  # PyStemmer
import totalreclaw_core

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

STOP_WORDS: frozenset[str] = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "for",
    "from", "had", "has", "have", "he", "her", "him", "his", "how", "if",
    "in", "into", "is", "it", "its", "me", "my", "no", "not", "of", "on",
    "or", "our", "out", "she", "so", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "up", "us", "was", "we",
    "were", "what", "when", "where", "which", "who", "whom", "why", "will",
    "with", "you", "your",
])

# Module-level stemmer instance (PyStemmer is thread-safe for reads)
_stemmer = Stemmer.Stemmer("english")


def tokenize(text: str, remove_stop_words: bool = True) -> list[str]:
    """Tokenize a text string for BM25 scoring.

    Pipeline:
      1. Lowercase
      2. Remove punctuation (replace non-letter/non-digit/non-whitespace and
         underscores with space — matches the TS ``[^\\p{L}\\p{N}\\s]`` regex)
      3. Split on whitespace
      4. Filter tokens shorter than 2 characters
      5. Optionally remove stop words
      6. Stem with Porter stemmer (via PyStemmer)
    """
    lowered = text.lower()
    # Remove punctuation: replace anything that isn't a Unicode letter, digit,
    # or whitespace with a space. Also replace underscores.
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    cleaned = cleaned.replace("_", " ")
    tokens = cleaned.split()
    tokens = [t for t in tokens if len(t) >= 2]

    if remove_stop_words:
        tokens = [t for t in tokens if t not in STOP_WORDS]

    return [_stemmer.stemWord(t) for t in tokens]


# ---------------------------------------------------------------------------
# BM25 Scoring (Okapi BM25)
# ---------------------------------------------------------------------------

def bm25_score(
    query_terms: list[str],
    doc_terms: list[str],
    avg_doc_len: float,
    doc_count: int,
    term_doc_freqs: dict[str, int],
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    """Compute the Okapi BM25 score for a single document against a query.

    Formula:
      score = SUM_i IDF(qi) * (f(qi,D) * (k1+1)) /
              (f(qi,D) + k1 * (1 - b + b * |D|/avgdl))

    where:
      IDF(qi) = ln((N - n(qi) + 0.5) / (n(qi) + 0.5) + 1)
    """
    if not doc_terms or avg_doc_len == 0 or doc_count == 0:
        return 0.0

    # Count term frequencies in this document
    doc_tf: dict[str, int] = {}
    for term in doc_terms:
        doc_tf[term] = doc_tf.get(term, 0) + 1

    doc_len = len(doc_terms)
    score = 0.0

    for qi in query_terms:
        freq = doc_tf.get(qi, 0)
        if freq == 0:
            continue

        nqi = term_doc_freqs.get(qi, 0)

        # IDF with Robertson-Walker floor
        idf = math.log((doc_count - nqi + 0.5) / (nqi + 0.5) + 1)

        # TF saturation with length normalization
        tf_norm = (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * doc_len / avg_doc_len))

        score += idf * tf_norm

    return score


# ---------------------------------------------------------------------------
# Cosine Similarity
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Delegates to totalreclaw_core (Rust/PyO3) for the actual computation.
    Returns dot(a, b) / (||a|| * ||b||).
    Returns 0 if either vector has zero magnitude.

    Note: The Rust implementation requires equal-length vectors and uses f32
    precision. We handle empty/mismatched-length edge cases in Python before
    delegating.
    """
    if not a or not b:
        return 0.0

    # The Rust core requires equal-length vectors; truncate to min length
    # (matches the previous Python behavior of using min(len(a), len(b)))
    length = min(len(a), len(b))
    a_trunc = [float(x) for x in a[:length]]
    b_trunc = [float(x) for x in b[:length]]

    return totalreclaw_core.cosine_similarity(a_trunc, b_trunc)


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion (RRF)
# ---------------------------------------------------------------------------

@dataclass
class RankedItem:
    id: str
    score: float


def rrf_fuse(rankings: list[list[RankedItem]], k: int = 60) -> list[RankedItem]:
    """Fuse multiple ranking lists using Reciprocal Rank Fusion.

    For each document d appearing in any ranking list:
      rrfScore(d) = SUM_i 1 / (k + rank_i(d))

    where rank_i(d) is the 1-based rank of d in the i-th list.
    """
    fused_scores: dict[str, float] = {}

    for ranking in rankings:
        for rank, item in enumerate(ranking):
            contribution = 1.0 / (k + rank + 1)  # rank is 0-based, formula uses 1-based
            fused_scores[item.id] = fused_scores.get(item.id, 0.0) + contribution

    fused = [RankedItem(id=id_, score=score) for id_, score in fused_scores.items()]
    fused.sort(key=lambda x: x.score, reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Weighted Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def weighted_rrf_fuse(
    rankings: list[list[RankedItem]],
    weights: list[float],
    k: int = 60,
) -> list[RankedItem]:
    """Fuse multiple ranking lists using Weighted Reciprocal Rank Fusion.

    Like standard RRF, but each ranking list's contribution is multiplied by
    its weight.
    """
    fused_scores: dict[str, float] = {}

    for r, ranking in enumerate(rankings):
        w = weights[r] if r < len(weights) else 1.0
        for rank, item in enumerate(ranking):
            contribution = w * (1.0 / (k + rank + 1))
            fused_scores[item.id] = fused_scores.get(item.id, 0.0) + contribution

    fused = [RankedItem(id=id_, score=score) for id_, score in fused_scores.items()]
    fused.sort(key=lambda x: x.score, reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Ranking Weights & Query Intent
# ---------------------------------------------------------------------------

@dataclass
class RankingWeights:
    bm25: float = 0.25
    cosine: float = 0.25
    importance: float = 0.25
    recency: float = 0.25


DEFAULT_WEIGHTS = RankingWeights()

QueryIntent = str  # Literal["factual", "temporal", "semantic"]

INTENT_WEIGHTS: dict[str, RankingWeights] = {
    "factual":  RankingWeights(bm25=0.40, cosine=0.20, importance=0.25, recency=0.15),
    "temporal":  RankingWeights(bm25=0.15, cosine=0.20, importance=0.20, recency=0.45),
    "semantic": RankingWeights(bm25=0.20, cosine=0.35, importance=0.25, recency=0.20),
}

_TEMPORAL_KEYWORDS = re.compile(
    r"\b(yesterday|today|last\s+week|last\s+month|recently|recent|latest|ago|"
    r"when|this\s+week|this\s+month|earlier|before|after|since|during|tonight|"
    r"morning|afternoon)\b",
    re.IGNORECASE,
)

_FACTUAL_PATTERNS = re.compile(
    r"^(what|who|where|which|how\s+many|how\s+much|is\s+|are\s+|does\s+|do\s+|"
    r"did\s+|was\s+|were\s+)\b",
    re.IGNORECASE,
)


def detect_query_intent(query: str) -> QueryIntent:
    """Classify a query into one of three intent types using lightweight heuristics.

    Temporal is checked first so "What did we discuss yesterday?" -> temporal.
    """
    if _TEMPORAL_KEYWORDS.search(query):
        return "temporal"
    if _FACTUAL_PATTERNS.search(query) and len(query) < 80:
        return "factual"
    return "semantic"


# ---------------------------------------------------------------------------
# Data Classes
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
    #: pre-v1 legacy blob and receives the legacy-claim fallback weight.
    source: Optional[str] = None


@dataclass
class RerankerResult:
    """Result from the reranker — extends RerankerCandidate with scores."""
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


# ---------------------------------------------------------------------------
# Recency Scoring
# ---------------------------------------------------------------------------

def _recency_score(created_at: float) -> float:
    """Compute a recency score with a 1-week half-life.

    Score = 1 / (1 + hours_since_creation / 168)

    A fact created just now scores ~1.0, one week ago scores 0.5,
    two weeks ago scores ~0.33.
    """
    now_seconds = time.time()
    hours_since = (now_seconds - created_at) / 3600.0
    return 1.0 / (1.0 + hours_since / 168.0)


# ---------------------------------------------------------------------------
# MMR (Maximal Marginal Relevance)
# ---------------------------------------------------------------------------

def apply_mmr(
    candidates: list[RerankerResult],
    lam: float = 0.7,
    top_k: int = 8,
) -> list[RerankerResult]:
    """Apply Maximal Marginal Relevance to promote diversity in results.

    MMR re-orders a ranked list so highly similar candidates are spread out.
    The algorithm greedily selects the candidate that maximizes:

      MMR(d) = lambda * relevance(d) - (1 - lambda) * max_sim(d, selected)

    where:
      - relevance(d) = linear decay from 1.0 (first) to near 0 (last)
      - max_sim(d, selected) = max cosine similarity to any already-selected
        candidate (0 if no embeddings available)
    """
    if len(candidates) <= 1:
        return candidates[:top_k]

    remaining = [(c, i) for i, c in enumerate(candidates)]
    selected: list[RerankerResult] = []
    n = len(candidates)

    while len(selected) < top_k and remaining:
        best_idx = -1
        best_mmr = -math.inf

        for i, (candidate, original_index) in enumerate(remaining):
            # Relevance: linear decay from 1.0 (first) to near 0 (last)
            relevance = 1.0 - original_index / n

            # Max similarity to any already-selected candidate
            max_sim = 0.0
            if candidate.embedding and len(candidate.embedding) > 0:
                for sel in selected:
                    if sel.embedding and len(sel.embedding) > 0:
                        sim = cosine_similarity(candidate.embedding, sel.embedding)
                        if sim > max_sim:
                            max_sim = sim

            mmr = lam * relevance - (1 - lam) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i

        if best_idx >= 0:
            selected.append(remaining[best_idx][0])
            remaining.pop(best_idx)
        else:
            break

    return selected


# ---------------------------------------------------------------------------
# Combined Re-Ranker
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Retrieval v2 Tier 1 — source-weighted reranking (v1 taxonomy)
# ---------------------------------------------------------------------------

#: Weight for a legacy (pre-v1) candidate whose source is unknown.
#: Mirrors ``reranker::LEGACY_CLAIM_FALLBACK_WEIGHT`` in the Rust core.
LEGACY_CLAIM_FALLBACK_WEIGHT: float = 0.85


def source_weight(source: Optional[str]) -> float:
    """Return the Tier 1 provenance weight for a given v1 source string.

    Unknown / missing source falls back to the legacy weight (0.85) so a
    pre-v1 blob does not get nuked at retrieval time.

    Delegates to :func:`totalreclaw_core.source_weight` when available so
    the weight table stays in lockstep with the Rust core; the pure-Python
    fallback mirrors the table in ``rust/totalreclaw-core/src/reranker.rs``.
    """
    if source is None:
        return LEGACY_CLAIM_FALLBACK_WEIGHT
    try:
        return float(totalreclaw_core.source_weight(source))
    except Exception:
        # Fallback — keep in sync with ``MemorySource`` → weight table.
        table = {
            "user": 1.0,
            "user-inferred": 0.9,
            "external": 0.7,
            "derived": 0.7,
            "assistant": 0.55,
        }
        return table.get(source, 0.9)  # unknown ≈ user-inferred


def rerank(
    query: str,
    query_embedding: Optional[list[float]],
    candidates: list[RerankerCandidate],
    top_k: int = 8,
    weights: Optional[RankingWeights] = None,
    apply_source_weights: bool = False,
) -> list[RerankerResult]:
    """Re-rank decrypted candidates using BM25 + Cosine + Importance + Recency
    with Weighted RRF fusion and MMR diversity.

    Pipeline:
      1. Tokenize query and all candidate texts
      2. Build corpus statistics (term doc freqs, avg doc length)
      3. Score each candidate with BM25
      4. Score each candidate with cosine similarity (if embedding available)
      5. Score each candidate by importance
      6. Score each candidate by recency
      7. Fuse all rankings with weighted RRF
      8. Apply MMR for diversity
      9. Return top-k candidates sorted by fused score

    Backward compatibility:
      - Candidates without embeddings get cosine score = 0 (excluded from
        cosine ranking list). They can still rank well via other signals.
      - If NO candidates have embeddings, cosine ranking is omitted.
      - Candidates without importance get neutral score (0.5).
      - Candidates without created_at get neutral recency score (0.5).
    """
    if not candidates:
        return []

    # Merge caller weights with defaults
    w = weights if weights is not None else RankingWeights()

    # --- Step 1: Tokenize ---
    query_terms = tokenize(query)
    candidate_terms = [tokenize(c.text) for c in candidates]

    # --- Step 2: Corpus statistics ---
    doc_count = len(candidates)
    total_doc_len = 0
    term_doc_freqs: dict[str, int] = {}

    for terms in candidate_terms:
        total_doc_len += len(terms)
        unique_terms = set(terms)
        for term in unique_terms:
            term_doc_freqs[term] = term_doc_freqs.get(term, 0) + 1

    avg_doc_len = total_doc_len / doc_count if doc_count > 0 else 0.0

    # --- Step 3: BM25 scores ---
    bm25_ranking: list[RankedItem] = []
    for i, c in enumerate(candidates):
        score = bm25_score(query_terms, candidate_terms[i], avg_doc_len, doc_count, term_doc_freqs)
        bm25_ranking.append(RankedItem(id=c.id, score=score))
    bm25_ranking.sort(key=lambda x: x.score, reverse=True)

    # --- Step 4: Cosine similarity scores ---
    cosine_scores: dict[str, float] = {}
    cosine_ranking: list[RankedItem] = []
    for c in candidates:
        if c.embedding and len(c.embedding) > 0:
            score = cosine_similarity(query_embedding, c.embedding)
            cosine_scores[c.id] = score
            cosine_ranking.append(RankedItem(id=c.id, score=score))
    cosine_ranking.sort(key=lambda x: x.score, reverse=True)

    # --- Step 5: Importance ranking ---
    importance_ranking: list[RankedItem] = [
        RankedItem(id=c.id, score=c.importance if c.importance is not None else 0.5)
        for c in candidates
    ]
    importance_ranking.sort(key=lambda x: x.score, reverse=True)

    # --- Step 6: Recency ranking ---
    recency_ranking: list[RankedItem] = [
        RankedItem(
            id=c.id,
            score=_recency_score(c.created_at) if c.created_at is not None else 0.5,
        )
        for c in candidates
    ]
    recency_ranking.sort(key=lambda x: x.score, reverse=True)

    # --- Step 7: Weighted RRF fusion ---
    rankings: list[list[RankedItem]] = [bm25_ranking]
    rank_weights: list[float] = [w.bm25]

    if cosine_ranking:
        rankings.append(cosine_ranking)
        rank_weights.append(w.cosine)

    rankings.append(importance_ranking)
    rank_weights.append(w.importance)

    rankings.append(recency_ranking)
    rank_weights.append(w.recency)

    fused = weighted_rrf_fuse(rankings, rank_weights)

    # --- Step 8: Build result objects with scores ---
    candidate_map = {c.id: c for c in candidates}

    rrf_results: list[RerankerResult] = []
    for item in fused:
        c = candidate_map.get(item.id)
        if c:
            # Retrieval v2 Tier 1: multiply the fused RRF score by the v1
            # source weight when requested. Candidates without a v1 source
            # (pre-v1 legacy blobs) receive LEGACY_CLAIM_FALLBACK_WEIGHT.
            sw = source_weight(c.source) if apply_source_weights else None
            final_score = item.score * sw if sw is not None else item.score
            rrf_results.append(RerankerResult(
                id=c.id,
                text=c.text,
                embedding=c.embedding,
                importance=c.importance,
                created_at=c.created_at,
                category=c.category,
                rrf_score=final_score,
                cosine_sim=cosine_scores.get(c.id),
                source=c.source,
                source_weight=sw,
            ))

    # Re-sort after the source-weight multiply so top-k reflects the
    # weighted order, not the pre-weight order.
    if apply_source_weights:
        rrf_results.sort(key=lambda r: r.rrf_score, reverse=True)

    # --- Step 9: Apply MMR for diversity, then return top-k ---
    return apply_mmr(rrf_results, lam=0.7, top_k=top_k)
