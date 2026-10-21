/**
 * TotalReclaw MCP - Subgraph store path
 *
 * Writes facts on-chain via ERC-4337 UserOps.
 *
 * Used when the managed service is active. Replaces the HTTP POST
 * to /v1/store with an on-chain transaction flow.
 *
 * Builds UserOps client-side using `permissionless` + `viem` and submits
 * them through the TotalReclaw relay server, which proxies bundler/paymaster
 * JSON-RPC to Pimlico with its own API key. Clients never need a Pimlico key.
 *
 * Adapted from skill/plugin/subgraph-store.ts for the MCP server context.
 * Config can be injected directly (for MCP server state) or read from env vars.
 */

import { createPublicClient, createWalletClient, http, type Hex, type Address, type Chain, type LocalAccount } from 'viem';
import { getClientId } from '../client-id.js';
import { entryPoint07Address } from 'viem/account-abstraction';
import { mnemonicToAccount, privateKeyToAccount } from 'viem/accounts';
import { gnosis, baseSepolia, foundry } from 'viem/chains';
import { createSmartAccountClient } from 'permissionless';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { createPimlicoClient } from 'permissionless/clients/pimlico';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Default EventfulDataEdge contract address on Gnosis mainnet — the
 * PRODUCTION DataEdge. Exported (not just a local const) so callers that
 * need to name it in a diagnostic/warning log (e.g. `index.ts`'s
 * mnemonic-mode billing-unavailable fallback, #618 adversarial-review
 * fixup) don't duplicate the literal and risk it drifting.
 */
export const DEFAULT_DATA_EDGE_ADDRESS = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca';

/** Well-known ERC-4337 EntryPoint v0.7 address (same on all chains) */
const DEFAULT_ENTRYPOINT_ADDRESS = '0x0000000071727De22E5E9d8BAf0edAc6f37da032';

export interface SubgraphStoreConfig {
  relayUrl: string;           // TotalReclaw relay server URL (proxies bundler + subgraph)
  /**
   * BIP-39 mnemonic for key derivation. Mutually exclusive with
   * `ownerPrivateKeyHex` (Option E Phase 2 / #581, P2-13) — a bundle-mode
   * caller has no mnemonic anywhere in the process (derived-bundle-v1.md
   * §4.6 point 1) and threads the bundle's `signing.private_key` directly
   * instead. Exactly one of the two must be present; see
   * `resolveOwnerAccount` below for the resolution + error contract.
   */
  mnemonic?: string;
  /**
   * Bundle-mode signing key — 64 lowercase hex chars, no `0x` prefix
   * (`bundle.signing.private_key`, `signing.kind === "owner-eoa"` only;
   * Phase 2 does not support submitting on-chain with a `session-key`
   * bundle — that requires the signing-delegation phase, see
   * derived-bundle-v1.md §4.2). Takes precedence over `mnemonic` when both
   * happen to be set (never expected in practice).
   */
  ownerPrivateKeyHex?: string;
  cachePath: string;          // Hot cache file path
  chainId: number;            // 100 for Gnosis mainnet, 10200 for Chiado testnet, 84532 for Base Sepolia
  dataEdgeAddress: string;    // EventfulDataEdge contract address
  entryPointAddress: string;  // ERC-4337 EntryPoint v0.7
  authKeyHex?: string;        // HKDF auth key hex for relay Authorization header
  walletAddress?: string;     // Smart Account address for X-Wallet-Address header
}

/**
 * Resolve the Smart Account **owner** signing account from whichever
 * credential source `config` carries.
 *
 * Prefers `ownerPrivateKeyHex` (bundle mode) over `mnemonic` (legacy mode)
 * when — contrary to the normal precedence contract — both are somehow
 * present, since the private key is the more direct source of truth and
 * skips a redundant BIP-44 derivation. Throws a loud, actionable error when
 * NEITHER is present rather than letting `mnemonicToAccount(undefined)`
 * throw viem's own less legible error.
 */
export function resolveOwnerAccount(config: SubgraphStoreConfig): LocalAccount {
  if (config.ownerPrivateKeyHex) {
    const hex = config.ownerPrivateKeyHex.startsWith('0x')
      ? config.ownerPrivateKeyHex
      : `0x${config.ownerPrivateKeyHex}`;
    return privateKeyToAccount(hex as Hex);
  }
  if (config.mnemonic) {
    return mnemonicToAccount(config.mnemonic);
  }
  throw new Error(
    'On-chain submission requires either a mnemonic or a bundle-mode ' +
      'ownerPrivateKeyHex — neither is present on this SubgraphStoreConfig.',
  );
}

// ---------------------------------------------------------------------------
// Fact payload type + protobuf encoding
// ---------------------------------------------------------------------------
//
// Moved to ./protobuf.ts (dependency-free, so tests/parity can load it
// without the MCP runtime deps). Re-exported so every existing import from
// './subgraph/store.js' keeps working. PRD-04 F8 / DEP-6: that encoder no
// longer writes outer fields 9 (source) / 11 (agent_id).
export {
  encodeFactProtobuf,
  encodeVarint,
  PROTOBUF_VERSION_V4,
  type FactPayload,
} from './protobuf.js';

// ---------------------------------------------------------------------------
// Chain helpers
// ---------------------------------------------------------------------------

/** Resolve a viem Chain object from chain ID */
export function getChainFromId(chainId: number): Chain {
  switch (chainId) {
    case 100:
      return gnosis;
    case 84532:
      return baseSepolia;
    case 31337:
      return foundry; // Local Anvil
    default:
      return gnosis;
  }
}

/** Build the relay bundler RPC URL from the relay server URL */
export function getRelayBundlerUrl(relayUrl: string): string {
  return `${relayUrl}/v1/bundler`;
}

// ---------------------------------------------------------------------------
// On-chain submission (Pimlico UserOps)
// ---------------------------------------------------------------------------

/**
 * Submit a fact on-chain via ERC-4337 UserOp through the relay server.
 *
 * Builds a UserOp client-side using `permissionless` + `viem`:
 * 1. Derives private key from mnemonic (BIP-39 + BIP-44 m/44'/60'/0'/0/0)
 * 2. Creates a SimpleSmartAccount
 * 3. Gets paymaster sponsorship (via relay proxy to Pimlico)
 * 4. Signs and submits the UserOp to relay bundler endpoint
 * 5. Waits for the transaction receipt
 *
 * The relay server proxies all bundler/paymaster JSON-RPC to Pimlico
 * with its own API key. Clients never need a Pimlico API key.
 */
export async function submitFactOnChain(
  protobufPayload: Buffer,
  config: SubgraphStoreConfig,
): Promise<{ txHash: string; userOpHash: string; success: boolean }> {
  if (!config.relayUrl) {
    throw new Error('Relay URL is required for on-chain submission');
  }

  if (!config.mnemonic && !config.ownerPrivateKeyHex) {
    throw new Error('Mnemonic or bundle-mode ownerPrivateKeyHex is required for on-chain submission');
  }

  const chain = getChainFromId(config.chainId);
  const bundlerRpcUrl = getRelayBundlerUrl(config.relayUrl);
  const dataEdgeAddress = config.dataEdgeAddress as Address;
  const entryPointAddr = (config.entryPointAddress || entryPoint07Address) as Address;

  // Build authenticated transport for relay server proxy
  const headers: Record<string, string> = {
    'X-TotalReclaw-Client': getClientId(),
  };
  if (config.authKeyHex) headers['Authorization'] = `Bearer ${config.authKeyHex}`;
  if (config.walletAddress) headers['X-Wallet-Address'] = config.walletAddress;

  const authTransport = Object.keys(headers).length > 0
    ? http(bundlerRpcUrl, { fetchOptions: { headers } })
    : http(bundlerRpcUrl);

  // 1. Resolve the owner signer — mnemonic-derived EOA (BIP-44
  //    m/44'/60'/0'/0/0) or bundle-mode `ownerPrivateKeyHex` directly.
  const ownerAccount = resolveOwnerAccount(config);

  // 2. Create a public client for chain reads (use default RPC, not bundler proxy)
  const publicClient = createPublicClient({
    chain,
    transport: http(),
  });

  // 3. Create Pimlico client for bundler + paymaster operations (via relay)
  const pimlicoClient = createPimlicoClient({
    chain,
    transport: authTransport,
    entryPoint: {
      address: entryPointAddr,
      version: '0.7',
    },
  });

  // 4. Create a SimpleSmartAccount (auto-generates initCode if undeployed)
  const smartAccount = await toSimpleSmartAccount({
    // @ts-ignore - viem/permissionless type intersection conflict
    client: publicClient,
    owner: ownerAccount,
    entryPoint: {
      address: entryPointAddr,
      version: '0.7',
    },
  });

  // 5. Create smart account client wired to relay bundler + paymaster
  const smartAccountClient = createSmartAccountClient({
    account: smartAccount,
    chain,
    bundlerTransport: authTransport,
    // Paymaster sponsorship proxied through relay to Pimlico
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () => {
        return (await pimlicoClient.getUserOperationGasPrice()).fast;
      },
    },
  });

  // 6. Send the transaction: Smart Account execute(dataEdgeAddress, 0, protobufPayload)
  //    The DataEdge contract has a fallback() that emits Log(bytes), so the calldata
  //    IS the protobuf payload directly (no function selector needed).
  //    permissionless encodes the execute() call internally from to/value/data.
  const calldata = `0x${protobufPayload.toString('hex')}` as Hex;

  // Use sendUserOperation to get the userOpHash, then wait for receipt
  const userOpHash = await (smartAccountClient as any).sendUserOperation({
    calls: [
      {
        to: dataEdgeAddress,
        value: 0n,
        data: calldata,
      },
    ],
  });

  // 7. Wait for the UserOp to be included in a transaction
  const receipt = await pimlicoClient.waitForUserOperationReceipt({
    hash: userOpHash,
  });

  return {
    txHash: receipt.receipt.transactionHash,
    userOpHash,
    success: receipt.success,
  };
}

/**
 * Submit multiple facts on-chain in a single ERC-4337 UserOp (batched).
 *
 * Each protobuf payload becomes one call in a multi-call UserOp. The
 * DataEdge contract emits a separate Log(bytes) event per call, and the
 * subgraph indexes each event independently (by txHash + logIndex).
 *
 * Falls back to single-fact path for batches of 1 (no multicall overhead).
 */
export async function submitFactBatchOnChain(
  protobufPayloads: Buffer[],
  config: SubgraphStoreConfig,
): Promise<{ txHash: string; userOpHash: string; success: boolean; batchSize: number }> {
  if (!protobufPayloads.length) {
    return { txHash: '', userOpHash: '', success: true, batchSize: 0 };
  }

  // Single fact — use standard path (avoids multicall overhead)
  if (protobufPayloads.length === 1) {
    const result = await submitFactOnChain(protobufPayloads[0], config);
    return { ...result, batchSize: 1 };
  }

  if (!config.relayUrl) {
    throw new Error('Relay URL is required for on-chain submission');
  }
  if (!config.mnemonic && !config.ownerPrivateKeyHex) {
    throw new Error('Mnemonic or bundle-mode ownerPrivateKeyHex is required for on-chain submission');
  }

  const chain = getChainFromId(config.chainId);
  const bundlerRpcUrl = getRelayBundlerUrl(config.relayUrl);
  const dataEdgeAddress = config.dataEdgeAddress as Address;
  const entryPointAddr = (config.entryPointAddress || entryPoint07Address) as Address;

  const headers: Record<string, string> = {
    'X-TotalReclaw-Client': getClientId(),
  };
  if (config.authKeyHex) headers['Authorization'] = `Bearer ${config.authKeyHex}`;
  if (config.walletAddress) headers['X-Wallet-Address'] = config.walletAddress;

  const authTransport = Object.keys(headers).length > 0
    ? http(bundlerRpcUrl, { fetchOptions: { headers } })
    : http(bundlerRpcUrl);

  const ownerAccount = resolveOwnerAccount(config);
  const publicClient = createPublicClient({
    chain,
    transport: http(),
  });

  const pimlicoClient = createPimlicoClient({
    chain,
    transport: authTransport,
    entryPoint: {
      address: entryPointAddr,
      version: '0.7',
    },
  });

  const smartAccount = await toSimpleSmartAccount({
    // @ts-ignore - viem/permissionless type intersection conflict
    client: publicClient,
    owner: ownerAccount,
    entryPoint: {
      address: entryPointAddr,
      version: '0.7',
    },
  });

  const smartAccountClient = createSmartAccountClient({
    account: smartAccount,
    chain,
    bundlerTransport: authTransport,
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () => {
        return (await pimlicoClient.getUserOperationGasPrice()).fast;
      },
    },
  });

  // Build multi-call batch: each payload → one call to DataEdge fallback()
  const calls = protobufPayloads.map(payload => ({
    to: dataEdgeAddress,
    value: 0n,
    data: `0x${payload.toString('hex')}` as Hex,
  }));

  const userOpHash = await (smartAccountClient as any).sendUserOperation({ calls });
  const receipt = await pimlicoClient.waitForUserOperationReceipt({ hash: userOpHash });

  return {
    txHash: receipt.receipt.transactionHash,
    userOpHash,
    success: receipt.success,
    batchSize: protobufPayloads.length,
  };
}

// ---------------------------------------------------------------------------
// Local mode (direct-to-RPC, no ERC-4337)
// ---------------------------------------------------------------------------

/**
 * Check if local mode is enabled.
 * When TOTALRECLAW_LOCAL_RPC is set, facts are stored via raw eth_sendTransaction
 * instead of ERC-4337 UserOps through the relay/Pimlico.
 */
export function isLocalMode(): boolean {
  return !!process.env.TOTALRECLAW_LOCAL_RPC;
}

/**
 * Submit a fact directly to a local RPC (e.g., Anvil) via raw transaction.
 * No ERC-4337, no paymaster, no relay. Uses a pre-funded account.
 *
 * The sender is derived from the mnemonic (BIP-44 EOA), NOT a Smart Account.
 * The fact's `owner` field should be set to this EOA address.
 */
export async function submitFactLocal(
  protobufPayload: Buffer,
  config: SubgraphStoreConfig,
): Promise<{ txHash: string; success: boolean }> {
  const rpcUrl = process.env.TOTALRECLAW_LOCAL_RPC!;
  const dataEdgeAddress = config.dataEdgeAddress as Address;
  const ownerAccount = resolveOwnerAccount(config);

  const walletClient = createWalletClient({
    account: ownerAccount,
    chain: foundry,
    transport: http(rpcUrl),
  });

  const calldata = `0x${protobufPayload.toString('hex')}` as Hex;
  // viem's SendTransactionParameters narrows to require `kzg` in some conditional branches;
  // the cast avoids ts-jest tripping without a runtime impact (local-mode only path).
  const txHash = await (walletClient.sendTransaction as unknown as (p: unknown) => Promise<Hex>)({
    to: dataEdgeAddress,
    data: calldata,
    value: 0n,
  });

  return { txHash, success: true };
}

/**
 * Submit multiple facts directly to a local RPC in individual transactions.
 * Each fact is a separate tx (no multicall needed on local chains).
 */
export async function submitFactBatchLocal(
  protobufPayloads: Buffer[],
  config: SubgraphStoreConfig,
): Promise<{ txHashes: string[]; success: boolean; batchSize: number }> {
  const txHashes: string[] = [];
  for (const payload of protobufPayloads) {
    const result = await submitFactLocal(payload, config);
    txHashes.push(result.txHash);
  }
  return { txHashes, success: true, batchSize: protobufPayloads.length };
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/**
 * Check if the managed service (subgraph) is enabled.
 *
 * Returns true unless TOTALRECLAW_SELF_HOSTED is explicitly set to "true".
 * The managed service is the default; self-hosted mode is opt-in.
 * Can be overridden by passing an explicit value (useful when MCP server
 * manages its own config state).
 */
export function isSubgraphMode(override?: boolean): boolean {
  if (override !== undefined) return override;
  return process.env.TOTALRECLAW_SELF_HOSTED !== 'true';
}

/**
 * Get subgraph configuration from environment variables, with optional
 * overrides for constructor injection from MCP server state.
 *
 * After the v1 env var cleanup, clients only need:
 *   - TOTALRECLAW_RECOVERY_PHRASE -- BIP-39 mnemonic
 *   - TOTALRECLAW_SERVER_URL -- relay server URL (default: https://api.totalreclaw.xyz)
 *   - TOTALRECLAW_SELF_HOSTED -- set "true" for self-hosted HTTP mode (default: managed service)
 *
 * Chain ID is no longer user-configurable — auto-detected from billing tier
 * (free = Base Sepolia, Pro = Gnosis mainnet). Callers inject the resolved
 * chain ID via the `overrides` argument from `initSubgraphState`.
 *
 * Removed from client-side config (now server-side only):
 *   - PIMLICO_API_KEY
 *   - TOTALRECLAW_SUBGRAPH_ENDPOINT
 *
 * @param overrides - Optional partial config to override env var values.
 *                    Useful for MCP server injecting config from its state.
 */
export function getSubgraphConfig(overrides?: Partial<SubgraphStoreConfig>): SubgraphStoreConfig {
  const envConfig: SubgraphStoreConfig = {
    relayUrl: process.env.TOTALRECLAW_SERVER_URL || 'https://api.totalreclaw.xyz',
    mnemonic: process.env.TOTALRECLAW_RECOVERY_PHRASE || '',
    cachePath: process.env.TOTALRECLAW_CACHE_PATH || `${process.env.HOME}/.totalreclaw/cache.enc`,
    // Single-chain Gnosis (chain 100) after ops-1 — both tiers. The relay's
    // billing chain_id (threaded via overrides) is authoritative; 84532
    // (Base Sepolia) was retired. See chain-config.ts / #439.
    chainId: 100,
    dataEdgeAddress: process.env.TOTALRECLAW_DATA_EDGE_ADDRESS || DEFAULT_DATA_EDGE_ADDRESS,
    entryPointAddress: process.env.TOTALRECLAW_ENTRYPOINT_ADDRESS || DEFAULT_ENTRYPOINT_ADDRESS,
  };

  if (overrides) {
    return { ...envConfig, ...overrides };
  }

  return envConfig;
}
