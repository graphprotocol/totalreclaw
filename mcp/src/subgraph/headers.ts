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

/**
 * Keyed blind-index scheme header (internal#573). Every request to the
 * relay's `/v1/subgraph` and `/v1/bundler` carries it; the relay refuses
 * trapdoor queries and UserOps without it. Defined once, here.
 */
export const INDEX_SCHEME_HEADER = 'X-TotalReclaw-Index-Scheme';
export const INDEX_SCHEME_VALUE = '1';

export function buildSubgraphHeaders(opts: {
  /** Smart Account address the request is for (the owner the query filters on). */
  walletAddress?: string;
  authKeyHex?: string;
}): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-TotalReclaw-Client': getClientId(),
    [INDEX_SCHEME_HEADER]: INDEX_SCHEME_VALUE,
  };
  if (opts.walletAddress) headers['X-Wallet-Address'] = opts.walletAddress;
  if (opts.authKeyHex) headers['Authorization'] = `Bearer ${opts.authKeyHex}`;
  return headers;
}

/**
 * Headers for the bundler and paymaster JSON-RPC calls (`submitFactOnChain`,
 * `submitFactBatchOnChain`). Same client id and scheme header as the subgraph
 * calls, plus the wallet address and auth key when supplied.
 */
export function buildBundlerHeaders(opts: {
  walletAddress?: string;
  authKeyHex?: string;
} = {}): Record<string, string> {
  const headers: Record<string, string> = {
    'X-TotalReclaw-Client': getClientId(),
    [INDEX_SCHEME_HEADER]: INDEX_SCHEME_VALUE,
  };
  if (opts.authKeyHex) headers['Authorization'] = `Bearer ${opts.authKeyHex}`;
  if (opts.walletAddress) headers['X-Wallet-Address'] = opts.walletAddress;
  return headers;
}
