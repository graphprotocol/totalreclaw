/**
 * TotalReclaw MCP - Billing Upgrade Tool
 *
 * Creates a Stripe checkout session for upgrading to Pro tier.
 */

import { UPGRADE_TOOL_DESCRIPTION } from '../prompts.js';

export const upgradeToolDefinition = {
  name: 'totalreclaw_upgrade',
  description: UPGRADE_TOOL_DESCRIPTION,
  inputSchema: {
    type: 'object',
    properties: {
      wallet_address: {
        type: 'string',
        description: 'Smart Account address',
      },
    },
    required: ['wallet_address'],
  },
  annotations: {
    readOnlyHint: false,
    destructiveHint: false,
    idempotentHint: false,
  },
};

export interface CheckoutResponse {
  checkout_url: string;
  session_id?: string;
}

/**
 * Handle a totalreclaw_upgrade tool call.
 *
 * Creates a checkout session via the relay server and returns
 * the checkout URL for the user to complete payment.
 */
export async function handleUpgrade(
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
    const baseUrl = serverUrl.replace(/\/+$/, '');
    const endpoint = `${baseUrl}/v1/billing/checkout`;

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${authKey}`,
        'Content-Type': 'application/json',
        'X-TotalReclaw-Client': 'mcp-server',
      },
      body: JSON.stringify({
        wallet_address: walletAddress,
        tier: 'pro',
      }),
    });

    if (!response.ok) {
      const body = await response.text().catch(() => '');
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            error: `Failed to create checkout session (HTTP ${response.status})`,
            details: body || response.statusText,
          }),
        }],
      };
    }

    const data = (await response.json()) as CheckoutResponse;

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          checkout_url: data.checkout_url,
          message: `Open this URL to complete your upgrade to Pro: ${data.checkout_url}`,
        }),
      }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: `Failed to create checkout session: ${message}`,
        }),
      }],
    };
  }
}
