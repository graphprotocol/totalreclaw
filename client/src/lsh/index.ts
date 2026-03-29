/**
 * TotalReclaw LSH Module
 *
 * Locality-Sensitive Hashing for approximate nearest neighbor search
 * while preserving privacy.
 */

export { LSHHasher, hammingDistance, estimateSimilarity } from './hyperplane';
export {
  LSH_DEFAULTS,
  calculateCandidatePool,
  LSH_SCALING_TABLE,
  mergeLSHConfig,
} from './config';
