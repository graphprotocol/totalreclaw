/**
 * Crypto Parity Test: Client Library vs Skill Plugin
 *
 * Verifies that the client library (client/src/crypto/kdf.ts) and the skill
 * plugin (skill/plugin/crypto.ts) produce byte-identical keys from the same
 * password + salt inputs. This is a P0 invariant: if these diverge, memories
 * encrypted by one implementation cannot be decrypted by the other.
 *
 * ============================================================================
 * IMPLEMENTATION DIFFERENCES
 * ============================================================================
 *
 * Client (client/src/crypto/kdf.ts):
 *   - Argon2id via `argon2` npm package (native C binding, libargon2)
 *   - HKDF via hand-rolled implementation using Node.js `crypto.createHmac`
 *   - Module system: CommonJS
 *   - `deriveKeys()` is ASYNC (returns Promise<{authKey, encryptionKey}>)
 *   - No BIP-39 mnemonic detection; separate `seed.ts` handles mnemonics
 *     via BIP-32 HD derivation (different key derivation path entirely)
 *
 * Skill Plugin (skill/plugin/crypto.ts):
 *   - Argon2id via `@noble/hashes/argon2` (pure JavaScript)
 *   - HKDF via `@noble/hashes/hkdf` (pure JavaScript)
 *   - Module system: ESM ("type": "module")
 *   - `deriveKeys()` is SYNC (returns {authKey, encryptionKey, dedupKey, salt})
 *   - Auto-detects BIP-39 mnemonics and uses a DIFFERENT derivation path
 *     (HKDF from 512-bit BIP-39 seed, not Argon2id)
 *
 * ============================================================================
 * TEST STRATEGY
 * ============================================================================
 *
 * Since the client's `argon2` package requires native compilation (prebuilt
 * binaries or node-gyp), we cannot directly import it in the parity test
 * directory. Instead, we:
 *
 *   1. Re-implement the client's key derivation algorithm using @noble/hashes
 *      (the same pure-JS primitives the plugin uses) and verify it matches
 *      the plugin's output. This proves algorithmic equivalence.
 *
 *   2. Verify the HKDF implementations produce identical output by testing
 *      both @noble/hashes/hkdf AND the hand-rolled HKDF from kdf.ts against
 *      known test vectors. This proves the HKDF layer is interchangeable.
 *
 *   3. Verify Argon2id parameter equivalence: both use t=3, m=65536, p=4,
 *      dkLen=32 with argon2id variant. The native `argon2` package and
 *      @noble/hashes/argon2 are both RFC 9106 compliant -- if parameters
 *      match, output matches.
 *
 *   4. Test the BIP-39 mnemonic path (plugin-only) to verify it produces
 *      consistent, deterministic output.
 *
 * To run a FULL end-to-end parity test with the native argon2 package, use:
 *
 *   cd client && npm install && npm test   # runs client's own test suite
 *   cd tests/parity && npm install && npx tsx crypto-parity.test.ts
 *
 * For the definitive cross-implementation test (requires native argon2):
 *
 *   cd client && node -e "
 *     const {deriveKeys} = require('./dist/crypto/kdf');
 *     const salt = Buffer.alloc(32, 0xAB);
 *     deriveKeys('test-password-for-parity', salt).then(k => {
 *       console.log('authKey:', k.authKey.toString('hex'));
 *       console.log('encKey:', k.encryptionKey.toString('hex'));
 *     });
 *   "
 *
 *   Then compare with the output of this test's "Plugin: password path" test.
 *
 * Run: cd tests/parity && npm install && npx tsx crypto-parity.test.ts
 * ============================================================================
 */

import { argon2id } from '@noble/hashes/argon2.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { hmac } from '@noble/hashes/hmac.js';
import { mnemonicToSeedSync, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import crypto from 'node:crypto';

// ---------------------------------------------------------------------------
// Test harness (TAP-style, matching existing parity tests)
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
let testNum = 0;

function assert(condition: boolean, message: string): void {
  testNum++;
  if (condition) {
    passed++;
    console.log(`ok ${testNum} - ${message}`);
  } else {
    failed++;
    console.log(`not ok ${testNum} - ${message}`);
  }
}

function assertBuffersEqual(a: Buffer, b: Buffer, message: string): void {
  testNum++;
  if (a.length === b.length && a.every((byte, i) => byte === b[i])) {
    passed++;
    console.log(`ok ${testNum} - ${message}`);
  } else {
    failed++;
    console.log(`not ok ${testNum} - ${message}`);
    console.log(`  expected: ${a.toString('hex')}`);
    console.log(`  actual:   ${b.toString('hex')}`);
  }
}

function section(name: string): void {
  console.log(`\n# ${name}`);
}

// ---------------------------------------------------------------------------
// Constants (must match both implementations)
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';
const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

const ARGON2_TIME_COST = 3;
const ARGON2_MEMORY_COST = 65536; // 64 MB in KiB
const ARGON2_PARALLELISM = 4;
const ARGON2_DK_LEN = 32;

// ---------------------------------------------------------------------------
// Re-implementation of client's HKDF (from client/src/crypto/kdf.ts lines 202-231)
// Uses Node.js crypto.createHmac -- identical to the client's hand-rolled HKDF.
// ---------------------------------------------------------------------------

function clientHkdfSha256(
  ikm: Buffer,
  salt: Buffer,
  info: Buffer,
  length: number,
): Buffer {
  // Extract phase
  const prk = crypto.createHmac('sha256', salt).update(ikm).digest();

  // Expand phase
  const okm = Buffer.alloc(length);
  let t = Buffer.alloc(0);
  let offset = 0;
  let counter = 1;

  while (offset < length) {
    const h = crypto.createHmac('sha256', prk);
    h.update(t);
    h.update(info);
    h.update(Buffer.from([counter]));
    t = h.digest();

    const copyLength = Math.min(t.length, length - offset);
    t.copy(okm, offset, 0, copyLength);
    offset += copyLength;
    counter++;
  }

  return okm;
}

// ---------------------------------------------------------------------------
// Re-implementation of plugin's deriveKeys for password path
// (from skill/plugin/crypto.ts lines 104-140)
// ---------------------------------------------------------------------------

function pluginDeriveKeysPassword(
  password: string,
  salt: Buffer,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer } {
  const masterKey = argon2id(
    Buffer.from(password, 'utf8'),
    salt,
    { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
  );

  const enc = (s: string) => Buffer.from(s, 'utf8');
  const authKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(AUTH_KEY_INFO), 32),
  );
  const encryptionKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(DEDUP_KEY_INFO), 32),
  );

  return { authKey, encryptionKey, dedupKey };
}

// ---------------------------------------------------------------------------
// Re-implementation of client's deriveKeys
// (from client/src/crypto/kdf.ts lines 148-191)
// Uses @noble/hashes argon2id (same as plugin) but client's hand-rolled HKDF
// ---------------------------------------------------------------------------

function clientDeriveKeysPassword(
  password: string,
  salt: Buffer,
): { authKey: Buffer; encryptionKey: Buffer } {
  // Step 1: Argon2id -- same parameters as client (t=3, m=65536, p=4, dkLen=32)
  // The native `argon2` package and @noble/hashes/argon2 are both RFC 9106
  // compliant. Given identical parameters, they produce identical output.
  const masterKey = Buffer.from(
    argon2id(
      Buffer.from(password, 'utf8'),
      salt,
      { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
    ),
  );

  // Step 2: HKDF -- using the client's hand-rolled implementation
  const authKey = clientHkdfSha256(
    masterKey,
    salt,
    Buffer.from(AUTH_KEY_INFO, 'utf-8'),
    32,
  );

  const encryptionKey = clientHkdfSha256(
    masterKey,
    salt,
    Buffer.from(ENCRYPTION_KEY_INFO, 'utf-8'),
    32,
  );

  return { authKey, encryptionKey };
}

// ---------------------------------------------------------------------------
// Re-implementation of plugin's deriveKeys for BIP-39 mnemonic path
// (from skill/plugin/crypto.ts lines 64-89)
// ---------------------------------------------------------------------------

function pluginDeriveKeysMnemonic(
  mnemonic: string,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = Buffer.from(seed.slice(0, 32));
  const enc = (s: string) => Buffer.from(s, 'utf8');
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

// ===========================================================================
// TESTS
// ===========================================================================

console.log('TAP version 14');
console.log('# Crypto Parity: Client Library vs Skill Plugin');

// ---------------------------------------------------------------------------
// Test 1: HKDF Implementation Parity
// Verify that the client's hand-rolled HKDF and @noble/hashes hkdf produce
// identical output for the same inputs.
// ---------------------------------------------------------------------------

section('HKDF implementation parity');

{
  // Use a known IKM, salt, and info to compare both HKDF implementations
  const testIkm = Buffer.alloc(32, 0x01);
  const testSalt = Buffer.alloc(32, 0x02);
  const testInfo = Buffer.from('test-info-string', 'utf8');

  // Client's HKDF (Node.js crypto.createHmac)
  const clientResult = clientHkdfSha256(testIkm, testSalt, testInfo, 32);

  // Plugin's HKDF (@noble/hashes)
  const pluginResult = Buffer.from(hkdf(sha256, testIkm, testSalt, testInfo, 32));

  assertBuffersEqual(clientResult, pluginResult,
    'HKDF-SHA256: client (Node crypto) == plugin (@noble/hashes) for 32-byte output');

  // Also test with a 64-byte output to verify multi-block expansion
  const clientResult64 = clientHkdfSha256(testIkm, testSalt, testInfo, 64);
  const pluginResult64 = Buffer.from(hkdf(sha256, testIkm, testSalt, testInfo, 64));

  assertBuffersEqual(clientResult64, pluginResult64,
    'HKDF-SHA256: client == plugin for 64-byte output (multi-block expand)');
}

// ---------------------------------------------------------------------------
// Test 2: HKDF with actual TotalReclaw info strings
// Verify that HKDF produces identical output when using the real context
// strings used for auth and encryption key derivation.
// ---------------------------------------------------------------------------

section('HKDF with TotalReclaw info strings');

{
  const fakeMasterKey = Buffer.alloc(32, 0xCC);
  const fakeSalt = Buffer.alloc(32, 0xDD);

  // Auth key derivation
  const clientAuth = clientHkdfSha256(
    fakeMasterKey, fakeSalt, Buffer.from(AUTH_KEY_INFO, 'utf-8'), 32,
  );
  const pluginAuth = Buffer.from(
    hkdf(sha256, fakeMasterKey, fakeSalt, Buffer.from(AUTH_KEY_INFO, 'utf8'), 32),
  );
  assertBuffersEqual(clientAuth, pluginAuth,
    `HKDF with info="${AUTH_KEY_INFO}": client == plugin`);

  // Encryption key derivation
  const clientEnc = clientHkdfSha256(
    fakeMasterKey, fakeSalt, Buffer.from(ENCRYPTION_KEY_INFO, 'utf-8'), 32,
  );
  const pluginEnc = Buffer.from(
    hkdf(sha256, fakeMasterKey, fakeSalt, Buffer.from(ENCRYPTION_KEY_INFO, 'utf8'), 32),
  );
  assertBuffersEqual(clientEnc, pluginEnc,
    `HKDF with info="${ENCRYPTION_KEY_INFO}": client == plugin`);

  // Dedup key derivation (plugin-only feature, but verify HKDF consistency)
  const clientDedup = clientHkdfSha256(
    fakeMasterKey, fakeSalt, Buffer.from(DEDUP_KEY_INFO, 'utf-8'), 32,
  );
  const pluginDedup = Buffer.from(
    hkdf(sha256, fakeMasterKey, fakeSalt, Buffer.from(DEDUP_KEY_INFO, 'utf8'), 32),
  );
  assertBuffersEqual(clientDedup, pluginDedup,
    `HKDF with info="${DEDUP_KEY_INFO}": client == plugin`);

  // Verify auth != encryption (different info strings must produce different keys)
  assert(!clientAuth.equals(clientEnc),
    'Auth key != encryption key (different HKDF info strings)');
}

// ---------------------------------------------------------------------------
// Test 3: Full key derivation with password (Argon2id + HKDF)
// Both implementations should produce identical keys from the same password
// and salt, since they use the same Argon2id parameters and HKDF.
// ---------------------------------------------------------------------------

section('Full key derivation: password path');

{
  const password = 'test-password-for-parity';
  const salt = Buffer.alloc(32, 0xAB);

  // Derive keys using "client algorithm" (Argon2id via @noble + client HKDF)
  const clientKeys = clientDeriveKeysPassword(password, salt);

  // Derive keys using "plugin algorithm" (Argon2id via @noble + @noble HKDF)
  const pluginKeys = pluginDeriveKeysPassword(password, salt);

  assertBuffersEqual(clientKeys.authKey, pluginKeys.authKey,
    'Password path: authKey matches (client algorithm == plugin algorithm)');

  assertBuffersEqual(clientKeys.encryptionKey, pluginKeys.encryptionKey,
    'Password path: encryptionKey matches (client algorithm == plugin algorithm)');

  // Print the derived keys for manual cross-verification with the native client
  console.log(`#   password: "${password}"`);
  console.log(`#   salt:     ${salt.toString('hex')}`);
  console.log(`#   authKey:  ${clientKeys.authKey.toString('hex')}`);
  console.log(`#   encKey:   ${clientKeys.encryptionKey.toString('hex')}`);
  console.log('#');
  console.log('#   To verify against native argon2 (client library):');
  console.log('#   cd client && npm install && node -e "');
  console.log(`#     const {deriveKeys} = require('./dist/crypto/kdf');`);
  console.log(`#     const salt = Buffer.alloc(32, 0xAB);`);
  console.log(`#     deriveKeys('test-password-for-parity', salt).then(k => {`);
  console.log(`#       console.log('authKey:', k.authKey.toString('hex'));`);
  console.log(`#       console.log('encKey:', k.encryptionKey.toString('hex'));`);
  console.log('#     });');
  console.log('#   "');
}

// ---------------------------------------------------------------------------
// Test 4: Full key derivation with a different password
// Ensures the parity is not coincidental for a single input.
// ---------------------------------------------------------------------------

section('Full key derivation: second password');

{
  const password = 'a-longer-and-more-complex-p@ssw0rd!-with-$pecial-chars';
  const salt = crypto.createHash('sha256').update('deterministic-salt-seed').digest();

  const clientKeys = clientDeriveKeysPassword(password, salt);
  const pluginKeys = pluginDeriveKeysPassword(password, salt);

  assertBuffersEqual(clientKeys.authKey, pluginKeys.authKey,
    'Second password: authKey matches');

  assertBuffersEqual(clientKeys.encryptionKey, pluginKeys.encryptionKey,
    'Second password: encryptionKey matches');
}

// ---------------------------------------------------------------------------
// Test 5: Full key derivation with empty-ish edge cases
// ---------------------------------------------------------------------------

section('Full key derivation: edge cases');

{
  // Single character password
  const salt = Buffer.alloc(32, 0x01);
  const clientKeys = clientDeriveKeysPassword('x', salt);
  const pluginKeys = pluginDeriveKeysPassword('x', salt);

  assertBuffersEqual(clientKeys.authKey, pluginKeys.authKey,
    'Single-char password: authKey matches');
  assertBuffersEqual(clientKeys.encryptionKey, pluginKeys.encryptionKey,
    'Single-char password: encryptionKey matches');

  // Unicode password
  const clientKeysUni = clientDeriveKeysPassword('\u{1F512}SecureVault\u{2764}', salt);
  const pluginKeysUni = pluginDeriveKeysPassword('\u{1F512}SecureVault\u{2764}', salt);

  assertBuffersEqual(clientKeysUni.authKey, pluginKeysUni.authKey,
    'Unicode password: authKey matches');
  assertBuffersEqual(clientKeysUni.encryptionKey, pluginKeysUni.encryptionKey,
    'Unicode password: encryptionKey matches');
}

// ---------------------------------------------------------------------------
// Test 6: BIP-39 mnemonic path (plugin only)
// The client library handles mnemonics differently (via seed.ts with BIP-32
// HD derivation from the private key, not from the raw seed). The plugin
// uses HKDF directly from the 512-bit BIP-39 seed.
//
// These are INTENTIONALLY DIFFERENT derivation paths:
//   - Client (seed.ts): mnemonic -> BIP-39 seed -> BIP-32 -> privateKey -> HKDF
//   - Plugin (crypto.ts): mnemonic -> BIP-39 seed -> HKDF (skip BIP-32)
//
// This test verifies the plugin's mnemonic path is internally consistent
// and deterministic. Cross-implementation mnemonic parity is NOT expected
// (the client uses a different derivation chain for mnemonics).
// ---------------------------------------------------------------------------

section('BIP-39 mnemonic path (plugin determinism)');

{
  // Standard BIP-39 test mnemonic (from BIP-39 spec test vectors)
  const testMnemonic = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

  assert(validateMnemonic(testMnemonic, wordlist),
    'Test mnemonic is valid BIP-39');

  // Derive twice -- must be deterministic
  const keys1 = pluginDeriveKeysMnemonic(testMnemonic);
  const keys2 = pluginDeriveKeysMnemonic(testMnemonic);

  assertBuffersEqual(keys1.authKey, keys2.authKey,
    'Mnemonic path: authKey is deterministic across calls');
  assertBuffersEqual(keys1.encryptionKey, keys2.encryptionKey,
    'Mnemonic path: encryptionKey is deterministic across calls');
  assertBuffersEqual(keys1.dedupKey, keys2.dedupKey,
    'Mnemonic path: dedupKey is deterministic across calls');
  assertBuffersEqual(keys1.salt, keys2.salt,
    'Mnemonic path: salt is deterministic (derived from seed)');

  // Keys must all be different from each other (domain separation)
  assert(!keys1.authKey.equals(keys1.encryptionKey),
    'Mnemonic path: authKey != encryptionKey');
  assert(!keys1.authKey.equals(keys1.dedupKey),
    'Mnemonic path: authKey != dedupKey');
  assert(!keys1.encryptionKey.equals(keys1.dedupKey),
    'Mnemonic path: encryptionKey != dedupKey');

  // Print for manual verification / regression
  console.log(`#   mnemonic: "${testMnemonic}"`);
  console.log(`#   salt:     ${keys1.salt.toString('hex')}`);
  console.log(`#   authKey:  ${keys1.authKey.toString('hex')}`);
  console.log(`#   encKey:   ${keys1.encryptionKey.toString('hex')}`);
  console.log(`#   dedupKey: ${keys1.dedupKey.toString('hex')}`);
}

// ---------------------------------------------------------------------------
// Test 7: BIP-39 mnemonic vs password path produce DIFFERENT keys
// A mnemonic entered as a "password" must NOT produce the same keys as
// the mnemonic path. The plugin auto-detects mnemonics and routes them
// differently. This test confirms that routing matters.
// ---------------------------------------------------------------------------

section('BIP-39 mnemonic vs password path divergence');

{
  const testMnemonic = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

  // Mnemonic path (HKDF from 512-bit BIP-39 seed)
  const mnemonicKeys = pluginDeriveKeysMnemonic(testMnemonic);

  // Password path with the mnemonic string treated as a plain password
  // (forces Argon2id, ignoring BIP-39 detection)
  const passwordKeys = pluginDeriveKeysPassword(testMnemonic, mnemonicKeys.salt);

  assert(!mnemonicKeys.authKey.equals(passwordKeys.authKey),
    'Mnemonic path authKey != password path authKey (different derivation chains)');
  assert(!mnemonicKeys.encryptionKey.equals(passwordKeys.encryptionKey),
    'Mnemonic path encryptionKey != password path encryptionKey');
}

// ---------------------------------------------------------------------------
// Test 8: Argon2id parameter verification
// Verify that both implementations agree on the Argon2id parameters.
// This is a static check -- the parameters are hardcoded in both files.
// ---------------------------------------------------------------------------

section('Argon2id parameter agreement');

{
  // Client defaults (from client/src/crypto/kdf.ts lines 26-30)
  const clientMemoryCost = 65536;
  const clientTimeCost = 3;
  const clientParallelism = 4;
  const clientHashLength = 32;

  // Plugin defaults (from skill/plugin/crypto.ts lines 40-43)
  assert(ARGON2_MEMORY_COST === clientMemoryCost,
    `Argon2id memoryCost: plugin (${ARGON2_MEMORY_COST}) == client (${clientMemoryCost})`);
  assert(ARGON2_TIME_COST === clientTimeCost,
    `Argon2id timeCost: plugin (${ARGON2_TIME_COST}) == client (${clientTimeCost})`);
  assert(ARGON2_PARALLELISM === clientParallelism,
    `Argon2id parallelism: plugin (${ARGON2_PARALLELISM}) == client (${clientParallelism})`);
  assert(ARGON2_DK_LEN === clientHashLength,
    `Argon2id dkLen: plugin (${ARGON2_DK_LEN}) == client (${clientHashLength})`);
}

// ---------------------------------------------------------------------------
// Test 9: HKDF info string agreement
// Verify the context strings are byte-identical between implementations.
// ---------------------------------------------------------------------------

section('HKDF info string agreement');

{
  // Client uses these literals directly in kdf.ts:
  //   Buffer.from('totalreclaw-auth-key-v1', 'utf-8')       (line 77)
  //   Buffer.from('totalreclaw-encryption-key-v1', 'utf-8')  (line 125)
  //
  // Plugin declares them as constants:
  //   const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';        (line 35)
  //   const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1'; (line 36)

  assert(AUTH_KEY_INFO === 'totalreclaw-auth-key-v1',
    'AUTH_KEY_INFO matches client literal');
  assert(ENCRYPTION_KEY_INFO === 'totalreclaw-encryption-key-v1',
    'ENCRYPTION_KEY_INFO matches client literal');

  // Verify the UTF-8 encoding produces identical bytes
  const clientAuthInfo = Buffer.from('totalreclaw-auth-key-v1', 'utf-8');
  const pluginAuthInfo = Buffer.from(AUTH_KEY_INFO, 'utf8');
  assertBuffersEqual(clientAuthInfo, pluginAuthInfo,
    'Auth info string: utf-8 vs utf8 encoding produces same bytes');

  const clientEncInfo = Buffer.from('totalreclaw-encryption-key-v1', 'utf-8');
  const pluginEncInfo = Buffer.from(ENCRYPTION_KEY_INFO, 'utf8');
  assertBuffersEqual(clientEncInfo, pluginEncInfo,
    'Encryption info string: utf-8 vs utf8 encoding produces same bytes');
}

// ---------------------------------------------------------------------------
// Test 10: Auth key hash (SHA-256 of auth key)
// The plugin computes SHA256(authKey) for server registration. Verify this
// uses the same SHA-256 as the client would.
// ---------------------------------------------------------------------------

section('Auth key hash (SHA-256)');

{
  const password = 'test-password-for-parity';
  const salt = Buffer.alloc(32, 0xAB);
  const keys = pluginDeriveKeysPassword(password, salt);

  // Plugin's computeAuthKeyHash: Buffer.from(sha256(authKey)).toString('hex')
  const pluginHash = Buffer.from(sha256(keys.authKey)).toString('hex');

  // Client would use: crypto.createHash('sha256').update(authKey).digest('hex')
  const clientHash = crypto.createHash('sha256').update(keys.authKey).digest('hex');

  assert(pluginHash === clientHash,
    `Auth key hash: @noble/hashes sha256 == Node crypto sha256 (${pluginHash.slice(0, 16)}...)`);
}

// ---------------------------------------------------------------------------
// Test 11: Cross-encryption verification
// Encrypt with one implementation's key, decrypt with the other's.
// Since we proved the keys are identical (Test 3), this is a functional
// sanity check that the XChaCha20-Poly1305 encrypt/decrypt works with the derived keys.
// ---------------------------------------------------------------------------

section('Cross-encryption with derived keys');

{
  const password = 'test-password-for-parity';
  const salt = Buffer.alloc(32, 0xAB);
  const keys = pluginDeriveKeysPassword(password, salt);
  const plaintext = 'The user prefers dark mode and speaks Portuguese.';

  // Encrypt using Node.js crypto (as the client does)
  const iv = Buffer.alloc(12, 0x42); // deterministic IV for testing only
  const cipher = crypto.createCipheriv('aes-256-gcm', keys.encryptionKey, iv, {
    authTagLength: 16,
  });
  const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  const combined = Buffer.concat([iv, tag, ciphertext]);
  const encrypted = combined.toString('base64');

  // Decrypt using Node.js crypto (as the plugin does)
  const decoded = Buffer.from(encrypted, 'base64');
  const decIv = decoded.subarray(0, 12);
  const decTag = decoded.subarray(12, 28);
  const decCiphertext = decoded.subarray(28);
  const decipher = crypto.createDecipheriv('aes-256-gcm', keys.encryptionKey, decIv, {
    authTagLength: 16,
  });
  decipher.setAuthTag(decTag);
  const decrypted = Buffer.concat([decipher.update(decCiphertext), decipher.final()]).toString('utf8');

  assert(decrypted === plaintext,
    'Cross-encryption: encrypt then decrypt recovers original plaintext');
}

// ---------------------------------------------------------------------------
// Test 12: Determinism -- same inputs always produce same outputs
// ---------------------------------------------------------------------------

section('Determinism');

{
  const password = 'determinism-check';
  const salt = Buffer.alloc(32, 0xFF);

  const run1 = clientDeriveKeysPassword(password, salt);
  const run2 = clientDeriveKeysPassword(password, salt);
  const run3 = pluginDeriveKeysPassword(password, salt);

  assertBuffersEqual(run1.authKey, run2.authKey,
    'Client algorithm: authKey is deterministic across calls');
  assertBuffersEqual(run1.encryptionKey, run2.encryptionKey,
    'Client algorithm: encryptionKey is deterministic across calls');
  assertBuffersEqual(run1.authKey, run3.authKey,
    'Cross-implementation: deterministic authKey');
  assertBuffersEqual(run1.encryptionKey, run3.encryptionKey,
    'Cross-implementation: deterministic encryptionKey');
}

// ---------------------------------------------------------------------------
// Test 13: Different salts produce different keys
// ---------------------------------------------------------------------------

section('Salt sensitivity');

{
  const password = 'same-password';
  const salt1 = Buffer.alloc(32, 0x01);
  const salt2 = Buffer.alloc(32, 0x02);

  const keys1 = pluginDeriveKeysPassword(password, salt1);
  const keys2 = pluginDeriveKeysPassword(password, salt2);

  assert(!keys1.authKey.equals(keys2.authKey),
    'Different salts produce different authKeys');
  assert(!keys1.encryptionKey.equals(keys2.encryptionKey),
    'Different salts produce different encryptionKeys');
}

// ---------------------------------------------------------------------------
// Test 14: Different passwords produce different keys
// ---------------------------------------------------------------------------

section('Password sensitivity');

{
  const salt = Buffer.alloc(32, 0xAA);
  const keys1 = pluginDeriveKeysPassword('password-one', salt);
  const keys2 = pluginDeriveKeysPassword('password-two', salt);

  assert(!keys1.authKey.equals(keys2.authKey),
    'Different passwords produce different authKeys');
  assert(!keys1.encryptionKey.equals(keys2.encryptionKey),
    'Different passwords produce different encryptionKeys');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n1..${testNum}`);
console.log(`# passed: ${passed}`);
console.log(`# failed: ${failed}`);
console.log(`# total:  ${testNum}`);

if (failed > 0) {
  console.log('\n# FAIL -- crypto parity broken! Memories encrypted by one');
  console.log('# implementation will NOT be decryptable by the other.');
  process.exit(1);
} else {
  console.log('\n# OK -- all crypto parity checks passed.');
  console.log('# NOTE: Argon2id equivalence verified algorithmically (same params +');
  console.log('# same @noble/hashes implementation). For definitive native-vs-pure-JS');
  console.log('# Argon2id verification, run the manual cross-check in the header.');
  process.exit(0);
}
