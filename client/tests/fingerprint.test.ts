/**
 * Content Fingerprint Tests
 *
 * Tests for HMAC-SHA256 content fingerprint derivation (v0.3.1b).
 * Spec: docs/specs/totalreclaw/server.md section 8.2
 */
import { deriveKeys, generateSalt } from '../src/crypto/kdf';
import {
  deriveDedupKey,
  normalizeText,
  computeContentFingerprint,
} from '../src/crypto/fingerprint';

describe('Content Fingerprint (v0.3.1b)', () => {
  const testSalt = Buffer.alloc(32, 0xab); // deterministic for testing

  // Fingerprinting is HKDF+HMAC over an already-derived key — the Argon2id
  // KDF itself is covered in crypto.test.ts. Derive shared fixtures once
  // with cheap parameters so this suite stays fast.
  const FAST_KDF = { memoryCost: 1024, timeCost: 1 };
  let keys: Awaited<ReturnType<typeof deriveKeys>>;

  beforeAll(async () => {
    keys = await deriveKeys('test-password', testSalt, FAST_KDF);
  });

  describe('normalizeText', () => {
    test('lowercases text', () => {
      expect(normalizeText('Hello World')).toBe('hello world');
    });

    test('collapses multiple spaces', () => {
      expect(normalizeText('hello    world')).toBe('hello world');
    });

    test('collapses tabs and newlines to single space', () => {
      expect(normalizeText('hello\t\n\nworld')).toBe('hello world');
    });

    test('trims leading and trailing whitespace', () => {
      expect(normalizeText('  hello world  ')).toBe('hello world');
    });

    test('applies Unicode NFC normalization', () => {
      // e + combining accent (NFD) should normalize to single char (NFC)
      const nfd = 'caf\u0065\u0301'; // e + combining acute
      const nfc = 'caf\u00e9'; // e-acute
      expect(normalizeText(nfd)).toBe(normalizeText(nfc));
    });

    test('handles empty string', () => {
      expect(normalizeText('')).toBe('');
    });

    test('handles whitespace-only string', () => {
      expect(normalizeText('   \t\n  ')).toBe('');
    });
  });

  describe('deriveDedupKey', () => {
    test('derives a 32-byte key', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      expect(dedupKey.length).toBe(32);
    });

    test('is deterministic for same inputs', async () => {
      const key1 = deriveDedupKey(keys.encryptionKey, testSalt);
      const key2 = deriveDedupKey(keys.encryptionKey, testSalt);
      expect(key1).toEqual(key2);
    });

    test('differs from encryption key', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      expect(dedupKey).not.toEqual(keys.encryptionKey);
    });

    test('differs from auth key', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      expect(dedupKey).not.toEqual(keys.authKey);
    });

    test('differs for different salts', async () => {
      const keys1 = keys;
      const salt2 = Buffer.alloc(32, 0xcd);
      const keys2 = await deriveKeys('test-password', salt2, FAST_KDF);
      const dedup1 = deriveDedupKey(keys1.encryptionKey, testSalt);
      const dedup2 = deriveDedupKey(keys2.encryptionKey, salt2);
      expect(dedup1).not.toEqual(dedup2);
    });
  });

  describe('computeContentFingerprint', () => {
    test('returns hex-encoded HMAC-SHA256', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      const fp = computeContentFingerprint(dedupKey, 'User prefers Python');
      // HMAC-SHA256 produces 64-char hex string (32 bytes)
      expect(fp).toMatch(/^[a-f0-9]{64}$/);
    });

    test('is deterministic for same content', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      const fp1 = computeContentFingerprint(dedupKey, 'User prefers Python');
      const fp2 = computeContentFingerprint(dedupKey, 'User prefers Python');
      expect(fp1).toBe(fp2);
    });

    test('normalizes before hashing (case insensitive)', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      const fp1 = computeContentFingerprint(dedupKey, 'User Prefers Python');
      const fp2 = computeContentFingerprint(dedupKey, 'user prefers python');
      expect(fp1).toBe(fp2);
    });

    test('normalizes whitespace before hashing', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      const fp1 = computeContentFingerprint(dedupKey, 'User prefers Python');
      const fp2 = computeContentFingerprint(dedupKey, '  User   prefers  Python  ');
      expect(fp1).toBe(fp2);
    });

    test('differs for different content', async () => {
      const dedupKey = deriveDedupKey(keys.encryptionKey, testSalt);
      const fp1 = computeContentFingerprint(dedupKey, 'User prefers Python');
      const fp2 = computeContentFingerprint(dedupKey, 'User prefers JavaScript');
      expect(fp1).not.toBe(fp2);
    });

    test('differs for different dedup keys', async () => {
      const keys1 = await deriveKeys('password-1', testSalt, FAST_KDF);
      const keys2 = await deriveKeys('password-2', testSalt, FAST_KDF);
      const dedup1 = deriveDedupKey(keys1.encryptionKey, testSalt);
      const dedup2 = deriveDedupKey(keys2.encryptionKey, testSalt);
      const fp1 = computeContentFingerprint(dedup1, 'same content');
      const fp2 = computeContentFingerprint(dedup2, 'same content');
      expect(fp1).not.toBe(fp2);
    });
  });
});
