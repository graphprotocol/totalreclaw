/**
 * TotalReclaw MCP - Billing Upgrade Tool
 *
 * Creates a checkout session for upgrading to Pro tier.
 * Supports both Stripe (card) and Coinbase Commerce (crypto) payment methods.
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
      payment_method: {
        type: 'string',
        enum: ['card', 'crypto'],
        default: 'card',
        description: 'Payment method preference',
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
  const paymentMethod = (input?.payment_method as string) || 'card';

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

  if (paymentMethod !== 'card' && paymentMethod !== 'crypto') {
    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          error: 'payment_method must be "card" or "crypto"',
        }),
      }],
    };
  }

  try {
    const baseUrl = serverUrl.replace(/\/+$/, '');
    const endpoint = paymentMethod === 'crypto'
      ? `${baseUrl}/v1/billing/checkout/crypto`
      : `${baseUrl}/v1/billing/checkout`;

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

    const methodLabel = paymentMethod === 'crypto'
      ? 'Coinbase Commerce (USDC, USDT, ETH)'
      : 'Stripe (credit/debit card)';

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          checkout_url: data.checkout_url,
          payment_method: methodLabel,
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
