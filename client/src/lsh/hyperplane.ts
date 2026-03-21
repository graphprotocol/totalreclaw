/**
 * Random Hyperplane LSH Implementation
 *
 * Implements locality-sensitive hashing for cosine similarity using
 * random hyperplanes. This allows approximate nearest neighbor search
 * while preserving privacy (the server only sees bucket IDs, not vectors).
 */

import * as crypto from 'crypto';
import { LSHConfig, TotalReclawError, TotalReclawErrorCode } from '../types';
import { mergeLSHConfig } from './config';

/**
 * LSH Index using Random Hyperplane method
 *
 * The algorithm works by:
 * 1. Generate n_tables sets of n_bits random hyperplanes
 * 2. For each vector, compute which side of each hyperplane it falls on (+ or -)
 * 3. This gives n_bits binary digits, forming a bucket ID per table
 * 4. Similar vectors will likely land in the same buckets
 */
export class LSHIndex {
  private config: Required<LSHConfig>;
  private hyperplanes: Array<Array<Float64Array>> = [];
  private embeddingDim: number = 1024; // Default for Qwen3-Embedding-0.6B
  private isBuilt: boolean = false;

  /**
   * Create a new LSH index
   *
   * @param config - LSH configuration parameters
   * @param seed - Optional seed for reproducible hyperplane generation
   */
  constructor(config?: Partial<LSHConfig>, seed?: number) {
    this.config = {
      n_bits_per_table: config?.n_bits_per_table ?? 64,
      n_tables: config?.n_tables ?? 12,
      candidate_pool: config?.candidate_pool ?? 3000,
    } as Required<LSHConfig>;

    if (seed !== undefined) {
      this.seedRandom(seed);
    }
  }

  /**
   * Seed the random number generator for reproducible hyperplanes
   *
   * Note: This uses a simple seeded PRNG for determinism.
   * For production, consider using a more robust seeded RNG.
   */
  private seedRandom(seed: number): () => number {
    let s = seed;
    return () => {
      s = (s * 1103515245 + 12345) & 0x7fffffff;
      return s / 0x7fffffff;
    };
  }

  /**
   * Generate random hyperplanes
   *
   * @param dim - Embedding dimension
   * @param seed - Optional seed for reproducibility
   */
  private generateHyperplanes(dim: number, seed?: number): void {
    this.embeddingDim = dim;
    this.hyperplanes = [];

    const random = seed !== undefined ? this.seedRandom(seed) : Math.random;

    for (let t = 0; t < this.config.n_tables; t++) {
      const tableHyperplanes: Array<Float64Array> = [];

      for (let b = 0; b < this.config.n_bits_per_table; b++) {
        // Generate random unit vector (hyperplane normal)
        const hyperplane = new Float64Array(dim);
        let norm = 0;

        for (let i = 0; i < dim; i++) {
          // Box-Muller transform for Gaussian distribution
          const u1 = random();
          const u2 = random();
          const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
          hyperplane[i] = z;
          norm += z * z;
        }

        // Normalize to unit vector
        norm = Math.sqrt(norm);
        for (let i = 0; i < dim; i++) {
          hyperplane[i] /= norm;
        }

        tableHyperplanes.push(hyperplane);
      }

      this.hyperplanes.push(tableHyperplanes);
    }

    this.isBuilt = true;
  }

  /**
   * Build the LSH index by generating hyperplanes
   *
   * @param embeddings - Sample embeddings (used to determine dimension)
   * @param seed - Optional seed for reproducibility
   */
  buildIndex(embeddings: number[][], seed?: number): void {
    if (embeddings.length === 0) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'Cannot build index with empty embeddings'
      );
    }

    const dim = embeddings[0].length;
    this.generateHyperplanes(dim, seed);
  }

  /**
   * Initialize hyperplanes with known embedding dimension
   *
   * @param dim - Embedding dimension
   * @param seed - Optional seed for reproducibility
   */
  initialize(dim: number, seed?: number): void {
    this.generateHyperplanes(dim, seed);
  }

  /**
   * Compute a single bit for a vector against a hyperplane
   *
   * Returns 1 if the vector is on the positive side of the hyperplane,
   * 0 otherwise.
   */
  private computeBit(vector: Float64Array, hyperplane: Float64Array): number {
    let dot = 0;
    for (let i = 0; i < vector.length; i++) {
      dot += vector[i] * hyperplane[i];
    }
    return dot >= 0 ? 1 : 0;
  }

  /**
   * Compute bucket ID for a vector in a single table
   *
   * @param vector - Input vector
   * @param tableIndex - Which hash table to use
   * @returns Binary bucket ID as string (e.g., "1011001...")
   */
  private computeBucketId(vector: Float64Array, tableIndex: number): string {
    if (!this.isBuilt) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'LSH index not initialized. Call buildIndex() or initialize() first.'
      );
    }

    const bits: number[] = [];
    const hyperplanes = this.hyperplanes[tableIndex];

    for (let b = 0; b < this.config.n_bits_per_table; b++) {
      bits.push(this.computeBit(vector, hyperplanes[b]));
    }

    return bits.join('');
  }

  /**
   * Hash a vector to get all bucket IDs
   *
   * @param vector - Input vector (embedding)
   * @returns Array of bucket IDs (one per table)
   */
  hashVector(vector: number[]): string[] {
    if (!this.isBuilt) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'LSH index not initialized. Call buildIndex() or initialize() first.'
      );
    }

    if (vector.length !== this.embeddingDim) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `Vector dimension mismatch: expected ${this.embeddingDim}, got ${vector.length}`
      );
    }

    const vecArray = new Float64Array(vector);
    const bucketIds: string[] = [];

    for (let t = 0; t < this.config.n_tables; t++) {
      bucketIds.push(this.computeBucketId(vecArray, t));
    }

    return bucketIds;
  }

  /**
   * Get bucket IDs with table prefix for uniqueness
   *
   * Format: "table_<tableIndex>_<bucketBits>"
   *
   * @param vector - Input vector
   * @returns Array of prefixed bucket IDs
   */
  hashVectorWithPrefix(vector: number[]): string[] {
    const buckets = this.hashVector(vector);
    return buckets.map((bucket, index) => `table_${index}_${bucket}`);
  }

  /**
   * Query for candidate IDs (requires external storage of bucket->id mappings)
   *
   * This method returns the bucket IDs that should be queried.
   * The actual candidate retrieval is done by the server using
   * the blind index.
   *
   * @param vector - Query vector
   * @returns Array of bucket IDs to query
   */
  query(vector: number[]): string[] {
    return this.hashVectorWithPrefix(vector);
  }

  /**
   * Get the configuration used by this index
   */
  getConfig(): Required<LSHConfig> {
    return { ...this.config };
  }

  /**
   * Get the embedding dimension
   */
  getEmbeddingDimension(): number {
    return this.embeddingDim;
  }

  /**
   * Check if the index is ready to use
   */
  isReady(): boolean {
    return this.isBuilt;
  }

  /**
   * Export hyperplanes for persistence
   *
   * @returns Serialized hyperplane data
   */
  exportHyperplanes(): {
    config: Required<LSHConfig>;
    embeddingDim: number;
    hyperplanes: number[][][];
  } {
    if (!this.isBuilt) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'Cannot export: index not initialized'
      );
    }

    return {
      config: this.config,
      embeddingDim: this.embeddingDim,
      hyperplanes: this.hyperplanes.map((table) =>
        table.map((h) => Array.from(h))
      ),
    };
  }

  /**
   * Import hyperplanes from previous export
   *
   * @param data - Serialized hyperplane data
   */
  importHyperplanes(data: {
    config: Required<LSHConfig>;
    embeddingDim: number;
    hyperplanes: number[][][];
  }): void {
    this.config = data.config;
    this.embeddingDim = data.embeddingDim;
    this.hyperplanes = data.hyperplanes.map((table) =>
      table.map((h) => new Float64Array(h))
    );
    this.isBuilt = true;
  }
}

/**
 * Compute the Hamming distance between two bucket IDs
 *
 * @param bucket1 - First bucket ID (binary string)
 * @param bucket2 - Second bucket ID (binary string)
 * @returns Number of differing bits
 */
export function hammingDistance(bucket1: string, bucket2: string): number {
  if (bucket1.length !== bucket2.length) {
    return Math.max(bucket1.length, bucket2.length);
  }

  let distance = 0;
  for (let i = 0; i < bucket1.length; i++) {
    if (bucket1[i] !== bucket2[i]) {
      distance++;
    }
  }
  return distance;
}

/**
 * Estimate similarity from Hamming distance
 *
 * For random hyperplane LSH, the expected Hamming distance
 * is proportional to the angle between vectors.
 *
 * @param hammingDist - Hamming distance
 * @param nBits - Number of bits per bucket
 * @returns Estimated cosine similarity (approximate)
 */
export function estimateSimilarity(hammingDist: number, nBits: number): number {
  // Pr(bit differs) = angle / pi
  // So angle = Pr * pi
  // And cos(angle) = similarity
  const angleRatio = hammingDist / nBits;
  const angle = angleRatio * Math.PI;
  return Math.cos(angle);
}
