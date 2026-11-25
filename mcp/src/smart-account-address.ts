/**
 * Smart Account address derivation, shared by managed-mode startup
 * (`index.ts` `initSubgraphState`) and the pair flow's relay ack
 * (`tools/pair.ts`, totalreclaw-relay#79).
 *
 * The address is deterministic (CREATE2, byte-equal on every chain), but
 * computing it goes through the EntryPoint (`getSenderAddress`, an RPC
 * round-trip) — core has no CREATE2 helper, so this stays the single
 * client-side implementation rather than growing a second one.
 */
import { mnemonicToAccount } from 'viem/accounts';
import { createPublicClient, http, type Address } from 'viem';
import { baseSepolia } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { entryPoint07Address } from 'viem/account-abstraction';

/** Lowercased Smart Account address for `mnemonic`. Throws on RPC failure. */
export async function deriveSmartAccountAddress(mnemonic: string): Promise<string> {
  const entryPointAddr = (process.env.TOTALRECLAW_ENTRYPOINT_ADDRESS || entryPoint07Address) as Address;
  const ownerAccount = mnemonicToAccount(mnemonic);

  // Smart Account address is deterministic via CREATE2 — use Base Sepolia for derivation
  // (same address regardless of chain).
  const publicClient = createPublicClient({
    chain: baseSepolia,
    transport: http(),
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

  return smartAccount.address.toLowerCase();
}

/**
 * Pair-ack resolver: the paired Smart Account, or `undefined` on ANY failure.
 * Never returns a fallback (e.g. the EOA) — the relay uses the value to look
 * up the account's plan, so a wrong address is worse than none.
 */
export async function resolvePairedSmartAccount(mnemonic: string): Promise<string | undefined> {
  try {
    return await deriveSmartAccountAddress(mnemonic);
  } catch {
    return undefined;
  }
}
