/**
 * PRD-04 DEP-5 Part B — pin awareness in the MCP server: recall rerank boost
 * and `pinned`, forget `was_pinned`, and store-time dedup never superseding a
 * pinned fact.
 *
 * Run with: npx jest tests/pin-awareness.test.ts --no-cache
 */

export {};

import { rerank, defaultPinBoost } from '../src/subgraph/reranker';
import { blobIsPinned, buildV1ClaimBlob } from '../src/claims-helper';
import { dedupAction, type DecryptedCandidate } from '../src/consolidation';
import { factWasPinned } from '../src/tools/forget';

const EMB = [1, 0, 0, 0];

function candidate(id: string, pinned: boolean) {
  return { id, text: 'The user lives in Lisbon', embedding: EMB, createdAt: 1_700_000_000, pinned };
}

describe('recall pin boost', () => {
  test('core exposes a default boost above 1', () => {
    expect(defaultPinBoost()).toBeGreaterThan(1);
  });

  test('a pinned fact ranks above an equal unpinned one (listed second)', () => {
    const out = rerank('where does the user live', EMB, [candidate('plain', false), candidate('pinned', true)], 2, undefined, false, defaultPinBoost());
    expect(out.map((r) => r.id)).toEqual(['pinned', 'plain']);
    expect(out[0].rrfScore).toBeGreaterThan(out[1].rrfScore);
    expect(out[0].pinned).toBe(true);
    expect(out[1].pinned).toBe(false);
  });

  test('without the boost the equal pair scores the same', () => {
    const out = rerank('where does the user live', EMB, [candidate('plain', false), candidate('pinned', true)], 2);
    expect(out[0].rrfScore).toBeCloseTo(out[1].rrfScore, 10);
  });

  test('pinning does not lift a fact that does not match the query', () => {
    const off = { id: 'off', text: 'Prefers espresso over filter coffee', embedding: [0, 1, 0, 0], createdAt: 1_700_000_000, pinned: true };
    const out = rerank('where does the user live', EMB, [candidate('hit', false), off], 2, undefined, false, defaultPinBoost());
    expect(out[0].id).toBe('hit');
  });
});

describe('blobIsPinned', () => {
  test('v1.1 pin_status', () => {
    const pinned = buildV1ClaimBlob({ text: 'x', type: 'claim', source: 'user', scope: 'unspecified', importance: 5, pinStatus: 'pinned' });
    const unpinned = buildV1ClaimBlob({ text: 'x', type: 'claim', source: 'user', scope: 'unspecified', importance: 5, pinStatus: 'unpinned' });
    const plain = buildV1ClaimBlob({ text: 'x', type: 'claim', source: 'user', scope: 'unspecified', importance: 5 });
    expect(blobIsPinned(pinned)).toBe(true);
    expect(blobIsPinned(unpinned)).toBe(false);
    expect(blobIsPinned(plain)).toBe(false);
  });

  test('v0 short-key sentinel, and non-JSON', () => {
    expect(blobIsPinned(JSON.stringify({ t: 'x', c: 'fact', st: 'p' }))).toBe(true);
    expect(blobIsPinned(JSON.stringify({ t: 'x', c: 'fact', st: 'a' }))).toBe(false);
    expect(blobIsPinned('not json')).toBe(false);
    expect(blobIsPinned('null')).toBe(false);
  });
});

describe('store-time dedup never tombstones a pinned fact', () => {
  const base: DecryptedCandidate = {
    id: 'f1', text: 't', embedding: EMB, importance: 5, decayScore: 0.5, createdAt: 0, version: 1,
  };

  test('unpinned: explicit supersedes; batch follows shouldSupersede', () => {
    expect(dedupAction(base, 1, true)).toBe('supersede');
    expect(dedupAction({ ...base, importance: 9 }, 1, false)).toBe('skip');
    expect(dedupAction({ ...base, importance: 1 }, 9, false)).toBe('supersede');
  });

  test('pinned: skipped for explicit and automatic writes alike', () => {
    const pinned = { ...base, pinned: true };
    expect(dedupAction(pinned, 10, true)).toBe('skip_pinned');
    expect(dedupAction(pinned, 10, false)).toBe('skip_pinned');
  });
});

describe('forget was_pinned', () => {
  const hex = (s: string) => Buffer.from(s).toString('hex');
  const decryptStub = (h: string) => Buffer.from(h, 'hex').toString();

  test('true for a pinned blob (with or without 0x), false otherwise', () => {
    const pinnedBlob = JSON.stringify({ text: 'x', type: 'claim', pin_status: 'pinned' });
    expect(factWasPinned({ encryptedBlob: hex(pinnedBlob) }, decryptStub)).toBe(true);
    expect(factWasPinned({ encryptedBlob: '0x' + hex(pinnedBlob) }, decryptStub)).toBe(true);
    expect(factWasPinned({ encryptedBlob: hex(JSON.stringify({ text: 'x', type: 'claim' })) }, decryptStub)).toBe(false);
  });

  test('false when the blob is missing or cannot be decrypted', () => {
    expect(factWasPinned({}, decryptStub)).toBe(false);
    expect(factWasPinned(null, decryptStub)).toBe(false);
    expect(factWasPinned({ encryptedBlob: 'zz' }, () => { throw new Error('bad key'); })).toBe(false);
  });
});

describe('managed batch keeps block notices at their input index', () => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { mergeBlockedResults } = require('../src/blocked-results') as typeof import('../src/blocked-results');

  test('results[i] maps to facts[i] for blocked facts', () => {
    const stored = [{ id: 'a' }, { id: 'c' }];
    const out = mergeBlockedResults(stored, [{ index: 1, result: { blocked: 'sensitive' } }]);
    expect(out).toEqual([{ id: 'a' }, { blocked: 'sensitive' }, { id: 'c' }]);
  });

  test('no blocked facts: unchanged', () => {
    const stored = [{ id: 'a' }];
    expect(mergeBlockedResults(stored, [])).toBe(stored);
  });

  test('blocked first and last', () => {
    const out = mergeBlockedResults(['b'], [
      { index: 0, result: { n: 0 } },
      { index: 2, result: { n: 2 } },
    ]);
    expect(out).toEqual([{ n: 0 }, 'b', { n: 2 }]);
  });
});
