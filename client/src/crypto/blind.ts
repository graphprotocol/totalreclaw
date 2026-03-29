/**
 * Blind Index Generation
 *
 * Creates SHA-256 hashes of tokens (with Porter stemming) and LSH buckets
 * for searchable encryption. The server can match these hashes without
 * knowing the original content.
 *
 * Matches mcp/src/subgraph/crypto.ts:generateBlindIndices() exactly.
 */

import * as crypto from "crypto";
import { stemmer } from "porter-stemmer";

/**
 * Tokenize text into words for blind indexing.
 *
 * Performs basic tokenization:
 * - Converts to lowercase
 * - Removes punctuation (keeps Unicode letters, numbers, whitespace)
 * - Splits on whitespace
 * - Filters out short tokens (< 2 chars)
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
 * @param input - String to hash
 * @returns Hex-encoded SHA-256 hash
 */
export function sha256Hash(input: string): string {
  return crypto.createHash("sha256").update(input, "utf-8").digest("hex");
}

/**
 * Generate blind indices from text.
 *
 * Creates SHA-256 hashes of:
 * 1. All tokens in the text (for keyword search)
 * 2. Stemmed variants of tokens (for morphological matching)
 *
 * Stemmed tokens are prefixed with "stem:" before hashing to avoid
 * collisions between a word that happens to equal another word's stem
 * (e.g., the word "commun" vs the stem of "community").
 *
 * Matches mcp/src/subgraph/crypto.ts:generateBlindIndices().
 *
 * @param text - Document text
 * @returns Array of blind indices (hex-encoded SHA-256 hashes), deduplicated
 */
export function generateBlindIndices(text: string): string[] {
  const tokens = tokenize(text);

  const seen = new Set<string>();
  const indices: string[] = [];

  for (const token of tokens) {
    // Exact word hash
    const hash = sha256Hash(token);
    if (!seen.has(hash)) {
      seen.add(hash);
      indices.push(hash);
    }

    // Stemmed word hash (prefixed with "stem:" to avoid collisions)
    const stem = stemmer(token);
    if (stem.length >= 2 && stem !== token) {
      const stemHash = sha256Hash(`stem:${stem}`);
      if (!seen.has(stemHash)) {
        seen.add(stemHash);
        indices.push(stemHash);
      }
    }
  }

  return indices;
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
  // Start with word + stem indices from the query text
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
