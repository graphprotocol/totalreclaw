/**
 * TotalReclaw LSH Hasher (WASM-backed)
 *
 * Thin wrapper over `WasmLshHasher` from `@totalreclaw/core`. Same class
 * interface as the previous pure-TS implementation so callers don't need
 * to change.
 *
 * Default parameters:
 *   - 32 bits per table (balanced discrimination vs. recall)
 *   - 20 tables (moderate table count for good coverage)
 *
 * Matches mcp/src/subgraph/lsh.ts exactly.
 */

import { WasmLshHasher } from "@totalreclaw/core";
import { TotalReclawError, TotalReclawErrorCode } from "../types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default number of independent hash tables. */
const DEFAULT_N_TABLES = 20;

/** Default number of bits (hyperplanes) per table. */
const DEFAULT_N_BITS = 32;

// ---------------------------------------------------------------------------
// LSHHasher
// ---------------------------------------------------------------------------

/**
 * Random Hyperplane LSH hasher.
 *
 * All state is deterministic from the seed -- no randomness at hash time.
 * Construct once per session; call `hash()` for every store/search operation.
 */
export class LSHHasher {
  private inner: WasmLshHasher;

  /** Embedding dimensionality (cached for error messages). */
  private readonly dims: number;

  /**
   * Create a new LSH hasher.
   *
   * @param seed    - 32-byte seed from `deriveLshSeed()` in seed.ts.
   * @param dims    - Embedding dimensionality (e.g. 1024 for Qwen3-Embedding-0.6B).
   * @param nTables - Number of independent hash tables (default 20).
   * @param nBits   - Number of bits per table (default 32).
   */
  constructor(
    seed: Uint8Array,
    dims: number,
    nTables: number = DEFAULT_N_TABLES,
    nBits: number = DEFAULT_N_BITS,
  ) {
    if (seed.length < 16) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `LSH seed too short: expected >= 16 bytes, got ${seed.length}`,
      );
    }
    if (dims < 1) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `dims must be positive, got ${dims}`,
      );
    }
    if (nTables < 1) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `nTables must be positive, got ${nTables}`,
      );
    }
    if (nBits < 1) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `nBits must be positive, got ${nBits}`,
      );
    }

    this.dims = dims;
    const seedHex = Buffer.from(seed).toString("hex");
    this.inner = WasmLshHasher.withParams(seedHex, dims, nTables, nBits);
  }

  // -------------------------------------------------------------------------
  // Hash function
  // -------------------------------------------------------------------------

  /**
   * Hash an embedding vector to an array of blind-hashed bucket IDs.
   *
   * For each table:
   *   1. Compute the N-bit signature (sign of dot product with each hyperplane).
   *   2. Build the bucket string: `lsh_t{tableIndex}_{binarySignature}`.
   *   3. SHA-256 the bucket string to produce a blind hash (hex).
   *
   * @param embedding - The embedding vector (must have `dims` elements).
   * @returns Array of `nTables` hex strings (one blind hash per table).
   */
  hash(embedding: number[]): string[] {
    if (embedding.length !== this.dims) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        `Embedding dimension mismatch: expected ${this.dims}, got ${embedding.length}`,
      );
    }

    return this.inner.hash(new Float64Array(embedding));
  }

  // -------------------------------------------------------------------------
  // Accessors
  // -------------------------------------------------------------------------

  /** Number of hash tables. */
  get tables(): number {
    return this.inner.tables;
  }

  /** Number of bits per table. */
  get bits(): number {
    return this.inner.bits;
  }

  /** Embedding dimensionality. */
  get dimensions(): number {
    return this.inner.dimensions;
  }
}

/**
 * Compute the Hamming distance between two binary signature strings.
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
 * Estimate similarity from Hamming distance.
 *
 * For random hyperplane LSH, the expected Hamming distance
 * is proportional to the angle between vectors.
 */
export function estimateSimilarity(
  hammingDist: number,
  nBits: number,
): number {
  const angleRatio = hammingDist / nBits;
  const angle = angleRatio * Math.PI;
  return Math.cos(angle);
}
