/**
 * TotalReclaw MCP - Subgraph search path
 *
 * Queries facts via GraphQL hash_in through the relay server.
 *
 * Used when the managed service is active. Replaces the HTTP POST
 * to /v1/search with a GraphQL query to the subgraph via the relay server.
 *
 * The relay server proxies GraphQL queries to Graph Studio with its own
 * API key at `${relayUrl}/v1/subgraph`. Clients never need a subgraph endpoint.
 *
 * Adapted from skill/plugin/subgraph-search.ts for the MCP server context.
 * Accepts relay URL as a parameter (not just env var) so the MCP server can
 * pass its configured URL.
 *
 * Improvements (v3):
 *   A1: blindIndices -> blindIndexes (Graph Node pluralization)
 *   A2: Small parallel batches (5 trapdoors each) for recall
 *   A3: orderBy: id, orderDirection: desc (recency proxy)
 *   A4: Cursor-based pagination for saturated batches
 *   C4: globalStates for lightweight fact count
 *   T322: Index compaction — filter out inactive facts at query level
 *         (fact_: { isActive: true }) + client-side safety net
 */

import { getSubgraphConfig } from './store.js';

export interface SubgraphSearchFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  timestamp: string;
  isActive: boolean;
}

/** Small batches so rare trapdoor matches aren't drowned by common ones. */
const DEFAULT_TRAPDOOR_BATCH_SIZE = 5;
const DEFAULT_PAGE_SIZE = 5000;

/**
 * Execute a single GraphQL query against the subgraph endpoint.
 * Returns null on any network or HTTP error (never throws).
 */
async function gqlQuery<T>(
  endpoint: string,
  query: string,
  variables: Record<string, unknown>,
  authKeyHex?: string,
): Promise<T | null> {
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authKeyHex) headers['Authorization'] = `Bearer ${authKeyHex}`;
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, variables }),
    });
    if (!response.ok) return null;
    const json = await response.json() as { data?: T };
    return json.data ?? null;
  } catch {
    return null;
  }
}

/** GraphQL query for blind index lookup — uses Graph Node's `blindIndexes` pluralization.
 *  T322: Added `fact_: { isActive: true }` relation filter to exclude blind index
 *  entries pointing to soft-deleted facts (decayScore < 0.3) at the query level.
 *  Graph Node relation filters use the `field_: { subfield: value }` syntax. */
const SEARCH_QUERY = `
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: desc
    ) {
      id
      fact {
        id
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        isActive
        contentFp
        sequenceId
        version
      }
    }
  }
`;

/** Pagination query — cursor-based using id_gt, ascending for deterministic walk.
 *  T322: Added `fact_: { isActive: true }` to match SEARCH_QUERY filtering. */
const PAGINATE_QUERY = `
  query PaginateBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!, $lastId: String!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, id_gt: $lastId, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      fact {
        id
        encryptedBlob
        encryptedEmbedding
        timestamp
        decayScore
        isActive
        contentFp
        sequenceId
        version
      }
    }
  }
`;

interface BlindIndexEntry {
  id: string;
  fact: SubgraphSearchFact;
}

interface SearchResponse {
  blindIndexes?: BlindIndexEntry[];
}

/**
 * Search the subgraph for facts matching the given trapdoors.
 *
 * Strategy:
 *   1. Split trapdoors into small chunks (TRAPDOOR_BATCH_SIZE=5).
 *   2. Fire all chunks in parallel via Promise.all.
 *   3. If any chunk returns exactly PAGE_SIZE results (saturated),
 *      paginate that chunk using id_gt cursor until exhausted or
 *      maxCandidates reached.
 *   4. Dedup across all chunks by fact id.
 *
 * @param owner         - Smart Account address (hex)
 * @param trapdoors     - Blind index hashes to search for
 * @param maxCandidates - Maximum number of candidate facts to return
 * @param relayUrl      - Optional relay URL override. If not provided,
 *                        falls back to getSubgraphConfig().relayUrl.
 */
export async function searchSubgraph(
  owner: string,
  trapdoors: string[],
  maxCandidates: number,
  relayUrl?: string,
  authKeyHex?: string,
): Promise<SubgraphSearchFact[]> {
  const effectiveRelayUrl = relayUrl ?? getSubgraphConfig().relayUrl;
  const subgraphUrl = `${effectiveRelayUrl}/v1/subgraph`;
  const allResults = new Map<string, SubgraphSearchFact>();

  const trapdoorBatchSize = parseInt(process.env.TOTALRECLAW_TRAPDOOR_BATCH_SIZE ?? String(DEFAULT_TRAPDOOR_BATCH_SIZE), 10);
  const pageSize = parseInt(process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE ?? String(DEFAULT_PAGE_SIZE), 10);

  // Split trapdoors into small chunks for parallel dispatch.
  const chunks: string[][] = [];
  for (let i = 0; i < trapdoors.length; i += trapdoorBatchSize) {
    chunks.push(trapdoors.slice(i, i + trapdoorBatchSize));
  }

  // Phase 1: Parallel initial queries (one per chunk).
  const initialResults = await Promise.all(
    chunks.map(async (chunk) => {
      const data = await gqlQuery<SearchResponse>(
        subgraphUrl,
        SEARCH_QUERY,
        { trapdoors: chunk, owner, first: pageSize },
        authKeyHex,
      );
      return { chunk, entries: data?.blindIndexes ?? [] };
    }),
  );

  // Collect initial results and identify saturated batches.
  // T322: Client-side safety net — skip facts where isActive is false,
  // in case the subgraph endpoint doesn't support relation filters (e.g. mock).
  const saturatedChunks: string[][] = [];
  for (const { chunk, entries } of initialResults) {
    for (const entry of entries) {
      if (entry.fact && entry.fact.isActive !== false && !allResults.has(entry.fact.id)) {
        allResults.set(entry.fact.id, entry.fact);
      }
    }
    if (entries.length >= pageSize) {
      saturatedChunks.push(chunk);
    }
  }

  // Phase 2: Cursor-based pagination for saturated batches.
  // Only paginate if we haven't yet reached maxCandidates.
  for (const chunk of saturatedChunks) {
    if (allResults.size >= maxCandidates) break;

    // Find the last blind-index id from the initial results for this chunk.
    // We need to re-query with ascending order for deterministic cursor walk.
    let lastId = '';

    while (allResults.size < maxCandidates) {
      const data = await gqlQuery<SearchResponse>(
        subgraphUrl,
        PAGINATE_QUERY,
        { trapdoors: chunk, owner, first: pageSize, lastId },
        authKeyHex,
      );

      const entries = data?.blindIndexes ?? [];
      if (entries.length === 0) break;

      for (const entry of entries) {
        // T322: Client-side safety net for pagination results too.
        if (entry.fact && entry.fact.isActive !== false && !allResults.has(entry.fact.id)) {
          allResults.set(entry.fact.id, entry.fact);
        }
      }

      // If we got fewer than PAGE_SIZE, this chunk is exhausted.
      if (entries.length < pageSize) break;

      lastId = entries[entries.length - 1].id;
    }
  }

  return Array.from(allResults.values());
}

/**
 * Get fact count from the subgraph for dynamic pool sizing.
 * Uses the globalStates entity for a lightweight single-row lookup
 * instead of fetching and counting individual fact IDs.
 *
 * @param owner    - Smart Account address (hex) — currently unused by globalStates
 *                   (which is aggregate), but kept for API consistency.
 * @param relayUrl - Optional relay URL override. If not provided,
 *                   falls back to getSubgraphConfig().relayUrl.
 */
export async function getSubgraphFactCount(
  owner: string,
  relayUrl?: string,
  authKeyHex?: string,
): Promise<number> {
  const effectiveRelayUrl = relayUrl ?? getSubgraphConfig().relayUrl;
  const subgraphUrl = `${effectiveRelayUrl}/v1/subgraph`;

  // globalStates is a singleton entity (id: "global") with aggregate counters.
  // It is NOT per-owner — it tracks totals across the entire subgraph.
  const query = `
    query FactCount {
      globalStates(first: 1) {
        totalFacts
      }
    }
  `;

  const data = await gqlQuery<{ globalStates?: Array<{ totalFacts: string }> }>(
    subgraphUrl,
    query,
    {},
    authKeyHex,
  );

  if (data?.globalStates && data.globalStates.length > 0) {
    const count = parseInt(data.globalStates[0].totalFacts, 10);
    return isNaN(count) ? 0 : count;
  }

  return 0;
}
