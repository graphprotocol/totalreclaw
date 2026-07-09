/**
 * Stage A (#440) — sessionStorage persistence for the unlocked vault session.
 *
 * Kills the "refresh forces a passkey re-auth" annoyance by surviving a
 * same-tab reload. Cleared automatically on tab close (sessionStorage
 * semantics) or explicitly on lock/forget-device.
 *
 * PHRASE-SAFETY: this persists exactly the `SessionKeys` shape CryptoContext
 * already holds unlocked in RAM — the derived vault key (`encryptionKey`)
 * and derived auth key (`authKey`), plus public routing metadata. It NEVER
 * touches the mnemonic; `SessionKeys` structurally cannot carry one (see
 * lib/types.ts). Origin-readable, tab-lifetime exposure is the deliberate
 * Stage A tradeoff — Stage B (IndexedDB wrapped-key + TTL) is deferred.
 */
import type { SessionKeys } from "../types";

const STORAGE_KEY = "totalreclaw-spa:session:v1";
const KEY_LEN = 32;

interface StoredSession {
  v: 1;
  authKey: string; // base64
  encryptionKey: string; // base64
  authKeyHex: string;
  eoaAddress: string;
  walletAddress: string;
  chainId: number;
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

function b64ToBytes(s: string): Uint8Array {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Persist the unlocked session so a refresh in this tab can skip the passkey. */
export function saveSessionKeys(keys: SessionKeys): void {
  const rec: StoredSession = {
    v: 1,
    authKey: bytesToB64(keys.authKey),
    encryptionKey: bytesToB64(keys.encryptionKey),
    authKeyHex: keys.authKeyHex,
    eoaAddress: keys.eoaAddress,
    walletAddress: keys.walletAddress,
    chainId: keys.chainId,
  };
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(rec));
  } catch {
    // Storage unavailable/full (private browsing, quota) — non-fatal, the
    // next load just falls back to the normal passkey unlock.
  }
}

/** Restore a persisted session, or null if absent, malformed, or corrupt. */
export function loadSessionKeys(): SessionKeys | null {
  let raw: string | null;
  try {
    raw = sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const rec = JSON.parse(raw) as Partial<StoredSession>;
    if (
      rec.v !== 1 ||
      typeof rec.authKey !== "string" ||
      typeof rec.encryptionKey !== "string" ||
      typeof rec.authKeyHex !== "string" ||
      typeof rec.walletAddress !== "string" ||
      typeof rec.chainId !== "number"
    ) {
      return null;
    }
    const authKey = b64ToBytes(rec.authKey);
    const encryptionKey = b64ToBytes(rec.encryptionKey);
    if (authKey.length !== KEY_LEN || encryptionKey.length !== KEY_LEN) return null;
    return {
      authKey,
      encryptionKey,
      authKeyHex: rec.authKeyHex,
      eoaAddress: rec.eoaAddress ?? "",
      walletAddress: rec.walletAddress,
      chainId: rec.chainId,
    };
  } catch {
    // Malformed JSON / bad base64 — never crash the app over a stale record.
    return null;
  }
}

/** Drop the persisted session (lock / forget-device). */
export function clearSessionKeys(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
