/**
 * subgraph-wallet-header.test.ts — internal #513 step 1.
 *
 * The relay routes /v1/subgraph reads by tier from `X-Wallet-Address` and
 * fails open to the Free subgraph without it. Every MCP subgraph request
 * must send the header (Smart Account address only; the auth key stays in
 * Authorization).
 */

import {
  searchSubgraph,
  searchSubgraphBroadened,
  getSubgraphFactCount,
  getOwnerFactCount,
  lookupFactById,
  fetchFactById,
  fetchFactForForget,
} from '../src/subgraph/search';
import { confirmIndexed } from '../src/subgraph/confirm-indexed';
import { buildSubgraphHeaders, INDEX_SCHEME_HEADER, INDEX_SCHEME_VALUE } from '../src/subgraph/headers';

const RELAY = 'https://relay.test';
const SA = '0xAbC0000000000000000000000000000000000001';
const AUTH = 'deadbeef'.repeat(8);

interface Seen { url: string; headers: Record<string, string>; body?: string }
let seen: Seen[] = [];
const realFetch = globalThis.fetch;
const realEnv = process.env.TOTALRECLAW_SUBGRAPH_URL;

beforeEach(() => {
  seen = [];
  delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  globalThis.fetch = (async (url: any, init: any) => {
    seen.push({ url: String(url), headers: { ...(init?.headers ?? {}) }, body: init?.body });
    const q = String(JSON.parse(init?.body ?? '{}').query ?? '');
    const isSearch = q.includes('SearchByBlindIndex');
    const isByIds = q.includes('id_in');
    return new Response(JSON.stringify({
      data: {
        blindIndexes: isSearch ? [{ id: 'bi-1', fact: { id: 'f1' } }] : [],
        facts: isByIds ? [{ id: 'f1', isActive: true }] : [],
        fact: null,
        globalStates: [],
      },
    }), { status: 200 });
  }) as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realEnv === undefined) delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  else process.env.TOTALRECLAW_SUBGRAPH_URL = realEnv;
});

function expectAllHaveWallet() {
  expect(seen.length).toBeGreaterThan(0);
  for (const s of seen) {
    expect(s.headers['X-Wallet-Address']).toBe(SA);
    expect(s.headers['Authorization']).toBe(`Bearer ${AUTH}`);
    // internal#573: every subgraph request carries the keyed-scheme header.
    expect(s.headers['X-TotalReclaw-Index-Scheme']).toBe('1');
    // Nothing but the address in the wallet header; no key material elsewhere.
    for (const [k, v] of Object.entries(s.headers)) {
      if (k !== 'Authorization') expect(v).not.toContain(AUTH);
    }
  }
}

describe('X-Wallet-Address on every subgraph read (#513)', () => {
  it('searchSubgraph (blind-index + payload fetch)', async () => {
    process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE = '1'; // saturated page -> paginate request
    try {
      await searchSubgraph(SA, ['t1'], 10, RELAY, AUTH);
    } finally {
      delete process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE;
    }
    const bodies = seen.map((s) => String(s.body));
    expect(bodies.some((b) => b.includes('SearchByBlindIndex'))).toBe(true);
    expect(bodies.some((b) => b.includes('id_gt'))).toBe(true); // paginate
    expect(bodies.some((b) => b.includes('id_in'))).toBe(true); // payload fetch
    expectAllHaveWallet();
  });
  it('searchSubgraphBroadened', async () => {
    await searchSubgraphBroadened(SA, 10, RELAY, AUTH);
    expectAllHaveWallet();
  });
  it('getSubgraphFactCount', async () => {
    await getSubgraphFactCount(SA, RELAY, AUTH);
    expectAllHaveWallet();
  });
  it('getOwnerFactCount', async () => {
    await getOwnerFactCount(SA, RELAY, AUTH);
    expectAllHaveWallet();
  });
  it('lookupFactById and fetchFactById', async () => {
    await lookupFactById(SA, 'id-1', RELAY, AUTH);
    await fetchFactById(SA, 'id-1', RELAY, AUTH);
    expect(seen.length).toBe(2);
    expectAllHaveWallet();
  });
  it('fetchFactForForget (separate fetch path)', async () => {
    await fetchFactForForget(SA, 'id-1', RELAY, AUTH);
    expectAllHaveWallet();
  });
  it('searchSubgraph with a self-hosted TOTALRECLAW_SUBGRAPH_URL override sends it too (shared path)', async () => {
    process.env.TOTALRECLAW_SUBGRAPH_URL = 'https://own.test/graph';
    await searchSubgraph(SA, ['t1'], 10, RELAY, AUTH);
    expect(seen.every((s) => s.url === 'https://own.test/graph')).toBe(true);
    expectAllHaveWallet();
  });
  it('confirmIndexed default poster', async () => {
    globalThis.fetch = (async (url: any, init: any) => {
      seen.push({ url: String(url), headers: { ...(init?.headers ?? {}) } });
      return new Response('{"data":{"fact":{"isActive":true}}}', { status: 200 });
    }) as typeof fetch;
    await confirmIndexed('id-1', {
      subgraphUrl: `${RELAY}/v1/subgraph`,
      authKeyHex: AUTH,
      walletAddress: SA,
      timeoutMs: 50,
      pollIntervalMs: 10,
    });
    expectAllHaveWallet();
  });
  it('the scheme header name and value are defined once', () => {
    expect(INDEX_SCHEME_HEADER).toBe('X-TotalReclaw-Index-Scheme');
    expect(INDEX_SCHEME_VALUE).toBe('1');
    expect(buildSubgraphHeaders({})[INDEX_SCHEME_HEADER]).toBe('1');
  });
  it('buildSubgraphHeaders omits the header only when no address is given', () => {
    expect(buildSubgraphHeaders({ walletAddress: SA })['X-Wallet-Address']).toBe(SA);
    expect('X-Wallet-Address' in buildSubgraphHeaders({})).toBe(false);
  });
});
