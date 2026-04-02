/**
 * AES-256-GCM Encryption (WASM-backed)
 *
 * Thin wrappers over `@totalreclaw/core` WASM module. Same function
 * signatures as the previous pure-TS implementation so callers don't
 * need to change.
 *
 * Wire format (base64-encoded): [iv: 12 bytes][tag: 16 bytes][ciphertext: variable]
 *
 * Matches mcp/src/subgraph/crypto.ts:encrypt()/decrypt() exactly.
 */

import * as wasm from "@totalreclaw/core";
import { TotalReclawError, TotalReclawErrorCode } from "../types";

/** Key length for AES-256 (32 bytes) */
const KEY_LENGTH = 32;

/**
 * Encrypt a UTF-8 plaintext string with AES-256-GCM.
 *
 * Wire format (base64-encoded):
 *   [iv: 12 bytes][tag: 16 bytes][ciphertext: variable]
 *
 * Matches mcp/src/subgraph/crypto.ts:encrypt().
 *
 * @param plaintext - Text to encrypt
 * @param encryptionKey - 32-byte encryption key
 * @returns Base64-encoded string containing iv || tag || ciphertext
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
 * Decrypt a base64-encoded AES-256-GCM blob back to a UTF-8 string.
 *
 * Expects the wire format produced by encrypt() above.
 *
 * @param encryptedBase64 - Base64-encoded string of iv || tag || ciphertext
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
