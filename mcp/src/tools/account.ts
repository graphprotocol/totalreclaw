/**
 * TotalReclaw MCP - Account Details Tool
 *
 * Shows account information: billing status, fact count, features, and
 * which credential kind this host holds.
 *
 * PHRASE SAFETY (#670): this tool's output lands in the LLM context. It MUST
 * NOT carry anything derived from the recovery phrase: no word, prefix,
 * length, checksum, hash or index. It used to return a
 * `recovery_phrase_hint` of the first and last words; that field is gone.
 * `accountContextFromState` takes the managed-service state but never reads
 * the phrase; the credential status is a constant picked by kind.
 */

import type { ToolContext } from './types.js';
import { ACCOUNT_TOOL_DESCRIPTION } from '../prompts.js';
import { getClientId } from '../client-id.js';

export const accountToolDefinition = {
  name: 'totalreclaw_account',
  description: ACCOUNT_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {},
  },
  annotations: {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
  },
};

/**
 * Minimal view of index.ts's `SubgraphState` that the account tool needs.
 * Deliberately has no `mnemonic` field: nothing here may read the phrase.
 */
export interface AccountStateView {
  credentialKind: 'mnemonic' | 'bundle';
  smartAccountAddress: string;
  authKey: Uint8Array;
}

/**
 * Constant, non-derived credential description for a credential kind. The
 * same string is returned for every phrase.
 */
export function credentialStatus(kind: AccountStateView['credentialKind']): string {
  return kind === 'bundle'
    ? 'derived-bundle-v1 credential (no recovery phrase on this host)'
    : 'recovery phrase (stored locally, never shown)';
}

/** Response when no managed-service state exists (self-hosted / unconfigured). */
export function accountUnavailableResponse(): { content: Array<{ type: string; text: string }>; isError: true } {
  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        error: 'Account details require managed service mode (subgraph). Self-hosted mode does not track billing.',
      }),
    }],
    isError: true,
  };
}

/**
 * Build the account tool's ToolContext from managed-service state. Reads
 * only the credential KIND, never the phrase itself (#670).
 */
export function accountContextFromState(
  state: AccountStateView,
  opts: { serverUrl: string; getFactCount: () => Promise<number> },
): ToolContext {
  return {
    serverUrl: opts.serverUrl,
    authKeyHex: Buffer.from(state.authKey).toString('hex'),
    walletAddress: state.smartAccountAddress,
    credentialStatus: credentialStatus(state.credentialKind),
    getFactCount: opts.getFactCount,
  };
}

interface BillingStatusResponse {
  tier: string;
  free_writes_used: number;
  free_writes_limit: number;
  expires_at: string | null;
  features?: Record<string, unknown>;
}

/**
 * Handle a totalreclaw_account tool call.
 *
 * Fetches billing status and fact count, then returns a comprehensive
 * account overview plus a constant credential status. The `account` tool takes
 * no user args — everything it needs is dependency-injected via {@link
 * ToolContext}.
 */
export async function handleAccount(
  ctx: ToolContext,
  _args?: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const serverUrl = ctx.serverUrl ?? '';
  const authKeyHex = ctx.authKeyHex ?? '';
  const walletAddress = ctx.walletAddress ?? '';
  // Constant per credential kind (see credentialStatus()). Never derived
  // from the phrase (#670).
  const credentialStatus = ctx.credentialStatus ?? '';
  const getFactCount = ctx.getFactCount ?? (async () => null as unknown as number);

  // Fetch billing status and fact count in parallel
  const [billingResult, factCount] = await Promise.all([
    fetchBillingStatus(serverUrl, authKeyHex, walletAddress),
    getFactCount().catch(() => null),
  ]);

  if (billingResult.error) {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: billingResult.error,
          details: billingResult.details,
        }),
      }],
    };
  }

  const billing = billingResult.data!;
  const tierLabel = billing.tier === 'pro' ? 'Pro' : 'Free';
  const remaining = billing.free_writes_limit - billing.free_writes_used;

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        wallet_address: walletAddress,
        tier: billing.tier,
        tier_label: tierLabel,
        expires_at: billing.expires_at,
        writes_used: billing.free_writes_used,
        writes_limit: billing.free_writes_limit,
        remaining_writes: remaining,
        total_facts_stored: factCount,
        features: billing.features ?? {},
        credential: credentialStatus,
      }),
    }],
  };
}

/**
 * Fetch billing status from the relay server.
 */
async function fetchBillingStatus(
  serverUrl: string,
  authKeyHex: string,
  walletAddress: string,
): Promise<{ data?: BillingStatusResponse; error?: string; details?: string }> {
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
      const body = await response.text().catch(() => '');
      return {
        error: `Failed to fetch billing status (HTTP ${response.status})`,
        details: body || response.statusText,
      };
    }

    const billingJson = (await response.json()) as BillingStatusResponse;
    return { data: billingJson };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return { error: `Failed to fetch billing status: ${message}` };
  }
}
