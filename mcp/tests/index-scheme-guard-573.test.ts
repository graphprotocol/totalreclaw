/**
 * internal#573 (M5): pin / unpin / retype / set_scope refuse unless the target
 * fact is confirmed keyed (`Fact.indexScheme == 1`).
 *
 * The real `lookupFactIndexScheme` runs under a `fetch` mock; the dep is built
 * with the production composition (`createIndexSchemeLookup`). Ops are the real
 * `executePinOperation` / `executeRetype` / `executeSetScope`.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { executePinOperation, feedbackLogPath, type PinOpDeps } from '../src/tools/pin';
import { executeRetype, type MetadataOpDeps } from '../src/tools/retype';
import { executeSetScope } from '../src/tools/set-scope';
import { decisionsLogPath } from '../src/contradiction-sync';
import { buildV1ClaimBlob } from '../src/claims-helper';
import { lookupFactIndexScheme, FACT_INDEX_SCHEME_QUERY } from '../src/subgraph/search';
import { RelayReadBlocked, clearReadBlock } from '../src/subgraph/read-block';
import { RecentWrites, recordSubmittedPayloads } from '../src/subgraph/recent-writes';
import { createIndexSchemeLookup, INDEX_SCHEME_UNCONFIRMED_MSG } from '../src/subgraph/index-scheme-guard';
import { confirmed } from './helpers/confirmed';
import { MOCK_KEYED_TAGS } from './helpers/keyed-fixture';

const TEST_STATE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-guard-test-'));
process.env.TOTALRECLAW_STATE_DIR = TEST_STATE_DIR;
afterAll(() => {
  try { fs.rmSync(TEST_STATE_DIR, { recursive: true, force: true }); } catch { /* ignore */ }
  delete process.env.TOTALRECLAW_STATE_DIR;
});

const OWNER = '0x1234567890abcdef1234567890abcdef12345678';
const RELAY = 'https://relay.test';
const ID = '7f3c2a10-6b1e-4c1f-9d2e-0000000000b1';
const hex = (s: string) => Buffer.from(s, 'utf-8').toString('hex');
const UNHEX = (h: string) => Buffer.from(h, 'hex').toString('utf-8');
const GENERIC_BLOCK = { status: 429, body: JSON.stringify({ error: 'read_quota_exceeded', message: 'x' }) };

interface Sent { query: string; variables: Record<string, unknown>; headers: Record<string, string> }
let sent: Sent[] = [];
let answer: (req: Sent) => { status?: number; body: unknown } = () => ({ body: {} });
const realFetch = globalThis.fetch;

beforeEach(() => {
  clearReadBlock(OWNER); // a read block from an earlier case must not leak
  sent = [];
  delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  globalThis.fetch = (async (_url: unknown, init: { body?: string; headers?: Record<string, string> }) => {
    const b = JSON.parse(init?.body ?? '{}');
    const req = { query: b.query, variables: b.variables, headers: { ...(init?.headers ?? {}) } };
    sent.push(req);
    const a = answer(req);
    const text = typeof a.body === 'string' ? a.body : JSON.stringify(a.body);
    return new Response(text, { status: a.status ?? 200 });
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = realFetch; });

const factAnswer = (fact: unknown) => () => ({ body: { data: { fact } } });

function claimBlob(pin: 'pinned' | 'unpinned' | undefined = undefined): string {
  return buildV1ClaimBlob({
    id: ID, text: 'Tidewater analytics moves to ClickHouse', type: 'claim', source: 'user', scope: 'work', importance: 8,
    ...(pin ? { pinStatus: pin } : {}),
  });
}

function harness(opts: { cache?: RecentWrites; blob?: string } = {}) {
  const cache = opts.cache ?? new RecentWrites();
  const submitted: Buffer[][] = [];
  const order: string[] = [];
  const blob = opts.blob ?? claimBlob();
  const deps: PinOpDeps & MetadataOpDeps = {
    owner: OWNER,
    sourceAgent: 'mcp-server',
    fetchFactById: async (id) => ({ id, encryptedBlob: hex(blob), encryptedEmbedding: null, decayScore: '1', timestamp: '1700000000', isActive: true }),
    decryptBlob: UNHEX,
    encryptBlob: hex,
    submitBatch: async (p) => { order.push('userop'); submitted.push(p); return { txHash: '0xabc', success: true }; },
    fetchIndexScheme: createIndexSchemeLookup({
      owner: OWNER,
      cache,
      lookupSubgraph: (id) => { order.push('guard'); return lookupFactIndexScheme(OWNER, id, RELAY, 'ab'.repeat(32)); },
    }),
    generateIndices: async () => ({ blindIndices: MOCK_KEYED_TAGS, encryptedEmbedding: 'e' }),
  };
  return { deps, cache, submitted, order };
}

const guardQueries = () => sent.filter((s) => s.query.includes('FactIndexScheme'));

type Op = { name: string; run: (deps: PinOpDeps & MetadataOpDeps) => Promise<Record<string, unknown>>; idKey: 'fact_id' | 'memory_id' };
const OPS: Op[] = [
  { name: 'pin', idKey: 'fact_id', run: (d) => executePinOperation(ID, 'pinned', d) as never },
  { name: 'unpin', idKey: 'fact_id', run: (d) => executePinOperation(ID, 'active', d, undefined, confirmed('unpin', ID)) as never },
  { name: 'retype', idKey: 'memory_id', run: (d) => executeRetype(ID, 'preference', d) as never },
  { name: 'set_scope', idKey: 'memory_id', run: (d) => executeSetScope(ID, 'personal', d) as never },
];
const blobFor = (op: string) => (op === 'unpin' ? claimBlob('pinned') : claimBlob());

describe('guard query', () => {
  test('is the dedicated query, with the exact selection', () => {
    expect(FACT_INDEX_SCHEME_QUERY.replace(/\s+/g, ' ').trim()).toBe(
      'query FactIndexScheme($id: ID!) { fact(id: $id) { id owner indexScheme } }',
    );
  });
});

for (const op of OPS) {
  describe(`${op.name}: guard`, () => {
    test('indexScheme 1 proceeds: exactly one guard query, sent before any UserOp', async () => {
      answer = factAnswer({ id: ID, owner: OWNER, indexScheme: 1 });
      const h = harness({ blob: blobFor(op.name) });
      const r = await op.run(h.deps);
      expect(r.success).toBe(true);
      expect(guardQueries()).toHaveLength(1);
      expect(guardQueries()[0].variables).toEqual({ id: ID });
      expect(h.order).toEqual(['guard', 'userop']);
    });

    test('the guard request carries the scheme header and the wallet header', async () => {
      answer = factAnswer({ id: ID, owner: OWNER, indexScheme: 1 });
      await op.run(harness({ blob: blobFor(op.name) }).deps);
      expect(guardQueries()[0].headers['X-TotalReclaw-Index-Scheme']).toBe('1');
      expect(guardQueries()[0].headers['X-Wallet-Address']).toBe(OWNER);
    });

    const refusals: Array<[string, () => { status?: number; body: unknown }]> = [
      ['a GraphQL error body (old subgraph)', () => ({ body: { errors: [{ message: 'Type `Fact` has no field `indexScheme`' }] } })],
      ['fact: null', () => ({ body: { data: { fact: null } } })],
      ['indexScheme 0', () => ({ body: { data: { fact: { id: ID, owner: OWNER, indexScheme: 0 } } } })],
      ['indexScheme missing', () => ({ body: { data: { fact: { id: ID, owner: OWNER } } } })],
      ['indexScheme 2', () => ({ body: { data: { fact: { id: ID, owner: OWNER, indexScheme: 2 } } } })],
      ['owner mismatch', () => ({ body: { data: { fact: { id: ID, owner: '0x' + 'ee'.repeat(20), indexScheme: 1 } } } })],
      ['a malformed body', () => ({ body: 'not json' })],
      ['a body with no data', () => ({ body: {} })],
      ['a relay 500', () => ({ status: 500, body: 'boom' })],
    ];
    for (const [label, a] of refusals) {
      test(`refuses on ${label}: nothing written, no UserOp, no token`, async () => {
        answer = a;
        const h = harness({ blob: blobFor(op.name) });
        const r = await op.run(h.deps);
        expect(r.success).toBe(false);
        expect(r.error).toBe(INDEX_SCHEME_UNCONFIRMED_MSG);
        expect(r.error_code).toBe('index_scheme_unconfirmed');
        expect(r[op.idKey]).toBe(ID);
        expect(h.submitted).toHaveLength(0);
        expect(r.confirm_token).toBeUndefined();
      });
    }

    test('owner match is case-insensitive', async () => {
      answer = factAnswer({ id: ID, owner: OWNER.toUpperCase().replace('0X', '0x'), indexScheme: 1 });
      const r = await op.run(harness({ blob: blobFor(op.name) }).deps);
      expect(r.success).toBe(true);
    });

    test('a read block propagates unchanged (so the "reads paused" response shows)', async () => {
      answer = () => ({ status: GENERIC_BLOCK.status, body: GENERIC_BLOCK.body });
      const h = harness({ blob: blobFor(op.name) });
      await expect(op.run(h.deps)).rejects.toBeInstanceOf(RelayReadBlocked);
      expect(h.submitted).toHaveLength(0);
    });
  });
}

describe('refusal writes no feedback row', () => {
  test('pin of a prior formula-loser, refused: feedback.jsonl is not created', async () => {
    try { fs.rmSync(feedbackLogPath(), { force: true }); } catch { /* ignore */ }
    fs.mkdirSync(path.dirname(decisionsLogPath()), { recursive: true });
    const comp = { confidence: 0.85, corroboration: 1, recency: 0.5, validation: 0.7, weighted_total: 0.8 };
    fs.writeFileSync(decisionsLogPath(), JSON.stringify({
      ts: 1_776_000_000, entity_id: 'editor', new_claim_id: 'winner', existing_claim_id: ID, similarity: 0.5,
      action: 'supersede_existing', reason: 'new_wins', winner_score: 0.83, loser_score: 0.73,
      winner_components: comp, loser_components: comp, mode: 'active',
    }) + '\n');
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'pinned', harness().deps);
    expect(r.success).toBe(false);
    expect(fs.existsSync(feedbackLogPath())).toBe(false);
  });
});

describe('idempotent no-op makes no guard query', () => {
  test('pin of an already pinned fact', async () => {
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'pinned', harness({ blob: claimBlob('pinned') }).deps);
    expect(r.idempotent).toBe(true);
    expect(guardQueries()).toHaveLength(0);
  });
  test('unpin of an unpinned fact', async () => {
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'active', harness().deps);
    expect(r.idempotent).toBe(true);
    expect(guardQueries()).toHaveLength(0);
  });
  test('retype to the current type', async () => {
    answer = factAnswer(null);
    const r = await executeRetype(ID, 'claim', harness().deps);
    expect(r.idempotent).toBe(true);
    expect(guardQueries()).toHaveLength(0);
  });
  test('set_scope to the current scope', async () => {
    answer = factAnswer(null);
    const r = await executeSetScope(ID, 'work', harness().deps);
    expect(r.idempotent).toBe(true);
    expect(guardQueries()).toHaveLength(0);
  });
});

describe('the guard runs before the unpin / change confirmation ask', () => {
  test('unpin of a pinned fact with no token: refused by the guard, no confirm_token issued', async () => {
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'active', harness({ blob: claimBlob('pinned') }).deps);
    expect(r.error_code).toBe('index_scheme_unconfirmed');
    expect(r.confirm_token).toBeUndefined();
    expect(r.confirmation_required).toBeUndefined();
  });
  test('confirmed keyed fact then reaches the confirmation ask', async () => {
    answer = factAnswer({ id: ID, owner: OWNER, indexScheme: 1 });
    const r = await executePinOperation(ID, 'active', harness({ blob: claimBlob('pinned') }).deps);
    expect(r.success).toBe(false);
    expect(r.error_code).toBeUndefined();
    expect(r.confirm_token).toBeTruthy();
  });
});

describe('facts this process wrote pass without a query', () => {
  function rememberHere(cache: RecentWrites) {
    recordSubmittedPayloads(cache, OWNER, [{ id: ID, timestamp: new Date().toISOString(), encryptedBlob: hex(claimBlob()), decayScore: 1 }], { success: true });
  }
  for (const op of OPS.filter((o) => o.name !== 'unpin')) {
    test(`${op.name} on a fact in the process's write record: no guard query`, async () => {
      answer = factAnswer(null); // a lagging indexer would say "no such fact"
      const cache = new RecentWrites();
      rememberHere(cache);
      const r = await op.run(harness({ cache }).deps);
      expect(r.success).toBe(true);
      expect(guardQueries()).toHaveLength(0);
    });
  }
  test('a different process (no record) queries and, on a lagging indexer, refuses', async () => {
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'pinned', harness().deps);
    expect(r.success).toBe(false);
    expect(guardQueries()).toHaveLength(1);
  });
  test('an expired record queries', async () => {
    let now = 0;
    const cache = new RecentWrites({ now: () => now });
    rememberHere(cache);
    now += 16 * 60_000;
    answer = factAnswer(null);
    const r = await executePinOperation(ID, 'pinned', harness({ cache }).deps);
    expect(r.success).toBe(false);
    expect(guardQueries()).toHaveLength(1);
  });
  test('after a forget (tombstone record) the query runs', async () => {
    const cache = new RecentWrites();
    rememberHere(cache);
    cache.recordTombstone(OWNER, ID);
    answer = factAnswer(null);
    const lookup = createIndexSchemeLookup({ owner: OWNER, cache, lookupSubgraph: (id) => lookupFactIndexScheme(OWNER, id, RELAY) });
    expect(await lookup(ID)).toBeNull();
    expect(guardQueries()).toHaveLength(1);
  });
});

describe('indexScheme appears in exactly one query', () => {
  test('no src file has it in a query string except the guard query', () => {
    const SRC = path.join(__dirname, '..', 'src');
    const hits: string[] = [];
    const walk = (dir: string) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, e.name);
        if (e.isDirectory()) { walk(full); continue; }
        if (!e.name.endsWith('.ts')) continue;
        const text = fs.readFileSync(full, 'utf8');
        for (const m of text.matchAll(/`([^`]*)`/g)) {
          if (m[1].includes('indexScheme') && /\b(query|fact|facts|blindIndexes)\b/.test(m[1]) && m[1].includes('{')) {
            hits.push(`${path.relative(SRC, full)}:${m[1].replace(/\s+/g, ' ').trim().slice(0, 60)}`);
          }
        }
      }
    };
    walk(SRC);
    expect(hits).toEqual([expect.stringContaining('subgraph/search.ts:query FactIndexScheme')]);
  });

  test('no other subgraph request body mentions indexScheme', async () => {
    const { searchSubgraph, searchSubgraphBroadened, getOwnerFactCount, getSubgraphFactCount, lookupFactById, fetchFactForForget } =
      await import('../src/subgraph/search');
    answer = () => ({ body: { data: { blindIndexes: [], facts: [], fact: null, globalStates: [] } } });
    await searchSubgraph(OWNER, ['t'], 10, RELAY, 'ab');
    await searchSubgraphBroadened(OWNER, 10, RELAY, 'ab');
    await getOwnerFactCount(OWNER, RELAY, 'ab');
    await getSubgraphFactCount(OWNER, RELAY, 'ab');
    await lookupFactById(OWNER, 'id', RELAY, 'ab');
    await fetchFactForForget(OWNER, 'id', RELAY, 'ab');
    expect(sent.length).toBeGreaterThan(5);
    expect(sent.filter((s) => s.query.includes('indexScheme'))).toEqual([]);
  });
});

describe('refusal text', () => {
  test('does not suggest saving the memory again', () => {
    expect(INDEX_SCHEME_UNCONFIRMED_MSG.toLowerCase()).not.toMatch(/save|remember|store it again|re-?add/);
  });
});
