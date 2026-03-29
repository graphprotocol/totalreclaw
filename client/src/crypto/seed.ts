/**
 * BIP-39 Seed Management for TotalReclaw Phase 3 (Subgraph).
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
import { hkdf } from "@noble/hashes/hkdf.js";
import { sha256 } from "@noble/hashes/sha2.js";

/**
 * Standard Ethereum BIP-44 derivation path.
 */
export const DERIVATION_PATH = "m/44'/60'/0'/0/0";

/**
 * Default chain ID for Smart Account address computation.
 * Gnosis mainnet (100) for production, Base Sepolia (84532) for staging.
 */
export const DEFAULT_CHAIN_ID = 100;

/** HKDF context strings -- must match mcp/src/subgraph/crypto.ts exactly. */
const AUTH_KEY_INFO = "totalreclaw-auth-key-v1";
const ENCRYPTION_KEY_INFO = "totalreclaw-encryption-key-v1";
const DEDUP_KEY_INFO = "openmemory-dedup-v1";
const LSH_SEED_INFO = "openmemory-lsh-seed-v1";

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
 * Uses the 512-bit BIP-39 seed as HKDF input (NOT the derived private key)
 * for proper key separation. Matches mcp/src/subgraph/crypto.ts:deriveKeysFromMnemonic().
 */
export function deriveKeysFromMnemonic(
  mnemonic: string,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const seed = mnemonicToSeedSync(mnemonic.trim());

  // Use first 32 bytes of seed as deterministic salt for HKDF
  // (BIP-39 mnemonics are self-salting via PBKDF2, no random salt needed)
  const salt = Buffer.from(seed.slice(0, 32));

  const enc = (s: string) => Buffer.from(s, "utf8");
  const seedBuf = Buffer.from(seed);

  const authKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(AUTH_KEY_INFO), 32),
  );
  const encryptionKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(DEDUP_KEY_INFO), 32),
  );

  return { authKey, encryptionKey, dedupKey, salt };
}

/**
 * Derive a 32-byte seed for the LSH hasher from a BIP-39 mnemonic.
 *
 * Call this once during initialization and pass the result to
 * `new LSHHasher(seed, dims)`.
 *
 * Matches mcp/src/subgraph/crypto.ts:deriveLshSeed().
 */
export function deriveLshSeed(mnemonic: string): Uint8Array {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = Buffer.from(seed.slice(0, 32));

  return new Uint8Array(
    hkdf(sha256, Buffer.from(seed), salt, Buffer.from(LSH_SEED_INFO, "utf8"), 32),
  );
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
  return Buffer.from(sha256(authKey)).toString("hex");
}
