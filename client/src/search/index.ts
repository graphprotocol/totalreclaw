/**
 * TotalReclaw Search Module
 *
 * Client-side search reranking with BM25, cosine similarity, and RRF fusion.
 */

export {
  cosineSimilarity,
  BM25Scorer,
  rrfFusion,
  normalizeScores,
  combineSignals,
  bm25Score,
  bm25Scorer,
  wasmRerank,
} from './rerank';

export type { WasmRankedResult } from './rerank';

export {
  calculateDecayScore,
  exponentialDecay,
  halfLifeToDecayRate,
  daysUntilThreshold,
  boostOnAccess,
  multiFactorDecay,
  searchTimeDecay,
  batchUpdateDecayScores,
  type DecayParams,
} from './decay';
