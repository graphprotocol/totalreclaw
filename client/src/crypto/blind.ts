/**
 * Blind Index Generation (WASM-backed)
 *
 * Thin wrappers over `@totalreclaw/core` WASM module for SHA-256 blind
 * indices with Porter stemming. The `generateTrapdoors` and utility
 * functions remain in TypeScript as they compose on top of the WASM
 * blind index output.
 *
 * Matches mcp/src/subgraph/crypto.ts:generateBlindIndices() exactly.
 */

import * as wasm from "@totalreclaw/core";

/**
 * Tokenize text into words for blind indexing.
 *
 * Performs basic tokenization:
 * - Converts to lowercase
 * - Removes punctuation (keeps Unicode letters, numbers, whitespace)
 * - Splits on whitespace
 * - Filters out short tokens (< 2 chars)
 *
 * Note: This TS tokenizer is kept for callers that need raw tokens
 * (e.g., BM25 scoring). The WASM module has its own tokenizer used
 * internally by generateBlindIndices().
 *
 * @param text - Text to tokenize
 * @returns Array of tokens
 */
export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ") // Remove punctuation, keep letters/numbers
    .split(/\s+/)
    .filter((token) => token.length >= 2);
}

/**
 * Compute SHA-256 hash of a string.
 *
 * Uses Node.js crypto for callers that need ad-hoc hashing outside of
 * the blind index pipeline.
 *
 * @param input - String to hash
 * @returns Hex-encoded SHA-256 hash
 */
export function sha256Hash(input: string): string {
  const crypto = require("crypto");
  return crypto.createHash("sha256").update(input, "utf-8").digest("hex");
}

/**
 * Generate blind indices from text.
 *
 * Delegates to the WASM module which performs tokenization, Porter
 * stemming, and SHA-256 hashing. Stemmed tokens are prefixed with
 * "stem:" before hashing to avoid collisions.
 *
 * Matches mcp/src/subgraph/crypto.ts:generateBlindIndices().
 *
 * @param text - Document text
 * @returns Array of blind indices (hex-encoded SHA-256 hashes), deduplicated
 */
export function generateBlindIndices(text: string): string[] {
  return wasm.generateBlindIndices(text);
}

/**
 * Generate trapdoors for search query.
 *
 * Trapdoors are SHA-256 hashes of:
 * 1. All tokens in the query (for keyword matching)
 * 2. Stemmed variants of tokens (for morphological matching)
 * 3. All LSH bucket identifiers from query embedding (for semantic matching)
 *
 * @param query - Search query text
 * @param lshBuckets - LSH bucket identifiers from query embedding (already blind-hashed)
 * @returns Array of trapdoors (hex-encoded SHA-256 hashes)
 */
export function generateTrapdoors(
  query: string,
  lshBuckets: string[],
): string[] {
  // Start with word + stem indices from the query text (via WASM)
  const trapdoors = new Set<string>(generateBlindIndices(query));

  // LSH buckets are already blind-hashed by LSHHasher, so add them directly
  for (const bucket of lshBuckets) {
    trapdoors.add(bucket);
  }

  return Array.from(trapdoors);
}

/**
 * Generate only token-based blind indices (without LSH).
 *
 * Useful for keyword-only search or when embedding is not available.
 *
 * @param text - Text to index
 * @returns Array of blind indices for tokens only
 */
export function generateTokenIndices(text: string): string[] {
  return generateBlindIndices(text);
}

/**
 * Verify that a blind index matches a trapdoor.
 *
 * Since both are SHA-256 hashes, this is a simple equality check.
 */
export function verifyBlindIndexMatch(
  blindIndex: string,
  trapdoor: string,
): boolean {
  return blindIndex === trapdoor;
}

/**
 * Compute the overlap between two sets of blind indices.
 *
 * Used to estimate recall quality during development.
 */
export function computeIndexOverlap(
  indices1: string[],
  indices2: string[],
): number {
  const set1 = new Set(indices1);
  const set2 = new Set(indices2);
  let overlap = 0;

  for (const index of set1) {
    if (set2.has(index)) {
      overlap++;
    }
  }

  return overlap;
}
