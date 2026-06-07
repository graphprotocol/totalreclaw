/**
 * Browser-native CryptoProvider
 *
 * TS port of client/src/crypto/ for browser use (no WASM, no Node.js).
 * Uses @scure/bip39 for mnemonic, WebCrypto for HKDF, @noble/ciphers for XChaCha20.
 * Swap the XChaCha20 path to @totalreclaw/core WASM when #104 ships (rc.16+).
 */

import { validateMnemonic, mnemonicToSeed, generateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english";
import { HDKey } from "@scure/bip32";
import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";
import { xchacha20poly1305 } from "@noble/ciphers/chacha";
import { SessionKeys } from "./types";

const BIP44_PATH = "m/44'/60'/0'/0/0";
const DEFAULT_CHAIN_ID = 84532; // Base Sepolia (free tier)

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

/** Hex-encode bytes (lowercase, no 0x). Exported for the auth wrap/unlock path. */
export { bytesToHex };

/** Generate a fresh 12-word BIP-39 recovery phrase (128-bit entropy). */
export function generateRecoveryPhrase(): string {
  return generateMnemonic(wordlist, 128);
}

/**
 * L3 — phrase-safety. Derive the master-wallet EOA *private* key (32 bytes)
 * from the mnemonic, at BIP-32 m/44'/60'/0'/0/0. Used ONLY to wrap the master
 * key at bootstrap (passkey-PRF at-rest model) and to sign UserOps in A.2.
 * NEVER log, print, or transmit the result. Caller must zero it after use.
 */
export async function deriveEoaPrivateKey(mnemonic: string): Promise<Uint8Array> {
  const normalized = mnemonic.trim().toLowerCase();
  if (!isMnemonicValid(normalized)) {
    throw new Error("Invalid recovery phrase");
  }
  const seed = await mnemonicToSeed(normalized);
  const hdKey = HDKey.fromMasterSeed(seed);
  const child = hdKey.derive(BIP44_PATH);
  if (!child.privateKey) {
    throw new Error("BIP-32 derivation failed");
  }
  return child.privateKey; // 32 bytes
}

/**
 * Derive the EOA Ethereum address that owns the ERC-4337 Smart Account.
 *
 * Mirrors the canonical client/src/crypto/seed.ts derivation:
 *   - BIP-32 HD key at m/44'/60'/0'/0/0
 *   - secp256k1 uncompressed public key (65 bytes, leading 0x04)
 *   - EOA = keccak256(pubkey[1:65])[12:32] (last 20 bytes)
 *
 * Returns lowercase 0x-prefixed hex (not EIP-55 checksummed).
 */
function deriveEoaFromSeed(seed: Uint8Array): string {
  const hdKey = HDKey.fromMasterSeed(seed);
  const child = hdKey.derive(BIP44_PATH);
  if (!child.privateKey) {
    throw new Error("BIP-32 derivation failed");
  }
  const pubUncompressed = secp256k1.getPublicKey(child.privateKey, false); // 65 bytes
  const hash = keccak_256(pubUncompressed.slice(1));
  const addr = hash.slice(-20);
  return "0x" + bytesToHex(addr);
}

/**
 * Fetch the deterministic Smart Account address from the relay.
 *
 * The relay calls `SimpleAccountFactory.getAddress(eoa, 0)` via a public RPC
 * (CREATE2 view function). Same answer on every chain where the factory is
 * deployed — but we still pass chainId so the relay routes to the correct
 * RPC endpoint.
 */
async function fetchSmartAccountAddress(
  serverUrl: string,
  eoa: string,
  chainId: number,
): Promise<string> {
  const url = `${serverUrl.replace(/\/$/, "")}/v1/smart-account?eoa=${eoa}&chain=${chainId}`;
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`smart-account derivation failed (${res.status}): ${body}`);
  }
  const json = (await res.json()) as { smart_account?: string };
  if (!json.smart_account || !/^0x[0-9a-fA-F]{40}$/.test(json.smart_account)) {
    throw new Error("smart-account response missing valid address");
  }
  return json.smart_account.toLowerCase();
}

/**
 * Derive session keys + Smart Account address from a 12-word BIP-39 mnemonic.
 *
 * Mirrors client/src/crypto/seed.ts deriveKeysFromMnemonic() + the Smart
 * Account derivation flow used by every other client. The Smart Account
 * address is fetched from the relay (browser can't easily call the factory
 * view function without bundle-heavy deps).
 *
 *   seed     = BIP-39 PBKDF2(mnemonic, "mnemonic", 2048, SHA-512, 64 bytes)
 *   salt     = seed[0:32]
 *   authKey  = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1",       32)
 *   encKey   = HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32)
 *   eoa      = keccak256(secp256k1_pubkey(BIP32(m/44'/60'/0'/0/0)))[-20:]
 *   wallet   = SimpleAccountFactory.getAddress(eoa, 0) [via relay]
 */
export async function deriveSessionKeys(
  mnemonic: string,
  serverUrl: string,
  chainId: number = DEFAULT_CHAIN_ID,
): Promise<SessionKeys> {
  const normalized = mnemonic.trim().toLowerCase();
  if (!isMnemonicValid(normalized)) {
    throw new Error("Invalid 12-word recovery phrase");
  }

  const seed = await mnemonicToSeed(normalized); // 512-bit BIP-39 seed
  const salt = seed.slice(0, 32);
  const [authKey, encryptionKey, eoaAddress] = await Promise.all([
    hkdf(seed, salt, "totalreclaw-auth-key-v1", 32),
    hkdf(seed, salt, "totalreclaw-encryption-key-v1", 32),
    Promise.resolve(deriveEoaFromSeed(seed)),
  ]);

  const walletAddress = await fetchSmartAccountAddress(
    serverUrl,
    eoaAddress,
    chainId,
  );

  return {
    mnemonic: normalized,
    authKey,
    encryptionKey,
    authKeyHex: bytesToHex(authKey),
    eoaAddress,
    walletAddress,
    chainId,
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
