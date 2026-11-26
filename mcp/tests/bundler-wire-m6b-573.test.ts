/**
 * internal#573 (M6b): viem-level capture. Drive `submitFactBatchOnChain` with
 * `fetch` mocked until the first JSON-RPC body that carries `callData`
 * (`pm_getPaymasterStubData` or `eth_estimateUserOperationGas`), then scan that
 * body and check the scheme header. Everything before it (the Smart Account
 * address `eth_call`, nonce, code check, gas price) gets canned answers.
 */

import { submitFactBatchOnChain, encodeFactProtobufKeyed, encodeTombstoneProtobufKeyed, type SubgraphStoreConfig } from '../src/subgraph/store';
import { fx, makeFixtureTagger } from './helpers/keyed-fixture';

const AUTH = 'cd'.repeat(32);
const SA = '0xAbC0000000000000000000000000000000000001';
const ADDR_WORD = '0x' + '00'.repeat(12) + '11'.repeat(20);

// viem lowercases header names, so captured headers are keyed lowercase.
interface Req { url: string; headers: Record<string, string>; body: { method?: string; params?: unknown[]; id?: number } }
const seen: Req[] = [];
const realFetch = globalThis.fetch;

class Stop extends Error {}

function config(): SubgraphStoreConfig {
  return {
    relayUrl: 'https://relay.test', mnemonic: fx.meta.mnemonic, cachePath: '/nonexistent', chainId: 100,
    dataEdgeAddress: '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca',
    entryPointAddress: '0x0000000071727De22E5E9d8BAf0edAc6f37da032',
    authKeyHex: AUTH, walletAddress: SA,
  };
}

function rpcAnswer(method: string): unknown {
  switch (method) {
    case 'eth_chainId': return '0x64';
    case 'eth_call': return ADDR_WORD;
    case 'eth_getCode': return '0x';
    case 'eth_getTransactionCount': return '0x0';
    case 'pimlico_getUserOperationGasPrice':
      return { slow: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' }, standard: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' }, fast: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' } };
    default: return '0x0';
  }
}

beforeEach(() => {
  seen.length = 0;
  globalThis.fetch = (async (url: unknown, init: { body?: string; headers?: Record<string, string> }) => {
    const body = JSON.parse(init?.body ?? '{}');
    const calls = Array.isArray(body) ? body : [body];
    const req: Req = { url: String(url), headers: { ...(init?.headers ?? {}) }, body: calls[0] };
    seen.push(req);
    if (JSON.stringify(body).includes('callData')) throw new Stop('captured');
    const out = calls.map((c) => ({ jsonrpc: '2.0', id: c.id, result: rpcAnswer(c.method) }));
    return new Response(JSON.stringify(Array.isArray(body) ? out : out[0]), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = realFetch; });

describe('first UserOp body (callData) on the bundler transport', () => {
  test('carries the scheme header and no unkeyed value', async () => {
    const tagger = makeFixtureTagger();
    const payloads = [
      encodeFactProtobufKeyed({
        id: 'f-1', timestamp: '2026-11-25T12:00:00.000Z', owner: SA, encryptedBlob: '00ff11ee',
        blindIndices: tagger.factTags('prefers dark mode', fx.lsh.embedding, ['Acme']), decayScore: 1, source: 's',
        contentFp: tagger.contentFp('prefers dark mode'), agentId: 'a',
      }),
      encodeTombstoneProtobufKeyed('f-0', SA),
    ];
    await submitFactBatchOnChain(payloads, config()).catch(() => {});
    const withCallData = seen.find((r) => JSON.stringify(r.body).includes('callData'));
    expect(withCallData).toBeDefined();
    expect(withCallData!.url.endsWith('/v1/bundler')).toBe(true);
    expect(withCallData!.headers['x-totalreclaw-index-scheme']).toBe('1');
    // every request that reached the relay bundler carries it
    for (const r of seen.filter((x) => x.url.endsWith('/v1/bundler'))) {
      expect(r.headers['x-totalreclaw-index-scheme']).toBe('1');
    }
    // the inner payload is in the calldata: scan hex for every legacy value
    const text = JSON.stringify(withCallData!.body).toLowerCase();
    const legacy = [...fx.legacy_sha256.tokens, ...fx.legacy_sha256.stems, ...fx.legacy_sha256.entities, ...fx.legacy_sha256.lsh, fx.legacy_sha256.marker] as string[];
    for (const h of legacy) {
      expect(text.includes(h)).toBe(false);
      expect(text.includes(Buffer.from(h, 'ascii').toString('hex'))).toBe(false);
    }
    // positive control: a keyed tag IS visible in the calldata hex (so the scan can see inside it)
    const keyedTag = tagger.factTags('prefers dark mode', fx.lsh.embedding, ['Acme'])[0];
    expect(text.includes(Buffer.from(keyedTag, 'ascii').toString('hex'))).toBe(true);
  });
});
