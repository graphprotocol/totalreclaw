/**
 * Subgraph store path — writes facts on-chain via ERC-4337 UserOps.
 *
 * Used when the managed service is active (TOTALRECLAW_SELF_HOSTED is not
 * "true"). Replaces the HTTP POST to /v1/store with an on-chain transaction
 * flow.
 *
 * Uses @totalreclaw/core WASM for calldata encoding, UserOp hashing, and
 * ECDSA signing. Raw fetch() for all JSON-RPC calls to the relay bundler
 * and chain RPCs. No viem, no permissionless.
 */

import * as wasm from '@totalreclaw/core';
import { CONFIG } from './config.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SubgraphStoreConfig {
  relayUrl: string;           // TotalReclaw relay server URL (proxies bundler + subgraph)
  mnemonic: string;           // BIP-39 mnemonic for key derivation
  cachePath: string;          // Hot cache file path
  chainId: number;            // 100 for Gnosis mainnet, 84532 for Base Sepolia
  dataEdgeAddress: string;    // EventfulDataEdge contract address
  entryPointAddress: string;  // ERC-4337 EntryPoint v0.7
  authKeyHex?: string;        // HKDF auth key for relay server Authorization header
  rpcUrl?: string;            // Override chain RPC URL for public client reads
  walletAddress?: string;     // Smart Account address for billing (X-Wallet-Address header)
}

export interface FactPayload {
  id: string;
  timestamp: string;
  owner: string;           // Smart Account address (hex)
  encryptedBlob: string;   // Hex-encoded AES-256-GCM ciphertext
  blindIndices: string[];   // SHA-256 hashes (word + LSH)
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
  encryptedEmbedding?: string;
}

// Stub 65-byte signature for gas estimation (pm_sponsorUserOperation).
// Must be a structurally valid ECDSA signature (r,s,v) so that ecrecover does
// NOT revert inside SimpleAccount._validateSignature.  All-zeros causes
// OpenZeppelin ECDSA.recover() to revert with ECDSAInvalidSignature() (0xf645eedf),
// which the EntryPoint surfaces as AA23.
// This matches the stub used by permissionless/viem — ecrecover returns a
// non-owner address, so validateUserOp returns SIG_VALIDATION_FAILED (1)
// instead of reverting, which is what bundlers expect during simulation.
const DUMMY_SIGNATURE =
  '0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c';

// ---------------------------------------------------------------------------
// Protobuf encoding (WASM)
// ---------------------------------------------------------------------------

/**
 * Encode a fact payload as a minimal Protobuf wire format via WASM core.
 *
 * Field numbers match server/proto/totalreclaw.proto.
 */
export function encodeFactProtobuf(fact: FactPayload): Buffer {
  const json = JSON.stringify({
    id: fact.id,
    timestamp: fact.timestamp,
    owner: fact.owner,
    encrypted_blob_hex: fact.encryptedBlob,
    blind_indices: fact.blindIndices,
    decay_score: fact.decayScore,
    source: fact.source,
    content_fp: fact.contentFp,
    agent_id: fact.agentId,
    encrypted_embedding: fact.encryptedEmbedding || null,
  });
  return Buffer.from(wasm.encodeFactProtobuf(json));
}

// ---------------------------------------------------------------------------
// Chain helpers
// ---------------------------------------------------------------------------

/** Get the default public RPC URL for a chain ID */
function getDefaultRpcUrl(chainId: number): string {
  switch (chainId) {
    case 100:
      return 'https://rpc.gnosischain.com';
    case 84532:
      return 'https://sepolia.base.org';
    default:
      return 'https://sepolia.base.org';
  }
}

// ---------------------------------------------------------------------------
// Smart Account address derivation
// ---------------------------------------------------------------------------

/**
 * Derive the Smart Account address from a BIP-39 mnemonic.
 *
 * Uses the SimpleAccountFactory's getAddress(owner, salt=0) view function
 * via a raw eth_call to the chain RPC. The address is deterministic (CREATE2).
 */
export async function deriveSmartAccountAddress(mnemonic: string, chainId?: number): Promise<string> {
  const eoa = wasm.deriveEoa(mnemonic) as { private_key: string; address: string };
  const resolvedChainId = chainId ?? 84532;

  // SimpleAccountFactory.getAddress(address owner, uint256 salt) — view function
  // Selector: 0x8cb84e18 = keccak256("getAddress(address,uint256)")[0:4]
  const factoryAddress = wasm.getSimpleAccountFactory();
  const ownerPadded = eoa.address.slice(2).toLowerCase().padStart(64, '0');
  const saltPadded = '0'.repeat(64);
  const selector = '8cb84e18';
  const calldata = `0x${selector}${ownerPadded}${saltPadded}`;

  const rpcUrl = CONFIG.rpcUrl || getDefaultRpcUrl(resolvedChainId);
  const response = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'eth_call',
      params: [{ to: factoryAddress, data: calldata }, 'latest'],
    }),
  });
  const json = await response.json() as { result?: string; error?: { message: string } };
  if (json.error) {
    throw new Error(`Failed to resolve Smart Account address: ${json.error.message}`);
  }
  if (!json.result || json.result === '0x') {
    throw new Error('Failed to resolve Smart Account address: empty result');
  }
  // Result is a 32-byte ABI-encoded address — take last 20 bytes
  return `0x${json.result.slice(-40)}`.toLowerCase();
}

// ---------------------------------------------------------------------------
// Smart Account deployment check (with session cache)
// ---------------------------------------------------------------------------

/**
 * Session-level cache for account deployment status.
 * Once an account is deployed (first successful UserOp), we skip the
 * eth_getCode check and omit factory/factoryData for all subsequent calls.
 * This prevents AA10 "duplicate deployment" errors when multiple facts
 * are stored in rapid succession for a first-time user.
 */
const deployedAccounts = new Set<string>();

/**
 * Check if a Smart Account is deployed and return factory/factoryData if not.
 *
 * For ERC-4337 v0.7, undeployed accounts need `factory` and `factoryData`
 * in the UserOp so the EntryPoint can deploy them during the first transaction.
 */
async function getInitCode(
  sender: string,
  eoaAddress: string,
  rpcUrl: string,
): Promise<{ factory: string | null; factoryData: string | null }> {
  // Session cache: if we already deployed this account, skip the RPC check
  if (deployedAccounts.has(sender.toLowerCase())) {
    return { factory: null, factoryData: null };
  }

  // Check if the Smart Account contract is deployed
  const codeResp = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'eth_getCode',
      params: [sender, 'latest'],
    }),
  });
  const codeJson = await codeResp.json() as { result?: string };
  const isDeployed = codeJson.result && codeJson.result !== '0x' && codeJson.result !== '0x0';

  if (isDeployed) {
    deployedAccounts.add(sender.toLowerCase());
    return { factory: null, factoryData: null };
  }

  // Account not deployed — build factory + factoryData for first-time deployment.
  // createAccount(address owner, uint256 salt) — state-changing function
  // Selector: 0x5fbfb9cf = keccak256("createAccount(address,uint256)")[0:4]
  const factory = wasm.getSimpleAccountFactory();
  const ownerPadded = eoaAddress.slice(2).toLowerCase().padStart(64, '0');
  const saltPadded = '0'.repeat(64);
  const selector = '5fbfb9cf';
  const factoryData = `0x${selector}${ownerPadded}${saltPadded}`;

  return { factory, factoryData };
}

// ---------------------------------------------------------------------------
// On-chain submission (ERC-4337 UserOps via raw fetch)
// ---------------------------------------------------------------------------

/**
 * Submit a fact on-chain via ERC-4337 UserOp through the relay server.
 *
 * Uses @totalreclaw/core WASM for:
 * 1. EOA derivation from mnemonic (BIP-39 + BIP-44)
 * 2. Calldata encoding (SimpleAccount.execute)
 * 3. UserOp hashing (ERC-4337 v0.7)
 * 4. ECDSA signing (EIP-191 prefixed)
 *
 * All JSON-RPC calls go through raw fetch() to the relay bundler endpoint.
 */
export async function submitFactOnChain(
  protobufPayload: Buffer,
  config: SubgraphStoreConfig,
): Promise<{ txHash: string; userOpHash: string; success: boolean }> {
  if (!config.relayUrl) {
    throw new Error('Relay URL (TOTALRECLAW_SERVER_URL) is required for on-chain submission');
  }

  if (!config.mnemonic) {
    throw new Error('Mnemonic (TOTALRECLAW_RECOVERY_PHRASE) is required for on-chain submission');
  }

  const bundlerUrl = `${config.relayUrl}/v1/bundler`;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-TotalReclaw-Client': 'openclaw-plugin',
  };
  if (config.authKeyHex) headers['Authorization'] = `Bearer ${config.authKeyHex}`;
  if (config.walletAddress) headers['X-Wallet-Address'] = config.walletAddress;

  // Helper for JSON-RPC calls to relay bundler
  async function rpc(method: string, params: unknown[]): Promise<any> {
    const resp = await fetch(bundlerUrl, {
      method: 'POST',
      headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    });
    if (!resp.ok) {
      throw new Error(`Relay returned HTTP ${resp.status} for ${method}`);
    }
    const json = await resp.json() as { result?: any; error?: { message: string } };
    if (json.error) throw new Error(`RPC ${method}: ${json.error.message}`);
    return json.result;
  }

  // 1. Derive EOA from mnemonic
  const eoa = wasm.deriveEoa(config.mnemonic) as { private_key: string; address: string };
  const sender = config.walletAddress || await deriveSmartAccountAddress(config.mnemonic, config.chainId);
  const entryPoint = config.entryPointAddress || wasm.getEntryPointAddress();

  // 2. Encode calldata (SimpleAccount.execute → DataEdge fallback)
  const calldataBytes = wasm.encodeSingleCall(protobufPayload);
  const callData = `0x${Buffer.from(calldataBytes).toString('hex')}`;

  // 3. Get gas prices from Pimlico
  const gasPrices = await rpc('pimlico_getUserOperationGasPrice', []);
  const fast = gasPrices.fast;

  const rpcUrl = config.rpcUrl || CONFIG.rpcUrl || getDefaultRpcUrl(config.chainId);

  // 4. Check if Smart Account is deployed (needed for factory/factoryData)
  const { factory, factoryData } = await getInitCode(sender, eoa.address, rpcUrl);

  // 5. Get nonce from EntryPoint via eth_call
  //    getNonce(address sender, uint192 key) — selector 0x35567e1a
  const senderPadded = sender.slice(2).toLowerCase().padStart(64, '0');
  const keyPadded = '0'.repeat(64);
  const nonceCalldata = `0x35567e1a${senderPadded}${keyPadded}`;

  const nonceResp = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'eth_call',
      params: [{ to: entryPoint, data: nonceCalldata }, 'latest'],
    }),
  });
  const nonceJson = await nonceResp.json() as { result?: string };
  const nonce = nonceJson.result || '0x0';

  // 6. Build unsigned UserOp (v0.7 fields, camelCase for Rust JSON serde)
  const unsignedOp: Record<string, any> = {
    sender,
    nonce,
    callData,
    callGasLimit: '0x0',
    verificationGasLimit: '0x0',
    preVerificationGas: '0x0',
    maxFeePerGas: fast.maxFeePerGas,
    maxPriorityFeePerGas: fast.maxPriorityFeePerGas,
    signature: DUMMY_SIGNATURE,
  };
  if (factory) {
    unsignedOp.factory = factory;
    unsignedOp.factoryData = factoryData;
  }

  // 7. Get paymaster sponsorship (fills gas limits + paymaster fields)
  const sponsorResult = await rpc('pm_sponsorUserOperation', [unsignedOp, entryPoint]);
  Object.assign(unsignedOp, sponsorResult);

  // 8. Hash and sign the UserOp via WASM
  const opJson = JSON.stringify(unsignedOp);
  const hashHex = wasm.hashUserOp(opJson, entryPoint, BigInt(config.chainId));
  const sigHex = wasm.signUserOp(hashHex, eoa.private_key);
  unsignedOp.signature = `0x${sigHex}`;

  // 9. Submit the signed UserOp
  const userOpHash = await rpc('eth_sendUserOperation', [unsignedOp, entryPoint]);

  // 10. Wait for receipt (poll up to 120s)
  let receipt = null;
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      receipt = await rpc('eth_getUserOperationReceipt', [userOpHash]);
      if (receipt) break;
    } catch { /* not mined yet */ }
  }

  const success = receipt?.success ?? false;

  // Mark account as deployed after first successful submission
  if (success) {
    deployedAccounts.add(sender.toLowerCase());
  }

  return {
    txHash: receipt?.receipt?.transactionHash || '',
    userOpHash,
    success,
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
    throw new Error('Relay URL (TOTALRECLAW_SERVER_URL) is required for on-chain submission');
  }
  if (!config.mnemonic) {
    throw new Error('Mnemonic (TOTALRECLAW_RECOVERY_PHRASE) is required for on-chain submission');
  }

  const bundlerUrl = `${config.relayUrl}/v1/bundler`;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-TotalReclaw-Client': 'openclaw-plugin',
  };
  if (config.authKeyHex) headers['Authorization'] = `Bearer ${config.authKeyHex}`;
  if (config.walletAddress) headers['X-Wallet-Address'] = config.walletAddress;

  async function rpc(method: string, params: unknown[]): Promise<any> {
    const resp = await fetch(bundlerUrl, {
      method: 'POST',
      headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    });
    if (!resp.ok) {
      throw new Error(`Relay returned HTTP ${resp.status} for ${method}`);
    }
    const json = await resp.json() as { result?: any; error?: { message: string } };
    if (json.error) throw new Error(`RPC ${method}: ${json.error.message}`);
    return json.result;
  }

  const eoa = wasm.deriveEoa(config.mnemonic) as { private_key: string; address: string };
  const sender = config.walletAddress || await deriveSmartAccountAddress(config.mnemonic, config.chainId);
  const entryPoint = config.entryPointAddress || wasm.getEntryPointAddress();

  // Encode batch calldata (SimpleAccount.executeBatch)
  // encodeBatchCall expects a JSON array of hex-encoded payload strings
  const payloadsHex = protobufPayloads.map(p => p.toString('hex'));
  const calldataBytes = wasm.encodeBatchCall(JSON.stringify(payloadsHex));
  const callData = `0x${Buffer.from(calldataBytes).toString('hex')}`;

  // Get gas prices
  const gasPrices = await rpc('pimlico_getUserOperationGasPrice', []);
  const fast = gasPrices.fast;

  const rpcUrl = config.rpcUrl || CONFIG.rpcUrl || getDefaultRpcUrl(config.chainId);

  // Check if Smart Account is deployed (needed for factory/factoryData)
  const { factory, factoryData } = await getInitCode(sender, eoa.address, rpcUrl);

  // Get nonce
  const senderPadded = sender.slice(2).toLowerCase().padStart(64, '0');
  const keyPadded = '0'.repeat(64);
  const nonceCalldata = `0x35567e1a${senderPadded}${keyPadded}`;

  const nonceResp = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'eth_call',
      params: [{ to: entryPoint, data: nonceCalldata }, 'latest'],
    }),
  });
  const nonceJson = await nonceResp.json() as { result?: string };
  const nonce = nonceJson.result || '0x0';

  // Build unsigned UserOp
  const unsignedOp: Record<string, any> = {
    sender,
    nonce,
    callData,
    callGasLimit: '0x0',
    verificationGasLimit: '0x0',
    preVerificationGas: '0x0',
    maxFeePerGas: fast.maxFeePerGas,
    maxPriorityFeePerGas: fast.maxPriorityFeePerGas,
    signature: DUMMY_SIGNATURE,
  };
  if (factory) {
    unsignedOp.factory = factory;
    unsignedOp.factoryData = factoryData;
  }

  // Paymaster sponsorship
  const sponsorResult = await rpc('pm_sponsorUserOperation', [unsignedOp, entryPoint]);
  Object.assign(unsignedOp, sponsorResult);

  // Hash and sign via WASM
  const opJson = JSON.stringify(unsignedOp);
  const hashHex = wasm.hashUserOp(opJson, entryPoint, BigInt(config.chainId));
  const sigHex = wasm.signUserOp(hashHex, eoa.private_key);
  unsignedOp.signature = `0x${sigHex}`;

  // Submit
  const userOpHash = await rpc('eth_sendUserOperation', [unsignedOp, entryPoint]);

  // Wait for receipt (poll up to 120s)
  let receipt = null;
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 2000));
    try {
      receipt = await rpc('eth_getUserOperationReceipt', [userOpHash]);
      if (receipt) break;
    } catch { /* not mined yet */ }
  }

  const batchSuccess = receipt?.success ?? false;

  // Mark account as deployed after first successful submission
  if (batchSuccess) {
    deployedAccounts.add(sender.toLowerCase());
  }

  return {
    txHash: receipt?.receipt?.transactionHash || '',
    userOpHash,
    success: batchSuccess,
    batchSize: protobufPayloads.length,
  };
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/**
 * Check if subgraph mode is enabled (i.e. using the managed service).
 *
 * Returns true when TOTALRECLAW_SELF_HOSTED is NOT set to "true".
 * The managed service (subgraph mode) is the default.
 */
export function isSubgraphMode(): boolean {
  return !CONFIG.selfHosted;
}

/**
 * Get subgraph configuration from environment variables.
 *
 * After the relay refactor, clients only need:
 *   - TOTALRECLAW_RECOVERY_PHRASE -- BIP-39 mnemonic
 *   - TOTALRECLAW_SERVER_URL -- relay server URL (default: https://api.totalreclaw.xyz)
 *   - TOTALRECLAW_SELF_HOSTED -- set "true" to use self-hosted server (default: managed service)
 *   - TOTALRECLAW_CHAIN_ID -- optional, defaults to 84532 (Base Sepolia)
 */
export function getSubgraphConfig(): SubgraphStoreConfig {
  return {
    relayUrl: CONFIG.serverUrl || 'https://api.totalreclaw.xyz',
    mnemonic: CONFIG.recoveryPhrase,
    cachePath: CONFIG.cachePath,
    chainId: CONFIG.chainId,
    dataEdgeAddress: CONFIG.dataEdgeAddress || wasm.getDataEdgeAddress(),
    entryPointAddress: CONFIG.entryPointAddress || wasm.getEntryPointAddress(),
    rpcUrl: CONFIG.rpcUrl || undefined,
  };
}
