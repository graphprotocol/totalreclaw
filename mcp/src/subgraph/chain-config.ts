/**
 * Chain + DataEdge resolution for the managed-service (subgraph) write path.
 *
 * Client-consistency contract (#439, sibling of the OpenClaw plugin #402/#460):
 * the relay returns authoritative `chain_id` + `data_edge_address` in
 * `GET /v1/billing/status`; chain-aware clients consume them verbatim so a
 * future chain change needs zero client release. Both tiers run on Gnosis
 * (chain 100) after ops-1 — the legacy Free → Base Sepolia (84532) routing
 * was retired, so the local default here is 100, not 84532.
 */

import type { SubgraphStoreConfig } from './store.js';

/** Default chain when billing is silent — Gnosis mainnet (single-chain, both tiers). */
const DEFAULT_CHAIN_ID = 100;

/** A DataEdge address is a 20-byte hex string. Reject anything else. */
const DATA_EDGE_RE = /^0x[0-9a-fA-F]{40}$/;

export interface ResolvedChainConfig {
  /** Chain to submit the write to (verbatim from billing, else Gnosis 100). */
  chainId: number;
  /** DataEdge contract from billing, or undefined to fall through to env/default. */
  dataEdgeAddress?: string;
}

/**
 * Resolve chain + DataEdge from a `/v1/billing/status` response.
 *
 * - `chain_id`: used verbatim when a finite number; otherwise Gnosis (100).
 * - `data_edge_address`: used only when a valid `0x…40hex` string; otherwise
 *   left undefined so the caller falls through to env / the store default.
 */
export function resolveChainConfig(
  billing: Record<string, unknown> | null | undefined,
): ResolvedChainConfig {
  let chainId = DEFAULT_CHAIN_ID;
  const rawChain = billing?.chain_id;
  if (typeof rawChain === 'number' && Number.isFinite(rawChain)) {
    chainId = rawChain;
  }

  let dataEdgeAddress: string | undefined;
  const rawEdge = billing?.data_edge_address;
  if (typeof rawEdge === 'string' && DATA_EDGE_RE.test(rawEdge)) {
    dataEdgeAddress = rawEdge;
  }

  return { chainId, dataEdgeAddress };
}

export interface WriteConfigInput {
  relayUrl: string;
  mnemonic: string;
  authKeyHex: string;
  walletAddress: string;
  chainId: number;
  dataEdgeAddress?: string;
}

/**
 * Build the `getSubgraphConfig` override object for a managed-service write,
 * threading the billing-resolved chainId + DataEdge alongside the relay creds.
 *
 * Precedence for DataEdge: an explicit `TOTALRECLAW_DATA_EDGE_ADDRESS` env var
 * must win. Because a passed override key supersedes env inside
 * getSubgraphConfig's merge, we inject the billing-derived DataEdge ONLY when
 * the env override is unset — mirroring the plugin's env || billing || default
 * ordering.
 */
export function buildSubgraphOverrides(
  input: WriteConfigInput,
): Partial<SubgraphStoreConfig> {
  const overrides: Partial<SubgraphStoreConfig> = {
    relayUrl: input.relayUrl,
    mnemonic: input.mnemonic,
    authKeyHex: input.authKeyHex,
    walletAddress: input.walletAddress,
  };

  // Only thread a finite chainId — never let an undefined clobber the store's
  // Gnosis (100) default via the spread merge.
  if (typeof input.chainId === 'number' && Number.isFinite(input.chainId)) {
    overrides.chainId = input.chainId;
  }

  if (input.dataEdgeAddress && !process.env.TOTALRECLAW_DATA_EDGE_ADDRESS) {
    overrides.dataEdgeAddress = input.dataEdgeAddress;
  }

  return overrides;
}
