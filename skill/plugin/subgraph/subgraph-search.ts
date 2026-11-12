/**
 * Subgraph search path — queries facts via GraphQL hash_in.
 *
 * Used when the managed service is active (TOTALRECLAW_SELF_HOSTED is not
 * "true"). Replaces the HTTP POST to /v1/search with a GraphQL query to
 * the subgraph via the relay server.
 *
 * The relay server proxies GraphQL queries to Graph Studio with its own
 * API key at `${relayUrl}/v1/subgraph`. Clients never need a subgraph endpoint.
 *
 * Query cost optimization:
 *   Phase 1: Single query with ALL trapdoors (1 query).
 *   Phase 2: If saturated (1000 results), split into small parallel batches
 *            so rare trapdoor matches aren't drowned by common ones.
 *   Phase 3: Cursor-based pagination for any saturated batch.
 *
 * This minimizes Graph Network query costs (pay-per-query via GRT):
 *   - Small datasets (<1000 matches): 1 query total
 *   - Medium datasets: 1 + N batch queries
 *   - Large datasets: 1 + N batches + pagination queries
 *
 * Ids first (internal #666, ported from the Python client, public #703):
 *   The blind-index queries above return fact ids only. A batch can match
 *   most of the vault, so carrying each fact's blob + embedding (~3.5 KB)
 *   in every batch downloaded the same fact many times. After the phases,
 *   each unique hit's payload is loaded once with FACTS_BY_IDS_QUERY, in
 *   batches of FACT_FETCH_BATCH_SIZE. At most SEARCH_QUERY_CONCURRENCY
 *   queries are in flight. Results keep first-hit order, so callers get the
 *   same candidate list as before.
 */

import { getSubgraphConfig } from './subgraph-store.js';
import { CONFIG } from '../config.js';
import { buildRelayHeaders } from '../billing/relay-headers.js';
import { relayFetch } from '../billing/relay.js';

export interface SubgraphSearchFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  timestamp: string;
  isActive: boolean;
}

/** Batch size for Phase 2 split queries. */
const TRAPDOOR_BATCH_SIZE = CONFIG.trapdoorBatchSize;
/** Graph Studio / Graph Network hard limit on `first` argument. */
const PAGE_SIZE = CONFIG.pageSize;
/** Max subgraph queries in flight per search. Bounds the burst only. */
const SEARCH_QUERY_CONCURRENCY = 6;
/** Fact payloads loaded per FACTS_BY_IDS_QUERY (~3.5 KB each). */
const FACT_FETCH_BATCH_SIZE = 100;

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
    const overrides: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (authKeyHex) {
      overrides['Authorization'] = `Bearer ${authKeyHex}`;
    }
    const headers = buildRelayHeaders(overrides);
    const response = await relayFetch({
      url: endpoint,
      method: 'POST',
      headers,
      body: JSON.stringify({ query, variables }),
    });
    if (!response.ok) {
      const body = await response.text().catch(() => '');
      console.error(`[TotalReclaw] Subgraph query failed: HTTP ${response.status} — ${body.slice(0, 200)}`);
      return null;
    }
    const json = await response.json() as { data?: T; error?: string; errors?: Array<{ message: string }> };
    if (json.error || json.errors) {
      console.error(`[TotalReclaw] Subgraph query error: ${json.error || json.errors?.map(e => e.message).join('; ')}`);
    }
    return json.data ?? null;
  } catch (err) {
    console.error(`[TotalReclaw] Subgraph query exception: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}

/** GraphQL query for blind index lookup. Returns fact ids only. */
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
      }
    }
  }
`;

/** Pagination query — cursor-based using id_gt, ascending for deterministic walk. Ids only. */
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
      }
    }
  }
`;

/**
 * Payload query for the ids collected by the blind-index phases. Selects the
 * same fields the blind-index queries used to carry, so callers see the same
 * objects as before.
 */
const FACTS_BY_IDS_QUERY = `
  query FactsByIds($ids: [ID!]!, $owner: Bytes!, $first: Int!) {
    facts(
      where: { id_in: $ids, owner: $owner, isActive: true }
      first: $first
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      isActive
      contentFp
      sequenceId
      version
    }
  }
`;

interface BlindIndexEntry {
  id: string;
  fact: { id: string; isActive?: boolean } | null;
}

interface SearchResponse {
  blindIndexes?: BlindIndexEntry[];
}

/** Record hit fact ids in first-seen order (a Set keeps insertion order). */
function collectIds(entries: BlindIndexEntry[], hitIds: Set<string>): void {
  for (const entry of entries) {
    if (entry.fact && entry.fact.id && entry.fact.isActive !== false) {
      hitIds.add(entry.fact.id);
    }
  }
}

/** Run `fn` over `items` with at most `limit` calls in flight. Keeps input order. */
async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const worker = async (): Promise<void> => {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

/**
 * Load each fact's payload once, in `ids` order. Ids whose fact is missing,
 * inactive, or whose batch failed drop out (a failed batch already logged).
 */
async function fetchFactPayloads(
  subgraphUrl: string,
  owner: string,
  ids: string[],
  authKeyHex?: string,
): Promise<SubgraphSearchFact[]> {
  if (ids.length === 0) return [];
  const batches: string[][] = [];
  for (let i = 0; i < ids.length; i += FACT_FETCH_BATCH_SIZE) {
    batches.push(ids.slice(i, i + FACT_FETCH_BATCH_SIZE));
  }
  const pages = await mapWithConcurrency(batches, SEARCH_QUERY_CONCURRENCY, (batch) =>
    gqlQuery<{ facts?: SubgraphSearchFact[] }>(
      subgraphUrl,
      FACTS_BY_IDS_QUERY,
      { ids: batch, owner, first: batch.length },
      authKeyHex,
    ),
  );
  const payloads = new Map<string, SubgraphSearchFact>();
  for (const page of pages) {
    for (const fact of page?.facts ?? []) {
      if (fact && fact.isActive !== false) payloads.set(fact.id, fact);
    }
  }
  const out: SubgraphSearchFact[] = [];
  for (const id of ids) {
    const fact = payloads.get(id);
    if (fact) out.push(fact);
  }
  return out;
}

/**
 * Paginate a single trapdoor chunk until exhausted or maxCandidates reached.
 */
async function paginateChunk(
  subgraphUrl: string,
  chunk: string[],
  owner: string,
  hitIds: Set<string>,
  maxCandidates: number,
  authKeyHex?: string,
): Promise<void> {
  let lastId = '';
  while (hitIds.size < maxCandidates) {
    const subgraphResponse = await gqlQuery<SearchResponse>(
      subgraphUrl,
      PAGINATE_QUERY,
      { trapdoors: chunk, owner, first: PAGE_SIZE, lastId },
      authKeyHex,
    );
    const entries = subgraphResponse?.blindIndexes ?? [];
    if (entries.length === 0) break;
    collectIds(entries, hitIds);
    if (entries.length < PAGE_SIZE) break;
    lastId = entries[entries.length - 1].id;
  }
}

/**
 * Search the subgraph for facts matching the given trapdoors.
 *
 * Adaptive strategy to minimize query costs:
 *
 *   Phase 1: Single query with ALL trapdoors.
 *     - If not saturated (< PAGE_SIZE results): done. 1 query total.
 *     - If saturated: common trapdoors may be drowning rare ones. Go to Phase 2.
 *
 *   Phase 2: Split trapdoors into small parallel batches (TRAPDOOR_BATCH_SIZE=5).
 *     - Each batch independently gets up to PAGE_SIZE results.
 *     - Rare trapdoor matches get their own budget.
 *
 *   Phase 3: Cursor-based pagination for any saturated batch.
 *     - Only for power users with very large datasets.
 *
 *   The phases collect fact ids only; payloads are loaded once at the end.
 */
export async function searchSubgraph(
  owner: string,
  trapdoors: string[],
  maxCandidates: number,
  authKeyHex?: string,
): Promise<SubgraphSearchFact[]> {
  const config = getSubgraphConfig();
  const subgraphUrl = `${config.relayUrl}/v1/subgraph`;
  const hitIds = new Set<string>();

  // -----------------------------------------------------------------------
  // Phase 1: Single query with all trapdoors (1 query)
  // -----------------------------------------------------------------------
  const phase1 = await gqlQuery<SearchResponse>(
    subgraphUrl,
    SEARCH_QUERY,
    { trapdoors, owner, first: PAGE_SIZE },
    authKeyHex,
  );

  const phase1Entries = phase1?.blindIndexes ?? [];
  collectIds(phase1Entries, hitIds);

  // Not saturated — we got every hit in 1 query. Load the payloads.
  if (phase1Entries.length < PAGE_SIZE) {
    return fetchFactPayloads(subgraphUrl, owner, Array.from(hitIds), authKeyHex);
  }

  // -----------------------------------------------------------------------
  // Phase 2: Saturated — split into small batches for better rare-word recall.
  // Common trapdoors were drowning rare ones in the single-query result.
  // -----------------------------------------------------------------------
  const chunks: string[][] = [];
  for (let i = 0; i < trapdoors.length; i += TRAPDOOR_BATCH_SIZE) {
    chunks.push(trapdoors.slice(i, i + TRAPDOOR_BATCH_SIZE));
  }

  const batchResults = await mapWithConcurrency(
    chunks,
    SEARCH_QUERY_CONCURRENCY,
    async (chunk) => {
      const subgraphResponse = await gqlQuery<SearchResponse>(
        subgraphUrl,
        SEARCH_QUERY,
        { trapdoors: chunk, owner, first: PAGE_SIZE },
        authKeyHex,
      );
      return { chunk, entries: subgraphResponse?.blindIndexes ?? [] };
    },
  );

  const saturatedChunks: string[][] = [];
  for (const { chunk, entries } of batchResults) {
    collectIds(entries, hitIds);
    if (entries.length >= PAGE_SIZE) {
      saturatedChunks.push(chunk);
    }
  }

  // -----------------------------------------------------------------------
  // Phase 3: Cursor-based pagination for saturated batches (power users).
  // -----------------------------------------------------------------------
  for (const chunk of saturatedChunks) {
    if (hitIds.size >= maxCandidates) break;
    await paginateChunk(subgraphUrl, chunk, owner, hitIds, maxCandidates, authKeyHex);
  }

  return fetchFactPayloads(subgraphUrl, owner, Array.from(hitIds), authKeyHex);
}

/**
 * Broadened search: fetch recent active facts by owner without trapdoor filtering.
 * Used as a fallback when trapdoor search returns 0 candidates (e.g., vague queries
 * like "who am I?" where word trapdoors don't overlap with stored fact tokens).
 */
export async function searchSubgraphBroadened(
  owner: string,
  maxCandidates: number,
  authKeyHex?: string,
): Promise<SubgraphSearchFact[]> {
  const config = getSubgraphConfig();
  const subgraphUrl = `${config.relayUrl}/v1/subgraph`;

  const query = `
    query BroadenedSearch($owner: Bytes!, $first: Int!) {
      facts(
        where: { owner: $owner, isActive: true }
        first: $first
        orderBy: timestamp
        orderDirection: desc
      ) {
        id
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        createdAt
        isActive
        contentFp
        sequenceId
        version
      }
    }
  `;

  const subgraphResponse = await gqlQuery<{ facts?: SubgraphSearchFact[] }>(
    subgraphUrl,
    query,
    { owner, first: Math.min(maxCandidates, 1000) },
    authKeyHex,
  );

  return (subgraphResponse?.facts ?? []).filter(f => f.isActive !== false);
}

/**
 * Fetch a single fact by its client-generated UUID.
 *
 * Used by the pin/unpin tools to retrieve a known fact's encrypted blob and
 * metadata for re-encryption with an updated status. Returns null if the fact
 * is not found, has been tombstoned (isActive=false), or the owner does not
 * match the caller's Smart Account address (defense against stale IDs from
 * another user's recall results).
 */
export async function fetchFactById(
  owner: string,
  factId: string,
  authKeyHex?: string,
): Promise<(SubgraphSearchFact & { owner: string }) | null> {
  const config = getSubgraphConfig();
  const subgraphUrl = `${config.relayUrl}/v1/subgraph`;

  const query = `
    query GetFactById($id: ID!) {
      fact(id: $id) {
        id
        owner
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        createdAt
        isActive
        contentFp
        sequenceId
        version
      }
    }
  `;

  const subgraphResponse = await gqlQuery<{ fact?: (SubgraphSearchFact & { owner: string }) | null }>(
    subgraphUrl,
    query,
    { id: factId },
    authKeyHex,
  );

  const fact = subgraphResponse?.fact;
  if (!fact) return null;
  if (fact.isActive === false) return null;
  if (typeof fact.owner === 'string' && fact.owner.toLowerCase() !== owner.toLowerCase()) {
    return null;
  }
  return fact;
}

/**
 * Get fact count from the subgraph for dynamic pool sizing.
 * Uses the globalStates entity for a lightweight single-row lookup
 * instead of fetching and counting individual fact IDs.
 */
export async function getSubgraphFactCount(owner: string, authKeyHex?: string): Promise<number> {
  const config = getSubgraphConfig();
  const subgraphUrl = `${config.relayUrl}/v1/subgraph`;

  const query = `
    query FactCount {
      globalStates(first: 1) {
        totalFacts
      }
    }
  `;

  const subgraphResponse = await gqlQuery<{ globalStates?: Array<{ totalFacts: string }> }>(
    subgraphUrl,
    query,
    {},
    authKeyHex,
  );

  if (subgraphResponse?.globalStates && subgraphResponse.globalStates.length > 0) {
    const count = parseInt(subgraphResponse.globalStates[0].totalFacts, 10);
    return isNaN(count) ? 0 : count;
  }

  return 0;
}
