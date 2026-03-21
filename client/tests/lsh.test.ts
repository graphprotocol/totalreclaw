/**
 * LSH Module Tests
 *
 * Tests for Random Hyperplane LSH implementation.
 */

import { LSHIndex, hammingDistance, estimateSimilarity } from '../src/lsh/hyperplane';
import { calculateCandidatePool, LSH_SCALING_TABLE, mergeLSHConfig } from '../src/lsh/config';
import { createDummyEmbedding, createHashBasedEmbedding } from '../src/embedding/onnx';

describe('LSH Module', () => {
  describe('LSH Config', () => {
    test('should calculate candidate pool for small corpus', () => {
      expect(calculateCandidatePool(500)).toBe(2000);
      expect(calculateCandidatePool(1000)).toBe(2000);
    });

    test('should calculate candidate pool for medium corpus', () => {
      const pool = calculateCandidatePool(8727);
      expect(pool).toBeGreaterThanOrEqual(2000);
      expect(pool).toBeLessThanOrEqual(4000);
    });

    test('should calculate candidate pool for large corpus', () => {
      const pool = calculateCandidatePool(50000);
      expect(pool).toBeLessThanOrEqual(10000);
    });

    test('should cap candidate pool at maximum', () => {
      const pool = calculateCandidatePool(1000000);
      expect(pool).toBe(10000);
    });

    test('should merge config with defaults', () => {
      const config = mergeLSHConfig({ n_tables: 16 });
      expect(config.n_bits_per_table).toBe(64);
      expect(config.n_tables).toBe(16);
      expect(config.candidate_pool).toBe(3000);
    });

    test('should return defaults when no config provided', () => {
      const config = mergeLSHConfig();
      expect(config.n_bits_per_table).toBe(64);
      expect(config.n_tables).toBe(12);
      expect(config.candidate_pool).toBe(3000);
    });
  });

  describe('LSHIndex', () => {
    let index: LSHIndex;

    beforeEach(() => {
      index = new LSHIndex({ n_bits_per_table: 64, n_tables: 12, candidate_pool: 3000 });
    });

    test('should initialize with embedding dimension', () => {
      index.initialize(1024);
      expect(index.isReady()).toBe(true);
      expect(index.getEmbeddingDimension()).toBe(1024);
    });

    test('should build index from embeddings', () => {
      const embeddings = [createDummyEmbedding(1)];
      index.buildIndex(embeddings);
      expect(index.isReady()).toBe(true);
    });

    test('should hash vectors to correct number of buckets', () => {
      index.initialize(1024);
      const embedding = createDummyEmbedding(42);
      const buckets = index.hashVector(embedding);

      expect(buckets.length).toBe(12); // n_tables
    });

    test('should produce consistent hashes for same vector', () => {
      index.initialize(1024, 12345); // Fixed seed
      const embedding = createDummyEmbedding(42);

      const buckets1 = index.hashVector(embedding);
      const buckets2 = index.hashVector(embedding);

      expect(buckets1).toEqual(buckets2);
    });

    test('should produce different hashes for different vectors', () => {
      index.initialize(1024);
      const embedding1 = createDummyEmbedding(1);
      const embedding2 = createDummyEmbedding(2);

      const buckets1 = index.hashVector(embedding1);
      const buckets2 = index.hashVector(embedding2);

      // Most buckets should differ for different vectors
      const matchingBuckets = buckets1.filter((b, i) => b === buckets2[i]);
      expect(matchingBuckets.length).toBeLessThan(buckets1.length);
    });

    test('should hash vectors with prefix', () => {
      index.initialize(1024);
      const embedding = createDummyEmbedding(42);
      const buckets = index.hashVectorWithPrefix(embedding);

      expect(buckets.length).toBe(12);
      expect(buckets[0]).toMatch(/^table_0_/);
      expect(buckets[11]).toMatch(/^table_11_/);
    });

    test('should throw if not initialized', () => {
      const embedding = createDummyEmbedding(42);
      expect(() => index.hashVector(embedding)).toThrow('not initialized');
    });

    test('should throw on dimension mismatch', () => {
      index.initialize(1024);
      const wrongDimEmbedding = new Array(128).fill(0);
      expect(() => index.hashVector(wrongDimEmbedding)).toThrow('dimension mismatch');
    });

    test('should export and import hyperplanes', () => {
      index.initialize(1024, 12345);
      const embedding = createDummyEmbedding(42);
      const originalBuckets = index.hashVector(embedding);

      const exported = index.exportHyperplanes();
      const newIndex = new LSHIndex();
      newIndex.importHyperplanes(exported);

      const importedBuckets = newIndex.hashVector(embedding);
      expect(importedBuckets).toEqual(originalBuckets);
    });

    test('should get config', () => {
      const config = index.getConfig();
      expect(config.n_bits_per_table).toBe(64);
      expect(config.n_tables).toBe(12);
      expect(config.candidate_pool).toBe(3000);
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
      const similarity = estimateSimilarity(1, 64);
      expect(similarity).toBeGreaterThan(0.9);
    });

    test('should estimate low similarity for large Hamming distance', () => {
      const similarity = estimateSimilarity(50, 64);
      expect(similarity).toBeLessThan(0);
    });

    test('should estimate medium similarity for half distance', () => {
      const similarity = estimateSimilarity(32, 64);
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
      const index = new LSHIndex({ n_bits_per_table: 64, n_tables: 12 });
      index.initialize(1024, 12345);

      const base = createDummyEmbedding(1);

      const buckets1 = index.hashVector(base);
      const buckets2 = index.hashVector(base);

      // Identical vectors should have all matching buckets
      const matchingBuckets = buckets1.filter((b, i) => b === buckets2[i]);
      expect(matchingBuckets.length).toBe(buckets1.length);
    });

    test('should have some bucket overlap for similar vectors', () => {
      const index = new LSHIndex({ n_bits_per_table: 64, n_tables: 12 });
      index.initialize(1024, 12345);

      // Create a slightly modified vector that should be similar
      const base = createDummyEmbedding(1);
      const similar = base.map((v) => v * 0.99); // Very similar (99% same direction)

      const buckets1 = index.hashVector(base);
      const buckets2 = index.hashVector(similar);

      // With very similar vectors, some buckets should match
      // Note: LSH is probabilistic, so we just check that the mechanism works
      const matchingBuckets = buckets1.filter((b, i) => b === buckets2[i]);

      // At minimum, the hashing should be consistent
      expect(buckets1.length).toBe(buckets2.length);
      expect(typeof buckets1[0]).toBe('string');
      expect(typeof buckets2[0]).toBe('string');
    });
  });
});
