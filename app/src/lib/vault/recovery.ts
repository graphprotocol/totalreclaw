/**
 * Phrase escrow recovery — the pure, testable core behind
 * CryptoContext.restoreFromEscrow / restoreFromFile.
 *
 * Mirrors the split already established by lib/auth/master.ts: this module
 * holds the full recovery sequence (WebAuthn assertion → decrypt → derive →
 * register → re-wrap → persist) as plain async functions with no React
 * dependency, so it is unit-testable without a renderer. CryptoContext wires
 * these into React state (setStatus/setKeys) and the app's SERVER_URL/chain
 * constants.
 *
 * Spec: docs/specs/web/spa-passkey-unlock.md §6 (relay recovery) + §6.4/§7.4
 * (file recovery, phrase-escrow-relay.md).
 *
 * PHRASE-SAFETY: the mnemonic touches RAM only inside this module's call
 * stack, is never persisted, and every derived secret buffer is zeroed in a
 * `finally`, mirroring bootstrap()'s existing pattern in CryptoContext.tsx.
 */
import { SessionKeys } from "../types";
import { deriveSessionKeys, deriveEoaPrivateKey, bytesToHex } from "../crypto";
import { getPrfSecret } from "../auth/passkey";
import { deriveEscrowHandle, openEscrow, parseEscrowFile } from "../auth/escrow";
import { wrapKey, deriveMasterWrapSecret } from "../auth/wrap";
import { fetchEscrow, registerSession } from "../api";
import { saveVaultRecord, type VaultRecord } from "./idb";
import { b64urlEncode } from "../base64url";

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

export interface RecoveredSession {
  status: "unlocked";
  sk: SessionKeys;
  smartAccount: string;
  chainId: number;
  credentialId: Uint8Array;
}

export type EscrowRecoveryResult = RecoveredSession | { status: "no-escrow" };

export type FileRecoveryResult =
  | RecoveredSession
  | { status: "bad-file" }
  | { status: "unsupported-version" }
  | { status: "wrong-passkey" };

/**
 * Steps 5-9 of spa-passkey-unlock.md §6.1, shared verbatim by the relay and
 * file recovery paths — they converge the moment the mnemonic is in hand.
 *
 * Deliberately does NOT enrol a new passkey (spec §6.1): the passkey that
 * decrypted the escrow is by definition working on this device, and this is
 * the SAME prfSecret already asserted — no second biometric prompt.
 *
 * This is also the Phase 2 lazy-upgrade point (§2.2, §6.1 step 7): the
 * mnemonic and prfSecret are both in hand here, which is what minting a
 * `v: 2` VaultRecord will require. Kept as one function so that extension is
 * local, not a re-plumb.
 */
async function finishRecoveryFromMnemonic(
  mnemonic: string,
  prfSecret: Uint8Array,
  credentialId: Uint8Array,
  serverUrl: string,
  chainId: number,
): Promise<RecoveredSession> {
  let masterPriv: Uint8Array | null = null;
  let masterWrapSecret: Uint8Array | null = null;
  try {
    const sk = await deriveSessionKeys(mnemonic, serverUrl, chainId);
    masterPriv = await deriveEoaPrivateKey(mnemonic);
    await registerSession(sk);

    masterWrapSecret = deriveMasterWrapSecret(prfSecret);
    const rec: VaultRecord = {
      v: 1,
      smart_account: sk.walletAddress,
      chain_id: chainId,
      credential_id: b64urlEncode(credentialId),
      wrapped_vault_key: wrapKey(sk.encryptionKey, prfSecret),
      wrapped_auth_key: wrapKey(sk.authKey, prfSecret),
      wrapped_master_key: wrapKey(masterPriv, masterWrapSecret),
      created_at: nowSeconds(),
    };
    await saveVaultRecord(rec);

    return { status: "unlocked", sk, smartAccount: sk.walletAddress, chainId, credentialId };
  } finally {
    masterPriv?.fill(0);
    masterWrapSecret?.fill(0);
  }
}

/**
 * Recover a vault on a device with no local VaultRecord, using only a
 * (typically synced) passkey. See spa-passkey-unlock.md §6.1.
 *
 * Step 1 uses NO `allowCredentials` — discoverable-credential selection.
 * Enrolment already sets `residentKey: "required"`, so synced passkeys are
 * discoverable; this is the one place the SPA relies on that.
 */
export async function restoreFromEscrowCore(
  serverUrl: string,
  chainId: number,
): Promise<EscrowRecoveryResult> {
  const { prfSecret, credentialId } = await getPrfSecret();
  let handle: Uint8Array | null = null;
  try {
    handle = deriveEscrowHandle(prfSecret);
    const blob = await fetchEscrow(handle);
    if (!blob) return { status: "no-escrow" };

    // openEscrow throws (AEAD failure) on tamper/corruption — let it
    // propagate; the caller maps that to a distinct message (§6.2).
    const mnemonic = openEscrow(blob, handle, prfSecret);
    return await finishRecoveryFromMnemonic(mnemonic, prfSecret, credentialId, serverUrl, chainId);
  } finally {
    prfSecret.fill(0);
    handle?.fill(0);
  }
}

/**
 * Recover a vault from a downloaded encrypted backup file. See
 * spa-passkey-unlock.md §6.4 + phrase-escrow-relay.md §7.4.
 *
 * Parsing/shape validation happens BEFORE any WebAuthn call (parseEscrowFile
 * is pure) — an obviously-malformed file never prompts a biometric.
 */
export async function restoreFromFileCore(
  fileText: string,
  serverUrl: string,
  chainId: number,
): Promise<FileRecoveryResult> {
  const parsed = parseEscrowFile(fileText);
  if (!parsed.ok) {
    return parsed.error === "unsupported-version" ? { status: "unsupported-version" } : { status: "bad-file" };
  }

  const { prfSecret, credentialId } = await getPrfSecret();
  let handle: Uint8Array | null = null;
  try {
    handle = deriveEscrowHandle(prfSecret);
    let mnemonic: string;
    try {
      mnemonic = openEscrow(parsed.blob, handle, prfSecret);
    } catch {
      // Right file shape, wrong passkey (or tamper) — AEAD failure. Unlike
      // the relay path there is no lookup step, so the same underlying
      // condition surfaces here instead of as a 404 (relay spec §7.4).
      return { status: "wrong-passkey" };
    }
    return await finishRecoveryFromMnemonic(mnemonic, prfSecret, credentialId, serverUrl, chainId);
  } finally {
    prfSecret.fill(0);
    handle?.fill(0);
  }
}

// Re-exported for convenience at call sites that only need the hex form
// (kept internal to this module's own use today; harmless to export).
export { bytesToHex };
