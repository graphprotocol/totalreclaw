/**
 * Unit tests for memory consolidation & near-duplicate detection.
 *
 * Run with:
 *   npx jest tests/consolidation.test.ts
 *   # or standalone:
 *   npx tsx tests/consolidation.test.ts
 *
 * Ported from skill/plugin/consolidation.test.ts.
 */

import {
  findNearDuplicate,
  shouldSupersede,
  clusterFacts,
  getStoreDedupThreshold,
  getConsolidationThreshold,
  STORE_DEDUP_MAX_CANDIDATES,
} from '../src/consolidation.js';
import type { DecryptedCandidate } from '../src/consolidation.js';

// Helper: create a DecryptedCandidate
function makeCandidate(
  overrides: Partial<DecryptedCandidate> & { id: string },
): DecryptedCandidate {
  return {
    text: `fact ${overrides.id}`,
    embedding: null,
    importance: 5,
    decayScore: 1.0,
    createdAt: 1000,
    version: 1,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// getStoreDedupThreshold tests
// ---------------------------------------------------------------------------

describe('getStoreDedupThreshold', () => {
  const envKey = 'TOTALRECLAW_STORE_DEDUP_THRESHOLD';

  afterEach(() => {
    delete process.env[envKey];
  });

  it('returns 0.85 by default', () => {
    delete process.env[envKey];
    expect(getStoreDedupThreshold()).toBeCloseTo(0.85, 10);
  });

  it('respects custom threshold from env var', () => {
    process.env[envKey] = '0.75';
    expect(getStoreDedupThreshold()).toBeCloseTo(0.75, 10);
  });

  it('falls back to default for invalid env var', () => {
    process.env[envKey] = 'not-a-number';
    expect(getStoreDedupThreshold()).toBeCloseTo(0.85, 10);
  });
});

// ---------------------------------------------------------------------------
// getConsolidationThreshold tests
// ---------------------------------------------------------------------------

describe('getConsolidationThreshold', () => {
  const envKey = 'TOTALRECLAW_CONSOLIDATION_THRESHOLD';

  afterEach(() => {
    delete process.env[envKey];
  });

  it('returns 0.88 by default', () => {
    delete process.env[envKey];
    expect(getConsolidationThreshold()).toBeCloseTo(0.88, 10);
  });

  it('respects custom threshold from env var', () => {
    process.env[envKey] = '0.95';
    expect(getConsolidationThreshold()).toBeCloseTo(0.95, 10);
  });

  it('falls back to default for invalid env var', () => {
    process.env[envKey] = 'garbage';
    expect(getConsolidationThreshold()).toBeCloseTo(0.88, 10);
  });
});

// ---------------------------------------------------------------------------
// STORE_DEDUP_MAX_CANDIDATES constant
// ---------------------------------------------------------------------------

describe('STORE_DEDUP_MAX_CANDIDATES', () => {
  it('is 200', () => {
    expect(STORE_DEDUP_MAX_CANDIDATES).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// findNearDuplicate tests
// ---------------------------------------------------------------------------

describe('findNearDuplicate', () => {
  it('returns null for empty candidates', () => {
    expect(findNearDuplicate([1, 0, 0], [], 0.85)).toBeNull();
  });

  it('returns null when candidates have no embeddings', () => {
    const candidates = [
      makeCandidate({ id: 'a', embedding: null }),
      makeCandidate({ id: 'b', embedding: null }),
    ];
    expect(findNearDuplicate([1, 0, 0], candidates, 0.85)).toBeNull();
  });

  it('returns null when below threshold', () => {
    const candidates = [
      makeCandidate({ id: 'a', embedding: [0, 1, 0] }), // orthogonal, cosine = 0
    ];
    expect(findNearDuplicate([1, 0, 0], candidates, 0.85)).toBeNull();
  });

  it('returns match when above threshold', () => {
    const candidates = [
      makeCandidate({ id: 'a', embedding: [1, 0, 0] }), // cosine = 1.0
    ];
    const result = findNearDuplicate([1, 0, 0], candidates, 0.85);
    expect(result).not.toBeNull();
    expect(result!.existingFact.id).toBe('a');
    expect(result!.similarity).toBeCloseTo(1.0, 6);
  });

  it('returns highest similarity among multiple matches', () => {
    const candidates = [
      makeCandidate({ id: 'low', embedding: [0.86, Math.sqrt(1 - 0.86 * 0.86), 0] }),
      makeCandidate({ id: 'high', embedding: [0.99, Math.sqrt(1 - 0.99 * 0.99), 0] }),
      makeCandidate({ id: 'mid', embedding: [0.90, Math.sqrt(1 - 0.90 * 0.90), 0] }),
    ];
    const result = findNearDuplicate([1, 0, 0], candidates, 0.85);
    expect(result).not.toBeNull();
    expect(result!.existingFact.id).toBe('high');
  });

  it('matches parallel vectors (cosine = 1.0)', () => {
    const candidates = [
      makeCandidate({ id: 'parallel', embedding: [3, 6, 9] }), // parallel to [1, 2, 3]
    ];
    const result = findNearDuplicate([1, 2, 3], candidates, 0.85);
    expect(result).not.toBeNull();
    expect(result!.similarity).toBeCloseTo(1.0, 6);
  });

  it('returns null for orthogonal vectors', () => {
    const candidates = [
      makeCandidate({ id: 'ortho', embedding: [0, 1, 0] }),
    ];
    expect(findNearDuplicate([1, 0, 0], candidates, 0.85)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// shouldSupersede tests
// ---------------------------------------------------------------------------

describe('shouldSupersede', () => {
  it('returns supersede when new importance is higher', () => {
    const existing = makeCandidate({ id: 'old', importance: 5 });
    expect(shouldSupersede(8, existing)).toBe('supersede');
  });

  it('returns skip when new importance is lower', () => {
    const existing = makeCandidate({ id: 'old', importance: 8 });
    expect(shouldSupersede(3, existing)).toBe('skip');
  });

  it('returns supersede when importance is equal (newer wins)', () => {
    const existing = makeCandidate({ id: 'old', importance: 5 });
    expect(shouldSupersede(5, existing)).toBe('supersede');
  });
});

// ---------------------------------------------------------------------------
// clusterFacts tests
// ---------------------------------------------------------------------------

describe('clusterFacts', () => {
  it('returns no clusters for empty facts', () => {
    expect(clusterFacts([], 0.88)).toHaveLength(0);
  });

  it('returns no clusters for a single fact', () => {
    const facts = [makeCandidate({ id: 'a', embedding: [1, 0, 0] })];
    expect(clusterFacts(facts, 0.88)).toHaveLength(0);
  });

  it('clusters two identical embeddings', () => {
    const facts = [
      makeCandidate({ id: 'a', embedding: [1, 0, 0] }),
      makeCandidate({ id: 'b', embedding: [1, 0, 0] }),
    ];
    const clusters = clusterFacts(facts, 0.88);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].duplicates).toHaveLength(1);
  });

  it('returns no clusters for two dissimilar embeddings', () => {
    const facts = [
      makeCandidate({ id: 'a', embedding: [1, 0, 0] }),
      makeCandidate({ id: 'b', embedding: [0, 1, 0] }), // orthogonal
    ];
    expect(clusterFacts(facts, 0.88)).toHaveLength(0);
  });

  it('finds multiple clusters', () => {
    const facts = [
      makeCandidate({ id: 'a1', embedding: [1, 0, 0] }),
      makeCandidate({ id: 'a2', embedding: [1, 0, 0] }),
      makeCandidate({ id: 'b1', embedding: [0, 1, 0] }),
      makeCandidate({ id: 'b2', embedding: [0, 1, 0] }),
      makeCandidate({ id: 'c1', embedding: [0, 0, 1] }), // unique
    ];
    expect(clusterFacts(facts, 0.88)).toHaveLength(2);
  });

  it('skips facts without embeddings', () => {
    const facts = [
      makeCandidate({ id: 'a', embedding: [1, 0, 0] }),
      makeCandidate({ id: 'b', embedding: null }),
      makeCandidate({ id: 'c', embedding: [1, 0, 0] }),
    ];
    const clusters = clusterFacts(facts, 0.88);
    expect(clusters).toHaveLength(1);
    const allIds = clusters.flatMap(c => [c.representative.id, ...c.duplicates.map(d => d.id)]);
    expect(allIds).not.toContain('b');
  });

  it('picks representative with highest decayScore', () => {
    const facts = [
      makeCandidate({ id: 'low', embedding: [1, 0, 0], decayScore: 0.5 }),
      makeCandidate({ id: 'high', embedding: [1, 0, 0], decayScore: 0.9 }),
      makeCandidate({ id: 'mid', embedding: [1, 0, 0], decayScore: 0.7 }),
    ];
    const clusters = clusterFacts(facts, 0.88);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].representative.id).toBe('high');
    expect(clusters[0].duplicates).toHaveLength(2);
  });

  it('tiebreaks on recency when decayScore is equal', () => {
    const facts = [
      makeCandidate({ id: 'old', embedding: [1, 0, 0], decayScore: 1.0, createdAt: 1000 }),
      makeCandidate({ id: 'new', embedding: [1, 0, 0], decayScore: 1.0, createdAt: 2000 }),
    ];
    const clusters = clusterFacts(facts, 0.88);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].representative.id).toBe('new');
  });

  it('tiebreaks on text length when decayScore and createdAt are equal', () => {
    const facts = [
      makeCandidate({ id: 'short', text: 'abc', embedding: [1, 0, 0], decayScore: 1.0, createdAt: 1000 }),
      makeCandidate({ id: 'long', text: 'abcdefghij', embedding: [1, 0, 0], decayScore: 1.0, createdAt: 1000 }),
    ];
    const clusters = clusterFacts(facts, 0.88);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].representative.id).toBe('long');
  });
});
