/**
 * LSH Configuration
 *
 * Validated parameters from TS v0.3 specification.
 */

import { LSHConfig } from '../types';

/**
 * LSH configuration with defaults (32-bit, 20 tables -- matching MCP)
 */
export const LSH_DEFAULTS: LSHConfig = {
  n_bits_per_table: 32,
  n_tables: 20,
  candidate_pool: 3000,
};

/**
 * Calculate optimal candidate pool size based on corpus size
 *
 * Validated data points:
 * - 1,162 memories -> 2,000 candidates = 99.0% recall (34% of corpus)
 * - 8,727 memories -> 3,000 candidates = 93.6% recall (34% of corpus)
 *
 * @param corpusSize - Number of embeddings in corpus
 * @returns Optimal candidate pool size
 */
export function calculateCandidatePool(corpusSize: number): number {
  const MIN_POOL = 2000;
  const MAX_POOL = 10000;

  if (corpusSize < 2000) {
    return MIN_POOL;
  } else if (corpusSize < 10000) {
    // Validated: 8,727 -> 3,000 (34% ratio)
    return Math.max(MIN_POOL, Math.min(4000, Math.floor(corpusSize * 0.35)));
  } else if (corpusSize < 100000) {
    // Estimate: logarithmic scaling for medium corpora
    return Math.min(MAX_POOL, 3000 + Math.floor(Math.log10(corpusSize) * 500));
  } else {
    // Large corpora: cap at 10,000 but consider hierarchical LSH
    return MAX_POOL;
  }
}

/**
 * LSH scaling table for reference
 */
export const LSH_SCALING_TABLE: Array<{
  corpusSize: number;
  candidatePool: number;
  ratio: string;
  expectedRecall: string;
  validated: boolean;
}> = [
  { corpusSize: 1162, candidatePool: 2000, ratio: '172%', expectedRecall: '99.0%', validated: true },
  { corpusSize: 8727, candidatePool: 3000, ratio: '34%', expectedRecall: '93.6%', validated: true },
  { corpusSize: 10000, candidatePool: 3500, ratio: '35%', expectedRecall: '~93%', validated: false },
  { corpusSize: 50000, candidatePool: 5000, ratio: '10%', expectedRecall: '~90%', validated: false },
  { corpusSize: 100000, candidatePool: 6500, ratio: '6.5%', expectedRecall: '~88%', validated: false },
  { corpusSize: 1000000, candidatePool: 10000, ratio: '1%', expectedRecall: '~85%', validated: false },
];

/**
 * Merge user config with defaults
 *
 * @param userConfig - Partial user configuration
 * @returns Complete LSH configuration
 */
export function mergeLSHConfig(userConfig?: Partial<LSHConfig>): LSHConfig {
  if (!userConfig) {
    return { ...LSH_DEFAULTS };
  }

  return {
    n_bits_per_table: userConfig.n_bits_per_table ?? LSH_DEFAULTS.n_bits_per_table,
    n_tables: userConfig.n_tables ?? LSH_DEFAULTS.n_tables,
    candidate_pool: userConfig.candidate_pool ?? LSH_DEFAULTS.candidate_pool,
  };
}
