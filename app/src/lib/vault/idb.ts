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

/**
 * Defense-in-depth sliding TTL on the wrapped-key record (#440).
 *
 * The record already stores only passkey-wrapped keys — the mnemonic is NEVER
 * at rest (design §4.5) — so this is not a primary secrets control. It bounds
 * how long an abandoned device's wrapped keys survive in IndexedDB: a record
 * older than this is treated as absent on load (deleted; the UI falls back to
 * the normal passkey re-bootstrap / recovery-phrase flow).
 * `refreshVaultRecordTtl` bumps `created_at` on every successful passkey
 * unlock, so an actively-used vault never ages out — only an idle one does.
 */
export const VAULT_RECORD_TTL_SECONDS = 30 * 24 * 60 * 60; // 30 days

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

const recordKey = (sa: string) => PREFIX + sa.toLowerCase();

export async function saveVaultRecord(rec: VaultRecord): Promise<void> {
  await set(recordKey(rec.smart_account), rec);
}

/**
 * Is a record past its TTL? The clock is injected so the boundary is testable
 * exactly: a record whose age EQUALS the TTL is still live (strict `>`).
 */
export function isVaultRecordExpired(rec: VaultRecord, now: number): boolean {
  return now - rec.created_at > VAULT_RECORD_TTL_SECONDS;
}

/**
 * Sliding-TTL refresh: bump `created_at` to now and re-save. Call on every
 * successful passkey unlock so an active vault never ages out (#440). Wrapped
 * keys are carried through byte-identical — only the timestamp moves.
 */
export async function refreshVaultRecordTtl(rec: VaultRecord): Promise<void> {
  await saveVaultRecord({ ...rec, created_at: nowSeconds() });
}

/** Return `rec` only if it is live; otherwise delete it (treated as absent) and return null. */
async function liveOrNull(rec: VaultRecord | null): Promise<VaultRecord | null> {
  if (!rec || !isVaultRecordExpired(rec, nowSeconds())) return rec;
  await del(recordKey(rec.smart_account));
  return null;
}

/** Load by Smart Account, or (when omitted) the first vault on this device. */
export async function loadVaultRecord(smartAccount?: string): Promise<VaultRecord | null> {
  if (smartAccount) {
    const rec = ((await get<VaultRecord>(recordKey(smartAccount))) as VaultRecord | undefined) ?? null;
    return liveOrNull(rec);
  }
  const all = await keys();
  const first = all.map(String).find((k) => k.startsWith(PREFIX));
  if (!first) return null;
  const rec = ((await get<VaultRecord>(first)) as VaultRecord | undefined) ?? null;
  return liveOrNull(rec);
}

/**
 * Any live wrapped-key record on this device? Expired records encountered here
 * are treated as absent — deleted — so a device whose only record has aged out
 * reports `false` and the UI falls back to bootstrap/recovery (#440).
 */
export async function hasAnyVault(): Promise<boolean> {
  const all = await keys();
  const vaultKeys = all.map(String).filter((k) => k.startsWith(PREFIX));
  let anyLive = false;
  for (const k of vaultKeys) {
    const rec = (await get<VaultRecord>(k)) as VaultRecord | undefined;
    if (rec && !isVaultRecordExpired(rec, nowSeconds())) {
      anyLive = true;
    } else if (rec) {
      await del(k); // expired → purge
    }
  }
  return anyLive;
}

/** Remove a vault's wrapped keys from THIS device. On-chain data is untouched. */
export async function clearVault(smartAccount: string): Promise<void> {
  await del(recordKey(smartAccount));
}
