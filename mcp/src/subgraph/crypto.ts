/**
 * TotalReclaw MCP - Crypto Operations (WASM-backed)
 *
 * Thin wrappers over `@totalreclaw/core` WASM module. Same function
 * signatures as the previous pure-TS implementation so callers don't
 * need to change.
 *
 * The WASM module handles BIP-39 key derivation, AES-256-GCM encrypt/
 * decrypt, SHA-256 blind indices, HMAC-SHA256 content fingerprints,
 * and LSH seed derivation.
 *
 * The legacy Argon2id path (arbitrary password, not a BIP-39 mnemonic)
 * is kept in TypeScript because the WASM module only supports BIP-39.
 *
 * Key derivation chain (BIP-39 path — handled by WASM):
 *   mnemonic -> BIP-39 PBKDF2 -> 512-bit seed
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1",       32) -> authKey
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32) -> encryptionKey
 *     -> HKDF-SHA256(seed, salt, "openmemory-dedup-v1",           32) -> dedupKey
 *
 * Key derivation chain (Argon2id legacy path — kept in TS):
 *   password + salt -> Argon2id(t=3, m=65536, p=4, dkLen=32) -> masterKey
 *     -> HKDF-SHA256 per sub-key (same info strings as above)
 */

import { argon2id } from '@noble/hashes/argon2.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import crypto from 'node:crypto';

import * as wasm from '@totalreclaw/core';

// Re-export validateMnemonic so existing imports from this module continue to work
export { validateMnemonic };

// ---------------------------------------------------------------------------
// Constants (only needed for the Argon2id legacy path)
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';
const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

const ARGON2_TIME_COST = 3;
const ARGON2_MEMORY_COST = 65536;
const ARGON2_PARALLELISM = 4;
const ARGON2_DK_LEN = 32;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Check if the input looks like a BIP-39 mnemonic (12 or 24 words from
 * the BIP-39 English wordlist).
 */
function isBip39Mnemonic(input: string): boolean {
  const words = input.trim().split(/\s+/);
  if (words.length !== 12 && words.length !== 24) return false;
  return validateMnemonic(input.trim(), wordlist);
}

// ---------------------------------------------------------------------------
// Key Derivation
// ---------------------------------------------------------------------------

/**
 * Derive encryption keys from a BIP-39 mnemonic.
 *
 * Delegates to the WASM module which performs BIP-39 seed derivation
 * and HKDF key separation.
 */
export function deriveKeysFromMnemonic(
  mnemonic: string,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const result = wasm.deriveKeysFromMnemonic(mnemonic.trim());
  return {
    authKey: Buffer.from(result.auth_key, 'hex'),
    encryptionKey: Buffer.from(result.encryption_key, 'hex'),
    dedupKey: Buffer.from(result.dedup_key, 'hex'),
    salt: Buffer.from(result.salt, 'hex'),
  };
}

/**
 * Derive auth, encryption, and dedup keys from a recovery phrase.
 *
 * If the password is a valid BIP-39 mnemonic (12 or 24 words), keys are
 * derived via the WASM module. Otherwise, the legacy Argon2id path is used.
 */
export function deriveKeys(
  password: string,
  existingSalt?: Buffer,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  if (isBip39Mnemonic(password)) {
    return deriveKeysFromMnemonic(password);
  }

  // Legacy path: arbitrary password via Argon2id (not in WASM)
  const salt = existingSalt ?? crypto.randomBytes(32);

  const masterKey = argon2id(
    Buffer.from(password, 'utf8'),
    salt,
    { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
  );

  const enc = (s: string) => Buffer.from(s, 'utf8');
  const authKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(AUTH_KEY_INFO), 32),
  );
  const encryptionKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(DEDUP_KEY_INFO), 32),
  );

  return { authKey, encryptionKey, dedupKey, salt: Buffer.from(salt) };
}

// ---------------------------------------------------------------------------
// LSH Seed Derivation
// ---------------------------------------------------------------------------

/**
 * Derive a 32-byte seed for the LSH hasher.
 *
 * For the BIP-39 path, delegates to the WASM module. For the Argon2id
 * legacy path, stays in TypeScript.
 */
export function deriveLshSeed(
  password: string,
  salt: Buffer,
): Uint8Array {
  if (isBip39Mnemonic(password)) {
    const seedHex = wasm.deriveLshSeed(password.trim(), salt.toString('hex'));
    return new Uint8Array(Buffer.from(seedHex, 'hex'));
  }

  // Argon2id path: re-derive the master key, then HKDF with LSH info string.
  const LSH_SEED_INFO = 'openmemory-lsh-seed-v1';
  const masterKey = argon2id(
    Buffer.from(password, 'utf8'),
    salt,
    { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
  );

  return new Uint8Array(
    hkdf(sha256, masterKey, salt, Buffer.from(LSH_SEED_INFO, 'utf8'), 32),
  );
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
