/**
 * TotalReclaw MCP - LSH Hasher (WASM-backed)
 *
 * Thin wrapper over `WasmLshHasher` from `@totalreclaw/core`. Same class
 * interface as the previous pure-TS implementation so callers don't need
 * to change.
 *
 * Default parameters:
 *   - 32 bits per table
 *   - 20 tables
 */

import { WasmLshHasher } from '@totalreclaw/core';

/**
 * Random Hyperplane LSH hasher.
 *
 * All state is deterministic from the seed -- no randomness at hash time.
 * Construct once per session; `KeyedTagger` calls `hashKeyed()` for every store/search operation.
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
   * Keyed LSH tags for an embedding (one per table, in table order, NOT
   * sorted). Keyed by the vault's LSH bucket key inside core, so a bucket id
   * cannot be recomputed from the embedding without the vault key.
   *
   * @param embedding - The embedding vector (must have `dims` elements).
   * @returns Array of `nTables` hex strings.
   */
  hashKeyed(embedding: number[]): string[] {
    return this.inner.hashKeyed(new Float64Array(embedding));
  }

  /**
   * The underlying WASM hasher, for `generateSearchTrapdoorsKeyed` in
   * `keyed-tags.ts`. Internal: nothing else should reach for it.
   */
  get wasmHasher(): WasmLshHasher {
    return this.inner;
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
