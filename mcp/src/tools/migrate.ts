/**
 * TotalReclaw MCP - Testnet-to-Mainnet Migration Tool
 *
 * Migrates encrypted memories from Base Sepolia (free tier testnet) to
 * Gnosis mainnet (Pro tier) after a user upgrades.
 *
 * The encrypted data is chain-agnostic: same AES-256-GCM ciphertext works
 * on any chain. Smart Account addresses are deterministic (CREATE2), so
 * the owner is the same on both chains. No re-encryption needed.
 *
 * Flow:
 *   1. Verify user is Pro tier (via billing endpoint)
 *   2. Fetch ALL active facts from testnet subgraph (paginated)
 *   3. Fetch existing mainnet facts (by contentFp) for idempotency
 *   4. Filter out facts that already exist on mainnet
 *   5. Re-encode each fact as protobuf and batch-submit to mainnet via relay
 *   6. Report progress and summary
 *
 * Safety:
 *   - Dry-run by default (confirm=false): shows preview without migrating
 *   - Testnet facts are never deleted (they remain as a backup)
 *   - Idempotent: skips facts that already exist on mainnet (by contentFp)
 *   - Handles partial failures gracefully (reports per-batch results)
 */

import { MIGRATE_TOOL_DESCRIPTION } from '../prompts.js';
import { getClientId } from '../client-id.js';

export const migrateToolDefinition = {
  name: 'totalreclaw_migrate',
  description: MIGRATE_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      confirm: {
        type: 'boolean',
        description: 'Set to true to execute the migration. Without confirm=true, returns a dry-run preview.',
        default: false,
      },
    },
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: true,
  },
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SubgraphFactFull {
  id: string;
  owner: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  isActive: boolean;
  contentFp: string;
  source: string;
  agentId: string;
  version: number;
  timestamp: string;
}

export interface MigrationResult {
  success: boolean;
  mode: 'dry_run' | 'executed';
  testnet_facts: number;
  already_on_mainnet: number;
  to_migrate: number;
  migrated: number;
  failed_batches: number;
  batch_results: Array<{
    batch_number: number;
    size: number;
    success: boolean;
    tx_hash?: string;
    error?: string;
  }>;
  message: string;
}

// ---------------------------------------------------------------------------
// GraphQL queries for migration
// ---------------------------------------------------------------------------

/** Fetch all active facts by owner — paginated with cursor (id_gt) */
const FETCH_FACTS_QUERY = `
  query FetchFacts($owner: Bytes!, $first: Int!, $lastId: String!) {
    facts(
      where: { owner: $owner, isActive: true, id_gt: $lastId }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      owner
      encryptedBlob
      encryptedEmbedding
      decayScore
      isActive
      contentFp
      source
      agentId
      version
      timestamp
    }
  }
`;

/** Initial fetch (no cursor) */
const FETCH_FACTS_INITIAL_QUERY = `
  query FetchFactsInitial($owner: Bytes!, $first: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      owner
      encryptedBlob
      encryptedEmbedding
      decayScore
      isActive
      contentFp
      source
      agentId
      version
      timestamp
    }
  }
`;

/** Fetch contentFps from mainnet for idempotency check */
const FETCH_CONTENT_FPS_QUERY = `
  query FetchContentFps($owner: Bytes!, $first: Int!, $lastId: String!) {
    facts(
      where: { owner: $owner, isActive: true, id_gt: $lastId }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      contentFp
    }
  }
`;

const FETCH_CONTENT_FPS_INITIAL_QUERY = `
  query FetchContentFpsInitial($owner: Bytes!, $first: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      contentFp
    }
  }
`;

// ---------------------------------------------------------------------------
// GraphQL helper
// ---------------------------------------------------------------------------

const PAGE_SIZE = 1000; // Graph Studio limit

async function gqlQuery<T>(
  endpoint: string,
  query: string,
  variables: Record<string, unknown>,
  authKeyHex?: string,
): Promise<T | null> {
  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-TotalReclaw-Client': getClientId(),
    };
    if (authKeyHex) headers['Authorization'] = `Bearer ${authKeyHex}`;
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, variables }),
    });
    if (!response.ok) {
      console.error(`[migrate] gqlQuery HTTP ${response.status}: ${await response.text().catch(() => 'no body')}`);
      return null;
    }
    const json = await response.json() as { data?: T; errors?: unknown[] };
    if (json.errors) {
      console.error(`[migrate] gqlQuery GraphQL errors:`, JSON.stringify(json.errors));
    }
    return json.data ?? null;
  } catch (err) {
    console.error(`[migrate] gqlQuery exception:`, err);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Fetch all facts from a subgraph (paginated)
// ---------------------------------------------------------------------------

export async function fetchAllFactsFromSubgraph(
  subgraphUrl: string,
  owner: string,
  authKeyHex?: string,
): Promise<SubgraphFactFull[]> {
  const allFacts: SubgraphFactFull[] = [];
  let lastId = '';
  let page = 0;

  while (true) {
    page++;
    const isInitial = lastId === '';
    const query = isInitial ? FETCH_FACTS_INITIAL_QUERY : FETCH_FACTS_QUERY;
    const variables: Record<string, unknown> = isInitial
      ? { owner, first: PAGE_SIZE }
      : { owner, first: PAGE_SIZE, lastId };

    const data = await gqlQuery<{ facts?: SubgraphFactFull[] }>(
      subgraphUrl,
      query,
      variables,
      authKeyHex,
    );

    const facts = data?.facts ?? [];
    if (facts.length === 0) break;

    allFacts.push(...facts);
    console.error(`[migrate] Fetched page ${page}: ${facts.length} facts (total: ${allFacts.length})`);

    if (facts.length < PAGE_SIZE) break; // Last page
    lastId = facts[facts.length - 1].id;
  }

  return allFacts;
}

// ---------------------------------------------------------------------------
// Fetch content fingerprints from mainnet for idempotency
// ---------------------------------------------------------------------------

export async function fetchMainnetContentFps(
  subgraphUrl: string,
  owner: string,
  authKeyHex?: string,
): Promise<Set<string>> {
  const fps = new Set<string>();
  let lastId = '';

  while (true) {
    const isInitial = lastId === '';
    const query = isInitial ? FETCH_CONTENT_FPS_INITIAL_QUERY : FETCH_CONTENT_FPS_QUERY;
    const variables: Record<string, unknown> = isInitial
      ? { owner, first: PAGE_SIZE }
      : { owner, first: PAGE_SIZE, lastId };

    const data = await gqlQuery<{ facts?: Array<{ id: string; contentFp: string }> }>(
      subgraphUrl,
      query,
      variables,
      authKeyHex,
    );

    const facts = data?.facts ?? [];
    if (facts.length === 0) break;

    for (const f of facts) {
      if (f.contentFp) fps.add(f.contentFp);
    }

    if (facts.length < PAGE_SIZE) break;
    lastId = facts[facts.length - 1].id;
  }

  return fps;
}

// ---------------------------------------------------------------------------
// Billing check
// ---------------------------------------------------------------------------

interface BillingResponse {
  tier: string;
  free_writes_used: number;
  free_writes_limit: number;
}

export async function checkBillingTier(
  serverUrl: string,
  walletAddress: string,
  authKeyHex: string,
): Promise<{ tier: string; error?: string }> {
  try {
    const url = `${serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(walletAddress)}`;
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${authKeyHex}`,
        'Content-Type': 'application/json',
        'X-TotalReclaw-Client': getClientId(),
      },
    });

    if (!response.ok) {
      return { tier: 'unknown', error: `Billing check failed (HTTP ${response.status})` };
    }

    const data = (await response.json()) as BillingResponse;
    return { tier: data.tier };
  } catch (err) {
    return { tier: 'unknown', error: err instanceof Error ? err.message : 'Unknown error' };
  }
}

// ---------------------------------------------------------------------------
// Blind index fetching for migration
// ---------------------------------------------------------------------------

const BLIND_INDEX_QUERY = `
  query FetchBlindIndices($factIds: [String!]!, $first: Int!) {
    blindIndexes(
      where: { fact_in: $factIds }
      first: $first
    ) {
      hash
      fact {
        id
      }
    }
  }
`;

/**
 * Fetch blind index hashes for a list of fact IDs from the subgraph.
 * Returns a Map from fact ID to array of blind index hashes.
 *
 * Processes fact IDs in chunks of 50 to avoid query limits.
 * Each fact may have ~25-35 blind indices (20 LSH + word indices).
 */
export async function fetchBlindIndicesForFacts(
  subgraphUrl: string,
  factIds: string[],
  authKeyHex: string,
): Promise<Map<string, string[]>> {
  const result = new Map<string, string[]>();

  const CHUNK_SIZE = 50;
  for (let i = 0; i < factIds.length; i += CHUNK_SIZE) {
    const chunk = factIds.slice(i, i + CHUNK_SIZE);

    const data = await gqlQuery<{
      blindIndexes?: Array<{ hash: string; fact: { id: string } }>;
    }>(
      subgraphUrl,
      BLIND_INDEX_QUERY,
      { factIds: chunk, first: 1000 },
      authKeyHex,
    );

    for (const entry of data?.blindIndexes ?? []) {
      const existing = result.get(entry.fact.id) || [];
      existing.push(entry.hash);
      result.set(entry.fact.id, existing);
    }
  }

  return result;
}
