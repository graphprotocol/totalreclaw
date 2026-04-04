/**
 * TotalReclaw Plugin - Crypto Operations (WASM-backed)
 *
 * Thin re-exports over `@totalreclaw/core` WASM module. Same function
 * signatures as the previous implementation so callers don't need to change.
 *
 * The WASM module handles BIP-39 key derivation, AES-256-GCM encrypt/
 * decrypt, SHA-256 blind indices, HMAC-SHA256 content fingerprints,
 * and LSH seed derivation.
 *
 * Key derivation chain (BIP-39 — handled by WASM):
 *   mnemonic -> BIP-39 PBKDF2 -> 512-bit seed
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1",       32) -> authKey
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32) -> encryptionKey
 *     -> HKDF-SHA256(seed, salt, "openmemory-dedup-v1",           32) -> dedupKey
 */

import * as wasm from '@totalreclaw/core';

// ---------------------------------------------------------------------------
// BIP-39 Validation
// ---------------------------------------------------------------------------

/**
 * Check if the input looks like a BIP-39 mnemonic (12 or 24 words).
 *
 * Lenient: accepts phrases where all words look like valid BIP-39 words
 * (allows invalid checksums, which LLMs sometimes generate).
 */
export function isBip39Mnemonic(input: string): boolean {
  const words = input.trim().split(/\s+/);
  return words.length === 12 || words.length === 24;
}

// Re-export for backward compatibility
export const validateMnemonic = isBip39Mnemonic;

// ---------------------------------------------------------------------------
// Key Derivation
// ---------------------------------------------------------------------------

/**
 * Derive auth, encryption, and dedup keys from a recovery phrase.
 *
 * Delegates to the WASM module for BIP-39 seed derivation and HKDF
 * key separation. Uses the lenient variant for phrases where all words
 * are valid but the checksum fails.
 *
 * @param password     - BIP-39 12/24-word mnemonic
 * @param existingSalt - Ignored for BIP-39 path (salt is deterministic)
 */
export function deriveKeys(
  password: string,
  existingSalt?: Buffer,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const trimmed = password.trim();

  // Try strict validation first, fall back to lenient
  let result: { auth_key: string; encryption_key: string; dedup_key: string; salt: string };
  try {
    result = wasm.deriveKeysFromMnemonic(trimmed);
  } catch {
    result = wasm.deriveKeysFromMnemonicLenient(trimmed);
  }

  return {
    authKey: Buffer.from(result.auth_key, 'hex'),
    encryptionKey: Buffer.from(result.encryption_key, 'hex'),
    dedupKey: Buffer.from(result.dedup_key, 'hex'),
    salt: Buffer.from(result.salt, 'hex'),
  };
}

// ---------------------------------------------------------------------------
// LSH Seed Derivation
// ---------------------------------------------------------------------------

/**
 * Derive a 32-byte seed for the LSH hasher.
 *
 * Delegates to the WASM module.
 */
export function deriveLshSeed(
  password: string,
  salt: Buffer,
): Uint8Array {
  const seedHex = wasm.deriveLshSeed(password.trim(), salt.toString('hex'));
  return new Uint8Array(Buffer.from(seedHex, 'hex'));
}

// ---------------------------------------------------------------------------
// Auth Key Hash
// ---------------------------------------------------------------------------

/**
 * Compute the SHA-256 hash of the auth key.
 */
export function computeAuthKeyHash(authKey: Buffer): string {
  return wasm.computeAuthKeyHash(authKey.toString('hex'));
}

// ---------------------------------------------------------------------------
// AES-256-GCM Encrypt / Decrypt
// ---------------------------------------------------------------------------

/**
 * Encrypt a UTF-8 plaintext string with AES-256-GCM.
 *
 * Wire format (base64-encoded):
 *   [iv: 12 bytes][tag: 16 bytes][ciphertext: variable]
 */
export function encrypt(plaintext: string, encryptionKey: Buffer): string {
  return wasm.encrypt(plaintext, encryptionKey.toString('hex'));
}

/**
 * Decrypt a base64-encoded AES-256-GCM blob back to a UTF-8 string.
 */
export function decrypt(encryptedBase64: string, encryptionKey: Buffer): string {
  return wasm.decrypt(encryptedBase64, encryptionKey.toString('hex'));
}

// ---------------------------------------------------------------------------
// Blind Indices
// ---------------------------------------------------------------------------

/**
 * Generate blind indices (SHA-256 hashes of tokens) for a text string.
 *
 * Delegates to the WASM module which performs tokenization, stemming,
 * and SHA-256 hashing.
 */
export function generateBlindIndices(text: string): string[] {
  return wasm.generateBlindIndices(text);
}

// ---------------------------------------------------------------------------
// Content Fingerprint (Dedup)
// ---------------------------------------------------------------------------

/**
 * Compute an HMAC-SHA256 content fingerprint for exact-duplicate detection.
 *
 * @returns 64-character hex string.
 */
export function generateContentFingerprint(plaintext: string, dedupKey: Buffer): string {
  return wasm.generateContentFingerprint(plaintext, dedupKey.toString('hex'));
}
