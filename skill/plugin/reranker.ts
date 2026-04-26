/**
 * TotalReclaw Plugin -- Reranker (thin wrapper around `@totalreclaw/core`).
 *
 * As of rc.22 the plugin no longer ships its own BM25 / RRF / source-weight
 * implementation. All ranking decisions are delegated to the canonical Rust
 * core (`rerankWithConfig`, `cosineSimilarity`, `sourceWeight`,
 * `legacyClaimFallbackWeight`) so plugin / Hermes / MCP runtimes share one
 * source of truth and cannot drift again (rc.18 cosine-gate divergence).
 *
 * The previous client-side pipeline added importance, recency, and MMR
 * signals on top of BM25 + cosine. Those signals are dropped here:
 * core's intent-weighted RRF + source-weighted final score is the
 * canonical Pipeline G + Tier 1 mix that benchmark E13 calibrated. The
 * extra TS-side passes were not part of the validated baseline and were a
 * source of cross-client divergence.
 *
 * Public surface kept for callers (index.ts, semantic-dedup.ts,
 * consolidation.ts, pocv2-e2e-test.ts, v1-taxonomy.test.ts):
 *   - `rerank(query, queryEmbedding, candidates, topK, _legacyWeights, applySourceWeights)`
 *   - `cosineSimilarity(a, b)`
 *   - `getSourceWeight(source)`
 *   - `detectQueryIntent(query)` and `INTENT_WEIGHTS`
 *     (kept as no-ops for callers that pass them in -- core handles
 *      intent-weighting internally now, so the legacy weights argument is
 *      ignored.)
 *   - Types: `RerankerCandidate`, `RerankerResult`, `RankingWeights`,
 *     `QueryIntent`.
 */

import * as core from '@totalreclaw/core';

// ---------------------------------------------------------------------------
// Cosine Similarity (delegated to core WASM)
// ---------------------------------------------------------------------------

export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length === 0 || b.length === 0) return 0;
  if (a.length !== b.length) return 0;
  return core.cosineSimilarity(new Float32Array(a), new Float32Array(b));
}

// ---------------------------------------------------------------------------
// Source-weight lookup (delegated to core)
// ---------------------------------------------------------------------------

export function getSourceWeight(source: string | undefined): number {
  if (!source) return core.legacyClaimFallbackWeight();
  return core.sourceWeight(source);
}

// ---------------------------------------------------------------------------
// Query Intent Detection
//
// Kept as a no-op compat layer so existing call sites that still pass
// INTENT_WEIGHTS[intent] into `rerank()` continue to compile. Core does its
// own intent-weighting internally based on the per-candidate cosine score
// (see `rust/totalreclaw-core/src/reranker.rs` -- `bm25_weight = 0.3 + 0.3 *
// (1 - intent_score)`) so the TS-side weight argument is ignored.
// ---------------------------------------------------------------------------

export type QueryIntent = 'factual' | 'temporal' | 'semantic';

export interface RankingWeights {
  bm25: number;
  cosine: number;
  importance: number;
  recency: number;
}

const TEMPORAL_KEYWORDS = /\b(yesterday|today|last\s+week|last\s+month|recently|recent|latest|ago|when|this\s+week|this\s+month|earlier|before|after|since|during|tonight|morning|afternoon)\b/i;
const FACTUAL_PATTERNS = /^(what|who|where|which|how\s+many|how\s+much|is\s+|are\s+|does\s+|do\s+|did\s+|was\s+|were\s+)\b/i;

export const INTENT_WEIGHTS: Record<QueryIntent, RankingWeights> = {
  factual:  { bm25: 0.40, cosine: 0.20, importance: 0.25, recency: 0.15 },
  temporal: { bm25: 0.15, cosine: 0.20, importance: 0.20, recency: 0.45 },
  semantic: { bm25: 0.20, cosine: 0.35, importance: 0.25, recency: 0.20 },
};

export function detectQueryIntent(query: string): QueryIntent {
  if (TEMPORAL_KEYWORDS.test(query)) return 'temporal';
  if (FACTUAL_PATTERNS.test(query) && query.length < 80) return 'factual';
  return 'semantic';
}

// ---------------------------------------------------------------------------
// Candidate / Result types (cross-runtime stable)
// ---------------------------------------------------------------------------

export interface RerankerCandidate {
  id: string;
  text: string;
  embedding?: number[];
  /** Unused now -- core ignores importance. Kept for API stability. */
  importance?: number;
  /** Unused now -- core ignores recency. Kept for API stability. */
  createdAt?: number;
  /** Memory Taxonomy v1 source ("user" | "user-inferred" | ... ). */
  source?: string;
}

export interface RerankerResult extends RerankerCandidate {
  /** Final fused score from core (post source-weight multiplication). */
  rrfScore: number;
  cosineSimilarity?: number;
  /** Source weight multiplier applied (1.0 if no weighting). */
  sourceWeight?: number;
}

// Core's RankedResult JSON shape (returned by rerankWithConfig).
interface CoreRankedResult {
  id: string;
  text: string;
  score: number;
  bm25_score: number;
  cosine_score: number;
  timestamp: string;
  source_weight?: number;
}

// ---------------------------------------------------------------------------
// Re-ranker (delegates to core::reranker)
// ---------------------------------------------------------------------------

/**
 * Re-rank decrypted candidates by delegating to core's `rerankWithConfig`.
 *
 * @param query              The user's plaintext search query.
 * @param queryEmbedding     Embedding vector for the query.
 * @param candidates         Decrypted candidates with text and optional
 *                           embeddings + v1 source.
 * @param topK               Top-K to return (default 8).
 * @param _legacyWeights     IGNORED -- kept for API stability so existing
 *                           callers still compile. Core handles
 *                           intent-weighting internally based on per-candidate
 *                           cosine scores.
 * @param applySourceWeights When true, multiply the final fused score by
 *                           the v1 source-weight (Retrieval v2 Tier 1).
 *                           Default true at all production call sites.
 */
export function rerank(
  query: string,
  queryEmbedding: number[],
  candidates: RerankerCandidate[],
  topK: number = 8,
  _legacyWeights?: Partial<RankingWeights>,
  applySourceWeights: boolean = false,
): RerankerResult[] {
  if (candidates.length === 0) return [];

  // Build the core::Candidate JSON shape. Core requires:
  //   { id, text, embedding: number[], timestamp: string, source? }
  // Candidates without an embedding pass an empty array; core's cosine
  // similarity returns 0 for that case (see cosine_similarity_f32).
  const coreCandidates = candidates.map((c) => ({
    id: c.id,
    text: c.text,
    embedding: c.embedding ?? [],
    timestamp: c.createdAt != null ? String(c.createdAt) : '',
    ...(c.source ? { source: c.source } : {}),
  }));

  // Empty queryEmbedding is allowed: core will compute cosine = 0 across
  // all candidates and rely entirely on BM25 (intent_score clamps to 0
  // -> bm25_weight = 0.6, cosine_weight = 0.3).
  const queryVec = new Float32Array(queryEmbedding ?? []);
  const candidatesJson = JSON.stringify(coreCandidates);

  const ranked = core.rerankWithConfig(
    query,
    queryVec,
    candidatesJson,
    topK,
    applySourceWeights,
  ) as CoreRankedResult[];

  // Map core results back to RerankerResult, restoring the optional
  // metadata (importance/createdAt/source) so callers that still read those
  // fields keep working.
  const candidateMap = new Map<string, RerankerCandidate>();
  for (const c of candidates) candidateMap.set(c.id, c);

  return ranked.map((r) => {
    const orig = candidateMap.get(r.id);
    return {
      id: r.id,
      text: r.text,
      embedding: orig?.embedding,
      importance: orig?.importance,
      createdAt: orig?.createdAt,
      source: orig?.source,
      rrfScore: r.score,
      cosineSimilarity: r.cosine_score,
      sourceWeight: applySourceWeights ? (r.source_weight ?? 1.0) : undefined,
    };
  });
}
