/**
 * JSON-RPC client for the relay's `/v1/bundler` proxy (ERC-4337).
 *
 * The relay proxies these calls to the Pimlico bundler/paymaster with its own
 * API key — clients never hold a Pimlico key. Auth is the same Bearer + wallet
 * headers the read path already sends.
 *
 * Scope: the bundler/paymaster methods (gas price, sponsor, estimate, send,
 * receipt) PLUS the two node reads ERC-4337 needs — `eth_call` for the
 * EntryPoint nonce and `eth_getCode` for the deployment check. Since relay#37
 * the relay routes those two to the chain's node (tier-coherent, with an
 * EntryPoint `to`-constraint on `eth_call`), so no third-party RPC is ever
 * contacted and the Smart Account address never leaves the relay.
 *
 * This is a WRITE-path module — reached only via the lazy `userop.ts` chunk.
 */
import { SessionKeys } from "./types";
import { getServerUrl } from "./api";

export interface GasPriceTier {
  maxFeePerGas: string;
  maxPriorityFeePerGas: string;
}
export interface GasPriceResult {
  slow: GasPriceTier;
  standard: GasPriceTier;
  fast: GasPriceTier;
}

/** Fields the paymaster fills in during sponsorship. */
export interface SponsorResult {
  paymaster?: string;
  paymasterData?: string;
  paymasterVerificationGasLimit?: string;
  paymasterPostOpGasLimit?: string;
  callGasLimit?: string;
  verificationGasLimit?: string;
  preVerificationGas?: string;
  [k: string]: unknown;
}

export interface GasEstimateResult {
  callGasLimit?: string;
  verificationGasLimit?: string;
  preVerificationGas?: string;
  [k: string]: unknown;
}

export interface UserOperationReceipt {
  success: boolean;
  receipt?: { transactionHash?: string; [k: string]: unknown };
  [k: string]: unknown;
}

function bundlerHeaders(keys: SessionKeys): HeadersInit {
  return {
    "Content-Type": "application/json",
    Accept: "application/json",
    Authorization: `Bearer ${keys.authKeyHex}`,
    "X-Wallet-Address": keys.walletAddress,
    "X-TotalReclaw-Client": "ts-spa-vault",
  };
}

/** Raw JSON-RPC call to the relay bundler proxy. Throws on HTTP or RPC error. */
export async function bundlerRpc<T>(
  keys: SessionKeys,
  method: string,
  params: unknown[],
): Promise<T> {
  const res = await fetch(`${getServerUrl()}/v1/bundler`, {
    method: "POST",
    headers: bundlerHeaders(keys),
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`bundler ${method} → ${res.status}: ${body}`);
  }
  const json = (await res.json()) as { result?: T; error?: { message?: string } };
  if (json.error) {
    throw new Error(`bundler ${method}: ${json.error.message ?? JSON.stringify(json.error)}`);
  }
  return json.result as T;
}

export function getUserOperationGasPrice(keys: SessionKeys): Promise<GasPriceResult> {
  return bundlerRpc<GasPriceResult>(keys, "pimlico_getUserOperationGasPrice", []);
}

export function estimateUserOperationGas(
  keys: SessionKeys,
  unsignedOp: Record<string, unknown>,
  entryPoint: string,
): Promise<GasEstimateResult> {
  return bundlerRpc<GasEstimateResult>(keys, "eth_estimateUserOperationGas", [
    unsignedOp,
    entryPoint,
  ]);
}

export function sponsorUserOperation(
  keys: SessionKeys,
  unsignedOp: Record<string, unknown>,
  entryPoint: string,
): Promise<SponsorResult> {
  return bundlerRpc<SponsorResult>(keys, "pm_sponsorUserOperation", [unsignedOp, entryPoint]);
}

export function sendUserOperation(
  keys: SessionKeys,
  signedOp: Record<string, unknown>,
  entryPoint: string,
): Promise<string> {
  return bundlerRpc<string>(keys, "eth_sendUserOperation", [signedOp, entryPoint]);
}

/** EntryPoint.getNonce(address,uint192) selector. */
const GET_NONCE_SELECTOR = "35567e1a";

/** Read the EntryPoint nonce for `sender` (key = 0). Node read via the relay
 *  (`eth_call` is `to`-constrained to the EntryPoint on the relay side). */
export async function getNonce(
  keys: SessionKeys,
  entryPoint: string,
  sender: string,
): Promise<string> {
  const senderPadded = sender.slice(2).toLowerCase().padStart(64, "0");
  const calldata = `0x${GET_NONCE_SELECTOR}${senderPadded}${"0".repeat(64)}`;
  const result = await bundlerRpc<string>(keys, "eth_call", [
    { to: entryPoint, data: calldata },
    "latest",
  ]);
  return result || "0x0";
}

/** Bytecode at `address` — `0x` / `0x0` means the Smart Account is undeployed.
 *  Node read via the relay. */
export function getCode(keys: SessionKeys, address: string): Promise<string> {
  return bundlerRpc<string>(keys, "eth_getCode", [address, "latest"]);
}

export function getUserOperationReceipt(
  keys: SessionKeys,
  userOpHash: string,
): Promise<UserOperationReceipt | null> {
  return bundlerRpc<UserOperationReceipt | null>(keys, "eth_getUserOperationReceipt", [userOpHash]);
}
