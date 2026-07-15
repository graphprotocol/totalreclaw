/**
 * L3 — phrase-safety. Transient master-key unwrap for curation writes (A.2).
 *
 * The master wallet EOA private key is persisted ONLY as `wrapped_master_key`
 * (see wrap.ts / idb.ts), wrapped under an HKDF-domain-separated derivative of
 * the passkey PRF secret. It is unwrapped transiently to sign a single ERC-4337
 * UserOp, then zeroed. The mnemonic is never involved (A.1 mnemonic-never-at-
 * rest is preserved) and the key is NEVER derived from the seed in app code.
 *
 * This module holds the pure unwrap→run→zero core so it can be unit-tested
 * without a WebAuthn assertion; `CryptoContext.withMasterKey` wires it to a
 * live `getPrfSecret()` assertion (and zeroes the PRF secret itself).
 *
 * INVARIANTS:
 *   - Never log, print, or transmit the master key, PRF secret, or wrap secret.
 *   - The unwrapped key buffer is zeroed in a `finally`, even on error.
 */
import { unwrapKey, deriveMasterWrapSecret, type WrappedKey } from "./wrap";

/**
 * Unwrap the master wallet private key from `wrapped_master_key` under a PRF
 * secret, run `fn` with the raw 32 bytes, then best-effort zero the key.
 *
 * The caller owns the lifecycle of `prfSecret` (it is NOT zeroed here — the
 * live `withMasterKey` path zeroes it in its own `finally`, mirroring
 * `unlock()`). This function only zeroes the buffers it creates.
 */
export async function runWithMasterKey<T>(
  wrappedMasterKey: WrappedKey,
  prfSecret: Uint8Array,
  fn: (masterPriv: Uint8Array) => Promise<T>,
): Promise<T> {
  let masterWrapSecret: Uint8Array | null = null;
  let masterPriv: Uint8Array | null = null;
  try {
    masterWrapSecret = deriveMasterWrapSecret(prfSecret);
    masterPriv = unwrapKey(wrappedMasterKey, masterWrapSecret);
    return await fn(masterPriv);
  } finally {
    masterPriv?.fill(0);
    masterWrapSecret?.fill(0);
  }
}
