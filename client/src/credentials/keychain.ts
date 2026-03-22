/**
 * Keychain Credential Storage
 *
 * Provides secure OS keychain integration for storing the recovery phrase
 * using the `keytar` library. Supports macOS Keychain, Windows Credential Vault,
 * and Linux Secret Service (via libsecret).
 *
 * Security notes:
 * - Recovery phrases are stored in the OS-managed credential store
 * - The server never sees plaintext passwords
 * - Credentials are scoped per userId
 * - No passwords are logged or written to disk outside the OS keychain
 */

/**
 * Minimal interface for keytar's API surface that we consume.
 * Defined here so we do not need keytar's type declarations at compile time.
 */
interface KeytarApi {
  setPassword(service: string, account: string, password: string): Promise<void>;
  getPassword(service: string, account: string): Promise<string | null>;
  deletePassword(service: string, account: string): Promise<boolean>;
}

// keytar is an optional dependency — it requires native binaries and may
// not be available in all environments (CI, Docker, etc.).
let keytarInstance: KeytarApi | null = null;
let keytarResolved = false;

/**
 * The service name used for all TotalReclaw keychain entries.
 */
const SERVICE_NAME = 'totalreclaw';

/**
 * Lazy-load keytar. Returns null if the native module is unavailable.
 *
 * We use a dynamic require wrapped in try/catch so that:
 * 1. TypeScript does not try to resolve the module at compile time.
 * 2. Environments without the native binary gracefully degrade.
 */
function getKeytar(): KeytarApi | null {
  if (keytarResolved) {
    return keytarInstance;
  }
  keytarResolved = true;
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    keytarInstance = require('keytar') as KeytarApi;
    return keytarInstance;
  } catch {
    keytarInstance = null;
    return null;
  }
}

/**
 * Check whether the OS keychain backend is available in this environment.
 *
 * @returns true if keytar loaded successfully
 */
export function isKeychainAvailable(): boolean {
  return getKeytar() !== null;
}

/**
 * Store a recovery phrase in the OS keychain.
 *
 * @param userId  - The user identifier (used as the "account" in the keychain)
 * @param masterPassword - The plaintext recovery phrase to store
 * @throws Error if the keychain backend is unavailable
 */
export async function storeCredentials(
  userId: string,
  masterPassword: string
): Promise<void> {
  if (!userId || !masterPassword) {
    throw new Error('userId and masterPassword are required');
  }

  const kt = getKeytar();
  if (!kt) {
    throw new Error(
      'Keychain is not available in this environment. ' +
        'Install the "keytar" package for OS keychain support.'
    );
  }

  await kt.setPassword(SERVICE_NAME, userId, masterPassword);
}

/**
 * Retrieve a recovery phrase from the OS keychain.
 *
 * @param userId - The user identifier
 * @returns The stored recovery phrase, or null if none exists
 * @throws Error if the keychain backend is unavailable
 */
export async function getCredentials(
  userId: string
): Promise<string | null> {
  if (!userId) {
    throw new Error('userId is required');
  }

  const kt = getKeytar();
  if (!kt) {
    throw new Error(
      'Keychain is not available in this environment. ' +
        'Install the "keytar" package for OS keychain support.'
    );
  }

  return kt.getPassword(SERVICE_NAME, userId);
}

/**
 * Delete a stored recovery phrase from the OS keychain.
 *
 * @param userId - The user identifier
 * @returns true if a credential was deleted, false if none existed
 * @throws Error if the keychain backend is unavailable
 */
export async function deleteCredentials(
  userId: string
): Promise<boolean> {
  if (!userId) {
    throw new Error('userId is required');
  }

  const kt = getKeytar();
  if (!kt) {
    throw new Error(
      'Keychain is not available in this environment. ' +
        'Install the "keytar" package for OS keychain support.'
    );
  }

  return kt.deletePassword(SERVICE_NAME, userId);
}

/**
 * Check whether credentials exist in the OS keychain for a given user.
 *
 * @param userId - The user identifier
 * @returns true if a credential is stored for this userId
 * @throws Error if the keychain backend is unavailable
 */
export async function hasCredentials(
  userId: string
): Promise<boolean> {
  const password = await getCredentials(userId);
  return password !== null;
}
