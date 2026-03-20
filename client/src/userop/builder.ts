/**
 * ERC-4337 UserOperation Builder for TotalReclaw.
 *
 * Builds and submits UserOperations via a SimpleSmartAccount (ERC-4337 v0.7)
 * to write encrypted facts on-chain through the EventfulDataEdge contract.
 *
 * Architecture:
 *   - UserOp building and signing happens CLIENT-SIDE using `permissionless` + `viem`
 *   - Sponsorship via Pimlico paymaster, proxied through the TotalReclaw relay server
 *   - The relay server proxies bundler/paymaster JSON-RPC and handles billing
 *   - Clients never need a Pimlico API key -- the relay holds the key server-side
 *
 * Flow:
 *   1. Client encrypts fact + serializes to Protobuf (existing code)
 *   2. This module wraps the encrypted bytes into a UserOperation
 *   3. The UserOp is built via a SimpleSmartAccount (handles initCode, gas, signing)
 *   4. The UserOp is sponsored by Pimlico paymaster (via relay proxy)
 *   5. The signed UserOp is submitted to the relay's bundler endpoint
 *   6. Relay forwards to Pimlico bundler -> chain -> EventfulDataEdge
 *
 * Key addresses:
 *   - EntryPoint v0.7: 0x0000000071727De22E5E9d8BAf0edAc6f37da032
 *   - SimpleAccountFactory v0.7: 0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985
 *   - SimpleAccount implementation: 0xe6Cae83BdE06E4c305530e199D7217f42808555B
 *
 * Dependencies:
 *   - permissionless (Pimlico SDK for ERC-4337)
 *   - viem (Ethereum interactions, signing, encoding)
 */

import type { Chain, Hex } from "viem";

// ---------------------------------------------------------------------------
// Well-known ERC-4337 v0.7 addresses (deployed on all supported chains)
// ---------------------------------------------------------------------------

/** EntryPoint v0.7 -- canonical singleton across all EVM chains */
export const ENTRYPOINT_V07_ADDRESS =
  "0x0000000071727De22E5E9d8BAf0edAc6f37da032" as const;

/** SimpleAccountFactory v0.7 -- canonical factory for SimpleAccount */
export const SIMPLE_ACCOUNT_FACTORY_V07_ADDRESS =
  "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985" as const;

/** SimpleAccount v0.7 implementation (logic contract) */
export const SIMPLE_ACCOUNT_IMPLEMENTATION_ADDRESS =
  "0xe6Cae83BdE06E4c305530e199D7217f42808555B" as const;

// ---------------------------------------------------------------------------
// ABI fragments
// ---------------------------------------------------------------------------

/** ABI for SimpleAccountFactory.getAddress(owner, salt) view function */
const FACTORY_GET_ADDRESS_ABI = [
  {
    inputs: [
      { internalType: "address", name: "owner", type: "address" },
      { internalType: "uint256", name: "salt", type: "uint256" },
    ],
    name: "getAddress",
    outputs: [{ internalType: "address", name: "", type: "address" }],
    stateMutability: "view",
    type: "function",
  },
] as const;

// ---------------------------------------------------------------------------
// Configuration types
// ---------------------------------------------------------------------------

export interface UserOperationConfig {
  /** 32-byte private key derived from BIP-39 seed */
  privateKey: Buffer;
  /** EventfulDataEdge contract address */
  dataEdgeAddress: `0x${string}`;
  /** Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia) */
  chainId: number;
  /** Encrypted Protobuf payload to write on-chain */
  encryptedPayload: Buffer;
  /** TotalReclaw relay server URL (proxies bundler + paymaster JSON-RPC) */
  serverUrl: string;
  /** Optional: override nonce (for sequential operations) */
  nonce?: bigint;
}

export interface UserOperationResult {
  /** Hex-encoded calldata (the encrypted payload) */
  callData: string;
  /** Target contract address */
  target: string;
  /** Operation nonce */
  nonce: bigint;
  /** Sender (Smart Account) address */
  sender: string;
  /** UserOperation hash returned by the bundler */
  userOpHash: string;
}

export interface SendFactConfig {
  /** 32-byte private key derived from BIP-39 seed */
  privateKey: Buffer;
  /** EventfulDataEdge contract address */
  dataEdgeAddress: `0x${string}`;
  /** Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia) */
  chainId: number;
  /** Encrypted Protobuf payload to write on-chain */
  encryptedPayload: Buffer;
  /** TotalReclaw relay server URL (proxies bundler + paymaster JSON-RPC) */
  serverUrl: string;
  /** Timeout in ms to wait for on-chain confirmation (default: 60_000) */
  timeout?: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Resolve a viem Chain object from a numeric chain ID.
 *
 * Supports Gnosis mainnet (100), Chiado testnet (10200), and Base Sepolia (84532).
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
 * The relay proxies all bundler/paymaster JSON-RPC to Pimlico server-side.
 */
function relayBundlerUrl(serverUrl: string): string {
  return `${serverUrl}/v1/bundler`;
}

// ---------------------------------------------------------------------------
// Public API: encodeFactAsCalldata
// ---------------------------------------------------------------------------

/**
 * Encode an encrypted fact as hex calldata for the EventfulDataEdge fallback().
 *
 * The EventfulDataEdge contract uses a fallback() function, so the calldata
 * IS the encrypted payload directly (no function selector).
 *
 * @param encryptedBlob - Encrypted Protobuf bytes
 * @returns Hex-encoded calldata string (0x-prefixed)
 */
export function encodeFactAsCalldata(encryptedBlob: Buffer): string {
  if (encryptedBlob.length === 0) return "0x";
  return "0x" + encryptedBlob.toString("hex");
}

// ---------------------------------------------------------------------------
// Public API: getSmartAccountAddress
// ---------------------------------------------------------------------------

/**
 * Compute the deterministic Smart Account address for a given EOA owner
 * address and chain ID.
 *
 * This calls the SimpleAccountFactory.getAddress() **view function**, which is
 * a pure CREATE2 computation on-chain. It requires an RPC connection but does
 * NOT cost gas (it's an eth_call, not a transaction).
 *
 * The address is deterministic: same owner + same salt (0) = same address on
 * every chain where the factory is deployed.
 *
 * @param ownerAddress - EOA address that will own the Smart Account
 * @param chainId - Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia)
 * @param salt - Account index / salt (default: 0n)
 * @returns Deterministic Smart Account address (checksummed hex)
 */
export async function getSmartAccountAddress(
  ownerAddress: `0x${string}`,
  chainId: number,
  salt: bigint = 0n
): Promise<string> {
  const { createPublicClient, http } = await import("viem");

  const chain = await resolveChain(chainId);

  const publicClient = createPublicClient({
    chain,
    transport: http(),
  });

  const address = await publicClient.readContract({
    address: SIMPLE_ACCOUNT_FACTORY_V07_ADDRESS,
    abi: FACTORY_GET_ADDRESS_ABI,
    functionName: "getAddress",
    args: [ownerAddress, salt],
  });

  return address;
}

/**
 * Compute the deterministic Smart Account address from a private key.
 *
 * Convenience wrapper that derives the EOA address from the private key
 * and then calls getSmartAccountAddress().
 *
 * @param privateKey - 32-byte private key (Buffer)
 * @param chainId - Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia)
 * @param salt - Account index / salt (default: 0n)
 * @returns Deterministic Smart Account address (checksummed hex)
 */
export async function getSmartAccountAddressFromKey(
  privateKey: Buffer,
  chainId: number,
  salt: bigint = 0n
): Promise<string> {
  const { privateKeyToAccount } = await import("viem/accounts");

  const owner = privateKeyToAccount(
    `0x${privateKey.toString("hex")}` as Hex
  );

  return getSmartAccountAddress(owner.address, chainId, salt);
}

// ---------------------------------------------------------------------------
// Public API: buildUserOperation
// ---------------------------------------------------------------------------

/**
 * Build an ERC-4337 UserOperation for writing an encrypted fact on-chain.
 *
 * This creates a SmartAccountClient backed by a SimpleSmartAccount, then
 * submits the UserOperation to the Pimlico bundler with paymaster sponsorship.
 *
 * The SimpleSmartAccount:
 *   - Automatically generates initCode for first-time users (factory deployment)
 *   - Uses the canonical SimpleAccountFactory v0.7
 *   - Signs with the canonical ERC-4337 UserOperation hash
 *   - Gets gas estimates from the bundler
 *
 * @param config - UserOperation configuration
 * @returns UserOperation result with the userOp hash from the bundler
 */
export async function buildUserOperation(
  config: UserOperationConfig
): Promise<UserOperationResult> {
  const {
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayload,
    serverUrl,
  } = config;

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
  //    This handles:
  //    - Deterministic address computation (CREATE2)
  //    - initCode generation for first-time deployment
  //    - Canonical ERC-4337 UserOp signing
  const smartAccount = await toSimpleSmartAccount({
    client: publicClient,
    owner,
    entryPoint: {
      address: entryPoint07Address,
      version: "0.7",
    },
    index: 0n,
  });

  // 4. Create Pimlico client (bundler + paymaster in one)
  const pimlicoClient = createPimlicoClient({
    transport: http(rpcUrl),
    entryPoint: {
      address: entryPoint07Address,
      version: "0.7",
    },
  });

  // 5. Create SmartAccountClient with paymaster sponsorship
  //    The paymaster covers gas fees so users don't need native tokens
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

  // 6. Encode the fact as calldata
  const callData = encodeFactAsCalldata(encryptedPayload);

  // 7. Send the UserOperation via the SmartAccountClient
  //    This handles: initCode generation, gas estimation, paymaster sponsorship,
  //    canonical signing, and bundler submission -- all in one call
  const userOpHash = await smartAccountClient.sendUserOperation({
    calls: [
      {
        to: dataEdgeAddress,
        value: 0n,
        data: callData as Hex,
      },
    ],
  });

  return {
    callData,
    target: dataEdgeAddress,
    nonce: config.nonce ?? 0n,
    sender: smartAccount.address,
    userOpHash,
  };
}

// ---------------------------------------------------------------------------
// Public API: submitUserOperation
// ---------------------------------------------------------------------------

/**
 * Wait for a UserOperation to be mined and return its transaction hash.
 *
 * The UserOp was already submitted to the bundler by buildUserOperation().
 * This function just waits for on-chain confirmation via the relay.
 *
 * @param serverUrl - TotalReclaw relay server URL
 * @param chainId - Chain ID (100 for Gnosis, 10200 for Chiado, 84532 for Base Sepolia)
 * @param userOpHash - UserOperation hash from buildUserOperation()
 * @param timeout - Timeout in ms (default: 60_000)
 * @returns Transaction hash of the mined UserOperation
 */
export async function submitUserOperation(
  serverUrl: string,
  chainId: number,
  userOpHash: string,
  timeout: number = 60_000
): Promise<string> {
  const { createClient, http } = await import("viem");
  const { bundlerActions } = await import("viem/account-abstraction");

  const chain = await resolveChain(chainId);
  const rpcUrl = relayBundlerUrl(serverUrl);

  const bundlerClient = createClient({
    chain,
    transport: http(rpcUrl),
  }).extend(bundlerActions);

  const receipt = await bundlerClient.waitForUserOperationReceipt({
    hash: userOpHash as Hex,
    timeout,
  });

  return receipt.receipt.transactionHash;
}

// ---------------------------------------------------------------------------
// Public API: sendFactOnChain
// ---------------------------------------------------------------------------

/**
 * High-level function: build, sponsor, sign, submit a UserOperation, and
 * wait for on-chain confirmation.
 *
 * This is the primary entry point for writing an encrypted fact on-chain.
 * It combines buildUserOperation + submitUserOperation into a single call.
 *
 * @param config - Complete configuration for sending a fact on-chain
 * @returns Object with userOpHash and transactionHash
 */
export async function sendFactOnChain(
  config: SendFactConfig
): Promise<{ userOpHash: string; transactionHash: string }> {
  const {
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayload,
    serverUrl,
    timeout = 60_000,
  } = config;

  // Step 1: Build and submit the UserOperation
  const result = await buildUserOperation({
    privateKey,
    dataEdgeAddress,
    chainId,
    encryptedPayload,
    serverUrl,
  });

  // Step 2: Wait for on-chain confirmation
  const transactionHash = await submitUserOperation(
    serverUrl,
    chainId,
    result.userOpHash,
    timeout,
  );

  return {
    userOpHash: result.userOpHash,
    transactionHash,
  };
}
