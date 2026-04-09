/**
 * XChaCha20-Poly1305 Encryption (WASM-backed)
 *
 * Thin wrappers over `@totalreclaw/core` WASM module.
 *
 * Wire format (base64-encoded): [nonce: 24 bytes][tag: 16 bytes][ciphertext: variable]
 */

import * as wasm from "@totalreclaw/core";
import { TotalReclawError, TotalReclawErrorCode } from "../types";

/** Key length (32 bytes) */
const KEY_LENGTH = 32;

/**
 * Encrypt a UTF-8 plaintext string with XChaCha20-Poly1305.
 *
 * Wire format (base64-encoded):
 *   [nonce: 24 bytes][tag: 16 bytes][ciphertext: variable]
 *
 * @param plaintext - Text to encrypt
 * @param encryptionKey - 32-byte encryption key
 * @returns Base64-encoded string containing nonce || tag || ciphertext
 */
export function encrypt(plaintext: string, encryptionKey: Buffer): string {
  if (encryptionKey.length !== KEY_LENGTH) {
    throw new TotalReclawError(
      TotalReclawErrorCode.ENCRYPTION_FAILED,
      `Invalid key length: expected ${KEY_LENGTH}, got ${encryptionKey.length}`,
    );
  }

  try {
    return wasm.encrypt(plaintext, encryptionKey.toString("hex"));
  } catch (error) {
    throw new TotalReclawError(
      TotalReclawErrorCode.ENCRYPTION_FAILED,
      `Encryption failed: ${error instanceof Error ? error.message : "Unknown error"}`,
    );
  }
}

/**
 * Decrypt a base64-encoded XChaCha20-Poly1305 blob back to a UTF-8 string.
 *
 * Expects the wire format produced by encrypt() above.
 *
 * @param encryptedBase64 - Base64-encoded string of nonce || tag || ciphertext
 * @param encryptionKey - 32-byte encryption key
 * @returns Decrypted UTF-8 string
 */
export function decrypt(encryptedBase64: string, encryptionKey: Buffer): string {
  if (encryptionKey.length !== KEY_LENGTH) {
    throw new TotalReclawError(
      TotalReclawErrorCode.DECRYPTION_FAILED,
      `Invalid key length: expected ${KEY_LENGTH}, got ${encryptionKey.length}`,
    );
  }

  try {
    return wasm.decrypt(encryptedBase64, encryptionKey.toString("hex"));
  } catch (error) {
    throw new TotalReclawError(
      TotalReclawErrorCode.DECRYPTION_FAILED,
      `Decryption failed: ${error instanceof Error ? error.message : "Unknown error"}`,
    );
  }
}
