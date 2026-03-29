/**
 * AES-256-GCM Encryption
 *
 * Provides authenticated encryption using AES-256 in GCM mode.
 * Wire format (base64-encoded): [iv: 12 bytes][tag: 16 bytes][ciphertext: variable]
 *
 * Matches mcp/src/subgraph/crypto.ts:encrypt()/decrypt() exactly.
 */

import * as crypto from "crypto";
import { TotalReclawError, TotalReclawErrorCode } from "../types";

/** IV length for GCM mode (12 bytes is recommended) */
const IV_LENGTH = 12;

/** Tag length for GCM mode (16 bytes = 128 bits) */
const TAG_LENGTH = 16;

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
    const iv = crypto.randomBytes(IV_LENGTH);
    const cipher = crypto.createCipheriv("aes-256-gcm", encryptionKey, iv, {
      authTagLength: TAG_LENGTH,
    });

    const ciphertext = Buffer.concat([
      cipher.update(plaintext, "utf8"),
      cipher.final(),
    ]);
    const tag = cipher.getAuthTag();

    // Combine: iv || tag || ciphertext
    const combined = Buffer.concat([iv, tag, ciphertext]);
    return combined.toString("base64");
  } catch (error) {
    if (error instanceof TotalReclawError) throw error;
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
    const combined = Buffer.from(encryptedBase64, "base64");

    if (combined.length < IV_LENGTH + TAG_LENGTH) {
      throw new Error("Encrypted data too short");
    }

    const iv = combined.subarray(0, IV_LENGTH);
    const tag = combined.subarray(IV_LENGTH, IV_LENGTH + TAG_LENGTH);
    const ciphertext = combined.subarray(IV_LENGTH + TAG_LENGTH);

    const decipher = crypto.createDecipheriv("aes-256-gcm", encryptionKey, iv, {
      authTagLength: TAG_LENGTH,
    });
    decipher.setAuthTag(tag);

    const plaintext = Buffer.concat([
      decipher.update(ciphertext),
      decipher.final(),
    ]);
    return plaintext.toString("utf8");
  } catch (error) {
    if (error instanceof TotalReclawError) throw error;
    throw new TotalReclawError(
      TotalReclawErrorCode.DECRYPTION_FAILED,
      `Decryption failed: ${error instanceof Error ? error.message : "Unknown error"}`,
    );
  }
}
