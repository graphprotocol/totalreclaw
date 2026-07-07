/**
 * TotalReclaw MCP Server - Memory Consolidation & Near-Duplicate Detection
 *
 * Provides cross-session / cross-vault deduplication of stored facts using
 * cosine similarity on their embeddings. This module handles:
 *
 *   1. Store-time dedup — before writing a new fact, check whether a
 *      near-duplicate already exists in the vault (findNearDuplicate).
 *   2. Supersede logic — when a near-duplicate is found, decide whether
 *      the new fact should replace or be skipped (shouldSupersede).
 *   3. Bulk consolidation — cluster all facts in the vault and identify
 *      groups of near-duplicates for cleanup (clusterFacts).
 *
 * Delegates core computation to `@totalreclaw/core` Rust WASM module:
 * `findNearDuplicate`, `shouldSupersede`, and `clusterFacts` all call the
 * corresponding core functions directly.
 *
 * Threshold helpers remain local (they read process.env).
 */

// ---------------------------------------------------------------------------
// Lazy-load WASM core (same pattern as claims-helper.ts)
// ---------------------------------------------------------------------------
// eslint-disable-next-line @typescript-eslint/no-var-requires
let _wasm: typeof import('@totalreclaw/core') | null = null;
function getWasm(): typeof import('@totalreclaw/core') {
  if (!_wasm) {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    _wasm = require('@totalreclaw/core');
  }
  return _wasm!;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/**
 * Get the cosine similarity threshold for store-time dedup.
 *
 * Configurable via TOTALRECLAW_STORE_DEDUP_THRESHOLD env var.
 * Must be a number in [0, 1]. Falls back to 0.85 if invalid or unset.
 */
export function getStoreDedupThreshold(): number {
  const envVal = process.env.TOTALRECLAW_STORE_DEDUP_THRESHOLD;
  if (envVal !== undefined) {
    const parsed = parseFloat(envVal);
    if (!isNaN(parsed) && parsed >= 0 && parsed <= 1) return parsed;
  }
  return 0.85;
}

/**
 * Get the cosine similarity threshold for bulk consolidation clustering.
 *
 * Configurable via TOTALRECLAW_CONSOLIDATION_THRESHOLD env var.
 * Must be a number in [0, 1]. Falls back to 0.88 if invalid or unset.
 */
export function getConsolidationThreshold(): number {
  const envVal = process.env.TOTALRECLAW_CONSOLIDATION_THRESHOLD;
  if (envVal !== undefined) {
    const parsed = parseFloat(envVal);
    if (!isNaN(parsed) && parsed >= 0 && parsed <= 1) return parsed;
  }
  return 0.88;
}

/** Maximum candidates to compare against during store-time dedup. */
export const STORE_DEDUP_MAX_CANDIDATES = 200;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A decrypted fact candidate from the vault, with metadata for ranking. */
export interface DecryptedCandidate {
  id: string;
  text: string;
  embedding: number[] | null;
  importance: number;
  decayScore: number;
  createdAt: number;
  version: number;
}

/** A match result from near-duplicate detection. */
export interface NearDuplicateMatch {
  existingFact: DecryptedCandidate;
  similarity: number;
}

/** A cluster of near-duplicate facts for consolidation. */
export interface ConsolidationCluster {
  representative: DecryptedCandidate;
  duplicates: DecryptedCandidate[];
}

// ---------------------------------------------------------------------------
// Store-time dedup
// ---------------------------------------------------------------------------

/**
 * Find the best near-duplicate match for a new fact among existing candidates.
 *
 * Compares the new fact's embedding against all candidates using cosine
 * similarity. Returns the candidate with the highest similarity above the
 * threshold, or null if no match is found.
 *
 * Candidates without embeddings are skipped (fail-safe).
 *
 * @param newFactEmbedding - Embedding vector for the new fact
 * @param candidates       - Existing facts to compare against
 * @param threshold        - Cosine similarity threshold (e.g. 0.85)
 * @returns                - Best match above threshold, or null
 */
export function findNearDuplicate(
  newFactEmbedding: number[],
  candidates: DecryptedCandidate[],
  threshold: number,
): NearDuplicateMatch | null {
  const wasm = getWasm();

  const existing = candidates
    .filter((c) => c.embedding && c.embedding.length > 0)
    .map((c) => ({ id: c.id, embedding: c.embedding! }));

  if (existing.length === 0) return null;

  const bestMatchJs = (wasm as any).findBestNearDuplicate(
    JSON.stringify(newFactEmbedding),
    JSON.stringify(existing),
    threshold,
  );

  if (bestMatchJs == null) return null;

  const bestMatch: { fact_id: string; similarity: number } =
    typeof bestMatchJs === 'string' ? JSON.parse(bestMatchJs) : bestMatchJs;

  const matched = candidates.find((c) => c.id === bestMatch.fact_id);
  if (!matched) return null;

  return { existingFact: matched, similarity: bestMatch.similarity };
}

// ---------------------------------------------------------------------------
// Supersede logic
// ---------------------------------------------------------------------------

/**
 * Decide whether a new fact should supersede an existing near-duplicate.
 *
 * - Higher importance wins.
 * - Equal importance: new fact supersedes (newer is preferred).
 *
 * @param newImportance - Importance score of the new fact
 * @param existingFact  - The existing near-duplicate candidate
 * @returns             - 'supersede' if new fact should replace, 'skip' otherwise
 */
export function shouldSupersede(
  newImportance: number,
  existingFact: DecryptedCandidate,
): 'supersede' | 'skip' {
  const wasm = getWasm();
  return wasm.shouldSupersede(newImportance, existingFact.importance) ? 'supersede' : 'skip';
}

// ---------------------------------------------------------------------------
// Bulk consolidation
// ---------------------------------------------------------------------------

/**
 * Cluster facts by semantic similarity using greedy single-pass clustering.
 *
 * Delegates to `@totalreclaw/core` WASM `clusterFacts` which performs the
 * same greedy single-pass algorithm and representative selection. The WASM
 * function returns ID-only clusters; this wrapper maps IDs back to full
 * `DecryptedCandidate` objects for callers.
 *
 * Only returns clusters that have duplicates (i.e. more than one member).
 * Facts without embeddings are not clustered.
 *
 * @param facts     - All facts to cluster
 * @param threshold - Cosine similarity threshold (e.g. 0.88)
 * @returns         - Clusters with duplicates (representative + duplicates)
 */
export function clusterFacts(
  facts: DecryptedCandidate[],
  threshold: number,
): ConsolidationCluster[] {
  const wasm = getWasm();

  // Build ConsolidationCandidate JSON for WASM (snake_case fields).
  const wasmCandidates = facts
    .filter((f) => f.embedding && f.embedding.length > 0)
    .map((f) => ({
      id: f.id,
      text: f.text,
      embedding: f.embedding!,
      importance: f.importance,
      decay_score: f.decayScore,
      created_at: f.createdAt,
      version: f.version,
    }));

  if (wasmCandidates.length === 0) return [];

  const clustersJs = (wasm as any).clusterFacts(
    JSON.stringify(wasmCandidates),
    threshold,
  );

  // WASM returns a JSON string: [{ representative: string, duplicates: string[] }]
  const wasmClusters: { representative: string; duplicates: string[] }[] =
    typeof clustersJs === 'string' ? JSON.parse(clustersJs) : clustersJs;

  // Build a lookup map for fast ID -> DecryptedCandidate resolution.
  const byId = new Map<string, DecryptedCandidate>();
  for (const f of facts) byId.set(f.id, f);

  // Map ID-only clusters back to full DecryptedCandidate objects.
  const clusters: ConsolidationCluster[] = [];
  for (const wc of wasmClusters) {
    const rep = byId.get(wc.representative);
    if (!rep) continue;

    const dups = wc.duplicates
      .map((id) => byId.get(id))
      .filter((d): d is DecryptedCandidate => d !== undefined);

    if (dups.length > 0) {
      clusters.push({ representative: rep, duplicates: dups });
    }
  }

  return clusters;
}
