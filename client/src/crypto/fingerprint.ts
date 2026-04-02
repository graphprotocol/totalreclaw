/**
 * Content Fingerprint Derivation (WASM-backed, v0.3.1b)
 *
 * Thin wrappers over `@totalreclaw/core` WASM module for text
 * normalization and HMAC-SHA256 content fingerprinting.
 *
 * The `deriveDedupKey` function stays in TypeScript because it uses
 * the Argon2id legacy path's HKDF (not BIP-39). For the BIP-39 path,
 * the dedup key is returned directly by `deriveKeysFromMnemonic()`.
 *
 * Spec: docs/specs/totalreclaw/server.md v0.3.1b section 8.2
 */

import * as crypto from 'crypto';
import * as wasm from '@totalreclaw/core';

/**
 * HKDF context string for dedup key derivation.
 * Separate from auth ("totalreclaw-auth-key-v1") and encryption
 * ("totalreclaw-encryption-key-v1") context strings so that key
 * rotation of one does not affect the others.
 */
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

/**
 * Normalize text for deterministic fingerprinting.
 *
 * Delegates to WASM which applies:
 * 1. Unicode NFC normalization
 * 2. Lowercase
 * 3. Collapse whitespace (multiple spaces/tabs/newlines to single space)
 * 4. Trim leading/trailing whitespace
 *
 * @param text - Raw plaintext
 * @returns Normalized text ready for HMAC
 */
export function normalizeText(text: string): string {
  return wasm.normalizeText(text);
}

/**
 * Derive the dedup key from the master key material using HKDF-SHA256.
 *
 * This uses a separate HKDF context string ("openmemory-dedup-v1") so that
 * the dedup key is independent of the auth and encryption keys.
 *
 * Note: This stays in TypeScript because it serves the Argon2id legacy
 * path. For the BIP-39 path, use `deriveKeysFromMnemonic().dedupKey`.
 *
 * @param masterKeyMaterial - The Argon2id-derived master key (or encryption key)
 * @param salt - The user's salt
 * @returns 32-byte dedup key
 */
export function deriveDedupKey(masterKeyMaterial: Buffer, salt: Buffer): Buffer {
  // Extract phase
  const prk = crypto.createHmac('sha256', salt).update(masterKeyMaterial).digest();

  // Expand phase (single round, 32 bytes = one SHA-256 block)
  const info = Buffer.from(DEDUP_KEY_INFO, 'utf-8');
  const hmac = crypto.createHmac('sha256', prk);
  hmac.update(info);
  hmac.update(Buffer.from([1])); // counter = 1
  return hmac.digest();
}

/**
 * Compute a content fingerprint for exact dedup.
 *
 * Delegates to the WASM module which normalizes the text and computes
 * HMAC-SHA256(dedupKey, normalized).
 *
 * @param dedupKey - 32-byte dedup key from deriveDedupKey()
 * @param plaintext - The fact text before encryption
 * @returns Hex-encoded HMAC-SHA256 fingerprint (64 chars)
 */
export function computeContentFingerprint(dedupKey: Buffer, plaintext: string): string {
  return wasm.generateContentFingerprint(plaintext, dedupKey.toString('hex'));
}
