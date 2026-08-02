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
 * Stage A tradeoff.
 *
 * Current model (Stage A + Stage B, both shipped — see
 * spa-passkey-unlock.md §7): this module is the RAM-speed resume path for a
 * same-tab reload (Stage A). The passkey-wrapped, TTL'd persistence that
 * survives a tab close lives separately in `lib/vault/idb.ts` (`VaultRecord`,
 * `VAULT_RECORD_TTL_SECONDS` — Stage B). The two are complementary, not
 * sequential: idb.ts is what a `locked` boot re-derives from after a
 * passkey assertion; this file is what skips that assertion entirely within
 * one tab's lifetime. (Previously this comment said Stage B was deferred —
 * it shipped; this text describes the model as it actually stands, per G-7
 * of the same spec.)
 *
 * Absolute session max-age (§7): `unlocked_at` is stamped once, at the
 * moment of a real passkey assertion (`saveSessionKeys` is only called from
 * `enterUnlocked`, never on a mere same-tab restore), and is NOT bumped by
 * a refresh. `loadSessionKeys()` itself enforces `SESSION_MAX_AGE_SECONDS` —
 * a session older than that reads as absent, forcing a fresh assertion on
 * next boot. This bounds a tab kept "active" by background activity well
 * past a human forgetting about it.
 */
import type { SessionKeys } from "../types";

const STORAGE_KEY = "totalreclaw-spa:session:v1";
const KEY_LEN = 32;

/**
 * Absolute session max-age (§7): independent of the idle timer — this
 * bounds total session lifetime even under constant activity. Not
 * user-configurable in this phase; the trade (convenience vs. an unattended
 * tab staying unlocked) is recorded here rather than left implicit.
 */
export const SESSION_MAX_AGE_SECONDS = 8 * 60 * 60; // 8 hours

interface StoredSession {
  v: 1;
  authKey: string; // base64
  encryptionKey: string; // base64
  authKeyHex: string;
  eoaAddress: string;
  walletAddress: string;
  chainId: number;
  /** Unix seconds — set once per real passkey assertion, not on refresh. */
  unlocked_at: number;
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
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

/**
 * Persist the unlocked session so a refresh in this tab can skip the
 * passkey. Called ONLY from `enterUnlocked()` — i.e. once per real passkey
 * assertion (bootstrap, unlock, restoreFromEscrow/File, phrase recovery) —
 * so `unlocked_at` anchors the 8h absolute-max-age window to that assertion,
 * not to any later same-tab restore.
 */
export function saveSessionKeys(keys: SessionKeys): void {
  const rec: StoredSession = {
    v: 1,
    authKey: bytesToB64(keys.authKey),
    encryptionKey: bytesToB64(keys.encryptionKey),
    authKeyHex: keys.authKeyHex,
    eoaAddress: keys.eoaAddress,
    walletAddress: keys.walletAddress,
    chainId: keys.chainId,
    unlocked_at: nowSeconds(),
  };
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(rec));
  } catch {
    // Storage unavailable/full (private browsing, quota) — non-fatal, the
    // next load just falls back to the normal passkey unlock.
  }
}

/**
 * Restore a persisted session, or null if absent, malformed, corrupt, OR
 * past `SESSION_MAX_AGE_SECONDS` (§7) — an expired session reads as though
 * it were never persisted, so the caller's existing "no session" fallback
 * (passkey re-auth) handles it with no extra branch.
 */
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
      typeof rec.chainId !== "number" ||
      typeof rec.unlocked_at !== "number"
    ) {
      return null;
    }
    // Absolute session max-age (§7): a session older than this reads as
    // absent, same as any other invalid record — the caller's existing
    // "no session → normal locked/no-vault flow" handles it unchanged.
    if (nowSeconds() - rec.unlocked_at > SESSION_MAX_AGE_SECONDS) {
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
