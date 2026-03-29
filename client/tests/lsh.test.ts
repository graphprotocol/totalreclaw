/**
 * LSH Module Tests
 *
 * Tests for the HKDF-seeded Random Hyperplane LSH hasher (32-bit, 20 tables).
 * Matches mcp/src/subgraph/lsh.ts.
 */

import { LSHHasher, hammingDistance, estimateSimilarity } from '../src/lsh/hyperplane';
import { calculateCandidatePool, LSH_SCALING_TABLE, mergeLSHConfig, LSH_DEFAULTS } from '../src/lsh/config';
import { createDummyEmbedding, createHashBasedEmbedding } from '../src/embedding/onnx';
import { deriveLshSeed } from '../src/crypto/seed';

// A fixed 32-byte seed for deterministic testing
const TEST_SEED = new Uint8Array(32);
for (let i = 0; i < 32; i++) TEST_SEED[i] = i;

const TEST_MNEMONIC =
  'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

describe('LSH Module', () => {
  describe('LSH Config (32-bit, 20 tables)', () => {
    test('should have correct defaults', () => {
      expect(LSH_DEFAULTS.n_bits_per_table).toBe(32);
      expect(LSH_DEFAULTS.n_tables).toBe(20);
      expect(LSH_DEFAULTS.candidate_pool).toBe(3000);
    });

    test('should calculate candidate pool for small corpus', () => {
      expect(calculateCandidatePool(500)).toBe(2000);
      expect(calculateCandidatePool(1000)).toBe(2000);
    });

    test('should calculate candidate pool for medium corpus', () => {
      const pool = calculateCandidatePool(8727);
      expect(pool).toBeGreaterThanOrEqual(2000);
      expect(pool).toBeLessThanOrEqual(4000);
    });

    test('should cap candidate pool at maximum', () => {
      const pool = calculateCandidatePool(1000000);
      expect(pool).toBe(10000);
    });

    test('should merge config with defaults', () => {
      const config = mergeLSHConfig({ n_tables: 16 });
      expect(config.n_bits_per_table).toBe(32);
      expect(config.n_tables).toBe(16);
      expect(config.candidate_pool).toBe(3000);
    });

    test('should return defaults when no config provided', () => {
      const config = mergeLSHConfig();
      expect(config.n_bits_per_table).toBe(32);
      expect(config.n_tables).toBe(20);
      expect(config.candidate_pool).toBe(3000);
    });
  });

  describe('LSHHasher', () => {
    test('should construct with seed and dims', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      expect(hasher.tables).toBe(20);
      expect(hasher.bits).toBe(32);
      expect(hasher.dimensions).toBe(1024);
    });

    test('should construct with custom nTables and nBits', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024, 10, 16);
      expect(hasher.tables).toBe(10);
      expect(hasher.bits).toBe(16);
      expect(hasher.dimensions).toBe(1024);
    });

    test('should hash vectors to correct number of buckets', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const embedding = createDummyEmbedding(42);
      const buckets = hasher.hash(embedding);

      expect(buckets.length).toBe(20); // n_tables
    });

    test('should produce SHA-256 hex strings as bucket IDs', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const embedding = createDummyEmbedding(42);
      const buckets = hasher.hash(embedding);

      for (const bucket of buckets) {
        expect(bucket).toMatch(/^[a-f0-9]{64}$/);
      }
    });

    test('should produce consistent hashes for same vector', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const embedding = createDummyEmbedding(42);

      const buckets1 = hasher.hash(embedding);
      const buckets2 = hasher.hash(embedding);

      expect(buckets1).toEqual(buckets2);
    });

    test('should produce different hashes for different vectors', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const embedding1 = createDummyEmbedding(1);
      const embedding2 = createDummyEmbedding(2);

      const buckets1 = hasher.hash(embedding1);
      const buckets2 = hasher.hash(embedding2);

      // Most buckets should differ for different vectors
      const matchingBuckets = buckets1.filter((b, i) => b === buckets2[i]);
      expect(matchingBuckets.length).toBeLessThan(buckets1.length);
    });

    test('should throw on dimension mismatch', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const wrongDimEmbedding = new Array(128).fill(0);
      expect(() => hasher.hash(wrongDimEmbedding)).toThrow('dimension mismatch');
    });

    test('should throw if seed is too short', () => {
      expect(() => new LSHHasher(new Uint8Array(8), 1024)).toThrow('too short');
    });

    test('should be deterministic from seed', () => {
      const hasher1 = new LSHHasher(TEST_SEED, 1024);
      const hasher2 = new LSHHasher(TEST_SEED, 1024);
      const embedding = createDummyEmbedding(42);

      expect(hasher1.hash(embedding)).toEqual(hasher2.hash(embedding));
    });

    test('different seeds should produce different hashes', () => {
      const seed2 = new Uint8Array(32);
      for (let i = 0; i < 32; i++) seed2[i] = i + 100;

      const hasher1 = new LSHHasher(TEST_SEED, 1024);
      const hasher2 = new LSHHasher(seed2, 1024);
      const embedding = createDummyEmbedding(42);

      const buckets1 = hasher1.hash(embedding);
      const buckets2 = hasher2.hash(embedding);
      expect(buckets1).not.toEqual(buckets2);
    });
  });

  describe('deriveLshSeed integration', () => {
    test('should work with LSHHasher', () => {
      const seed = deriveLshSeed(TEST_MNEMONIC);
      const hasher = new LSHHasher(seed, 1024);
      const embedding = createDummyEmbedding(42);
      const buckets = hasher.hash(embedding);

      expect(buckets.length).toBe(20);
      for (const bucket of buckets) {
        expect(bucket).toMatch(/^[a-f0-9]{64}$/);
      }
    });

    test('same mnemonic should produce same LSH buckets', () => {
      const seed1 = deriveLshSeed(TEST_MNEMONIC);
      const seed2 = deriveLshSeed(TEST_MNEMONIC);
      const hasher1 = new LSHHasher(seed1, 1024);
      const hasher2 = new LSHHasher(seed2, 1024);
      const embedding = createDummyEmbedding(42);

      expect(hasher1.hash(embedding)).toEqual(hasher2.hash(embedding));
    });
  });

  describe('Hamming Distance', () => {
    test('should compute distance between identical strings', () => {
      expect(hammingDistance('0000', '0000')).toBe(0);
      expect(hammingDistance('1111', '1111')).toBe(0);
    });

    test('should compute distance between different strings', () => {
      expect(hammingDistance('0000', '1111')).toBe(4);
      expect(hammingDistance('0101', '1010')).toBe(4);
      expect(hammingDistance('0000', '0001')).toBe(1);
    });

    test('should handle different length strings', () => {
      expect(hammingDistance('00', '1111')).toBe(4);
    });
  });

  describe('Similarity Estimation', () => {
    test('should estimate high similarity for small Hamming distance', () => {
      const similarity = estimateSimilarity(1, 32);
      expect(similarity).toBeGreaterThan(0.9);
    });

    test('should estimate low similarity for large Hamming distance', () => {
      const similarity = estimateSimilarity(28, 32);
      expect(similarity).toBeLessThan(0);
    });

    test('should estimate medium similarity for half distance', () => {
      const similarity = estimateSimilarity(16, 32);
      expect(Math.abs(similarity)).toBeLessThan(0.1); // Close to 0 (orthogonal)
    });
  });

  describe('Hash-based Embeddings', () => {
    test('should create consistent hash-based embeddings', () => {
      const emb1 = createHashBasedEmbedding('test text');
      const emb2 = createHashBasedEmbedding('test text');

      expect(emb1).toEqual(emb2);
      expect(emb1.length).toBe(1024);
    });

    test('should create different embeddings for different texts', () => {
      const emb1 = createHashBasedEmbedding('test text 1');
      const emb2 = createHashBasedEmbedding('test text 2');

      expect(emb1).not.toEqual(emb2);
    });

    test('should create normalized embeddings', () => {
      const emb = createHashBasedEmbedding('test text');
      let norm = 0;
      for (const val of emb) {
        norm += val * val;
      }
      norm = Math.sqrt(norm);

      expect(Math.abs(norm - 1)).toBeLessThan(0.001);
    });

    test('should create dummy embeddings', () => {
      const emb = createDummyEmbedding(42);
      expect(emb.length).toBe(1024);
    });

    test('should create deterministic dummy embeddings with seed', () => {
      const emb1 = createDummyEmbedding(42);
      const emb2 = createDummyEmbedding(42);

      expect(emb1).toEqual(emb2);
    });
  });

  describe('LSH Recall Quality', () => {
    test('should bucket identical vectors together', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);
      const base = createDummyEmbedding(1);

      const buckets1 = hasher.hash(base);
      const buckets2 = hasher.hash(base);

      expect(buckets1).toEqual(buckets2);
    });

    test('should have some bucket overlap for similar vectors', () => {
      const hasher = new LSHHasher(TEST_SEED, 1024);

      const base = createDummyEmbedding(1);
      const similar = base.map((v) => v * 0.99);

      const buckets1 = hasher.hash(base);
      const buckets2 = hasher.hash(similar);

      // With very similar vectors, many or all buckets should match
      // (since scaling preserves direction and sign of dot products)
      const matchingBuckets = buckets1.filter((b, i) => b === buckets2[i]);
      expect(matchingBuckets.length).toBeGreaterThan(0);
    });
  });
});
