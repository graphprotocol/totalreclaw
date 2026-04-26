/**
 * Cross-runtime reranker parity check (MCP side).
 *
 * Mirror of `skill/plugin/reranker-cross-runtime-parity.test.ts` and
 * `python/tests/test_reranker_cross_runtime_parity.py`. All three runtimes
 * delegate ranking to core::reranker, so the SAME fixture + query MUST
 * produce the identical top-8 ordering. If this test diverges, one of the
 * runtimes is no longer routing through core.
 *
 * Run with: npx jest mcp/tests/reranker-cross-runtime-parity.test.ts
 */

import { rerank, type RerankerCandidate } from '../src/subgraph/reranker.js';

// Mirror of the plugin / Hermes fixture. Keep these in sync with:
//   - skill/plugin/reranker-cross-runtime-parity.test.ts
//   - python/tests/test_reranker_cross_runtime_parity.py
const FIXTURE: RerankerCandidate[] = [
  { id: 'g00', text: 'User set personal best 25:50 in charity 5K run', embedding: [0.9, 0.1, 0.05], source: 'user' },
  { id: 'g01', text: 'User completed half marathon in 1:55', embedding: [0.85, 0.15, 0.02], source: 'user' },
  { id: 'g02', text: 'Assistant suggested running shoes', embedding: [0.7, 0.3, 0.1], source: 'assistant' },
  { id: 'g03', text: 'User trains five days per week', embedding: [0.6, 0.2, 0.2], source: 'user' },
  { id: 'g04', text: 'Weather forecast says sunny tomorrow', embedding: [0.0, 0.1, 0.9], source: 'user' },
  { id: 'g05', text: 'User prefers PostgreSQL for analytics', embedding: [0.1, 0.9, 0.0], source: 'user' },
  { id: 'g06', text: 'Bob enjoys hiking on weekends', embedding: [0.3, 0.0, 0.6], source: 'user-inferred' },
  { id: 'g07', text: 'User had pizza for dinner', embedding: [0.0, 0.0, 1.0], source: 'user' },
  { id: 'g08', text: 'Marathon training tips and strategy', embedding: [0.5, 0.4, 0.1], source: 'external' },
  { id: 'g09', text: 'User runs in Central Park weekly', embedding: [0.7, 0.2, 0.1], source: 'user' },
  { id: 'g10', text: 'Charity 5K event raised funds for shelter', embedding: [0.5, 0.3, 0.2], source: 'derived' },
  { id: 'g11', text: 'User logged 25 minutes 50 seconds time', embedding: [0.95, 0.05, 0.0], source: 'user' },
  { id: 'g12', text: 'Project deadline next Friday', embedding: [0.0, 0.5, 0.5], source: 'user' },
  { id: 'g13', text: 'Coffee preference is dark roast', embedding: [0.0, 0.7, 0.3], source: 'user' },
  { id: 'g14', text: 'User won 5K race in 25 minutes 50', embedding: [0.92, 0.08, 0.0], source: 'user' },
  { id: 'g15', text: 'Assistant noted user enjoys running', embedding: [0.55, 0.4, 0.05], source: 'assistant' },
  { id: 'g16', text: 'Total kilometers run last month: 120', embedding: [0.6, 0.3, 0.1], source: 'derived' },
  { id: 'g17', text: 'Pace target is 5 minutes per kilometer', embedding: [0.65, 0.25, 0.1], source: 'user' },
  { id: 'g18', text: 'User dislikes interval training', embedding: [0.5, 0.4, 0.1], source: 'user' },
  { id: 'g19', text: 'Running playlist includes electronic music', embedding: [0.4, 0.3, 0.3], source: 'user' },
  { id: 'g20', text: 'Charity event was held on Saturday', embedding: [0.3, 0.5, 0.2], source: 'external' },
  { id: 'g21', text: 'User prefers morning runs over evening', embedding: [0.7, 0.2, 0.1], source: 'user-inferred' },
  { id: 'g22', text: 'Personal record was set in May', embedding: [0.85, 0.1, 0.05], source: 'user' },
  { id: 'g23', text: 'User uses Garmin watch for tracking', embedding: [0.5, 0.4, 0.1], source: 'user' },
  { id: 'g24', text: 'Distance was 5 kilometers exact', embedding: [0.7, 0.2, 0.1], source: 'user-inferred' },
  { id: 'g25', text: 'Random unrelated note about the weather', embedding: [0.05, 0.05, 0.9], source: 'user' },
  { id: 'g26', text: 'User trained six weeks for the race', embedding: [0.65, 0.25, 0.1], source: 'user' },
  { id: 'g27', text: 'Recovery routine includes stretching', embedding: [0.3, 0.4, 0.3], source: 'user' },
  { id: 'g28', text: 'Goal is sub-25-minute 5K next year', embedding: [0.8, 0.15, 0.05], source: 'user' },
  { id: 'g29', text: 'Assistant recommends hydration tips', embedding: [0.4, 0.3, 0.3], source: 'assistant' },
];

const QUERY = "What was my personal best time in the charity 5K run?";
const QUERY_EMBEDDING = [0.85, 0.1, 0.05];

// Expected top-8 ordering captured from the plugin run on rc.22 hoist.
// If this drifts, EITHER the plugin OR MCP is no longer routing through
// core::reranker.
const EXPECTED_TOP8 = ['g22', 'g00', 'g14', 'g11', 'g01', 'g28', 'g09', 'g26'];

describe('reranker cross-runtime parity', () => {
  it('top-8 ordering matches plugin / Hermes', () => {
    const results = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 8, undefined, true);
    const actual = results.map((r) => r.id);
    expect(actual).toEqual(EXPECTED_TOP8);
  });

  it('determinism: two invocations produce identical scores', () => {
    const a = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 16, undefined, true);
    const b = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 16, undefined, true);
    expect(a.map((r) => r.id)).toEqual(b.map((r) => r.id));
    for (let i = 0; i < a.length; i++) {
      expect(Math.abs(a[i].rrfScore - b[i].rrfScore)).toBeLessThan(1e-12);
    }
  });

  it('top-K=8 is the prefix of top-K=16', () => {
    const k8 = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 8, undefined, true);
    const k16 = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 16, undefined, true);
    expect(k8.length).toBe(8);
    expect(k16.length).toBe(16);
    expect(k8.map((r) => r.id)).toEqual(k16.slice(0, 8).map((r) => r.id));
  });

  it('source weighting keeps assistant off top-1', () => {
    const results = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 8, undefined, true);
    expect(results[0].source).not.toBe('assistant');
  });

  it('source-weight toggle changes the score distribution', () => {
    const swOn = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 16, undefined, true);
    const swOff = rerank(QUERY, QUERY_EMBEDDING, FIXTURE, 16, undefined, false);
    const sumOn = swOn.reduce((s, r) => s + r.rrfScore, 0);
    const sumOff = swOff.reduce((s, r) => s + r.rrfScore, 0);
    expect(sumOn).not.toBe(sumOff);
  });
});
