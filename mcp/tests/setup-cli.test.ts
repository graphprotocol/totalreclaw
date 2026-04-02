/**
 * @jest-environment node
 */

/**
 * Tests for the MCP Setup CLI (mcp/src/cli/setup.ts).
 *
 * Verifies:
 *   1. Key derivation produces correct keys from a known mnemonic
 *      (cross-referenced with plugin/crypto.ts derivation chain)
 *   2. Credential file is saved correctly
 *   3. Invalid mnemonic is rejected
 *   4. Auth key hash computation matches plugin behavior
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

// We import the crypto primitives used by plugin/crypto.ts to compute
// reference values. The setup CLI must produce identical output.
import { mnemonicToSeedSync, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';

import {
  deriveAuthKey,
  computeAuthKeyHash,
  saveCredentials,
  loadCredentials,
  type SavedCredentials,
} from '../src/cli/setup.js';

// ---------------------------------------------------------------------------
// Test Constants
// ---------------------------------------------------------------------------

// Known BIP-39 test mnemonic -- DO NOT use for real funds
const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

// HKDF info string -- must match plugin/crypto.ts
const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';

// ---------------------------------------------------------------------------
// Reference implementation (mirrors plugin/crypto.ts deriveKeysFromMnemonic)
// ---------------------------------------------------------------------------

/**
 * Reference key derivation that exactly mirrors plugin/crypto.ts.
 * Used to generate expected values for cross-validation.
 */
function referenceDerive(mnemonic: string): {
  authKey: Uint8Array;
  salt: Uint8Array;
  authKeyHash: string;
} {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = seed.slice(0, 32);
  const infoBytes = Buffer.from(AUTH_KEY_INFO, 'utf8');
  const seedBuf = Buffer.from(seed);
  const saltBuf = Buffer.from(salt);

  const authKey = hkdf(sha256, seedBuf, saltBuf, infoBytes, 32);
  const authKeyHash = Buffer.from(sha256(authKey)).toString('hex');

  return { authKey, salt, authKeyHash };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Setup CLI - Key Derivation', () => {
  it('should derive auth key matching plugin/crypto.ts for known mnemonic', () => {
    // Derive using the setup CLI function (WASM)
    const { authKeyHex, saltHex } = deriveAuthKey(TEST_MNEMONIC);

    // Derive using the reference (plugin) implementation
    const ref = referenceDerive(TEST_MNEMONIC);

    // Both must produce identical values
    expect(authKeyHex).toBe(Buffer.from(ref.authKey).toString('hex'));
    expect(saltHex).toBe(Buffer.from(ref.salt).toString('hex'));
  });

  it('should compute auth key hash matching plugin/crypto.ts', () => {
    const { authKeyHex } = deriveAuthKey(TEST_MNEMONIC);
    const hash = computeAuthKeyHash(authKeyHex);

    const ref = referenceDerive(TEST_MNEMONIC);

    expect(hash).toBe(ref.authKeyHash);
    // Hash should be 64 hex characters (SHA-256)
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it('should use first 32 bytes of BIP-39 seed as salt', () => {
    const seed = mnemonicToSeedSync(TEST_MNEMONIC.trim());
    const expectedSalt = seed.slice(0, 32);

    const { saltHex } = deriveAuthKey(TEST_MNEMONIC);

    expect(saltHex).toBe(Buffer.from(expectedSalt).toString('hex'));
  });

  it('should be deterministic (same mnemonic = same output)', () => {
    const result1 = deriveAuthKey(TEST_MNEMONIC);
    const result2 = deriveAuthKey(TEST_MNEMONIC);

    expect(result1.authKeyHex).toBe(result2.authKeyHex);
    expect(result1.saltHex).toBe(result2.saltHex);
  });

  it('should produce different keys for different mnemonics', () => {
    const mnemonic2 =
      'zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong';

    const result1 = deriveAuthKey(TEST_MNEMONIC);
    const result2 = deriveAuthKey(mnemonic2);

    expect(result1.authKeyHex).not.toBe(result2.authKeyHex);
  });

  it('should handle whitespace-padded mnemonics', () => {
    const padded = `  ${TEST_MNEMONIC}  `;
    const clean = deriveAuthKey(TEST_MNEMONIC);
    const paddedResult = deriveAuthKey(padded);

    expect(paddedResult.authKeyHex).toBe(clean.authKeyHex);
  });

  it('should produce 64-char hex auth key (32 bytes)', () => {
    const { authKeyHex } = deriveAuthKey(TEST_MNEMONIC);
    expect(authKeyHex.length).toBe(64);
    expect(authKeyHex).toMatch(/^[0-9a-f]{64}$/);
  });

  it('should produce 64-char hex salt (32 bytes)', () => {
    const { saltHex } = deriveAuthKey(TEST_MNEMONIC);
    expect(saltHex.length).toBe(64);
    expect(saltHex).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe('Setup CLI - Mnemonic Validation', () => {
  it('should accept valid 12-word mnemonic', () => {
    expect(validateMnemonic(TEST_MNEMONIC, wordlist)).toBe(true);
  });

  it('should reject invalid mnemonic (wrong words)', () => {
    const invalid = 'not a valid mnemonic phrase at all hello world foo bar';
    expect(validateMnemonic(invalid, wordlist)).toBe(false);
  });

  it('should reject mnemonic with wrong word count', () => {
    const tooShort = 'abandon abandon abandon';
    expect(validateMnemonic(tooShort, wordlist)).toBe(false);
  });

  it('should reject empty string', () => {
    expect(validateMnemonic('', wordlist)).toBe(false);
  });

  it('should reject mnemonic with invalid checksum', () => {
    // All valid BIP-39 words, but invalid checksum
    const badChecksum =
      'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon';
    expect(validateMnemonic(badChecksum, wordlist)).toBe(false);
  });
});

describe('Setup CLI - Credential Persistence', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'totalreclaw-test-'));
  });

  afterEach(() => {
    // Clean up temp directory
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('should save credentials to file', () => {
    const creds: SavedCredentials = {
      userId: 'test-user-123',
      salt: Buffer.from('test-salt-bytes').toString('hex'),
      serverUrl: 'https://api.totalreclaw.xyz',
    };

    const filePath = path.join(tmpDir, 'credentials.json');
    saveCredentials(creds, filePath);

    expect(fs.existsSync(filePath)).toBe(true);

    const raw = fs.readFileSync(filePath, 'utf-8');
    const parsed = JSON.parse(raw);

    expect(parsed.userId).toBe('test-user-123');
    expect(parsed.salt).toBe(creds.salt);
    expect(parsed.serverUrl).toBe('https://api.totalreclaw.xyz');
  });

  it('should load credentials from file', () => {
    const creds: SavedCredentials = {
      userId: 'load-test-user',
      salt: 'dGVzdC1zYWx0',
      serverUrl: 'http://localhost:8080',
    };

    const filePath = path.join(tmpDir, 'credentials.json');
    saveCredentials(creds, filePath);

    const loaded = loadCredentials(filePath);

    expect(loaded.userId).toBe(creds.userId);
    expect(loaded.salt).toBe(creds.salt);
    expect(loaded.serverUrl).toBe(creds.serverUrl);
  });

  it('should create parent directories if they do not exist', () => {
    const filePath = path.join(tmpDir, 'nested', 'dir', 'credentials.json');
    const creds: SavedCredentials = {
      userId: 'nested-user',
      salt: 'bmVzdGVk',
      serverUrl: 'https://api.totalreclaw.xyz',
    };

    saveCredentials(creds, filePath);

    expect(fs.existsSync(filePath)).toBe(true);
    const loaded = loadCredentials(filePath);
    expect(loaded.userId).toBe('nested-user');
  });

  it('should NOT save mnemonic or keys', () => {
    const creds: SavedCredentials = {
      userId: 'no-secrets',
      salt: 'c2FsdA==',
      serverUrl: 'https://api.totalreclaw.xyz',
    };

    const filePath = path.join(tmpDir, 'credentials.json');
    saveCredentials(creds, filePath);

    const raw = fs.readFileSync(filePath, 'utf-8');

    // Ensure no secret material is stored
    expect(raw).not.toContain('mnemonic');
    expect(raw).not.toContain('authKey');
    expect(raw).not.toContain('encryptionKey');
    expect(raw).not.toContain('privateKey');
    expect(raw).not.toContain('abandon'); // no mnemonic words

    // Only these fields should be present
    const parsed = JSON.parse(raw);
    const keys = Object.keys(parsed).sort();
    expect(keys).toEqual(['salt', 'serverUrl', 'userId']);
  });

  it('should throw when loading non-existent file', () => {
    const filePath = path.join(tmpDir, 'does-not-exist.json');
    expect(() => loadCredentials(filePath)).toThrow();
  });
});

describe('Setup CLI - Cross-validation with plugin/crypto.ts', () => {
  /**
   * This test verifies that the setup CLI and plugin/crypto.ts produce
   * byte-identical output for the BIP-39 derivation path. We inline the
   * exact same derivation logic from plugin/crypto.ts (using Buffer.from
   * for the info parameter, as the plugin does) and compare.
   */
  it('should match plugin derivation using Buffer.from for info param', () => {
    // Plugin uses: Buffer.from(AUTH_KEY_INFO, 'utf8')
    // WASM core uses the same derivation internally.
    // Both must produce identical bytes.
    const mnemonic = TEST_MNEMONIC;
    const seed = mnemonicToSeedSync(mnemonic.trim());
    const salt = Buffer.from(seed.slice(0, 32));
    const seedBuf = Buffer.from(seed);

    // Plugin-style derivation (using Buffer.from for info)
    const pluginAuthKey = Buffer.from(
      hkdf(sha256, seedBuf, salt, Buffer.from(AUTH_KEY_INFO, 'utf8'), 32),
    );
    const pluginHash = Buffer.from(sha256(pluginAuthKey)).toString('hex');

    // Setup CLI derivation (WASM)
    const { authKeyHex } = deriveAuthKey(mnemonic);
    const setupHash = computeAuthKeyHash(authKeyHex);

    expect(setupHash).toBe(pluginHash);
    expect(authKeyHex).toBe(pluginAuthKey.toString('hex'));
  });

  it('should produce non-trivial key values (not all zeros)', () => {
    const { authKeyHex, saltHex } = deriveAuthKey(TEST_MNEMONIC);
    const zeros = '0'.repeat(64);

    expect(authKeyHex).not.toBe(zeros);
    expect(saltHex).not.toBe(zeros);
  });

  it('should produce a valid 64-char hex auth key hash', () => {
    const { authKeyHex } = deriveAuthKey(TEST_MNEMONIC);
    const hash = computeAuthKeyHash(authKeyHex);

    expect(typeof hash).toBe('string');
    expect(hash.length).toBe(64);
    expect(hash).toMatch(/^[0-9a-f]+$/);
  });
});
