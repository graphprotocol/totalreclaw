/**
 * L3 — phrase-safety. Key-wrap for the passkey-PRF at-rest model.
 *
 * Wraps/unwraps a raw 32-byte key (vault key, auth key, or master wallet priv
 * key) under a 32-byte secret using XChaCha20-Poly1305 (same cipher the vault
 * uses for memory content). The wrap secret is the WebAuthn `prf` output; the
 * master-key entry uses an HKDF-domain-separated derivative so the two wrap
 * entries are independently rotatable.
 *
 * INVARIANTS:
 *   - Never log, print, or transmit key bytes or wrap secrets.
 *   - Wrapped blobs (ciphertext + nonce) are the ONLY form persisted at rest.
 *   - See docs/plans/2026-06-07-spa-functional-build-design.md §4.
 */
import { xchacha20poly1305 } from "@noble/ciphers/chacha";
import { hkdf } from "@noble/hashes/hkdf";
import { sha256 } from "@noble/hashes/sha256";

export interface WrappedKey {
  /** XChaCha20-Poly1305 ciphertext (key bytes ‖ Poly1305 tag, noble convention) */
  ciphertext: Uint8Array;
  /** 24-byte XChaCha nonce */
  nonce: Uint8Array;
  v: 1;
}

const KEY_LEN = 32;
const NONCE_LEN = 24;

/** Wrap a 32-byte key under a 32-byte secret. */
export function wrapKey(key: Uint8Array, wrapSecret: Uint8Array): WrappedKey {
  if (key.length !== KEY_LEN) throw new Error("wrapKey: key must be 32 bytes");
  if (wrapSecret.length !== KEY_LEN)
    throw new Error("wrapKey: wrapSecret must be 32 bytes");
  const nonce = crypto.getRandomValues(new Uint8Array(NONCE_LEN));
  const ciphertext = xchacha20poly1305(wrapSecret, nonce).encrypt(key);
  return { ciphertext, nonce, v: 1 };
}

/** Unwrap a WrappedKey. Throws (AEAD failure) on tamper or wrong secret. */
export function unwrapKey(blob: WrappedKey, wrapSecret: Uint8Array): Uint8Array {
  if (wrapSecret.length !== KEY_LEN)
    throw new Error("unwrapKey: wrapSecret must be 32 bytes");
  if (blob.nonce.length !== NONCE_LEN)
    throw new Error("unwrapKey: bad nonce length");
  return xchacha20poly1305(wrapSecret, blob.nonce).decrypt(blob.ciphertext);
}

/**
 * Domain-separated wrap secret for the master-wallet entry. Derived from the
 * same PRF secret as the vault wrap, with a distinct HKDF info string so the
 * master and vault wraps are cryptographically independent + independently
 * rotatable.
 */
export function deriveMasterWrapSecret(prfSecret: Uint8Array): Uint8Array {
  if (prfSecret.length !== KEY_LEN)
    throw new Error("deriveMasterWrapSecret: prfSecret must be 32 bytes");
  return hkdf(sha256, prfSecret, undefined, "tr-master-wrap-v1", KEY_LEN);
}
