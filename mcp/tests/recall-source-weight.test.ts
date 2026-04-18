/**
 * Retrieval v2 Tier 1 — recall source-weighted ordering test.
 *
 * Mocks the `@totalreclaw/client` client and verifies that `handleRecall`
 * applies the core's source-weight multiplier to produce user-favoring
 * ordering when two candidates tie on base score.
 */

export {};

const mockRecall = jest.fn();

jest.mock('@totalreclaw/client', () => ({
  TotalReclaw: jest.fn(),
}));

function createMockClient() {
  return {
    recall: mockRecall,
    isReady: jest.fn().mockReturnValue(true),
  };
}

import { handleRecall } from '../src/tools/recall';

function makeV1Fact(id: string, text: string, source: string, baseScore: number) {
  // Produce a blob text that the recall parser will detect as v1.
  const v1BlobText = JSON.stringify({
    id,
    text,
    type: 'claim',
    source,
    created_at: '2026-01-01T00:00:00Z',
    schema_version: '1.0',
    importance: 7,
  });
  return {
    fact: {
      id,
      text: v1BlobText, // recall.ts reads the text field as the blob
      embedding: [1, 0, 0],
      metadata: { importance: 0.7, tags: [] },
      decayScore: 0.7,
      createdAt: new Date('2026-01-01T00:00:00Z'),
    },
    score: baseScore,
    vectorScore: baseScore,
    textScore: baseScore,
    decayAdjustedScore: 0.7,
  };
}

describe('handleRecall — Retrieval v2 Tier 1 source weighting', () => {
  beforeEach(() => {
    mockRecall.mockReset();
  });

  test('user source beats assistant source when base scores are equal', async () => {
    // Two candidates with identical base score, different sources.
    const assistantFact = makeV1Fact('assistant-fact-id', 'Sheraton Grande Sukhumvit', 'assistant', 0.8);
    const userFact = makeV1Fact('user-fact-id', 'I want to go to Bangkok', 'user', 0.8);
    mockRecall.mockResolvedValueOnce([assistantFact, userFact]);

    const client = createMockClient() as any;
    const out = await handleRecall(client, { query: 'Bangkok trip' });
    const body = JSON.parse(out.content[0].text);
    expect(body.memories).toHaveLength(2);

    // User claim must rank first because sourceWeight(user)=1.0, sourceWeight(assistant)=0.55.
    expect(body.memories[0].fact_id).toBe('user-fact-id');
    expect(body.memories[0].source).toBe('user');
    expect(body.memories[1].fact_id).toBe('assistant-fact-id');
    expect(body.memories[1].source).toBe('assistant');

    // Weighted scores reflect the multiplier.
    expect(body.memories[0].score).toBeGreaterThan(body.memories[1].score);
    expect(body.memories[0].source_weight).toBe(1.0);
    expect(body.memories[1].source_weight).toBe(0.55);
  });

  test('unspecified / missing source falls back to legacy fallback weight', async () => {
    const noSourceFact = {
      fact: {
        id: 'no-source',
        // plain-text blob (no v1 schema)
        text: 'legacy text',
        embedding: [1, 0, 0],
        metadata: { importance: 0.6, tags: [] },
        decayScore: 0.6,
        createdAt: new Date(),
      },
      score: 1.0,
      vectorScore: 1.0,
      textScore: 1.0,
      decayAdjustedScore: 0.6,
    };
    const userFact = makeV1Fact('user-id', 'user-authored', 'user', 1.0);
    mockRecall.mockResolvedValueOnce([noSourceFact, userFact]);

    const client = createMockClient() as any;
    const out = await handleRecall(client, { query: 'test' });
    const body = JSON.parse(out.content[0].text);
    expect(body.memories[0].fact_id).toBe('user-id'); // user > fallback
    expect(body.memories[0].source_weight).toBe(1.0);
    expect(body.memories[1].fact_id).toBe('no-source');
    // Fallback weight applied (legacy; should be the core constant).
    expect(body.memories[1].source_weight).toBeLessThan(1.0);
  });

  test('all-assistant candidates still return top-k ordered by base score', async () => {
    const a = makeV1Fact('a', 'alpha', 'assistant', 0.9);
    const b = makeV1Fact('b', 'bravo', 'assistant', 0.5);
    mockRecall.mockResolvedValueOnce([b, a]);

    const client = createMockClient() as any;
    const out = await handleRecall(client, { query: 'q' });
    const body = JSON.parse(out.content[0].text);
    // Higher-scored assistant fact ranks first even with the same weight.
    expect(body.memories[0].fact_id).toBe('a');
    expect(body.memories[1].fact_id).toBe('b');
  });

  test('surfaces v1 type + source + scope in output', async () => {
    const v1WithScope = {
      fact: {
        id: 'scoped',
        text: JSON.stringify({
          id: 'scoped',
          text: 'work fact',
          type: 'directive',
          source: 'user',
          scope: 'work',
          created_at: '2026-01-01T00:00:00Z',
          schema_version: '1.0',
        }),
        embedding: [1, 0, 0],
        metadata: { importance: 0.5, tags: [] },
        decayScore: 0.5,
        createdAt: new Date(),
      },
      score: 1.0,
      vectorScore: 1.0,
      textScore: 1.0,
      decayAdjustedScore: 0.5,
    };
    mockRecall.mockResolvedValueOnce([v1WithScope]);

    const client = createMockClient() as any;
    const out = await handleRecall(client, { query: 'work' });
    const body = JSON.parse(out.content[0].text);
    expect(body.memories[0].type).toBe('directive');
    expect(body.memories[0].source).toBe('user');
    expect(body.memories[0].scope).toBe('work');
  });
});
