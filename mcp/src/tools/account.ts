/**
 * TotalReclaw MCP - Account Details Tool
 *
 * Shows account information: billing status, fact count, features,
 * and a recovery phrase hint (first + last word only).
 */

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
 * account overview including a recovery phrase hint.
 */
export async function handleAccount(
  serverUrl: string,
  authKeyHex: string,
  walletAddress: string,
  mnemonicHint: string,
  getFactCount: () => Promise<number>,
): Promise<{ content: Array<{ type: string; text: string }> }> {
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
        recovery_phrase_hint: mnemonicHint,
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

    const data = (await response.json()) as BillingStatusResponse;
    return { data };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return { error: `Failed to fetch billing status: ${message}` };
  }
}
