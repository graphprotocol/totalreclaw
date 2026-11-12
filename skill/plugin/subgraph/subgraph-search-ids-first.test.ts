/**
 * subgraph-search-ids-first.test.ts — internal #666 (port of public #703).
 *
 * searchSubgraph's blind-index queries now return fact ids only, and each
 * unique hit's payload is loaded once with a `facts(id_in)` query. This test
 * drives the REAL searchSubgraph against an in-memory subgraph mock and
 * checks:
 *
 *   1. Parity: the result equals what the old full-payload path returned.
 *      The blind-index requests are unchanged (same phases, same variables),
 *      so the test replays each recorded request with the old payload
 *      selection and rebuilds the old result with the old merge rule
 *      (first hit wins, inactive dropped). Same ids, same order, same fields
 *      means the reranker sees identical input.
 *   2. Fewer bytes: the new responses are smaller than the replayed old ones.
 *   3. Each payload is fetched once; blind-index queries carry no blobs.
 *   4. At most 6 queries are in flight.
 *   5. A failed payload batch drops only its own facts.
 *
 * Run with: npx tsx subgraph/subgraph-search-ids-first.test.ts
 */

// Small page so the saturated phases (split batches + pagination) run on a
// modest fixture. subgraph-search reads these from CONFIG at import time, so
// set them before importing it. (CONFIG, not env vars: this file mocks the
// network, and the scanner flags env reads next to network code.)
const { CONFIG } = await import('../config.js');
CONFIG.pageSize = 20;
CONFIG.trapdoorBatchSize = 5;
CONFIG.serverUrl = 'https://relay.test';

const { searchSubgraph } = await import('./subgraph-search.js');

let passed = 0;
let failed = 0;
function check(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.error(`not ok ${n} - ${name}`);
    failed++;
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
// In-memory subgraph
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

// 1. Small vault, not saturated: one blind-index query, one payload query.
{
  const vault = makeVault(12, new Set([4]));
  const ctl = installMock(vault);
  const out = await searchSubgraph(OWNER, ['mod3-1', 'w2', 'w4'], 250);
  ctl.restore();
  const old = replayOld(vault, ctl.log);
  check(JSON.stringify(out) === JSON.stringify(old.results), 'small vault: same candidates, order and fields as the old path');
  check(out.map((f) => f.id).join(',') === 'fact-0002,fact-0010,fact-0007,fact-0001', 'small vault: inactive fact-0004 dropped, first-hit order kept');
  check(ctl.log.filter((r) => r.kind === 'blind').length === 1, 'small vault: one blind-index query');
  check(ctl.log.filter((r) => r.kind === 'facts').length === 1, 'small vault: one payload query');
  check(ctl.log.filter((r) => r.kind === 'blind').every((r) => !/encryptedBlob/.test(r.query)), 'blind-index query selects no payload');
}

// 2. Saturated vault: split batches + pagination + maxCandidates cap.
{
  const vault = makeVault(120, new Set([5, 50, 99]));
  const trapdoors = ['common', 'mod3-0', 'mod3-1', 'mod3-2', ...Array.from({ length: 21 }, (_, i) => `w${i * 5}`)];
  const ctl = installMock(vault);
  const out = await searchSubgraph(OWNER, trapdoors, 90);
  ctl.restore();
  const old = replayOld(vault, ctl.log);
  const blind = ctl.log.filter((r) => r.kind === 'blind');
  check(JSON.stringify(out) === JSON.stringify(old.results), `saturated vault: same ${out.length} candidates, order and fields as the old path`);
  check(blind.some((r) => /id_gt/.test(r.query)), 'saturated vault: pagination ran');
  check(blind.length > 2, `saturated vault: split batches ran (${blind.length} blind-index queries)`);
  const fetchedIds = ctl.log.filter((r) => r.kind === 'facts').flatMap((r) => r.variables.ids as string[]);
  check(fetchedIds.length === new Set(fetchedIds).size, 'each fact payload requested once');
  check(!out.some((f) => ['fact-0005', 'fact-0050', 'fact-0099'].includes(f.id)), 'inactive facts excluded');
  const nb = newBytes(ctl.log);
  check(nb < old.bytes, `fewer bytes: ${nb} vs ${old.bytes} old (${((1 - nb / old.bytes) * 100).toFixed(0)}% less)`);
  check(ctl.maxInFlight <= 6, `at most 6 queries in flight (saw ${ctl.maxInFlight})`);
}

// 3. Many payload batches stay bounded and ordered.
{
  const vault = makeVault(450);
  const ctl = installMock(vault);
  // `w<i>` trapdoors: one hit each, so 450 hits over many split batches.
  const trapdoors = ['common', ...Array.from({ length: 450 }, (_, i) => `w${i}`)];
  const out = await searchSubgraph(OWNER, trapdoors, 1000);
  ctl.restore();
  const old = replayOld(vault, ctl.log);
  const factQueries = ctl.log.filter((r) => r.kind === 'facts');
  check(JSON.stringify(out) === JSON.stringify(old.results), `450 hits: same candidates and order as the old path (${out.length})`);
  check(factQueries.length === 5 && factQueries.every((r) => r.variables.ids.length <= 100), 'payloads fetched in batches of <= 100');
  const nb = newBytes(ctl.log);
  check(nb < old.bytes, `450 hits: fewer bytes: ${nb} vs ${old.bytes} old (${((1 - nb / old.bytes) * 100).toFixed(0)}% less)`);
  check(ctl.maxInFlight <= 6, `at most 6 queries in flight (saw ${ctl.maxInFlight})`);
}

// 4. A failed payload batch drops only its own facts.
{
  const vault = makeVault(250);
  const ctl = installMock(vault);
  const trapdoors = ['common', ...Array.from({ length: 250 }, (_, i) => `w${i}`)];
  // The first payload batch holds the first 100 hits, fact-0249 among them.
  ctl.failFactsBatch = (ids) => ids.includes('fact-0249');
  const origError = console.error;
  console.error = () => {};
  const out = await searchSubgraph(OWNER, trapdoors, 1000);
  console.error = origError;
  ctl.restore();
  check(out.length === 150 && !out.some((f) => f.id === 'fact-0249'), `failed payload batch: other batches still return (${out.length} of 250)`);
}

// 5. No hits: no payload query.
{
  const vault = makeVault(5);
  const ctl = installMock(vault);
  const out = await searchSubgraph(OWNER, ['nope'], 250);
  ctl.restore();
  check(out.length === 0 && ctl.log.every((r) => r.kind !== 'facts'), 'no hits: no payload query');
}

console.log(`\n# ${passed} passed, ${failed} failed`);
