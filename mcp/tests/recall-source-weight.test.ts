/**
 * Recall ordering test — source-weighting DISABLED (alignment 2026-06-08).
 *
 * Mocks the `@totalreclaw/client` client and verifies that `handleRecall`
 * ranks purely by BASE score and does NOT multiply by the provenance
 * source-weight (the 2026-06-08 agent-experienced benchmark showed source
 * weighting tie-or-worse on the shipped path). `source` / `source_weight`
 * are still surfaced in the output for observability, but must not affect order.
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

describe('handleRecall — source-weighting disabled (ranks by base score)', () => {
  beforeEach(() => {
    mockRecall.mockReset();
  });

  test('source weight does NOT override base score (assistant w/ higher base beats user)', async () => {
    // Discriminating case: under the OLD Tier-1 weighting, user(0.8 × 1.0)=0.80
    // would beat assistant(0.85 × 0.85)=0.7225 → user first. With weighting OFF,
    // ranking is by base score → the higher-base assistant fact ranks first.
    const assistantFact = makeV1Fact('assistant-fact-id', 'Sheraton Grande Sukhumvit', 'assistant', 0.85);
    const userFact = makeV1Fact('user-fact-id', 'I want to go to Bangkok', 'user', 0.8);
    mockRecall.mockResolvedValueOnce([userFact, assistantFact]);

    const client = createMockClient() as any;
    const out = await handleRecall(client, { query: 'Bangkok trip' });
    const body = JSON.parse(out.content[0].text);
    expect(body.memories).toHaveLength(2);

    // Higher base score wins regardless of provenance.
    expect(body.memories[0].fact_id).toBe('assistant-fact-id');
    expect(body.memories[1].fact_id).toBe('user-fact-id');

    // score == base_score (no source-weight multiplication).
    expect(body.memories[0].score).toBeCloseTo(0.85);
    expect(body.memories[0].score).toBe(body.memories[0].base_score);
    expect(body.memories[1].score).toBeCloseTo(0.8);
    // source_weight still surfaced for observability, but does not affect order.
    expect(body.memories[0].source_weight).toBeDefined();
  });

  test('source weight is informational only — equal base scores keep their base score', async () => {
    // Two equal-base candidates, different sources. Neither is reweighted:
    // both keep score == base_score; user is NOT promoted over assistant.
    const noSourceFact = {
      fact: {
        id: 'no-source',
        text: 'legacy text', // plain-text blob (no v1 schema)
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
    // Both keep base score 1.0 — no provenance reweighting promotes one over the other.
    for (const m of body.memories) {
      expect(m.score).toBe(m.base_score);
      expect(m.score).toBeCloseTo(1.0);
    }
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
