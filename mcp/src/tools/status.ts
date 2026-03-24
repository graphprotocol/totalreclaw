/**
 * TotalReclaw MCP - Billing Status Tool
 *
 * Shows subscription/billing status for the user's account.
 * Queries the relay server's billing endpoint.
 */

import { STATUS_TOOL_DESCRIPTION } from '../prompts.js';

export const statusToolDefinition = {
  name: 'totalreclaw_status',
  description: STATUS_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      wallet_address: {
        type: 'string',
        description: 'Smart Account address (derived from recovery phrase)',
      },
    },
    required: ['wallet_address'],
  },
  annotations: {
    readOnlyHint: true,
    destructiveHint: false,
    idempotentHint: true,
  },
};

export interface BillingFeatures {
  llm_dedup?: boolean;
  custom_extract_interval?: boolean;
  min_extract_interval?: number;
  extraction_interval?: number;
  max_facts_per_extraction?: number;
  max_candidate_pool?: number;
}

export interface BillingStatusResponse {
  tier: string;
  free_writes_used: number;
  free_writes_limit: number;
  expires_at: string | null;
  features?: BillingFeatures;
}

/** Last raw billing response (for candidate pool caching in index.ts). */
let lastBillingResponse: BillingStatusResponse | null = null;

/** Get the last raw billing response. */
export function getLastBillingResponse(): BillingStatusResponse | null {
  return lastBillingResponse;
}

/**
 * Handle a totalreclaw_status tool call.
 *
 * Fetches billing status from the relay server and formats it for the LLM.
 */
export async function handleStatus(
  serverUrl: string,
  authKey: string,
  args: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const input = args as Record<string, unknown>;
  const walletAddress = input?.wallet_address as string;

  if (!walletAddress || typeof walletAddress !== 'string') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: 'wallet_address is required',
        }),
      }],
    };
  }

  try {
    const url = `${serverUrl.replace(/\/+$/, '')}/v1/billing/status?wallet_address=${encodeURIComponent(walletAddress)}`;

    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${authKey}`,
        'Content-Type': 'application/json',
        'X-TotalReclaw-Client': 'mcp-server',
      },
    });

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            error: `Failed to fetch billing status (HTTP ${response.status})`,
            details: body || response.statusText,
          }),
        }],
      };
    }

    const data = (await response.json()) as BillingStatusResponse;

    // Cache the raw response for candidate pool sizing
    lastBillingResponse = data;

    // Format nicely for the LLM to present
    const tierLabel = data.tier === 'pro' ? 'Pro' : 'Free';
    const usage = `${data.free_writes_used}/${data.free_writes_limit}`;
    const remaining = data.free_writes_limit - data.free_writes_used;
    const expiresLabel = data.expires_at
      ? `Expires: ${new Date(data.expires_at).toLocaleDateString()}`
      : 'No expiry';

    const formatted = [
      `Tier: ${tierLabel}`,
      `Writes used: ${usage}`,
      `Remaining: ${remaining}`,
      expiresLabel,
    ].join('\n');

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          tier: data.tier,
          free_writes_used: data.free_writes_used,
          free_writes_limit: data.free_writes_limit,
          remaining_writes: remaining,
          expires_at: data.expires_at,
          formatted,
        }),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: `Failed to check billing status: ${message}`,
        }),
      }],
    };
  }
}
