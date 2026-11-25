import { lookupFactById, fetchFactById } from '../src/subgraph/search';

const OWNER = '0xabc0000000000000000000000000000000000001';
const realFetch = global.fetch;

function stub(body: unknown, ok = true, status = 200) {
  global.fetch = jest.fn(async () => ({
    ok, status, json: async () => body, text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
const fact = (extra: Record<string, unknown> = {}) => ({
  id: 'f1', owner: OWNER, encryptedBlob: 'aa', encryptedEmbedding: null,
  decayScore: '1', timestamp: '1', isActive: true, ...extra,
});

beforeEach(() => { delete process.env.TOTALRECLAW_SUBGRAPH_URL; });
afterEach(() => { global.fetch = realFetch; });

describe('lookupFactById', () => {
  const call = () => lookupFactById(OWNER, 'f1', 'https://relay.test');
  test('null fact is absent', async () => {
    stub({ data: { fact: null } });
    expect(await call()).toEqual({ state: 'absent' });
  });
  test('inactive fact of the owner is inactive', async () => {
    stub({ data: { fact: fact({ isActive: false }) } });
    expect(await call()).toEqual({ state: 'inactive' });
  });
  test('another owner is absent', async () => {
    stub({ data: { fact: fact({ owner: '0x0000000000000000000000000000000000000009' }) } });
    expect(await call()).toEqual({ state: 'absent' });
  });
  test('active fact, owner compared case-insensitively', async () => {
    stub({ data: { fact: fact({ owner: OWNER.toUpperCase().replace('0X', '0x') }) } });
    const r = await call();
    expect(r.state).toBe('active');
    if (r.state === 'active') expect(r.fact.id).toBe('f1');
  });
  test('HTTP 500 is absent', async () => {
    stub({}, false, 500);
    expect(await call()).toEqual({ state: 'absent' });
  });
  test('fetchFactById keeps its behaviour', async () => {
    stub({ data: { fact: fact() } });
    expect((await fetchFactById(OWNER, 'f1', 'https://relay.test'))?.id).toBe('f1');
    stub({ data: { fact: fact({ isActive: false }) } });
    expect(await fetchFactById(OWNER, 'f1', 'https://relay.test')).toBeNull();
  });
});
