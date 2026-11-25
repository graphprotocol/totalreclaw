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
import { buildSubgraphHeaders } from '../src/subgraph/headers';

const RELAY = 'https://relay.test';
const SA = '0xAbC0000000000000000000000000000000000001';
const AUTH = 'deadbeef'.repeat(8);

interface Seen { url: string; headers: Record<string, string> }
let seen: Seen[] = [];
const realFetch = globalThis.fetch;
const realEnv = process.env.TOTALRECLAW_SUBGRAPH_URL;

beforeEach(() => {
  seen = [];
  delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  globalThis.fetch = (async (url: any, init: any) => {
    seen.push({ url: String(url), headers: { ...(init?.headers ?? {}) } });
    return new Response(JSON.stringify({
      data: { blindIndexes: [], facts: [], fact: null, globalStates: [] },
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
    // Nothing but the address in the wallet header; no key material elsewhere.
    for (const [k, v] of Object.entries(s.headers)) {
      if (k !== 'Authorization') expect(v).not.toContain(AUTH);
    }
  }
}

describe('X-Wallet-Address on every subgraph read (#513)', () => {
  it('searchSubgraph (blind-index + payload fetch)', async () => {
    await searchSubgraph(SA, ['t1', 't2'], 10, RELAY, AUTH);
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
  it('buildSubgraphHeaders omits the header only when no address is given', () => {
    expect(buildSubgraphHeaders({ walletAddress: SA })['X-Wallet-Address']).toBe(SA);
    expect('X-Wallet-Address' in buildSubgraphHeaders({})).toBe(false);
  });
});
