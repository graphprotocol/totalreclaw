/**
 * Search Reranking Functions
 *
 * Implements BM25 text scoring, cosine similarity, Reciprocal Rank Fusion (RRF),
 * Weighted RRF, and Maximal Marginal Relevance (MMR) for combining multiple
 * ranking signals with diversity.
 *
 * Cosine similarity delegates to the Rust WASM core for performance.
 * BM25, RRF, and higher-order combinators remain in TypeScript because
 * they operate on Maps/objects that are expensive to serialize across the
 * WASM boundary.
 */

import * as wasm from "@totalreclaw/core";

/**
 * Compute cosine similarity between two vectors
 *
 * Delegates to the Rust WASM core for SIMD-style inner-product computation.
 *
 * @param a - First vector
 * @param b - Second vector
 * @returns Similarity score in range [-1, 1]
 */
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length) {
    throw new Error(`Vector length mismatch: ${a.length} vs ${b.length}`);
  }

  if (a.length === 0) return 0;

  // WASM cosineSimilarity takes Float32Array
  const fa = new Float32Array(a);
  const fb = new Float32Array(b);
  return wasm.cosineSimilarity(fa, fb);
}

/**
 * BM25 Parameters
 */
const BM25_K1 = 1.2; // Term frequency saturation parameter
const BM25_B = 0.75; // Length normalization parameter

/**
 * In-memory document statistics for BM25
 */
interface DocumentStats {
  docCount: number;
  avgDocLength: number;
  docLengths: Map<string, number>;
  termDocFreqs: Map<string, number>;
}

/**
 * BM25 Scorer for text relevance
 */
export class BM25Scorer {
  private stats: DocumentStats = {
    docCount: 0,
    avgDocLength: 0,
    docLengths: new Map(),
    termDocFreqs: new Map(),
  };

  /**
   * Tokenize text into terms
   */
  private tokenize(text: string): string[] {
    return text
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s]/gu, ' ')
      .split(/\s+/)
      .filter((t) => t.length > 0);
  }

  /**
   * Index documents for BM25 scoring
   *
   * @param documents - Array of {id, text} documents
   */
  indexDocuments(documents: Array<{ id: string; text: string }>): void {
    this.stats.docCount = documents.length;
    this.stats.docLengths.clear();
    this.stats.termDocFreqs.clear();

    let totalLength = 0;
    const termSeenInDoc = new Map<string, Set<string>>();

    for (const doc of documents) {
      const tokens = this.tokenize(doc.text);
      this.stats.docLengths.set(doc.id, tokens.length);
      totalLength += tokens.length;

      // Track which docs each term appears in
      const seenTerms = new Set<string>();
      for (const token of tokens) {
        if (!seenTerms.has(token)) {
          seenTerms.add(token);
          if (!termSeenInDoc.has(token)) {
            termSeenInDoc.set(token, new Set());
          }
          termSeenInDoc.get(token)!.add(doc.id);
        }
      }
    }

    this.stats.avgDocLength = totalLength / documents.length;

    // Convert to document frequencies
    for (const [term, docs] of termSeenInDoc) {
      this.stats.termDocFreqs.set(term, docs.size);
    }
  }

  /**
   * Compute IDF (Inverse Document Frequency) for a term
   */
  private idf(term: string): number {
    const df = this.stats.termDocFreqs.get(term) || 0;
    if (df === 0) return 0;
    return Math.log(
      (this.stats.docCount - df + 0.5) / (df + 0.5) + 1
    );
  }

  /**
   * Compute BM25 score for a document given a query
   *
   * @param query - Query text
   * @param docId - Document ID (must be indexed)
   * @param docText - Document text (for term frequency calculation)
   * @returns BM25 score
   */
  score(query: string, docId: string, docText: string): number {
    const queryTerms = this.tokenize(query);
    const docTerms = this.tokenize(docText);
    const docLength = this.stats.docLengths.get(docId) || docTerms.length;

    // Count term frequencies in document
    const termFreqs = new Map<string, number>();
    for (const term of docTerms) {
      termFreqs.set(term, (termFreqs.get(term) || 0) + 1);
    }

    let score = 0;
    for (const term of queryTerms) {
      const tf = termFreqs.get(term) || 0;
      if (tf === 0) continue;

      const idf = this.idf(term);
      const numerator = tf * (BM25_K1 + 1);
      const denominator =
        tf + BM25_K1 * (1 - BM25_B + BM25_B * (docLength / this.stats.avgDocLength));

      score += idf * (numerator / denominator);
    }

    return score;
  }

  /**
   * Quick BM25 score without indexing (uses document text directly)
   *
   * Less accurate but useful for one-off scoring.
   *
   * @param query - Query text
   * @param doc - Document text
   * @returns Approximate BM25 score
   */
  quickScore(query: string, doc: string): number {
    const queryTerms = this.tokenize(query);
    const docTerms = this.tokenize(doc);

    // Simple term frequency approach
    const termFreqs = new Map<string, number>();
    for (const term of docTerms) {
      termFreqs.set(term, (termFreqs.get(term) || 0) + 1);
    }

    let score = 0;
    const docLength = docTerms.length;
    const avgLength = docLength; // Assume average is same as doc length

    for (const term of queryTerms) {
      const tf = termFreqs.get(term) || 0;
      if (tf === 0) continue;

      // Simplified IDF approximation
      const idf = 1; // Would need corpus stats for real IDF

      const numerator = tf * (BM25_K1 + 1);
      const denominator =
        tf + BM25_K1 * (1 - BM25_B + BM25_B * (docLength / avgLength));

      score += idf * (numerator / denominator);
    }

    return score;
  }

  /**
   * Get the number of indexed documents
   */
  getDocCount(): number {
    return this.stats.docCount;
  }

  /**
   * Get average document length
   */
  getAvgDocLength(): number {
    return this.stats.avgDocLength;
  }
}

/**
 * Reciprocal Rank Fusion (RRF) for combining rankings
 *
 * RRF is a simple and effective method for merging multiple ranked lists.
 * Formula: RRF(d) = sum(1 / (k + rank_i(d))) for all rankings i
 *
 * @param rankings - Array of ranked item arrays (each array is a ranking)
 * @param k - RRF constant (default: 60)
 * @returns Map of item -> combined RRF score
 */
export function rrfFusion(
  rankings: Array<Array<{ id: string; score?: number }>>,
  k: number = 60
): Map<string, number> {
  const scores = new Map<string, number>();

  for (const ranking of rankings) {
    for (let rank = 0; rank < ranking.length; rank++) {
      const item = ranking[rank];
      const currentScore = scores.get(item.id) || 0;
      scores.set(item.id, currentScore + 1 / (k + rank + 1));
    }
  }

  return scores;
}

/**
 * Normalize scores to [0, 1] range
 *
 * @param scores - Array of scores
 * @returns Normalized scores
 */
export function normalizeScores(scores: number[]): number[] {
  if (scores.length === 0) return [];

  const min = Math.min(...scores);
  const max = Math.max(...scores);
  const range = max - min;

  if (range === 0) {
    return scores.map(() => 0.5);
  }

  return scores.map((s) => (s - min) / range);
}

/**
 * Combine multiple score signals with weights
 *
 * @param signals - Array of {id, score} arrays
 * @param weights - Weight for each signal (must sum to 1)
 * @returns Combined and sorted results
 */
export function combineSignals(
  signals: Array<Map<string, number>>,
  weights: number[]
): Array<{ id: string; score: number }> {
  if (signals.length !== weights.length) {
    throw new Error('Number of signals must match number of weights');
  }

  const weightSum = weights.reduce((a, b) => a + b, 0);
  if (Math.abs(weightSum - 1) > 0.001) {
    throw new Error(`Weights must sum to 1, got ${weightSum}`);
  }

  // Get all unique IDs
  const allIds = new Set<string>();
  for (const signal of signals) {
    for (const id of signal.keys()) {
      allIds.add(id);
    }
  }

  // Normalize each signal
  const normalizedSignals = signals.map((signal) => {
    const values = Array.from(signal.values());
    const normalized = normalizeScores(values);
    const idArray = Array.from(signal.keys());
    const result = new Map<string, number>();
    for (let i = 0; i < idArray.length; i++) {
      result.set(idArray[i], normalized[i]);
    }
    return result;
  });

  // Combine with weights
  const combined: Array<{ id: string; score: number }> = [];

  for (const id of allIds) {
    let score = 0;
    for (let i = 0; i < signals.length; i++) {
      const signalScore = normalizedSignals[i].get(id) || 0;
      score += weights[i] * signalScore;
    }
    combined.push({ id, score });
  }

  // Sort by score descending
  combined.sort((a, b) => b.score - a.score);

  return combined;
}

/**
 * Default BM25 scorer instance for convenience
 */
export const bm25Scorer = new BM25Scorer();

/**
 * Compute BM25 score using the default scorer
 *
 * @param query - Query text
 * @param doc - Document text
 * @returns BM25 score
 */
export function bm25Score(query: string, doc: string): number {
  return bm25Scorer.quickScore(query, doc);
}

// ---------------------------------------------------------------------------
// Weighted Reciprocal Rank Fusion
// ---------------------------------------------------------------------------

/**
 * Weighted Reciprocal Rank Fusion for combining rankings with per-signal weights.
 *
 * Like standard RRF, but each ranking list's contribution is multiplied by
 * its weight, allowing callers to emphasize or de-emphasize specific signals.
 *
 * @param rankings - Array of ranked item arrays (each array is a ranking, sorted by score desc)
 * @param weights  - Weight for each ranking list (same length as rankings)
 * @param k        - RRF smoothing constant (default 60)
 * @returns Map of item id -> weighted RRF score
 */
export function weightedRrfFusion(
  rankings: Array<Array<{ id: string; score?: number }>>,
  weights: number[],
  k: number = 60
): Map<string, number> {
  const scores = new Map<string, number>();

  for (let r = 0; r < rankings.length; r++) {
    const w = weights[r] ?? 1;
    const ranking = rankings[r];
    for (let rank = 0; rank < ranking.length; rank++) {
      const item = ranking[rank];
      const currentScore = scores.get(item.id) || 0;
      scores.set(item.id, currentScore + w * (1 / (k + rank + 1)));
    }
  }

  return scores;
}

// ---------------------------------------------------------------------------
// MMR (Maximal Marginal Relevance)
// ---------------------------------------------------------------------------

/**
 * Apply Maximal Marginal Relevance to promote diversity in results.
 *
 * MMR re-orders a ranked list of candidates so that highly similar candidates
 * are spread out. The algorithm greedily selects the candidate that maximizes:
 *
 *   MMR(d) = lambda * relevance(d) - (1 - lambda) * max_sim(d, selected)
 *
 * where:
 *   - relevance(d) = position-based score (1.0 for first, linearly decreasing)
 *   - max_sim(d, selected) = max cosine similarity between d and any already
 *     selected candidate (0 if no embeddings available)
 *
 * @param candidates - Candidates in relevance order (best first), with optional embeddings
 * @param lambda     - Trade-off between relevance and diversity (default 0.7)
 * @param topK       - Number of results to return (default 8)
 * @returns          - Re-ordered candidates with diversity
 */
export function applyMMR<T extends { id: string; embedding?: number[] }>(
  candidates: T[],
  lambda: number = 0.7,
  topK: number = 8,
): T[] {
  if (candidates.length === 0) return [];
  if (candidates.length <= 1) return candidates.slice(0, topK);

  const remaining = candidates.map((c, i) => ({ candidate: c, index: i }));
  const selected: T[] = [];
  const n = candidates.length;

  while (selected.length < topK && remaining.length > 0) {
    let bestIdx = -1;
    let bestMMR = -Infinity;

    for (let i = 0; i < remaining.length; i++) {
      const { candidate, index } = remaining[i];

      // Relevance: linear decay from 1.0 (first) to near 0 (last)
      const relevance = 1.0 - index / n;

      // Max similarity to any already-selected candidate
      let maxSim = 0;
      if (candidate.embedding && candidate.embedding.length > 0) {
        for (const sel of selected) {
          if (sel.embedding && sel.embedding.length > 0) {
            const sim = cosineSimilarity(candidate.embedding, sel.embedding);
            if (sim > maxSim) maxSim = sim;
          }
        }
      }

      const mmr = lambda * relevance - (1 - lambda) * maxSim;
      if (mmr > bestMMR) {
        bestMMR = mmr;
        bestIdx = i;
      }
    }

    if (bestIdx >= 0) {
      selected.push(remaining[bestIdx].candidate);
      remaining.splice(bestIdx, 1);
    } else {
      break;
    }
  }

  return selected;
}

// ---------------------------------------------------------------------------
// WASM-backed full rerank pipeline
// ---------------------------------------------------------------------------

/**
 * Result from the WASM rerank pipeline.
 */
export interface WasmRankedResult {
  id: string;
  text: string;
  score: number;
  cosine_score: number;
  bm25_score: number;
  decay_score: number;
}

/**
 * Rerank candidates using the Rust WASM BM25 + Cosine + RRF fusion pipeline.
 *
 * This is the high-performance path for managed-service search. It takes
 * decrypted candidates and returns top-K ranked results in a single WASM call.
 *
 * @param query - Search query text
 * @param queryEmbedding - Query embedding vector
 * @param candidates - Array of {id, text, embedding, timestamp} candidates
 * @param topK - Number of results to return
 * @returns Ranked results from WASM
 */
export function wasmRerank(
  query: string,
  queryEmbedding: number[],
  candidates: Array<{ id: string; text: string; embedding: number[]; timestamp?: string }>,
  topK: number,
): WasmRankedResult[] {
  if (candidates.length === 0) return [];

  const qe = new Float32Array(queryEmbedding);
  const candidatesJson = JSON.stringify(candidates);
  return wasm.rerank(query, qe, candidatesJson, topK) as WasmRankedResult[];
}
