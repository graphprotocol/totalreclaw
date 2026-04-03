/**
 * BIP-39 Seed Management for TotalReclaw Phase 3 (Subgraph).
 *
 * WASM-backed: BIP-39 key derivation, HKDF key separation, and SHA-256
 * auth key hashing are delegated to `@totalreclaw/core`. Mnemonic
 * generation/validation and Smart Account address derivation remain in TS.
 *
 * The user's 12-word mnemonic is the ONLY secret. From it we derive:
 *   1. A private key (for signing UserOperations)
 *   2. An EOA address (the owner of the Smart Account)
 *   3. An encryption key (for AES-256-GCM)
 *   4. An auth key (for HMAC operations)
 *   5. A dedup key (for content fingerprinting)
 *   6. A Smart Account address (deterministic ERC-4337 address)
 *
 * Derivation (matches mcp/src/subgraph/crypto.ts):
 *   mnemonic -> BIP-39 seed (512 bits via PBKDF2)
 *     -> salt = seed[0:32]
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1",       32) -> authKey
 *     -> HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32) -> encryptionKey
 *     -> HKDF-SHA256(seed, salt, "openmemory-dedup-v1",           32) -> dedupKey
 *     -> HKDF-SHA256(seed, salt, "openmemory-lsh-seed-v1",        32) -> lshSeed
 *     -> BIP-32/44 derive m/44'/60'/0'/0/0 -> privateKey + EOA address
 *     -> SimpleAccountFactory.getAddress(eoaAddress, 0) -> Smart Account address
 *
 * The encryption/auth/dedup keys use HKDF directly from the 512-bit BIP-39
 * seed (NOT the derived private key), matching the canonical MCP implementation.
 */

import { mnemonicToSeedSync, validateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english.js";
import * as wasm from "@totalreclaw/core";

/**
 * Standard Ethereum BIP-44 derivation path.
 */
export const DERIVATION_PATH = "m/44'/60'/0'/0/0";

/**
 * Default chain ID for Smart Account address computation.
 * Defaults to 84532 (Base Sepolia testnet). Free-tier users operate on
 * testnet; the relay promotes to Gnosis mainnet (100) for Pro-tier users.
 */
export const DEFAULT_CHAIN_ID = 84532;

/**
 * Keys derived from a BIP-39 mnemonic.
 */
export interface SeedDerivedKeys {
  /** 32-byte private key for signing UserOperations */
  privateKey: Buffer;
  /** Ethereum EOA address (checksummed hex) -- the Smart Account owner */
  eoaAddress: string;
  /** 32-byte encryption key for AES-256-GCM */
  encryptionKey: Buffer;
  /** 32-byte auth key for HMAC operations */
  authKey: Buffer;
  /** 32-byte dedup key for content fingerprinting */
  dedupKey: Buffer;
  /** Deterministic salt derived from seed (first 32 bytes) */
  salt: Buffer;
  /**
   * ERC-4337 Smart Account address (checksummed hex).
   * Deterministic CREATE2 address from SimpleAccountFactory v0.7.
   */
  smartAccountAddress: string;
}

/**
 * Generate a new 12-word BIP-39 mnemonic.
 *
 * Uses 128 bits of entropy (12 words). This is the standard for Ethereum
 * wallets and provides ~2^128 security -- more than sufficient.
 *
 * @returns 12-word mnemonic string (space-separated)
 */
export function generateMnemonic(): string {
  const { generateMnemonic: gen } = require("@scure/bip39");
  return gen(wordlist, 128);
}

/**
 * Validate a BIP-39 mnemonic.
 *
 * @param mnemonic - Space-separated word list
 * @returns true if valid
 */
export function isMnemonicValid(mnemonic: string): boolean {
  if (!mnemonic || mnemonic.trim().length === 0) return false;
  return validateMnemonic(mnemonic.trim(), wordlist);
}

// Re-export under old name for backward compatibility
export { isMnemonicValid as validateMnemonic };

/**
 * Derive encryption, auth, and dedup keys from a BIP-39 mnemonic.
 *
 * Delegates to the WASM module which performs BIP-39 seed derivation
 * and HKDF key separation. Matches mcp/src/subgraph/crypto.ts:deriveKeysFromMnemonic().
 */
export function deriveKeysFromMnemonic(
  mnemonic: string,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const result = wasm.deriveKeysFromMnemonic(mnemonic.trim());
  return {
    authKey: Buffer.from(result.auth_key, "hex"),
    encryptionKey: Buffer.from(result.encryption_key, "hex"),
    dedupKey: Buffer.from(result.dedup_key, "hex"),
    salt: Buffer.from(result.salt, "hex"),
  };
}

/**
 * Derive a 32-byte seed for the LSH hasher from a BIP-39 mnemonic.
 *
 * Call this once during initialization and pass the result to
 * `new LSHHasher(seed, dims)`.
 *
 * Delegates to the WASM module. Matches mcp/src/subgraph/crypto.ts:deriveLshSeed().
 */
export function deriveLshSeed(mnemonic: string): Uint8Array {
  // WASM deriveLshSeed requires the salt as hex. For the BIP-39 path,
  // the salt is the first 32 bytes of the BIP-39 seed -- same value
  // returned by deriveKeysFromMnemonic().salt.
  const { salt } = deriveKeysFromMnemonic(mnemonic);
  const seedHex = wasm.deriveLshSeed(mnemonic.trim(), salt.toString("hex"));
  return new Uint8Array(Buffer.from(seedHex, "hex"));
}

/**
 * Derive all keys from a BIP-39 mnemonic.
 *
 * This is the primary entry point for the seed path. It produces all the
 * keys needed for TotalReclaw operations:
 *   - privateKey: for signing ERC-4337 UserOperations
 *   - eoaAddress: the EOA that owns the Smart Account
 *   - encryptionKey: for AES-256-GCM encryption
 *   - authKey: for HMAC authentication
 *   - dedupKey: for content fingerprinting
 *   - salt: deterministic salt from seed
 *   - smartAccountAddress: deterministic ERC-4337 Smart Account address
 *
 * @param mnemonic - 12-word BIP-39 mnemonic
 * @param chainId - Chain ID for Smart Account address computation (default: 100 Gnosis)
 * @returns All derived keys including the Smart Account address
 */
export async function mnemonicToKeys(
  mnemonic: string,
  chainId: number = DEFAULT_CHAIN_ID,
): Promise<SeedDerivedKeys> {
  if (!isMnemonicValid(mnemonic)) {
    throw new Error("Invalid BIP-39 mnemonic");
  }

  // Step 1: Derive encryption/auth/dedup keys from BIP-39 seed via HKDF
  const { authKey, encryptionKey, dedupKey, salt } = deriveKeysFromMnemonic(mnemonic);

  // Step 2: BIP-32 HD key derivation -> private key at m/44'/60'/0'/0/0
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const { HDKey } = await import("viem/accounts");
  const hdKey = HDKey.fromMasterSeed(seed);
  const derived = hdKey.derive(DERIVATION_PATH);

  if (!derived.privateKey) {
    throw new Error("Failed to derive private key from seed");
  }

  const privateKey = Buffer.from(derived.privateKey);

  // Step 3: Compute EOA address from private key using viem
  const { privateKeyToAccount } = await import("viem/accounts");
  const account = privateKeyToAccount(
    `0x${privateKey.toString("hex")}` as `0x${string}`,
  );
  const eoaAddress = account.address;

  // Step 4: Compute the Smart Account address via the factory's getAddress()
  const { getSmartAccountAddress } = await import("../userop/builder");
  const smartAccountAddress = await getSmartAccountAddress(
    eoaAddress as `0x${string}`,
    chainId,
  );

  return {
    privateKey,
    eoaAddress,
    encryptionKey,
    authKey,
    dedupKey,
    salt,
    smartAccountAddress,
  };
}

/**
 * Compute the Smart Account counterfactual address from a mnemonic.
 *
 * This is a convenience function for the recovery flow. The Smart Account
 * address is deterministic: same mnemonic + same chain = same address,
 * regardless of whether the account has been deployed on-chain yet.
 *
 * @param mnemonic - 12-word BIP-39 mnemonic
 * @param chainId - Chain ID (default: 100 for Gnosis)
 * @returns Smart Account address (checksummed hex string)
 */
export async function mnemonicToSmartAccountAddress(
  mnemonic: string,
  chainId: number = DEFAULT_CHAIN_ID,
): Promise<string> {
  if (!isMnemonicValid(mnemonic)) {
    throw new Error("Invalid BIP-39 mnemonic");
  }

  const seed = mnemonicToSeedSync(mnemonic.trim());
  const { HDKey, privateKeyToAccount } = await import("viem/accounts");
  const hdKey = HDKey.fromMasterSeed(seed);
  const derived = hdKey.derive(DERIVATION_PATH);

  if (!derived.privateKey) {
    throw new Error("Failed to derive private key from seed");
  }

  const privateKey = Buffer.from(derived.privateKey);
  const account = privateKeyToAccount(
    `0x${privateKey.toString("hex")}` as `0x${string}`,
  );

  const { getSmartAccountAddress } = await import("../userop/builder");
  return getSmartAccountAddress(account.address, chainId);
}

/**
 * Store a mnemonic in the OS keychain.
 */
export async function storeSeedInKeychain(
  userId: string,
  mnemonic: string,
): Promise<void> {
  const { storeCredentials } = await import("../credentials/keychain");
  await storeCredentials(`${userId}-seed`, mnemonic);
}

/**
 * Retrieve a mnemonic from the OS keychain.
 */
export async function getSeedFromKeychain(
  userId: string,
): Promise<string | null> {
  const { getCredentials } = await import("../credentials/keychain");
  return getCredentials(`${userId}-seed`);
}

/**
 * Delete a mnemonic from the OS keychain.
 */
export async function deleteSeedFromKeychain(
  userId: string,
): Promise<boolean> {
  const { deleteCredentials } = await import("../credentials/keychain");
  return deleteCredentials(`${userId}-seed`);
}

/**
 * Compute the SHA-256 hash of the auth key.
 *
 * The server stores SHA256(authKey) during registration and uses it to look
 * up users on every request.
 */
export function computeAuthKeyHash(authKey: Buffer): string {
  return wasm.computeAuthKeyHash(authKey.toString("hex"));
}
