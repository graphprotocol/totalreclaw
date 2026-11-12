/**
 * subgraph-search-ids-first.test.ts — internal #666 (port of public #703).
 *
 * searchSubgraph's blind-index queries now return fact ids only, and each
 * unique hit's payload is loaded once with a `facts(id_in)` query. This test
 * drives the REAL searchSubgraph against an in-memory subgraph mock and
 * checks:
 *
 *   1. Parity: the result equals what the old full-payload path returned.
 *      The blind-index requests are unchanged (same batches, same
 *      variables), so the test replays each recorded request with the old
 *      payload selection and rebuilds the old result with the old merge rule
 *      (first hit wins, inactive dropped). Same ids, same order, same fields
 *      means the reranker sees identical input.
 *   2. Fewer bytes: the new responses are smaller than the replayed old ones.
 *   3. Each payload is fetched once; blind-index queries carry no blobs.
 *   4. At most 6 queries are in flight.
 *   5. A failed payload batch drops only its own facts.
 *
 * The mock is the same as skill/plugin/subgraph/subgraph-search-ids-first.test.ts.
 */

import { searchSubgraph } from '../src/subgraph/search';

const RELAY = 'https://relay.test';

const OWNER = '0xabc0000000000000000000000000000000000001';

interface MockFact {
  id: string;
  owner: string;
  encryptedBlob: string;
  encryptedEmbedding: string;
  decayScore: string;
  timestamp: string;
  createdAt: string;
  isActive: boolean;
  contentFp: string;
  sequenceId: string;
  version: number;
}

interface MockIndex {
  id: string;
  hash: string;
  owner: string;
  factId: string;
}

const PAYLOAD_FIELDS = [
  'id', 'encryptedBlob', 'encryptedEmbedding', 'decayScore', 'timestamp',
  'createdAt', 'isActive', 'contentFp', 'sequenceId', 'version',
] as const;

function hex(n: number, seed: number): string {
  let out = '';
  let x = seed;
  while (out.length < n) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    out += x.toString(16).padStart(8, '0');
  }
  return out.slice(0, n);
}

/**
 * `n` facts with realistic sizes (~1 KB blob, ~2.6 KB embedding as hex).
 * Fact i is indexed under `common` (every fact), `mod3-<i%3>` and `w<i>`.
 */
function makeVault(n: number, inactive: Set<number> = new Set()) {
  const facts = new Map<string, MockFact>();
  const indexes: MockIndex[] = [];
  for (let i = 0; i < n; i++) {
    const id = `fact-${String(i).padStart(4, '0')}`;
    facts.set(id, {
      id,
      owner: OWNER,
      encryptedBlob: '0x' + hex(2000, i + 1),
      encryptedEmbedding: hex(5200, i + 7),
      decayScore: '0.9',
      timestamp: String(1_700_000_000 + i),
      createdAt: String(1_700_000_000 + i),
      isActive: !inactive.has(i),
      contentFp: hex(64, i + 3),
      sequenceId: String(i),
      version: 4,
    });
    for (const hash of ['common', `mod3-${i % 3}`, `w${i}`]) {
      indexes.push({ id: `${hash}:${id}`, hash, owner: OWNER, factId: id });
    }
  }
  return { facts, indexes };
}

type Vault = ReturnType<typeof makeVault>;

interface Recorded {
  kind: 'blind' | 'facts' | 'other';
  query: string;
  variables: Record<string, any>;
  bytes: number;
}

/** The selection inside `fact { ... }` of a blind-index query. */
function factSelection(query: string): string {
  const m = query.match(/fact\s*\{([^}]*)\}/);
  return m ? m[1] : '';
}

function project(fact: MockFact, selection: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of PAYLOAD_FIELDS) if (new RegExp(`\\b${f}\\b`).test(selection)) out[f] = fact[f];
  return out;
}

function resolve(vault: Vault, query: string, variables: Record<string, any>): { kind: Recorded['kind']; data: any } {
  if (/blindIndexes\s*\(/.test(query)) {
    const trapdoors = new Set<string>(variables.trapdoors);
    const paginate = /id_gt/.test(query);
    let rows = vault.indexes.filter(
      (ix) => trapdoors.has(ix.hash) && ix.owner === variables.owner && vault.facts.get(ix.factId)!.isActive,
    );
    rows.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    if (paginate) rows = rows.filter((ix) => ix.id > variables.lastId);
    else rows.reverse();
    const sel = factSelection(query);
    const blindIndexes = rows.slice(0, variables.first).map((ix) => ({
      id: ix.id,
      fact: project(vault.facts.get(ix.factId)!, sel),
    }));
    return { kind: 'blind', data: { blindIndexes } };
  }
  if (/id_in/.test(query)) {
    const ids = new Set<string>(variables.ids);
    // Reverse vault order on purpose: the client must not rely on the
    // subgraph returning facts in request order.
    const rows = [...vault.facts.values()]
      .filter((f) => ids.has(f.id) && f.owner === variables.owner && f.isActive)
      .reverse()
      .slice(0, variables.first);
    const sel = query.slice(query.indexOf(') {'));
    return { kind: 'facts', data: { facts: rows.map((f) => project(f, sel)) } };
  }
  return { kind: 'other', data: {} };
}

interface MockCtl {
  log: Recorded[];
  maxInFlight: number;
  failFactsBatch?: (ids: string[]) => boolean;
  restore: () => void;
}

function installMock(vault: Vault): MockCtl {
  const ctl: MockCtl = { log: [], maxInFlight: 0, restore: () => {} };
  let inFlight = 0;
  const original = globalThis.fetch;
  globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
    const { query, variables } = JSON.parse(String(init?.body ?? '{}'));
    inFlight++;
    ctl.maxInFlight = Math.max(ctl.maxInFlight, inFlight);
    try {
      await new Promise((r) => setTimeout(r, 1));
      const { kind, data } = resolve(vault, query, variables);
      if (kind === 'facts' && ctl.failFactsBatch?.(variables.ids)) {
        ctl.log.push({ kind, query, variables, bytes: 0 });
        return new Response('boom', { status: 500 });
      }
      const body = JSON.stringify({ data });
      ctl.log.push({ kind, query, variables, bytes: Buffer.byteLength(body) });
      return new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } });
    } finally {
      inFlight--;
    }
  }) as typeof fetch;
  ctl.restore = () => {
    globalThis.fetch = original;
  };
  return ctl;
}

/** The pre-#666 blind-index payload selection. */
const OLD_FACT_SELECTION = PAYLOAD_FIELDS.join('\n');

/**
 * Rebuild the old path's result and byte count: replay every recorded
 * blind-index request with the old payload selection and merge the facts
 * the way the old collectFacts did.
 */
function replayOld(vault: Vault, log: Recorded[]) {
  const results = new Map<string, Record<string, unknown>>();
  let bytes = 0;
  for (const r of log.filter((x) => x.kind === 'blind')) {
    const oldQuery = r.query.replace(/fact\s*\{[^}]*\}/, `fact { ${OLD_FACT_SELECTION} }`);
    const { data } = resolve(vault, oldQuery, r.variables);
    bytes += Buffer.byteLength(JSON.stringify({ data }));
    for (const entry of data.blindIndexes) {
      if (entry.fact && entry.fact.isActive !== false && !results.has(entry.fact.id)) {
        results.set(entry.fact.id, entry.fact);
      }
    }
  }
  return { results: [...results.values()], bytes };
}

const newBytes = (log: Recorded[]) => log.reduce((s, r) => s + r.bytes, 0);

describe('searchSubgraph ids-first (#666)', () => {
  const savedEnv = { ...process.env };
  let errorSpy: jest.SpyInstance;

  beforeAll(() => {
    // Small page so the saturated path (pagination) runs on a modest fixture.
    process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE = '20';
    process.env.TOTALRECLAW_TRAPDOOR_BATCH_SIZE = '5';
    delete process.env.TOTALRECLAW_SUBGRAPH_URL;
    errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterAll(() => {
    process.env = savedEnv;
    errorSpy.mockRestore();
  });

  it('small vault: same candidates as the old path, one payload query', async () => {
    const vault = makeVault(12, new Set([4]));
    const ctl = installMock(vault);
    const out = await searchSubgraph(OWNER, ['mod3-1', 'w2', 'w4'], 250, RELAY);
    ctl.restore();
    const old = replayOld(vault, ctl.log);
    expect(JSON.stringify(out)).toBe(JSON.stringify(old.results));
    expect(out.map((f) => f.id)).toEqual(['fact-0002', 'fact-0010', 'fact-0007', 'fact-0001']);
    expect(ctl.log.filter((r) => r.kind === 'facts')).toHaveLength(1);
    expect(ctl.log.filter((r) => r.kind === 'blind').every((r) => !/encryptedBlob/.test(r.query))).toBe(true);
  });

  it('saturated vault: same candidates, each payload once, fewer bytes, bounded concurrency', async () => {
    const vault = makeVault(120, new Set([5, 50, 99]));
    const trapdoors = ['common', 'mod3-0', 'mod3-1', 'mod3-2', ...Array.from({ length: 21 }, (_, i) => `w${i * 5}`)];
    const ctl = installMock(vault);
    const out = await searchSubgraph(OWNER, trapdoors, 90, RELAY);
    ctl.restore();
    const old = replayOld(vault, ctl.log);
    expect(JSON.stringify(out)).toBe(JSON.stringify(old.results));
    expect(out.length).toBeGreaterThan(0);
    expect(ctl.log.some((r) => r.kind === 'blind' && /id_gt/.test(r.query))).toBe(true);
    const fetchedIds = ctl.log.filter((r) => r.kind === 'facts').flatMap((r) => r.variables.ids as string[]);
    expect(new Set(fetchedIds).size).toBe(fetchedIds.length);
    expect(out.some((f) => ['fact-0005', 'fact-0050', 'fact-0099'].includes(f.id))).toBe(false);
    expect(newBytes(ctl.log)).toBeLessThan(old.bytes);
    expect(ctl.maxInFlight).toBeLessThanOrEqual(6);
  });

  it('many hits: payloads in batches of <= 100, order kept, fewer bytes', async () => {
    const vault = makeVault(450);
    const ctl = installMock(vault);
    const trapdoors = ['common', ...Array.from({ length: 450 }, (_, i) => `w${i}`)];
    const out = await searchSubgraph(OWNER, trapdoors, 1000, RELAY);
    ctl.restore();
    const old = replayOld(vault, ctl.log);
    const factQueries = ctl.log.filter((r) => r.kind === 'facts');
    expect(JSON.stringify(out)).toBe(JSON.stringify(old.results));
    expect(out).toHaveLength(450);
    expect(factQueries).toHaveLength(5);
    expect(factQueries.every((r) => r.variables.ids.length <= 100)).toBe(true);
    expect(newBytes(ctl.log)).toBeLessThan(old.bytes / 1.5);
    expect(ctl.maxInFlight).toBeLessThanOrEqual(6);
  });

  it('a failed payload batch drops only its own facts', async () => {
    const vault = makeVault(250);
    const ctl = installMock(vault);
    // The first payload batch holds the first 100 hits, fact-0249 among them.
    ctl.failFactsBatch = (ids) => ids.includes('fact-0249');
    const trapdoors = ['common', ...Array.from({ length: 250 }, (_, i) => `w${i}`)];
    const out = await searchSubgraph(OWNER, trapdoors, 1000, RELAY);
    ctl.restore();
    expect(out).toHaveLength(150);
    expect(out.some((f) => f.id === 'fact-0249')).toBe(false);
  });

  it('no hits: no payload query', async () => {
    const vault = makeVault(5);
    const ctl = installMock(vault);
    const out = await searchSubgraph(OWNER, ['nope'], 250, RELAY);
    ctl.restore();
    expect(out).toHaveLength(0);
    expect(ctl.log.some((r) => r.kind === 'facts')).toBe(false);
  });
});
