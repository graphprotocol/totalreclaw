/**
 * ERC-4337 v0.7 UserOp assembly for Keeper curation writes (A.2).
 *
 * Byte-for-byte mirrors the plugin/mcp write path (`skill/plugin/subgraph/
 * subgraph-store.ts`): WASM core encodes the calldata (`encodeSingleCallTo` /
 * `encodeBatchCallTo`) + hashes the UserOp (`hashUserOp`); the relay proxies
 * bundler/paymaster JSON-RPC. The ONE deviation from the plugin: the SPA never
 * touches the mnemonic — signing goes through the caller-supplied `sign`, which
 * is `CryptoContext.withMasterKey` (transient PRF-unwrap of the master key).
 *
 * WRITE-path module: reached only via a dynamic `import()` from `api.ts` so the
 * 2.3 MB WASM stays out of the initial read chunk. See `wasm.ts`.
 */
import { SessionKeys } from "./types";
import { bytesToHex } from "./crypto";
import { loadCore } from "./wasm";
import {
  getUserOperationGasPrice,
  estimateUserOperationGas,
  sponsorUserOperation,
  sendUserOperation,
  getUserOperationReceipt,
  // Node reads (eth_call nonce + eth_getCode) — served by the relay since relay#37.
  getNonce,
  getCode,
} from "./bundler";

/** Memory Taxonomy v1 outer protobuf version — tombstones ride v4 like writes. */
export const PROTOBUF_VERSION_V4 = 4;

/**
 * Signs a 32-byte UserOp hash (hex, no 0x) and returns the 65-byte ECDSA
 * signature (r+s+v). Implemented by `withMasterKey` → WASM `signUserOp`.
 */
export type SignUserOpHash = (userOpHashHex: string) => Promise<string>;

export interface WriteContext {
  keys: SessionKeys;
  /** Authoritative DataEdge for the wallet's chain (relay billing
   *  `data_edge_address` — staging Gnosis 0xE7a4… ≠ prod 0xC445…). */
  dataEdgeAddress: string;
  chainId: number;
  sign: SignUserOpHash;
}

export interface SubmitResult {
  userOpHash: string;
  txHash: string;
  success: boolean;
}

// Structurally-valid stub signature for gas estimation / sponsorship. ecrecover
// yields a non-owner (SIG_VALIDATION_FAILED, not a revert) — same constant the
// plugin + permissionless/viem use so simulation gas matches the real op.
const DUMMY_SIGNATURE =
  "0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c";

const RECEIPT_POLL_ATTEMPTS = 60;
const RECEIPT_POLL_INTERVAL_MS = 2000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Assemble, sponsor, sign, submit, and confirm one v0.7 UserOp carrying N
 * protobuf calls (1 = `execute`, N = `executeBatch`) to the DataEdge.
 *
 * The Smart Account must already be deployed — curation edits target existing
 * on-chain memories, so a counterfactual (undeployed) account has nothing to
 * modify. This also means the SPA never needs the EOA address (only available
 * via the seed, which stays sealed) to build `initCode`.
 */
export async function submitUserOp(
  payloads: Uint8Array[],
  ctx: WriteContext,
): Promise<SubmitResult> {
  if (payloads.length === 0) throw new Error("submitUserOp: no payloads");
  const core = await loadCore();
  const entryPoint = core.getEntryPointAddress();
  const sender = ctx.keys.walletAddress;

  // 1. Calldata (execute / executeBatch → DataEdge fallback()).
  const calldataBytes =
    payloads.length === 1
      ? core.encodeSingleCallTo(payloads[0], ctx.dataEdgeAddress)
      : core.encodeBatchCallTo(
          JSON.stringify(payloads.map((p) => bytesToHex(p))),
          ctx.dataEdgeAddress,
        );
  const callData = `0x${bytesToHex(calldataBytes)}`;

  // 2. Gas price.
  const gas = await getUserOperationGasPrice(ctx.keys);
  const fast = gas.fast;

  // 3. Require a deployed Smart Account (see doc comment). Node read via relay.
  const code = await getCode(ctx.keys, sender);
  if (!code || code === "0x" || code === "0x0") {
    throw new Error(
      "This vault has no on-chain memories yet, so there is nothing to modify.",
    );
  }

  // 4. Nonce (node read via relay).
  const nonce = await getNonce(ctx.keys, entryPoint, sender);

  // 5. Unsigned op (v0.7, camelCase for the Rust serde in hashUserOp).
  const unsignedOp: Record<string, unknown> = {
    sender,
    nonce,
    callData,
    callGasLimit: "0x0",
    verificationGasLimit: "0x0",
    preVerificationGas: "0x0",
    maxFeePerGas: fast.maxFeePerGas,
    maxPriorityFeePerGas: fast.maxPriorityFeePerGas,
    signature: DUMMY_SIGNATURE,
  };

  // 6. Batches: estimate gas before sponsorship (can't bump after — invalidates
  //    the paymaster signature → AA34). Single-call ops let the paymaster fill it.
  if (payloads.length > 1) {
    try {
      const est = await estimateUserOperationGas(ctx.keys, unsignedOp, entryPoint);
      if (est.callGasLimit) unsignedOp.callGasLimit = est.callGasLimit;
      if (est.verificationGasLimit) unsignedOp.verificationGasLimit = est.verificationGasLimit;
      if (est.preVerificationGas) unsignedOp.preVerificationGas = est.preVerificationGas;
    } catch {
      /* fall back to paymaster-provided limits */
    }
  }

  // 7. Paymaster sponsorship (fills gas limits + paymaster fields).
  const sponsor = await sponsorUserOperation(ctx.keys, unsignedOp, entryPoint);
  Object.assign(unsignedOp, sponsor);

  // 8. Hash (WASM) → sign (withMasterKey → WASM signUserOp) → attach.
  const hashHex = core.hashUserOp(JSON.stringify(unsignedOp), entryPoint, BigInt(ctx.chainId));
  const sigHex = await ctx.sign(hashHex);
  unsignedOp.signature = sigHex.startsWith("0x") ? sigHex : `0x${sigHex}`;

  // 9. Submit.
  const userOpHash = await sendUserOperation(ctx.keys, unsignedOp, entryPoint);

  // 10. Confirm on-chain (poll up to ~120s).
  let receipt = null;
  for (let i = 0; i < RECEIPT_POLL_ATTEMPTS; i++) {
    await sleep(RECEIPT_POLL_INTERVAL_MS);
    try {
      receipt = await getUserOperationReceipt(ctx.keys, userOpHash);
      if (receipt) break;
    } catch {
      /* not mined yet */
    }
  }

  return {
    userOpHash,
    txHash: receipt?.receipt?.transactionHash ?? "",
    success: receipt?.success ?? false,
  };
}

/**
 * Tombstone (soft-delete) N facts in a single UserOp. `factId` == `VaultItem.id`
 * (subgraph Fact.id / protobuf field 1) — directly usable, no canonicalization.
 */
export async function submitTombstones(
  factIds: string[],
  ctx: WriteContext,
): Promise<SubmitResult> {
  if (factIds.length === 0) throw new Error("submitTombstones: no fact ids");
  const core = await loadCore();
  const owner = ctx.keys.walletAddress.toLowerCase();
  const payloads = factIds.map((id) =>
    core.encodeTombstoneProtobuf(id, owner, PROTOBUF_VERSION_V4),
  );
  return submitUserOp(payloads, ctx);
}
