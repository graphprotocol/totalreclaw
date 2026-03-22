/**
 * Key Derivation Functions
 *
 * Uses Argon2id for recovery phrase to key derivation and HKDF for
 * deriving auth and encryption keys.
 */

import * as crypto from 'crypto';
import * as argon2 from 'argon2';

/**
 * Key derivation parameters
 */
export interface KeyDerivationParams {
  /** Memory cost parameter for Argon2id (default: 65536 KB = 64 MB) */
  memoryCost?: number;
  /** Time cost parameter for Argon2id (default: 3 iterations) */
  timeCost?: number;
  /** Parallelism parameter for Argon2id (default: 4) */
  parallelism?: number;
}

/**
 * Default KDF parameters (OWASP recommendations)
 */
export const DEFAULT_KDF_PARAMS: Required<Omit<KeyDerivationParams, 'salt'>> = {
  memoryCost: 65536, // 64 MB
  timeCost: 3,
  parallelism: 4,
};

/**
 * Generate a random salt for key derivation
 * @param length - Salt length in bytes (default: 32)
 * @returns Random salt buffer
 */
export function generateSalt(length: number = 32): Buffer {
  return crypto.randomBytes(length);
}

/**
 * Derive the auth key from recovery phrase using Argon2id + HKDF
 *
 * The auth key is used for server authentication (HMAC operations).
 *
 * @param masterPassword - User's recovery phrase
 * @param salt - Random salt
 * @param params - KDF parameters
 * @returns 32-byte auth key hash
 */
export async function deriveAuthKey(
  masterPassword: string,
  salt: Buffer,
  params: KeyDerivationParams = {}
): Promise<Buffer> {
  const { memoryCost, timeCost, parallelism } = {
    ...DEFAULT_KDF_PARAMS,
    ...params,
  };

  try {
    // First, derive a master key using Argon2id
    const masterKey = await argon2.hash(masterPassword, {
      type: argon2.argon2id,
      salt: salt,
      memoryCost: memoryCost,
      timeCost: timeCost,
      parallelism: parallelism,
      hashLength: 32,
      raw: true,
    });

    // Then use HKDF to derive the auth key with context
    const authKey = hkdfSha256(
      masterKey,
      salt,
      Buffer.from('totalreclaw-auth-key-v1', 'utf-8'),
      32
    );

    return authKey;
  } catch (error) {
    throw new Error(
      `Failed to derive auth key: ${error instanceof Error ? error.message : 'Unknown error'}`
    );
  }
}

/**
 * Derive the encryption key from recovery phrase using Argon2id + HKDF
 *
 * The encryption key is used for AES-256-GCM encryption of documents and embeddings.
 *
 * @param masterPassword - User's recovery phrase
 * @param salt - Random salt
 * @param params - KDF parameters
 * @returns 32-byte encryption key
 */
export async function deriveEncryptionKey(
  masterPassword: string,
  salt: Buffer,
  params: KeyDerivationParams = {}
): Promise<Buffer> {
  const { memoryCost, timeCost, parallelism } = {
    ...DEFAULT_KDF_PARAMS,
    ...params,
  };

  try {
    // First, derive a master key using Argon2id
    const masterKey = await argon2.hash(masterPassword, {
      type: argon2.argon2id,
      salt: salt,
      memoryCost: memoryCost,
      timeCost: timeCost,
      parallelism: parallelism,
      hashLength: 32,
      raw: true,
    });

    // Then use HKDF to derive the encryption key with different context
    const encryptionKey = hkdfSha256(
      masterKey,
      salt,
      Buffer.from('totalreclaw-encryption-key-v1', 'utf-8'),
      32
    );

    return encryptionKey;
  } catch (error) {
    throw new Error(
      `Failed to derive encryption key: ${error instanceof Error ? error.message : 'Unknown error'}`
    );
  }
}

/**
 * Derive both auth and encryption keys from recovery phrase
 *
 * This is more efficient than calling deriveAuthKey and deriveEncryptionKey
 * separately because it only runs Argon2id once.
 *
 * @param masterPassword - User's recovery phrase
 * @param salt - Random salt
 * @param params - KDF parameters
 * @returns Object containing both auth and encryption keys
 */
export async function deriveKeys(
  masterPassword: string,
  salt: Buffer,
  params: KeyDerivationParams = {}
): Promise<{ authKey: Buffer; encryptionKey: Buffer }> {
  const { memoryCost, timeCost, parallelism } = {
    ...DEFAULT_KDF_PARAMS,
    ...params,
  };

  try {
    // Derive master key once using Argon2id
    const masterKey = await argon2.hash(masterPassword, {
      type: argon2.argon2id,
      salt: salt,
      memoryCost: memoryCost,
      timeCost: timeCost,
      parallelism: parallelism,
      hashLength: 32,
      raw: true,
    });

    // Derive both keys using HKDF with different contexts
    const authKey = hkdfSha256(
      masterKey,
      salt,
      Buffer.from('totalreclaw-auth-key-v1', 'utf-8'),
      32
    );

    const encryptionKey = hkdfSha256(
      masterKey,
      salt,
      Buffer.from('totalreclaw-encryption-key-v1', 'utf-8'),
      32
    );

    return { authKey, encryptionKey };
  } catch (error) {
    throw new Error(
      `Failed to derive keys: ${error instanceof Error ? error.message : 'Unknown error'}`
    );
  }
}

/**
 * HKDF-SHA256 implementation
 *
 * @param ikm - Input keying material
 * @param salt - Salt value
 * @param info - Context and application specific information
 * @param length - Output length in bytes
 * @returns Derived key material
 */
function hkdfSha256(
  ikm: Buffer,
  salt: Buffer,
  info: Buffer,
  length: number
): Buffer {
  // Extract phase
  const prk = crypto.createHmac('sha256', salt).update(ikm).digest();

  // Expand phase
  const okm = Buffer.alloc(length);
  let t = Buffer.alloc(0);
  let offset = 0;
  let counter = 1;

  while (offset < length) {
    const hmac = crypto.createHmac('sha256', prk);
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

/**
 * Create HMAC-SHA256 authentication proof
 *
 * @param key - Auth key
 * @param data - Data to authenticate
 * @returns HMAC digest
 */
export function createAuthProof(key: Buffer, data: Buffer): Buffer {
  return crypto.createHmac('sha256', key).update(data).digest();
}

/**
 * Verify HMAC-SHA256 authentication proof
 *
 * @param key - Auth key
 * @param data - Data to verify
 * @param proof - Expected HMAC digest
 * @returns True if verification succeeds
 */
export function verifyAuthProof(
  key: Buffer,
  data: Buffer,
  proof: Buffer
): boolean {
  const expected = createAuthProof(key, data);
  return crypto.timingSafeEqual(expected, proof);
}
