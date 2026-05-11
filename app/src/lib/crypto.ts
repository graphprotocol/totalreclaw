/**
 * Browser-native CryptoProvider
 *
 * TS port of client/src/crypto/ for browser use (no WASM, no Node.js).
 * Uses @scure/bip39 for mnemonic, WebCrypto for HKDF, @noble/ciphers for XChaCha20.
 * Swap the XChaCha20 path to @totalreclaw/core WASM when #104 ships (rc.16+).
 */

import { validateMnemonic, mnemonicToSeed } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english";
import { xchacha20poly1305 } from "@noble/ciphers/chacha";
import { SessionKeys } from "./types";

// HKDF-SHA256 via WebCrypto SubtleCrypto
async function hkdf(
  ikm: Uint8Array,
  salt: Uint8Array,
  info: string,
  length: number,
): Promise<Uint8Array> {
  const baseKey = await crypto.subtle.importKey(
    "raw",
    ikm as unknown as ArrayBuffer,
    "HKDF",
    false,
    ["deriveBits"],
  );
  const derived = await crypto.subtle.deriveBits(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: salt as unknown as ArrayBuffer,
      info: new TextEncoder().encode(info),
    },
    baseKey,
    length * 8,
  );
  return new Uint8Array(derived);
}

function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) throw new Error("invalid hex");
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    out[i / 2] = parseInt(hex.slice(i, i + 2), 16);
  }
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

export function isMnemonicValid(phrase: string): boolean {
  return validateMnemonic(phrase.trim().toLowerCase(), wordlist);
}

/**
 * Derive session keys from a 12-word BIP-39 mnemonic.
 *
 * Matches client/src/crypto/seed.ts deriveKeysFromMnemonic() + mcp/src/subgraph/crypto.ts:
 *   seed = BIP-39 PBKDF2(mnemonic, "mnemonic", 2048, SHA-512, 64 bytes)
 *   salt = seed[0:32]
 *   authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1",       32)
 *   encKey  = HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32)
 *
 * BIP-32 derivation is NOT used for session keys (only needed for UserOp signing).
 */
export async function deriveSessionKeys(mnemonic: string): Promise<SessionKeys> {
  const normalized = mnemonic.trim().toLowerCase();
  if (!isMnemonicValid(normalized)) {
    throw new Error("Invalid 12-word recovery phrase");
  }

  const seed = await mnemonicToSeed(normalized); // 512-bit BIP-39 seed
  const salt = seed.slice(0, 32);
  const authKey = await hkdf(seed, salt, "totalreclaw-auth-key-v1", 32);
  const encryptionKey = await hkdf(seed, salt, "totalreclaw-encryption-key-v1", 32);

  return {
    mnemonic: normalized,
    authKey,
    encryptionKey,
    authKeyHex: bytesToHex(authKey),
  };
}

/**
 * Decrypt an encrypted vault fact blob.
 *
 * Wire format: nonce[24] || tag[16] || ciphertext
 * Input can be base64 or hex string (export endpoint returns hex).
 */
export function decryptBlob(
  encryptedHexOrBase64: string,
  encryptionKey: Uint8Array,
): string {
  let raw: Uint8Array;
  // Heuristic: hex strings contain only 0-9a-f; base64 uses +/= chars
  if (/^[0-9a-fA-F]+$/.test(encryptedHexOrBase64)) {
    raw = hexToBytes(encryptedHexOrBase64);
  } else {
    raw = base64ToBytes(encryptedHexOrBase64);
  }

  if (raw.length < 40) throw new Error("blob too short");

  const nonce = raw.slice(0, 24);
  // noble/ciphers xchacha20poly1305 expects ciphertext = encrypted_data || tag
  // wire format has tag BEFORE ciphertext, so we rearrange
  const tag = raw.slice(24, 40);
  const ciphertext = raw.slice(40);
  const taggedCiphertext = new Uint8Array(ciphertext.length + 16);
  taggedCiphertext.set(ciphertext, 0);
  taggedCiphertext.set(tag, ciphertext.length);

  const cipher = xchacha20poly1305(encryptionKey, nonce);
  const plaintext = cipher.decrypt(taggedCiphertext);
  return new TextDecoder().decode(plaintext);
}

/**
 * Encrypt a plaintext string, returning the hex-encoded wire format.
 * Used when re-storing a claim after retype or pin update.
 */
export function encryptBlob(
  plaintext: string,
  encryptionKey: Uint8Array,
): string {
  const nonce = crypto.getRandomValues(new Uint8Array(24));
  const cipher = xchacha20poly1305(encryptionKey, nonce);
  const plaintextBytes = new TextEncoder().encode(plaintext);
  const encrypted = cipher.encrypt(plaintextBytes);
  // encrypted = ciphertext || tag (noble convention)
  // wire format: nonce || tag || ciphertext
  const ciphertext = encrypted.slice(0, encrypted.length - 16);
  const tag = encrypted.slice(encrypted.length - 16);
  const wire = new Uint8Array(24 + 16 + ciphertext.length);
  wire.set(nonce, 0);
  wire.set(tag, 24);
  wire.set(ciphertext, 40);
  return bytesToHex(wire);
}
