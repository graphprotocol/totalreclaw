/**
 * Client Batching — Multi-Call UserOperations for TotalReclaw.
 *
 * Batches multiple encrypted fact payloads into a SINGLE ERC-4337 UserOperation,
 * resulting in one on-chain transaction that emits multiple Log(bytes) events.
 *
 * Why batch?
 *   - Gas savings: ~21,000 gas base tx overhead paid once instead of N times
 *   - Rate limit efficiency: 1 UserOp counted against paymaster limit, not N
 *   - UX improvement: single confirmation for multi-fact extraction cycles
 *   - Network efficiency: one bundler submission instead of N sequential ones
 *
 * How it works:
 *   The ERC-4337 SimpleSmartAccount supports multi-call execution natively.
 *   Each call in the `calls` array triggers a separate `fallback()` on the
 *   EventfulDataEdge contract, emitting an independent `Log(bytes)` event.
 *   The subgraph indexes each event separately (using txHash-logIndex as ID).
 *
 * Gas savings estimate (Base Sepolia / Gnosis):
 *   Single fact:  ~5,300 gas (base) + ~21,000 (tx overhead) = ~26,300
 *   Batch of 5:   ~26,500 gas (base) + ~21,000 (tx overhead) = ~47,500
 *   Savings:      5 × 26,300 = 131,500 vs 47,500 → ~64% gas reduction
 *
 * Constraints:
 *   - MAX_BATCH_SIZE = 15 (matches extraction cap per cycle)
 *   - Empty batches are rejected
 *   - Each payload is independently encoded (no aggregation)
 *   - Paymaster counts this as 1 UserOp (not N)
 *
 * @module userop/batcher
 */

import type { Chain, Hex } from "viem";
import {
  encodeFactAsCalldata,
  ENTRYPOINT_V07_ADDRESS,
} from "./builder";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Maximum number of facts per batch UserOperation.
 *
 * Set to 15 to match the extraction cap (15 facts per cycle).
 * Going higher risks hitting block gas limits on some chains.
 */
export const MAX_BATCH_SIZE = 15;

/**
 * Minimum batch size — use regular sendFactOnChain for single facts.
 * A batch of 1 is technically valid but offers no savings.
 */
export const MIN_BATCH_SIZE = 1;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Configuration for a batched UserOperation.
 */
export interface BatchUserOperationConfig {
  /** 32-byte private key derived from BIP-39 seed */
  privateKey: Buffer;
  /** EventfulDataEdge contract address */
  dataEdgeAddress: `0x${string}`;
  /** Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia) */
  chainId: number;
  /** Array of encrypted Protobuf payloads to write on-chain */
  encryptedPayloads: Buffer[];
  /** TotalReclaw relay server URL (proxies bundler + paymaster JSON-RPC) */
  serverUrl: string;
  /** Optional: override nonce (for sequential operations) */
  nonce?: bigint;
}

/**
 * Result of a batched UserOperation submission.
 */
export interface BatchUserOperationResult {
  /** Number of facts included in the batch */
  batchSize: number;
  /** Hex-encoded calldata for each fact */
  callDataArray: string[];
  /** Target contract address */
  target: string;
  /** Operation nonce */
  nonce: bigint;
  /** Sender (Smart Account) address */
  sender: string;
  /** UserOperation hash returned by the bundler */
  userOpHash: string;
}

/**
 * Configuration for the high-level batch send function.
 */
export interface SendBatchConfig {
  /** 32-byte private key derived from BIP-39 seed */
  privateKey: Buffer;
  /** EventfulDataEdge contract address */
  dataEdgeAddress: `0x${string}`;
  /** Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia) */
  chainId: number;
  /** Array of encrypted Protobuf payloads to write on-chain */
  encryptedPayloads: Buffer[];
  /** TotalReclaw relay server URL (proxies bundler + paymaster JSON-RPC) */
  serverUrl: string;
  /** Timeout in ms to wait for on-chain confirmation (default: 120_000) */
  timeout?: number;
}

/**
 * Result of a completed batch send (built + confirmed on-chain).
 */
export interface SendBatchResult {
  /** Number of facts included in the batch */
  batchSize: number;
  /** UserOperation hash from the bundler */
  userOpHash: string;
  /** Transaction hash of the mined UserOperation */
  transactionHash: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Resolve a viem Chain object from a numeric chain ID.
 */
async function resolveChain(chainId: number): Promise<Chain> {
  const { gnosis, gnosisChiado, baseSepolia } = await import("viem/chains");
  switch (chainId) {
    case 100:
      return gnosis;
    case 10200:
      return gnosisChiado;
    case 84532:
      return baseSepolia;
    default:
      throw new Error(
        `Unsupported chain ID ${chainId}. Supported: 100 (Gnosis), 10200 (Chiado), 84532 (Base Sepolia)`
      );
  }
}

/**
 * Build the relay bundler RPC URL from the server base URL.
 */
function relayBundlerUrl(serverUrl: string): string {
  return `${serverUrl}/v1/bundler`;
}

/**
 * Encode multiple encrypted payloads as an array of call objects
 * for the SmartAccountClient.
 *
 * Each call targets the same EventfulDataEdge contract with value=0.
 * The calldata for each is just the raw encrypted bytes (fallback function).
 */
export function encodeBatchCalls(
  encryptedPayloads: Buffer[],
  dataEdgeAddress: `0x${string}`
): Array<{ to: `0x${string}`; value: bigint; data: Hex }> {
  return encryptedPayloads.map((payload) => ({
    to: dataEdgeAddress,
    value: 0n,
    data: encodeFactAsCalldata(payload) as Hex,
  }));
}

/**
 * Validate batch configuration.
 *
 * @throws Error if batch is empty or exceeds MAX_BATCH_SIZE
 */
export function validateBatchConfig(
  encryptedPayloads: Buffer[]
): void {
  if (!encryptedPayloads || encryptedPayloads.length === 0) {
    throw new Error("Batch must contain at least 1 encrypted payload");
  }
  if (encryptedPayloads.length > MAX_BATCH_SIZE) {
    throw new Error(
      `Batch size ${encryptedPayloads.length} exceeds maximum of ${MAX_BATCH_SIZE}. ` +
      `Split into multiple batches.`
    );
  }
  // Validate each payload is non-empty
  for (let i = 0; i < encryptedPayloads.length; i++) {
    if (encryptedPayloads[i].length === 0) {
      throw new Error(`Payload at index ${i} is empty`);
    }
  }
}

/**
 * Estimate gas savings from batching vs individual UserOps.
 *
 * Returns approximate savings percentage and absolute gas saved.
 * These are rough estimates — actual savings depend on chain and payload sizes.
 *
 * @param batchSize - Number of facts in the batch
 * @param avgPayloadBytes - Average encrypted payload size in bytes (default: 256)
 * @returns Gas savings estimate
 */
export function estimateGasSavings(
  batchSize: number,
  avgPayloadBytes: number = 256
): { savingsPercent: number; individualGas: number; batchedGas: number } {
  if (batchSize <= 0) return { savingsPercent: 0, individualGas: 0, batchedGas: 0 };

  // Constants (approximate for EVM)
  const TX_BASE_GAS = 21_000;
  const USEROP_OVERHEAD = 15_000; // ERC-4337 validation + signature verification
  const LOG_EVENT_BASE = 375;    // LOG0 base
  const LOG_TOPIC = 375;         // per topic
  const CALLDATA_NONZERO = 16;   // per non-zero byte
  const CALLDATA_ZERO = 4;       // per zero byte
  const MULTICALL_OVERHEAD = 2_600; // per-call overhead in executeBatch

  // Estimate per-fact gas (calldata + event emission)
  const avgNonZeroBytes = Math.ceil(avgPayloadBytes * 0.75);
  const avgZeroBytes = avgPayloadBytes - avgNonZeroBytes;
  const calldataGas = avgNonZeroBytes * CALLDATA_NONZERO + avgZeroBytes * CALLDATA_ZERO;
  const eventGas = LOG_EVENT_BASE + LOG_TOPIC + calldataGas; // Log(bytes) event
  const perFactGas = calldataGas + eventGas;

  // Individual: each fact = full tx + UserOp overhead + fact gas
  const individualTotal = batchSize * (TX_BASE_GAS + USEROP_OVERHEAD + perFactGas);

  // Batched: one tx + one UserOp overhead + N*(fact gas + multicall overhead)
  const batchedTotal = TX_BASE_GAS + USEROP_OVERHEAD +
    batchSize * (perFactGas + MULTICALL_OVERHEAD);

  const savings = individualTotal - batchedTotal;
  const savingsPercent = individualTotal > 0
    ? Math.round((savings / individualTotal) * 100)
    : 0;

  return {
    savingsPercent: Math.max(0, savingsPercent),
    individualGas: individualTotal,
    batchedGas: batchedTotal,
  };
}

// ---------------------------------------------------------------------------
// Public API: buildBatchUserOperation
// ---------------------------------------------------------------------------

/**
 * Build an ERC-4337 UserOperation containing multiple fact writes.
 *
 * This creates a single UserOperation with multiple calls to the
 * EventfulDataEdge contract. Each call triggers the fallback() function,
 * emitting a separate Log(bytes) event that the subgraph indexes independently.
 *
 * The SmartAccountClient handles:
 *   - initCode generation for first-time users
 *   - Gas estimation for the multi-call UserOp
 *   - Paymaster sponsorship (single sponsorship for all calls)
 *   - Canonical ERC-4337 signing
 *   - Bundler submission
 *
 * @param config - Batch UserOperation configuration
 * @returns Batch result with the userOp hash from the bundler
 * @throws Error if batch is empty, exceeds MAX_BATCH_SIZE, or chain is unsupported
 */
export async function buildBatchUserOperation(
  config: BatchUserOperationConfig
): Promise<BatchUserOperationResult> {
  const {
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayloads,
    serverUrl,
  } = config;

  // Validate batch
  validateBatchConfig(encryptedPayloads);

  const { createPublicClient, http } = await import("viem");
  const { privateKeyToAccount } = await import("viem/accounts");
  const { entryPoint07Address } = await import("viem/account-abstraction");
  const { toSimpleSmartAccount } = await import("permissionless/accounts");
  const { createSmartAccountClient } = await import("permissionless");
  const { createPimlicoClient } = await import(
    "permissionless/clients/pimlico"
  );

  const chain = await resolveChain(chainId);
  const rpcUrl = relayBundlerUrl(serverUrl);

  // 1. Create the owner account from the private key
  const owner = privateKeyToAccount(
    `0x${privateKey.toString("hex")}` as Hex
  );

  // 2. Create public client for chain queries
  const publicClient = createPublicClient({
    chain,
    transport: http(),
  });

  // 3. Create the SimpleSmartAccount (EntryPoint v0.7)
  const smartAccount = await toSimpleSmartAccount({
    client: publicClient,
    owner,
    entryPoint: {
      address: entryPoint07Address,
      version: "0.7",
    },
    index: 0n,
  });

  // 4. Create Pimlico client (bundler + paymaster)
  const pimlicoClient = createPimlicoClient({
    transport: http(rpcUrl),
    entryPoint: {
      address: entryPoint07Address,
      version: "0.7",
    },
  });

  // 5. Create SmartAccountClient with paymaster sponsorship
  const smartAccountClient = createSmartAccountClient({
    account: smartAccount,
    chain,
    bundlerTransport: http(rpcUrl),
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () => {
        return (await pimlicoClient.getUserOperationGasPrice()).fast;
      },
    },
  });

  // 6. Encode all facts as a multi-call batch
  const calls = encodeBatchCalls(encryptedPayloads, dataEdgeAddress);
  const callDataArray = calls.map((c) => c.data as string);

  // 7. Send the batched UserOperation
  //    The SmartAccountClient's executeBatch encodes all calls into a single
  //    UserOp with callData = abi.encode(executeBatch(targets, values, datas))
  const userOpHash = await smartAccountClient.sendUserOperation({
    calls,
  });

  return {
    batchSize: encryptedPayloads.length,
    callDataArray,
    target: dataEdgeAddress,
    nonce: config.nonce ?? 0n,
    sender: smartAccount.address,
    userOpHash,
  };
}

// ---------------------------------------------------------------------------
// Public API: sendBatchOnChain
// ---------------------------------------------------------------------------

/**
 * High-level function: build, sponsor, sign, submit a batched UserOperation,
 * and wait for on-chain confirmation.
 *
 * This is the primary entry point for writing multiple encrypted facts on-chain
 * in a single transaction. It combines buildBatchUserOperation + waitForReceipt.
 *
 * Default timeout is 120s (longer than single-fact 60s to account for larger gas).
 *
 * @param config - Complete configuration for sending a batch on-chain
 * @returns Object with batchSize, userOpHash, and transactionHash
 */
export async function sendBatchOnChain(
  config: SendBatchConfig
): Promise<SendBatchResult> {
  const {
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayloads,
    serverUrl,
    timeout = 120_000,
  } = config;

  // Step 1: Build and submit the batched UserOperation
  const result = await buildBatchUserOperation({
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayloads,
    serverUrl,
  });

  // Step 2: Wait for on-chain confirmation
  const { createClient, http } = await import("viem");
  const { bundlerActions } = await import("viem/account-abstraction");

  const chain = await resolveChain(chainId);
  const rpcUrl = relayBundlerUrl(serverUrl);

  const bundlerClient = createClient({
    chain,
    transport: http(rpcUrl),
  }).extend(bundlerActions);

  const receipt = await bundlerClient.waitForUserOperationReceipt({
    hash: result.userOpHash as Hex,
    timeout,
  });

  return {
    batchSize: result.batchSize,
    userOpHash: result.userOpHash,
    transactionHash: receipt.receipt.transactionHash,
  };
}
