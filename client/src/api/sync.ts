/**
 * Client Reconnection Protocol (v0.3.1b)
 *
 * Provides delta sync against the /sync endpoint and
 * local fact reconciliation for agent reconnection.
 *
 * Spec: docs/specs/totalreclaw/server.md v0.3.1b section 8.2
 *
 * Protocol:
 * 1. Agent comes online
 * 2. GET /sync?since_sequence={last_known_sequence}
 * 3. Build set of server fingerprints: { content_fp -> fact_id }
 * 4. For each local pending fact:
 *    a. If content_fp in server set -> skip
 *    b. Else -> POST /store (server also checks, but pre-filtering avoids round trips)
 * 5. Update local last_known_sequence
 */

import { TotalReclawError, TotalReclawErrorCode } from '../types';

// ============================================================================
// Types
// ============================================================================

/**
 * A fact returned by the /sync endpoint.
 */
export interface SyncedFact {
  id: string;
  sequence_id: number | null;
  encrypted_blob: string;
  blind_indices: string[];
  decay_score: number;
  is_active: boolean;
  version: number;
  source: string;
  content_fp: string | null;
  agent_id: string | null;
  created_at?: string;
  updated_at?: string;
}

/**
 * A fact pending local push to the server.
 */
export interface LocalPendingFact {
  id: string;
  content_fp: string | undefined;
  plaintext: string;
  // Other fields needed for store request omitted for simplicity;
  // the caller is responsible for assembling the full store payload.
}

/**
 * Result of sync.
 */
export interface SyncResult {
  facts: SyncedFact[];
  latestSequence: number;
  hasMore: boolean;
}

/**
 * Result of local reconciliation.
 */
export interface ReconciliationResult {
  /** Local facts that match server (should be skipped) */
  skip: LocalPendingFact[];
  /** Local facts that are genuinely new (should be pushed) */
  push: LocalPendingFact[];
}

/**
 * Config for the sync client.
 */
export interface SyncClientConfig {
  serverUrl: string;
  /** Optional: inject a fetch implementation for testing */
  fetchImpl?: typeof fetch;
}

// ============================================================================
// SyncState
// ============================================================================

/**
 * Tracks the sync watermark for an agent.
 */
export class SyncState {
  public lastSequence: number;
  public lastSyncAt: Date | null;

  constructor(lastSequence: number = 0) {
    this.lastSequence = lastSequence;
    this.lastSyncAt = null;
  }

  update(latestSequence: number): void {
    this.lastSequence = latestSequence;
    this.lastSyncAt = new Date();
  }

  toJSON(): string {
    return JSON.stringify({
      lastSequence: this.lastSequence,
      lastSyncAt: this.lastSyncAt?.toISOString() ?? null,
    });
  }

  static fromJSON(json: string): SyncState {
    const parsed = JSON.parse(json);
    const state = new SyncState(parsed.lastSequence ?? 0);
    state.lastSyncAt = parsed.lastSyncAt ? new Date(parsed.lastSyncAt) : null;
    return state;
  }
}

// ============================================================================
// SyncClient
// ============================================================================

/**
 * HTTP client for the /sync endpoint.
 */
export class SyncClient {
  private serverUrl: string;
  private fetchImpl: typeof fetch;

  constructor(config: SyncClientConfig) {
    this.serverUrl = config.serverUrl.replace(/\/$/, '');
    this.fetchImpl = config.fetchImpl ?? fetch;
  }

  /**
   * Fetch one page of facts since the given sequence.
   */
  async syncSince(
    sinceSequence: number,
    authKey: string,
    limit: number = 1000
  ): Promise<SyncResult> {
    const url = `${this.serverUrl}/v1/sync?since_sequence=${sinceSequence}&limit=${limit}`;
    const response = await this.fetchImpl(url, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${authKey}`,
        Accept: 'application/json',
      },
    });

    if (!response.ok) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NETWORK_ERROR,
        `Sync failed: HTTP ${response.status}`
      );
    }

    const syncJson = await response.json() as {
      success: boolean;
      facts?: SyncedFact[];
      latest_sequence?: number;
      has_more?: boolean;
      error_message?: string;
    };
    if (!syncJson.success) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NETWORK_ERROR,
        `Sync failed: ${syncJson.error_message}`
      );
    }

    return {
      facts: syncJson.facts ?? [],
      latestSequence: syncJson.latest_sequence ?? 0,
      hasMore: syncJson.has_more ?? false,
    };
  }

  /**
   * Fetch ALL facts since the given sequence, auto-paginating.
   */
  async syncAllSince(
    sinceSequence: number,
    authKey: string,
    limit: number = 1000
  ): Promise<SyncResult> {
    const allFacts: SyncedFact[] = [];
    let currentSeq = sinceSequence;
    let latestSequence = 0;

    // eslint-disable-next-line no-constant-condition
    while (true) {
      const page = await this.syncSince(currentSeq, authKey, limit);
      allFacts.push(...page.facts);
      latestSequence = page.latestSequence;

      if (!page.hasMore || page.facts.length === 0) {
        break;
      }

      // Advance the cursor to the last sequence in this page
      const lastFact = page.facts[page.facts.length - 1];
      if (lastFact.sequence_id != null) {
        currentSeq = lastFact.sequence_id;
      } else {
        break; // Cannot paginate without sequence_id
      }
    }

    return {
      facts: allFacts,
      latestSequence,
      hasMore: false,
    };
  }
}

// ============================================================================
// Reconciliation
// ============================================================================

/**
 * Reconcile local pending facts against server state.
 *
 * For each local fact:
 * - If content_fp matches a server fact -> skip (already stored)
 * - Otherwise -> push (genuinely new)
 *
 * @param serverFacts - Facts from the /sync response
 * @param localFacts - Local pending facts to reconcile
 * @returns Which local facts to skip and which to push
 */
export function reconcileLocalFacts(
  serverFacts: SyncedFact[],
  localFacts: LocalPendingFact[]
): ReconciliationResult {
  // Build server fingerprint set
  const serverFpSet = new Set<string>();
  for (const fact of serverFacts) {
    if (fact.content_fp) {
      serverFpSet.add(fact.content_fp);
    }
  }

  const skip: LocalPendingFact[] = [];
  const push: LocalPendingFact[] = [];

  for (const local of localFacts) {
    if (local.content_fp && serverFpSet.has(local.content_fp)) {
      skip.push(local);
    } else {
      push.push(local);
    }
  }

  return { skip, push };
}
