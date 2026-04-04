/**
 * TotalReclaw Plugin - LSH Hasher
 *
 * Re-exports `WasmLshHasher` from `@totalreclaw/core` as `LSHHasher`
 * for backward compatibility with existing plugin code.
 *
 * Default parameters: 32 bits per table, 20 tables.
 */

import { WasmLshHasher } from '@totalreclaw/core';

/**
 * Random Hyperplane LSH hasher.
 *
 * All state is deterministic from the seed -- no randomness at hash time.
 * Construct once per session; call `hash()` for every store/search operation.
 */
export class LSHHasher {
  private inner: WasmLshHasher;

  /**
   * Create a new LSH hasher.
   *
   * @param seed    - 32-byte seed from `deriveLshSeed()` in crypto.ts.
   * @param dims    - Embedding dimensionality (e.g. 640 for Harrier).
   * @param nTables - Number of independent hash tables (default 20).
   * @param nBits   - Number of bits per table (default 32).
   */
  constructor(
    seed: Uint8Array,
    dims: number,
    nTables: number = 20,
    nBits: number = 32,
  ) {
    const seedHex = Buffer.from(seed).toString('hex');
    this.inner = WasmLshHasher.withParams(seedHex, dims, nTables, nBits);
  }

  /**
   * Hash an embedding vector to an array of blind-hashed bucket IDs.
   *
   * @param embedding - The embedding vector (must have `dims` elements).
   * @returns Array of `nTables` hex strings (one blind hash per table).
   */
  hash(embedding: number[]): string[] {
    return this.inner.hash(new Float64Array(embedding));
  }

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
