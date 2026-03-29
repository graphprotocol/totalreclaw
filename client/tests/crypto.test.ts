/**
 * Crypto Module Tests
 *
 * Tests for encryption, key derivation, and blind index generation.
 */

import {
  deriveKeys,
  deriveAuthKey,
  deriveEncryptionKey,
  generateSalt,
  createAuthProof,
  verifyAuthProof,
} from '../src/crypto/kdf';
import { encrypt, decrypt } from '../src/crypto/aes';
import {
  tokenize,
  sha256Hash,
  generateBlindIndices,
  generateTrapdoors,
  computeIndexOverlap,
} from '../src/crypto/blind';

describe('Crypto Module', () => {
  describe('Key Derivation', () => {
    test('should generate random salt', () => {
      const salt1 = generateSalt();
      const salt2 = generateSalt();

      expect(salt1.length).toBe(32);
      expect(salt2.length).toBe(32);
      expect(salt1).not.toEqual(salt2);
    });

    test('should generate salt of custom length', () => {
      const salt = generateSalt(16);
      expect(salt.length).toBe(16);
    });

    test('should derive consistent keys from same password and salt', async () => {
      const password = 'test-password-123';
      const salt = generateSalt();

      const keys1 = await deriveKeys(password, salt);
      const keys2 = await deriveKeys(password, salt);

      expect(keys1.authKey).toEqual(keys2.authKey);
      expect(keys1.encryptionKey).toEqual(keys2.encryptionKey);
    });

    test('should derive different keys from different passwords', async () => {
      const salt = generateSalt();

      const keys1 = await deriveKeys('password1', salt);
      const keys2 = await deriveKeys('password2', salt);

      expect(keys1.authKey).not.toEqual(keys2.authKey);
      expect(keys1.encryptionKey).not.toEqual(keys2.encryptionKey);
    });

    test('should derive different keys from different salts', async () => {
      const password = 'test-password';

      const keys1 = await deriveKeys(password, generateSalt());
      const keys2 = await deriveKeys(password, generateSalt());

      expect(keys1.authKey).not.toEqual(keys2.authKey);
      expect(keys1.encryptionKey).not.toEqual(keys2.encryptionKey);
    });

    test('should derive 32-byte keys', async () => {
      const keys = await deriveKeys('test-password', generateSalt());

      expect(keys.authKey.length).toBe(32);
      expect(keys.encryptionKey.length).toBe(32);
    });
  });

  describe('Authentication', () => {
    test('should create and verify auth proof', async () => {
      const password = 'test-password';
      const salt = generateSalt();
      const { authKey } = await deriveKeys(password, salt);
      const data = Buffer.from('test-data', 'utf-8');

      const proof = createAuthProof(authKey, data);
      const verified = verifyAuthProof(authKey, data, proof);

      expect(verified).toBe(true);
    });

    test('should fail verification with wrong key', async () => {
      const password = 'test-password';
      const salt = generateSalt();
      const { authKey } = await deriveKeys(password, salt);
      const { authKey: wrongKey } = await deriveKeys('wrong-password', salt);
      const data = Buffer.from('test-data', 'utf-8');

      const proof = createAuthProof(authKey, data);
      const verified = verifyAuthProof(wrongKey, data, proof);

      expect(verified).toBe(false);
    });

    test('should fail verification with wrong data', async () => {
      const password = 'test-password';
      const salt = generateSalt();
      const { authKey } = await deriveKeys(password, salt);

      const proof = createAuthProof(authKey, Buffer.from('correct-data', 'utf-8'));
      const verified = verifyAuthProof(authKey, Buffer.from('wrong-data', 'utf-8'), proof);

      expect(verified).toBe(false);
    });
  });

  describe('AES-256-GCM Encryption (base64 wire format)', () => {
    test('should encrypt and decrypt strings correctly', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const text = 'Hello, World!';

      const encrypted = encrypt(text, encryptionKey);
      const decrypted = decrypt(encrypted, encryptionKey);

      expect(decrypted).toBe(text);
    });

    test('should return base64-encoded string from encrypt', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const encrypted = encrypt('test', encryptionKey);

      // Should be a valid base64 string
      expect(typeof encrypted).toBe('string');
      const decoded = Buffer.from(encrypted, 'base64');
      // iv(12) + tag(16) + ciphertext(>=1) = at least 29 bytes
      expect(decoded.length).toBeGreaterThanOrEqual(29);
    });

    test('should produce different ciphertexts for same plaintext (random IV)', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());

      const encrypted1 = encrypt('Hello, World!', encryptionKey);
      const encrypted2 = encrypt('Hello, World!', encryptionKey);

      // Different IVs mean different ciphertexts
      expect(encrypted1).not.toEqual(encrypted2);
    });

    test('should fail decryption with wrong key', async () => {
      const salt = generateSalt();
      const { encryptionKey: key1 } = await deriveKeys('password1', salt);
      const { encryptionKey: key2 } = await deriveKeys('password2', salt);

      const encrypted = encrypt('Secret message', key1);

      expect(() => {
        decrypt(encrypted, key2);
      }).toThrow();
    });

    test('should fail decryption with tampered ciphertext', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const encrypted = encrypt('Secret message', encryptionKey);

      // Tamper with the base64-encoded data
      const bytes = Buffer.from(encrypted, 'base64');
      bytes[bytes.length - 1] ^= 0xff;
      const tampered = bytes.toString('base64');

      expect(() => {
        decrypt(tampered, encryptionKey);
      }).toThrow();
    });

    test('should handle empty string encryption', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const encrypted = encrypt('', encryptionKey);
      const decrypted = decrypt(encrypted, encryptionKey);

      expect(decrypted).toBe('');
    });

    test('should handle unicode text', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const text = 'Olá mundo! 你好世界 🌍';

      const encrypted = encrypt(text, encryptionKey);
      const decrypted = decrypt(encrypted, encryptionKey);

      expect(decrypted).toBe(text);
    });

    test('wire format should be iv(12) || tag(16) || ciphertext', async () => {
      const { encryptionKey } = await deriveKeys('test-password', generateSalt());
      const encrypted = encrypt('test data', encryptionKey);

      const combined = Buffer.from(encrypted, 'base64');
      // iv = 12, tag = 16, ciphertext = length of "test data" encrypted
      expect(combined.length).toBe(12 + 16 + 9); // AES-GCM: plaintext len == ciphertext len
    });
  });

  describe('Blind Indices (with Porter stemming)', () => {
    test('should tokenize text correctly', () => {
      const tokens = tokenize('Hello, World! This is a test.');
      expect(tokens).toContain('hello');
      expect(tokens).toContain('world');
      expect(tokens).toContain('this');
      expect(tokens).toContain('is');
      // 'a' is filtered as short token (< 2 chars)
      expect(tokens).not.toContain('a');
      expect(tokens).toContain('test');
    });

    test('should filter short tokens', () => {
      const tokens = tokenize('I am a big fan');
      expect(tokens).not.toContain('i');
      expect(tokens).not.toContain('a');
      expect(tokens).toContain('am');
      expect(tokens).toContain('big');
      expect(tokens).toContain('fan');
    });

    test('should produce consistent SHA-256 hashes', () => {
      const hash1 = sha256Hash('test');
      const hash2 = sha256Hash('test');

      expect(hash1).toBe(hash2);
      expect(hash1.length).toBe(64); // 32 bytes hex = 64 chars
    });

    test('should produce different hashes for different inputs', () => {
      const hash1 = sha256Hash('test1');
      const hash2 = sha256Hash('test2');

      expect(hash1).not.toBe(hash2);
    });

    test('should generate blind indices with stemming', () => {
      const text = 'I love running and swimming';
      const indices = generateBlindIndices(text);

      // Should include both exact token hashes and stem hashes
      expect(indices.length).toBeGreaterThan(0);

      // Should be unique
      const uniqueIndices = new Set(indices);
      expect(uniqueIndices.size).toBe(indices.length);

      // "running" stems to "run", "swimming" stems to "swim"
      // We expect more indices than just the raw tokens because stems are added
      const rawTokens = tokenize(text);
      expect(indices.length).toBeGreaterThan(rawTokens.length);
    });

    test('should include stem hashes prefixed with stem:', () => {
      // "communities" should stem to "commun" (via porter-stemmer)
      const indices = generateBlindIndices('communities');
      // We should get at least 2 hashes: the exact word + the stem
      expect(indices.length).toBeGreaterThanOrEqual(2);
    });

    test('should not add stem hash when stem equals token', () => {
      // "test" stems to "test" -- no stem hash should be added
      const indices = generateBlindIndices('test');
      // Only the exact word hash
      expect(indices.length).toBe(1);
    });

    test('generateBlindIndices takes only text (no lshBuckets param)', () => {
      // The new API takes only text, not text + lshBuckets
      const indices = generateBlindIndices('coffee preferences');
      expect(indices.length).toBeGreaterThan(0);
    });

    test('should generate trapdoors consistently', () => {
      const query = 'coffee preferences';
      const lshBuckets = ['abc123', 'def456']; // Already-hashed bucket IDs

      const trapdoors1 = generateTrapdoors(query, lshBuckets);
      const trapdoors2 = generateTrapdoors(query, lshBuckets);

      expect(trapdoors1).toEqual(trapdoors2);
    });

    test('trapdoors should include both word/stem hashes and LSH bucket hashes', () => {
      const query = 'coffee preferences';
      const lshBuckets = ['abc123'];

      const trapdoors = generateTrapdoors(query, lshBuckets);

      // Should include the LSH bucket hash directly
      expect(trapdoors).toContain('abc123');
      // Should also include word hashes
      expect(trapdoors.length).toBeGreaterThan(1);
    });

    test('should compute index overlap correctly', () => {
      const indices1 = ['a', 'b', 'c', 'd'];
      const indices2 = ['b', 'c', 'e', 'f'];

      const overlap = computeIndexOverlap(indices1, indices2);
      expect(overlap).toBe(2); // 'b' and 'c'
    });
  });
});
