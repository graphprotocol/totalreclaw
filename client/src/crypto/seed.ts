/**
 * BIP-39 Seed Management for TotalReclaw Phase 3 (Subgraph).
 *
 * The user's 12-word mnemonic is the ONLY secret. From it we derive:
 *   1. A private key (for signing UserOperations)
 *   2. An EOA address (the owner of the Smart Account)
 *   3. An encryption key (for AES-256-GCM, same as Phase 1-2)
 *   4. An auth key (for HMAC operations, same as Phase 1-2)
 *   5. A Smart Account address (deterministic ERC-4337 address, computed on-chain)
 *
 * Derivation:
 *   mnemonic -> BIP-39 seed (512 bits)
 *     -> BIP-32/44 derive m/44'/60'/0'/0/0 -> private key (256 bits) + EOA address
 *     -> HKDF(private_key, "totalreclaw-encryption-key-v1") -> encryption key (256 bits)
 *     -> HKDF(private_key, "totalreclaw-auth-key-v1") -> auth key (256 bits)
 *     -> SimpleAccountFactory.getAddress(eoaAddress, 0) -> Smart Account address
 *
 * The encryption key derivation uses the SAME HKDF info strings as
 * client/src/crypto/kdf.ts so that the AES and blind-index modules work
 * identically regardless of whether the key came from a recovery phrase
 * or a BIP-39 seed.
 */

import * as bip39 from "bip39";
import * as crypto from "crypto";

/**
 * Standard Ethereum BIP-44 derivation path.
 */
export const DERIVATION_PATH = "m/44'/60'/0'/0/0";

/**
 * Default chain ID for Smart Account address computation.
 * Gnosis mainnet (100) for production, Base Sepolia (84532) for staging.
 */
export const DEFAULT_CHAIN_ID = 100;

/**
 * Keys derived from a BIP-39 mnemonic.
 */
export interface SeedDerivedKeys {
  /** 32-byte private key for signing UserOperations */
  privateKey: Buffer;
  /** Ethereum EOA address (checksummed hex) -- the Smart Account owner */
  eoaAddress: string;
  /** 32-byte encryption key for AES-256-GCM (same usage as kdf.ts) */
  encryptionKey: Buffer;
  /** 32-byte auth key for HMAC operations (same usage as kdf.ts) */
  authKey: Buffer;
  /**
   * ERC-4337 Smart Account address (checksummed hex).
   * Deterministic CREATE2 address from SimpleAccountFactory v0.7.
   * This is the on-chain identity for the user's encrypted memory vault.
   *
   * Computed via an RPC call to the factory's getAddress() view function.
   * Requires network connectivity. If computed offline (no RPC), this will
   * be undefined.
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
  return bip39.generateMnemonic(128); // 128 bits = 12 words
}

/**
 * Validate a BIP-39 mnemonic.
 *
 * @param mnemonic - Space-separated word list
 * @returns true if valid
 */
export function validateMnemonic(mnemonic: string): boolean {
  if (!mnemonic || mnemonic.trim().length === 0) return false;
  return bip39.validateMnemonic(mnemonic);
}

/**
 * Derive all keys from a BIP-39 mnemonic.
 *
 * This is the primary entry point for the seed path. It produces all the
 * keys needed for TotalReclaw operations:
 *   - privateKey: for signing ERC-4337 UserOperations
 *   - eoaAddress: the EOA that owns the Smart Account
 *   - encryptionKey: for AES-256-GCM encryption (identical to kdf.ts output)
 *   - authKey: for HMAC authentication (identical to kdf.ts output)
 *   - smartAccountAddress: deterministic ERC-4337 Smart Account address
 *
 * @param mnemonic - 12-word BIP-39 mnemonic
 * @param chainId - Chain ID for Smart Account address computation (default: 100 Gnosis)
 * @returns All derived keys including the Smart Account address
 */
export async function mnemonicToKeys(
  mnemonic: string,
  chainId: number = DEFAULT_CHAIN_ID
): Promise<SeedDerivedKeys> {
  if (!validateMnemonic(mnemonic)) {
    throw new Error("Invalid BIP-39 mnemonic");
  }

  // Step 1: Mnemonic -> BIP-39 seed (512 bits)
  const seed = bip39.mnemonicToSeedSync(mnemonic);

  // Step 2: BIP-32 HD key derivation -> private key at m/44'/60'/0'/0/0
  // Use viem's HDKey for BIP-32 derivation
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
    `0x${privateKey.toString("hex")}` as `0x${string}`
  );
  const eoaAddress = account.address;

  // Step 4: Derive TotalReclaw keys using HKDF with same info strings as kdf.ts
  // This ensures the AES and blind-index modules work identically.
  const encryptionKey = hkdfSha256(
    privateKey,
    Buffer.from("totalreclaw-seed-salt-v1", "utf-8"), // Fixed salt for seed path
    Buffer.from("totalreclaw-encryption-key-v1", "utf-8"),
    32
  );

  const authKey = hkdfSha256(
    privateKey,
    Buffer.from("totalreclaw-seed-salt-v1", "utf-8"),
    Buffer.from("totalreclaw-auth-key-v1", "utf-8"),
    32
  );

  // Step 5: Compute the Smart Account address via the factory's getAddress()
  // This is a view call (no gas), but requires RPC connectivity.
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
 * Uses the canonical SimpleAccountFactory v0.7 getAddress() view function.
 * Requires RPC connectivity (but no gas -- it's a view call).
 *
 * @param mnemonic - 12-word BIP-39 mnemonic
 * @param chainId - Chain ID (default: 100 for Gnosis, 10200 for Chiado)
 * @returns Smart Account address (checksummed hex string)
 */
export async function mnemonicToSmartAccountAddress(
  mnemonic: string,
  chainId: number = DEFAULT_CHAIN_ID
): Promise<string> {
  if (!validateMnemonic(mnemonic)) {
    throw new Error("Invalid BIP-39 mnemonic");
  }

  // Derive the EOA address from the mnemonic (no RPC needed for this step)
  const seed = bip39.mnemonicToSeedSync(mnemonic);
  const { HDKey, privateKeyToAccount } = await import("viem/accounts");
  const hdKey = HDKey.fromMasterSeed(seed);
  const derived = hdKey.derive(DERIVATION_PATH);

  if (!derived.privateKey) {
    throw new Error("Failed to derive private key from seed");
  }

  const privateKey = Buffer.from(derived.privateKey);
  const account = privateKeyToAccount(
    `0x${privateKey.toString("hex")}` as `0x${string}`
  );

  // Compute the Smart Account address via the factory
  const { getSmartAccountAddress } = await import("../userop/builder");
  return getSmartAccountAddress(account.address, chainId);
}

/**
 * Store a mnemonic in the OS keychain.
 *
 * Reuses the existing keychain module (client/src/credentials/keychain.ts).
 * The mnemonic is stored under the account name "{userId}-seed".
 *
 * @param userId - User identifier
 * @param mnemonic - 12-word mnemonic to store
 */
export async function storeSeedInKeychain(
  userId: string,
  mnemonic: string
): Promise<void> {
  const { storeCredentials } = await import("../credentials/keychain");
  await storeCredentials(`${userId}-seed`, mnemonic);
}

/**
 * Retrieve a mnemonic from the OS keychain.
 *
 * @param userId - User identifier
 * @returns The stored mnemonic, or null if not found
 */
export async function getSeedFromKeychain(
  userId: string
): Promise<string | null> {
  const { getCredentials } = await import("../credentials/keychain");
  return getCredentials(`${userId}-seed`);
}

/**
 * Delete a mnemonic from the OS keychain.
 *
 * @param userId - User identifier
 * @returns true if a credential was deleted
 */
export async function deleteSeedFromKeychain(
  userId: string
): Promise<boolean> {
  const { deleteCredentials } = await import("../credentials/keychain");
  return deleteCredentials(`${userId}-seed`);
}

// --- Internal: HKDF-SHA256 ---

/**
 * HKDF-SHA256 -- identical to the implementation in kdf.ts.
 * Duplicated here to avoid circular imports and keep the seed module self-contained.
 */
function hkdfSha256(
  ikm: Buffer,
  salt: Buffer,
  info: Buffer,
  length: number
): Buffer {
  const prk = crypto.createHmac("sha256", salt).update(ikm).digest();

  const okm = Buffer.alloc(length);
  let t = Buffer.alloc(0);
  let offset = 0;
  let counter = 1;

  while (offset < length) {
    const hmac = crypto.createHmac("sha256", prk);
    hmac.update(t);
    hmac.update(info);
    hmac.update(Buffer.from([counter]));
    t = hmac.digest();

    const copyLength = Math.min(t.length, length - offset);
    t.copy(okm, offset, 0, copyLength);
    offset += copyLength;
    counter++;
  }

  return okm;
}
