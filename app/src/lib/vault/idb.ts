/**
 * At-rest store for the passkey-wrapped vault. Holds ONLY wrapped keys +
 * public metadata — never a plaintext key, never the mnemonic (design §4.5).
 * Backed by idb-keyval (IndexedDB). One record per Smart Account.
 */
import { get, set, del, keys } from "idb-keyval";
import type { WrappedKey } from "../auth/wrap";

export interface VaultRecord {
  v: 1;
  /** lowercase 0x-prefixed Smart Account (CREATE2) address */
  smart_account: string;
  chain_id: number;
  /** base64url of the WebAuthn credential id */
  credential_id: string;
  wrapped_vault_key: WrappedKey;
  wrapped_auth_key: WrappedKey;
  wrapped_master_key: WrappedKey;
  created_at: number; // unix seconds
}

const PREFIX = "totalreclaw-spa:vault:";
const recordKey = (sa: string) => PREFIX + sa.toLowerCase();

export async function saveVaultRecord(rec: VaultRecord): Promise<void> {
  await set(recordKey(rec.smart_account), rec);
}

/** Load by Smart Account, or (when omitted) the first vault on this device. */
export async function loadVaultRecord(smartAccount?: string): Promise<VaultRecord | null> {
  if (smartAccount) {
    return ((await get<VaultRecord>(recordKey(smartAccount))) as VaultRecord | undefined) ?? null;
  }
  const all = await keys();
  const first = all.map(String).find((k) => k.startsWith(PREFIX));
  if (!first) return null;
  return ((await get<VaultRecord>(first)) as VaultRecord | undefined) ?? null;
}

export async function hasAnyVault(): Promise<boolean> {
  const all = await keys();
  return all.map(String).some((k) => k.startsWith(PREFIX));
}

/** Remove a vault's wrapped keys from THIS device. On-chain data is untouched. */
export async function clearVault(smartAccount: string): Promise<void> {
  await del(recordKey(smartAccount));
}
