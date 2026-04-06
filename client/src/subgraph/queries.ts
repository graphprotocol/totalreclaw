/**
 * GraphQL Query Strings for Subgraph
 *
 * Delegates to the Rust WASM core which holds the canonical query strings.
 * This ensures TypeScript and Rust clients always use identical queries.
 */

import * as wasm from "@totalreclaw/core";

/** Search: find facts matching any of the given blind index trapdoors. */
export const SEARCH_BY_BLIND_INDEX: string = wasm.getSearchQuery();

/** Broadened search: fetch recent active facts by owner without trapdoor filtering. */
export const BROADENED_SEARCH: string = wasm.getBroadenedSearchQuery();

/** Fetch all active facts for an owner (bulk download / recovery) */
export const FETCH_ALL_FACTS: string = wasm.getExportQuery();

/** Delta sync: facts since a given block number */
export const DELTA_SYNC_FACTS = `
  query DeltaSyncFacts($owner: Bytes!, $sinceBlock: BigInt!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner, blockNumber_gt: $sinceBlock }
      orderBy: blockNumber
      orderDirection: asc
      first: $first
      skip: $skip
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      isActive
      contentFp
      sequenceId
      blockNumber
      timestamp
      createdAt
      version
    }
  }
`;

/** Count facts for an owner (for dynamic pool sizing) */
export const COUNT_FACTS: string = wasm.getCountQuery();
