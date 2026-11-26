/**
 * internal#573 (M4): the bundler / paymaster transport carries the keyed-scheme
 * header. `authTransport` serves both the bundler transport and the Pimlico
 * paymaster client, so one header set covers every `pm_*`, gas estimate, send,
 * gas price and receipt call.
 *
 * `viem`'s `http` is spied; the submit functions are driven until they touch
 * the (never-used) transport, then the captured `fetchOptions.headers` are
 * checked. No network is reached.
 */

const httpCalls: Array<{ url?: string; opts?: { fetchOptions?: { headers?: Record<string, string> } } }> = [];

jest.mock('viem', () => {
  const actual = jest.requireActual('viem');
  return {
    ...actual,
    http: (url?: string, opts?: unknown) => {
      httpCalls.push({ url, opts: opts as never });
      // A transport that fails on first use: the test never reaches a network.
      return () => ({ request: async () => { throw new Error('transport-not-used'); }, config: {}, value: {} });
    },
  };
});

import { buildBundlerHeaders, INDEX_SCHEME_HEADER, INDEX_SCHEME_VALUE } from '../src/subgraph/headers';
import { submitFactOnChain, submitFactBatchOnChain, type SubgraphStoreConfig } from '../src/subgraph/store';
import { fx } from './helpers/keyed-fixture';

const AUTH = 'cd'.repeat(32);
const SA = '0xAbC0000000000000000000000000000000000001';

function config(): SubgraphStoreConfig {
  return {
    relayUrl: 'https://relay.test',
    mnemonic: fx.meta.mnemonic,
    cachePath: '/nonexistent',
    chainId: 100,
    dataEdgeAddress: '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca',
    entryPointAddress: '0x0000000071727De22E5E9d8BAf0edAc6f37da032',
    authKeyHex: AUTH,
    walletAddress: SA,
  };
}

beforeEach(() => { httpCalls.length = 0; });

describe('buildBundlerHeaders', () => {
  test('has the scheme header, the client id, the wallet and the auth key', () => {
    const h = buildBundlerHeaders({ walletAddress: SA, authKeyHex: AUTH });
    expect(h[INDEX_SCHEME_HEADER]).toBe(INDEX_SCHEME_VALUE);
    expect(h['X-TotalReclaw-Client']).toBeTruthy();
    expect(h['X-Wallet-Address']).toBe(SA);
    expect(h['Authorization']).toBe(`Bearer ${AUTH}`);
  });
  test('the scheme header is present with no options at all', () => {
    expect(buildBundlerHeaders()[INDEX_SCHEME_HEADER]).toBe('1');
  });
});

describe('submit functions pass the scheme header to the bundler transport', () => {
  const bundlerCalls = () => httpCalls.filter((c) => c.url?.endsWith('/v1/bundler') && c.opts?.fetchOptions);

  test('submitFactOnChain', async () => {
    await submitFactOnChain(Buffer.from('00', 'hex'), config()).catch(() => {});
    expect(bundlerCalls().length).toBeGreaterThan(0);
    for (const c of bundlerCalls()) {
      expect(c.opts!.fetchOptions!.headers![INDEX_SCHEME_HEADER]).toBe('1');
      expect(c.opts!.fetchOptions!.headers!['Authorization']).toBe(`Bearer ${AUTH}`);
    }
  });

  test('submitFactBatchOnChain (two payloads)', async () => {
    await submitFactBatchOnChain([Buffer.from('00', 'hex'), Buffer.from('01', 'hex')], config()).catch(() => {});
    expect(bundlerCalls().length).toBeGreaterThan(0);
    for (const c of bundlerCalls()) {
      expect(c.opts!.fetchOptions!.headers![INDEX_SCHEME_HEADER]).toBe('1');
    }
  });
});
