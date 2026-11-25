import crypto from 'node:crypto';
import { RecentWrites } from '../src/subgraph/recent-writes';
import { createRewriteLookup, createForgetLookup } from '../src/subgraph/fact-lookup';
import type { FactLookup, SubgraphSearchFact } from '../src/subgraph/search';

const OWNER = '0xabc0000000000000000000000000000000000001';
const ID = crypto.randomUUID();
const F: SubgraphSearchFact = { id: ID, encryptedBlob: 'aa', encryptedEmbedding: null, decayScore: '1', timestamp: '1', isActive: true };

function clock() {
  let t = 0;
  const sleeps: number[] = [];
  return { sleeps, retry: { sleep: async (ms: number) => { sleeps.push(ms); t += ms; }, now: () => t } };
}
function rewrite(queue: FactLookup[], cache = new RecentWrites(), id = ID) {
  const c = clock();
  const calls: string[] = [];
  const lookupSubgraph = jest.fn(async (i: string) => { calls.push(i); return queue.shift() ?? ({ state: 'absent' } as FactLookup); });
  const fn = createRewriteLookup({ owner: OWNER, cache, lookupSubgraph, retry: c.retry, log: () => {} });
  return { fn, cache, lookupSubgraph, sleeps: c.sleeps, id };
}
const cacheOne = (cache: RecentWrites) => cache.recordWrite(OWNER, { id: ID, encryptedBlob: 'bb', decayScore: 1, timestamp: 'x' });

describe('createRewriteLookup', () => {
  test('subgraph active wins over the cache', async () => {
    const h = rewrite([{ state: 'active', fact: F }]);
    cacheOne(h.cache);
    expect(await h.fn(ID)).toBe(F);
  });
  test('absent + cache hit returns the cached fact without waiting', async () => {
    const h = rewrite([]);
    cacheOne(h.cache);
    expect((await h.fn(ID))?.encryptedBlob).toBe('bb');
    expect(h.lookupSubgraph).toHaveBeenCalledTimes(1);
    expect(h.sleeps).toEqual([]);
  });
  test('inactive evicts the cache entry', async () => {
    const h = rewrite([{ state: 'inactive' }]);
    cacheOne(h.cache);
    expect(await h.fn(ID)).toBeNull();
    expect(h.cache.get(OWNER, ID)).toBeNull();
    expect(h.cache.isTombstoned(OWNER, ID)).toBe(true);
  });
  test('tombstoned here never asks the subgraph', async () => {
    const h = rewrite([]);
    h.cache.recordTombstone(OWNER, ID);
    expect(await h.fn(ID)).toBeNull();
    expect(h.lookupSubgraph).not.toHaveBeenCalled();
  });
  test('re-polls until the fact appears', async () => {
    const h = rewrite([{ state: 'absent' }, { state: 'absent' }, { state: 'active', fact: F }]);
    expect(await h.fn(ID)).toBe(F);
    expect(h.lookupSubgraph).toHaveBeenCalledTimes(3);
    expect(h.sleeps).toEqual([2000, 2000]);
  });
  test('retry stops at the budget', async () => {
    const h = rewrite([]);
    expect(await h.fn(ID)).toBeNull();
    expect(h.lookupSubgraph).toHaveBeenCalledTimes(6);
    expect(h.sleeps.reduce((a, b) => a + b, 0)).toBe(10000);
  });
  test('non-UUID ids do not wait', async () => {
    const h = rewrite([]);
    expect(await h.fn('not-a-uuid')).toBeNull();
    expect(h.lookupSubgraph).toHaveBeenCalledTimes(1);
    expect(h.sleeps).toEqual([]);
  });
  test('another owner never sees this owner\'s record', async () => {
    const cache = new RecentWrites();
    cacheOne(cache);
    const c = clock();
    const fn = createRewriteLookup({ owner: '0x0000000000000000000000000000000000000002', cache, lookupSubgraph: async () => ({ state: 'absent' }), retry: { ...c.retry, totalMs: 0 }, log: () => {} });
    expect(await fn(ID)).toBeNull();
  });
});

describe('createForgetLookup', () => {
  function forget(queue: Array<{ isActive?: boolean } | null | Error>) {
    const c = clock();
    const cache = new RecentWrites();
    const fetchForForget = jest.fn(async () => {
      const n = queue.shift() ?? null;
      if (n instanceof Error) throw n;
      return n;
    });
    return { cache, fetchForForget, sleeps: c.sleeps, fn: createForgetLookup({ owner: OWNER, cache, fetchForForget, retry: c.retry, log: () => {} }) };
  }
  test('absent + cache hit is active and carries the blob', async () => {
    const h = forget([null]);
    cacheOne(h.cache);
    expect(await h.fn(ID)).toEqual({ isActive: true, encryptedBlob: 'bb' });
  });
  test('tombstoned is inactive without a lookup', async () => {
    const h = forget([]);
    h.cache.recordTombstone(OWNER, ID);
    expect(await h.fn(ID)).toEqual({ isActive: false });
    expect(h.fetchForForget).not.toHaveBeenCalled();
  });
  test('forget lookup propagates errors without retry', async () => {
    const h = forget([new Error('fact lookup failed: HTTP 502')]);
    await expect(h.fn(ID)).rejects.toThrow('HTTP 502');
    expect(h.fetchForForget).toHaveBeenCalledTimes(1);
    expect(h.sleeps).toEqual([]);
  });
  test('re-polls on null', async () => {
    const h = forget([null, { isActive: true }]);
    expect(await h.fn(ID)).toEqual({ isActive: true });
  });
});
