import { SEARCH_BY_BLIND_INDEX, FETCH_ALL_FACTS, DELTA_SYNC_FACTS, COUNT_FACTS } from "./queries";

export interface SubgraphFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  isActive: boolean;
  contentFp?: string;
  sequenceId?: string;
  blockNumber?: string;
  timestamp?: string;
  version?: number;
}

const TRAPDOOR_BATCH_SIZE = 500;
const PAGE_SIZE = 1000;

export class SubgraphClient {
  constructor(private endpoint: string) {}

  async search(owner: string, trapdoors: string[]): Promise<SubgraphFact[]> {
    const allResults = new Map<string, SubgraphFact>();

    for (let i = 0; i < trapdoors.length; i += TRAPDOOR_BATCH_SIZE) {
      const batch = trapdoors.slice(i, i + TRAPDOOR_BATCH_SIZE);
      const data = await this.query(SEARCH_BY_BLIND_INDEX, {
        trapdoors: batch,
        owner,
        first: PAGE_SIZE,
      });

      if (data?.blindIndices) {
        for (const entry of data.blindIndices) {
          // T322: Client-side safety net — skip inactive facts even if the
          // subgraph endpoint doesn't support relation filters.
          if (entry.fact && entry.fact.isActive !== false && !allResults.has(entry.fact.id)) {
            allResults.set(entry.fact.id, entry.fact);
          }
        }
      }
    }

    return Array.from(allResults.values());
  }

  /**
   * Broadened search: fetch recent active facts by owner without trapdoor filtering.
   * Used as a fallback when trapdoor search returns 0 candidates.
   */
  async searchBroadened(owner: string, maxCandidates: number = 200): Promise<SubgraphFact[]> {
    const data = await this.query(
      `query BroadenedSearch($owner: Bytes!, $first: Int!) {
        facts(where: { owner: $owner, isActive: true }, first: $first, orderBy: timestamp, orderDirection: desc) {
          id encryptedBlob encryptedEmbedding decayScore timestamp isActive contentFp sequenceId version
        }
      }`,
      { owner, first: Math.min(maxCandidates, PAGE_SIZE) },
    );
    return (data?.facts ?? []).filter((f: SubgraphFact) => f.isActive !== false);
  }

  async fetchAllFacts(owner: string): Promise<SubgraphFact[]> {
    const allFacts: SubgraphFact[] = [];
    let skip = 0;

    while (true) {
      const data = await this.query(FETCH_ALL_FACTS, {
        owner,
        first: PAGE_SIZE,
        skip,
      });

      const facts = data?.facts || [];
      allFacts.push(...facts);

      if (facts.length < PAGE_SIZE) break;
      skip += PAGE_SIZE;
    }

    return allFacts;
  }

  async deltaSyncFacts(owner: string, sinceBlock: number): Promise<SubgraphFact[]> {
    const allFacts: SubgraphFact[] = [];
    let skip = 0;

    while (true) {
      const data = await this.query(DELTA_SYNC_FACTS, {
        owner,
        sinceBlock: sinceBlock.toString(),
        first: PAGE_SIZE,
        skip,
      });

      const facts = data?.facts || [];
      allFacts.push(...facts);

      if (facts.length < PAGE_SIZE) break;
      skip += PAGE_SIZE;
    }

    return allFacts;
  }

  async getFactCount(owner: string): Promise<number> {
    const data = await this.query(COUNT_FACTS, { owner });
    return data?.facts?.length || 0;
  }

  private async query(query: string, variables: Record<string, unknown>): Promise<any> {
    const response = await fetch(this.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables }),
    });

    if (!response.ok) {
      throw new Error(`Subgraph query failed: ${response.status} ${response.statusText}`);
    }

    const json = (await response.json()) as { data?: any; errors?: Array<{ message: string }> };
    if (json.errors) {
      throw new Error(`Subgraph query error: ${json.errors[0].message}`);
    }

    return json.data;
  }
}
