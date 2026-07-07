/**
 * TotalReclaw MCP - Support Tool
 *
 * Returns static support information and common troubleshooting steps.
 * No server calls needed -- works in all modes including unconfigured.
 */

import type { ToolContext } from './types.js';
import { SUPPORT_TOOL_DESCRIPTION } from '../prompts.js';

export const supportToolDefinition = {
  name: 'totalreclaw_support',
  description: SUPPORT_TOOL_DESCRIPTION,
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

const TROUBLESHOOTING = [
  {
    issue: 'Recovery phrase lost',
    solution:
      'There is no password reset or recovery mechanism. If you lose your 12-word recovery phrase, ' +
      'all encrypted memories are permanently inaccessible. Always store it in a secure location ' +
      '(password manager, physical backup). No one -- not even TotalReclaw support -- can recover it.',
  },
  {
    issue: 'Slow recall / search taking a long time',
    solution:
      'First-time recall downloads the embedding model (~600MB one-time). Subsequent searches ' +
      'should complete in under 140ms. If recall remains slow, try restarting the MCP server to ' +
      'clear stale caches. Large vaults (10K+ facts) may benefit from upgrading to Pro for higher ' +
      'candidate pool limits.',
  },
  {
    issue: 'Quota exceeded / cannot store more memories',
    solution:
      'Free tier has a monthly write limit. Use totalreclaw_status to check your remaining quota. ' +
      'Upgrade to Pro via totalreclaw_upgrade for a higher monthly limit on Gnosis mainnet.',
  },
  {
    issue: 'Pro tier not recognized after upgrade',
    solution:
      'All memories are stored on Gnosis mainnet regardless of tier, so nothing needs to move after ' +
      'upgrading. The billing cache refreshes every 2 hours -- restart the MCP server to force a ' +
      'refresh if your Pro tier is not yet recognized.',
  },
  {
    issue: 'Import failed or memories not importing',
    solution:
      'Ensure you are using the correct source format (mem0, mcp-memory, chatgpt, claude, gemini). ' +
      'Try dry_run=true first to preview what would be imported. If API keys are required, ' +
      'they are used in-memory only and never stored. Content fingerprint dedup prevents duplicate imports.',
  },
];

/**
 * Handle a totalreclaw_support tool call.
 *
 * Returns static support information. No server calls needed — `async` only
 * for signature uniformity with the rest of the tool surface.
 */
export async function handleSupport(
  ctx: ToolContext,
  _args?: unknown,
): Promise<{ content: Array<{ type: string; text: string }> }> {
  const walletAddress = ctx.walletAddress ?? null;
  const subject = walletAddress
    ? `TotalReclaw Support (wallet: ${walletAddress})`
    : 'TotalReclaw Support';

  const contactEmail = walletAddress
    ? `mailto:hi@totalreclaw.xyz?subject=${encodeURIComponent(subject)}`
    : 'hi@totalreclaw.xyz';

  return {
    content: [{
      type: 'text',
      text: JSON.stringify({
        contact_email: contactEmail,
        documentation_url: 'https://github.com/p-diogo/totalreclaw/tree/main/docs/guides',
        issues_url: 'https://github.com/p-diogo/totalreclaw/issues',
        wallet_address: walletAddress,
        troubleshooting: TROUBLESHOOTING,
      }),
    }],
  };
}
