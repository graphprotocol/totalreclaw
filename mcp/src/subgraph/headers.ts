/**
 * Single place that builds request headers for POSTs to the relay's
 * `/v1/subgraph` proxy. The relay routes reads by tier using the
 * `X-Wallet-Address` header and fails open to the Free subgraph when it is
 * absent (p-diogo/totalreclaw-internal#513), so every subgraph request MUST
 * come through here.
 *
 * Only the Smart Account address (public on-chain) is sent as the wallet
 * header, same as the Python client; the auth key stays in `Authorization`.
 */

import { getClientId } from '../client-id.js';

export function buildSubgraphHeaders(opts: {
  /** Smart Account address the request is for (the owner the query filters on). */
  walletAddress?: string;
  authKeyHex?: string;
}): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-TotalReclaw-Client': getClientId(),
  };
  if (opts.walletAddress) headers['X-Wallet-Address'] = opts.walletAddress;
  if (opts.authKeyHex) headers['Authorization'] = `Bearer ${opts.authKeyHex}`;
  return headers;
}
