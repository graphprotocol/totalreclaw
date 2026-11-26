/**
 * internal#573 (M6, "test 6"): no unkeyed hash leaves the client.
 *
 * Every managed-mode read and write path is driven through the real seams:
 *   reads  : the real searchSubgraph / searchSubgraphBroadened / fetchFactForForget /
 *            lookupFactById / lookupFactIndexScheme / collectCandidatesForEntities
 *            under a mocked `fetch`;
 *   writes : buildFactPayload + the keyed encoders (remember, debrief),
 *            the keyed tombstone encoder (forget, supersede), and the real
 *            executePinOperation / executeRetype / executeSetScope with a
 *            capturing submit step.
 * The DataEdge calldata is the protobuf verbatim, so each write buffer is
 * scanned as raw bytes and as hex.
 *
 * Banned set: every legacy unkeyed value (the fixture's `legacy_sha256`, plus
 * legacy core output recomputed for the corpus). Positive controls push a
 * banned value through the same pipeline and the same scan, in both forms.
 *
 * Self-hosted (HTTP) mode is not driven here: it runs through
 * `@totalreclaw/client` and is a documented carve-out.
 */

import { createHash } from 'node:crypto';
import { KeyedTagger, buildFactPayload } from '../src/subgraph/keyed-tags';
import { LSHHasher } from '../src/subgraph/lsh';
import { deriveKeys, deriveLshSeed, encrypt, decrypt } from '../src/subgraph/crypto';
import { bundleVaultToBuffers, type DerivedBundleV1 } from '../src/subgraph/bundle';
import { getEmbeddingDims } from '../src/subgraph/embedding';
import { encodeFactProtobufKeyed, encodeTombstoneProtobufKeyed } from '../src/subgraph/store';
import {
  searchSubgraph, searchSubgraphBroadened, fetchFactForForget, lookupFactById, lookupFactIndexScheme,
} from '../src/subgraph/search';
import { clearReadBlock } from '../src/subgraph/read-block';
import { RecentWrites, recordSubmittedPayloads } from '../src/subgraph/recent-writes';
import { createRewriteLookup } from '../src/subgraph/fact-lookup';
import { createIndexSchemeLookup } from '../src/subgraph/index-scheme-guard';
import { collectCandidatesForEntities } from '../src/contradiction-sync';
import { executePinOperation, type PinOpDeps } from '../src/tools/pin';
import { executeRetype, type MetadataOpDeps } from '../src/tools/retype';
import { executeSetScope } from '../src/tools/set-scope';
import { buildV1ClaimBlob } from '../src/claims-helper';
import { confirmed } from './helpers/confirmed';
import { fx } from './helpers/keyed-fixture';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const core = require('@totalreclaw/core') as typeof import('@totalreclaw/core');

const RELAY = 'https://relay.test';
const AUTH = 'ab'.repeat(32);
const SA = '0x2c0cf74b2b76110708ca431796367779e3738250';
const DIMS = getEmbeddingDims();
const sha256 = (s: string) => createHash('sha256').update(s).digest('hex');

// ── corpus and banned set ────────────────────────────────────────────────────

const TEXTS: string[] = [
  ...fx.stem_vectors.map((v: { text: string }) => v.text),
  'User prefers PostgreSQL over MySQL at Acme in 2024',
  'Café meeting moved to İstanbul',
];
const NAMES: string[] = [...fx.entity_vectors.map((v: { name: string }) => v.name), 'PostgreSQL', 'MySQL', 'Acme'];

function seededEmbedding(seed: number): number[] {
  let a = seed;
  return Array.from({ length: DIMS }, () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296 - 0.5;
  });
}
const EMBEDDINGS = [seededEmbedding(1), seededEmbedding(2), seededEmbedding(3)];
const embeddingFor = (text: string) => EMBEDDINGS[text.length % EMBEDDINGS.length];

const keyed = deriveKeys(fx.meta.mnemonic);
const lshSeed = deriveLshSeed(fx.meta.mnemonic, keyed.salt);

const banned = new Set<string>();
const addBanned = (h: string) => banned.add(h.toLowerCase());
for (const k of ['tokens', 'stems', 'entities', 'lsh']) for (const h of fx.legacy_sha256[k]) addBanned(h);
addBanned(fx.legacy_sha256.marker);
addBanned(sha256('type:digest'));
{
  const legacyLsh = core.WasmLshHasher.withParams(Buffer.from(lshSeed).toString('hex'), DIMS, 20, 32);
  for (const t of TEXTS) {
    for (const h of core.generateBlindIndices(t)) addBanned(h);
    addBanned(core.generateContentFingerprint(t, keyed.dedupKey.toString('hex')));
  }
  for (const n of NAMES) addBanned(sha256('entity:' + core.normalizeEntityName(n)));
  for (const e of EMBEDDINGS) for (const h of legacyLsh.hash(new Float64Array(e))) addBanned(h);
}

type Form = 'raw' | 'hex';
/** The one scan helper: every banned value found in a captured body, as raw text or as hex of its ASCII. */
function scan(body: string | Buffer): Array<[string, Form]> {
  const rawText = (typeof body === 'string' ? body : body.toString('latin1')).toLowerCase();
  // A string body already carries calldata as hex text; a Buffer is hex-rendered first.
  const hexText = typeof body === 'string' ? rawText : body.toString('hex').toLowerCase();
  const out: Array<[string, Form]> = [];
  for (const h of banned) {
    if (rawText.includes(h)) out.push([h, 'raw']);
    if (hexText.includes(Buffer.from(h, 'ascii').toString('hex'))) out.push([h, 'hex']);
  }
  return out;
}

// ── capture plumbing ─────────────────────────────────────────────────────────

interface SubCap { body: string; headers: Record<string, string> }
let subCaps: SubCap[] = [];
let writeCaps: Buffer[] = [];
let answer: (b: { query: string; variables: Record<string, unknown> }) => unknown = () => ({ data: {} });
const realFetch = globalThis.fetch;

function installFetch(target: () => SubCap[]) {
  globalThis.fetch = (async (_url: unknown, init: { body?: string; headers?: Record<string, string> }) => {
    const body = init?.body ?? '';
    target().push({ body, headers: { ...(init?.headers ?? {}) } });
    return new Response(JSON.stringify(answer(JSON.parse(body))), { status: 200 });
  }) as typeof fetch;
}
const submit = (target: () => Buffer[]) => async (payloads: Buffer[]) => { target().push(...payloads); return { txHash: '0xabc', success: true }; };

beforeEach(() => {
  clearReadBlock(SA);
  subCaps = [];
  writeCaps = [];
  installFetch(() => subCaps);
});
afterEach(() => { globalThis.fetch = realFetch; });

// ── drivers ──────────────────────────────────────────────────────────────────

const OWN_ID = '11111111-1111-4111-8111-111111111111'; // written by this process
const SEEDED_ID = '22222222-2222-4222-8222-222222222222'; // only in the mocked subgraph

function makeTagger(which: 'phrase' | 'bundle'): KeyedTagger {
  if (which === 'phrase') {
    return new KeyedTagger(keyed.encryptionKey, keyed.dedupKey, new LSHHasher(lshSeed, DIMS));
  }
  const bundle = { vault: { encryption_key: fx.meta.encryption_key_hex, dedup_key: fx.meta.dedup_key_hex, auth_key: '00'.repeat(32), lsh_seed: fx.meta.lsh_seed_hex } } as unknown as DerivedBundleV1;
  const v = bundleVaultToBuffers(bundle);
  return new KeyedTagger(v.encryptionKey, v.dedupKey, new LSHHasher(v.lshSeed, DIMS));
}

function claimHex(opts: { pinned?: boolean } = {}): string {
  const blob = buildV1ClaimBlob({
    id: 'x', text: TEXTS[TEXTS.length - 2], type: 'claim', source: 'user', scope: 'work', importance: 8,
    entities: [{ name: 'PostgreSQL', type: 'tool' }, { name: 'Acme', type: 'project' }] as never,
    ...(opts.pinned ? { pinStatus: 'pinned' as const } : {}),
  });
  return Buffer.from(encrypt(blob, keyed.encryptionKey), 'base64').toString('hex');
}

/** The remember write path: tags + dedup search + payload + keyed encoding. */
async function driveRemember(tagger: KeyedTagger, text: string, sink: Buffer[]) {
  const embedding = embeddingFor(text);
  const tags = tagger.factTags(text, embedding, []);
  await searchSubgraph(SA, tags, 10, RELAY, AUTH); // store-time dedup search
  const payload = buildFactPayload({
    tagger, id: crypto.randomUUID(), owner: SA, source: 'mcp_remember', text,
    encryptedBlobBase64: encrypt('{"claim":"x"}', keyed.encryptionKey),
    encryptedEmbedding: 'ZW1iZWRkaW5n', tags,
  });
  sink.push(encodeFactProtobufKeyed(payload));
  return payload;
}

function pinHarness(tagger: KeyedTagger, cache: RecentWrites, pinned: boolean) {
  answer = (b) => {
    if (b.query.includes('FactIndexScheme')) return { data: { fact: { id: SEEDED_ID, owner: SA, indexScheme: 1 } } };
    if (b.query.includes('GetFactById')) {
      if (b.variables.id === SEEDED_ID) {
        return { data: { fact: { id: SEEDED_ID, owner: SA, encryptedBlob: claimHex({ pinned }), encryptedEmbedding: null, decayScore: '1', timestamp: '1700000000', createdAt: '1700000000', isActive: true } } };
      }
      return { data: { fact: null } }; // a lagging indexer: our own fact is not indexed yet
    }
    return { data: {} };
  };
  const deps: PinOpDeps & MetadataOpDeps = {
    owner: SA,
    sourceAgent: 'mcp-server',
    fetchFactById: createRewriteLookup({
      owner: SA, cache, retry: { totalMs: 0 }, log: () => {},
      lookupSubgraph: (id) => lookupFactById(SA, id, RELAY, AUTH),
    }),
    decryptBlob: (h) => decrypt(Buffer.from(h, 'hex').toString('base64'), keyed.encryptionKey),
    encryptBlob: (p) => Buffer.from(encrypt(p, keyed.encryptionKey), 'base64').toString('hex'),
    submitBatch: submit(() => writeCaps),
    fetchIndexScheme: createIndexSchemeLookup({
      owner: SA, cache, lookupSubgraph: (id) => lookupFactIndexScheme(SA, id, RELAY, AUTH),
    }),
    // The same composition buildPinDepsFromState uses.
    generateIndices: async (text, entityNames) => ({
      blindIndices: tagger.factTags(text, embeddingFor(text), entityNames),
      encryptedEmbedding: 'ZW1iZWRkaW5n',
    }),
    recordWrites: (r) => recordSubmittedPayloads(cache, SA, r, { success: true }),
  };
  return deps;
}

function recordOwn(cache: RecentWrites, pinned: boolean) {
  recordSubmittedPayloads(cache, SA, [{ id: OWN_ID, timestamp: new Date().toISOString(), encryptedBlob: claimHex({ pinned }), decayScore: 1 }], { success: true });
}

// ── per-op results ───────────────────────────────────────────────────────────

interface OpResult { op: string; subs: SubCap[]; writes: Buffer[]; minSub: number; minWrite: number }
const results: OpResult[] = [];

async function runOp(op: string, minSub: number, minWrite: number, fn: () => Promise<void>) {
  const s0 = subCaps.length, w0 = writeCaps.length;
  await fn();
  results.push({ op, subs: subCaps.slice(s0), writes: writeCaps.slice(w0), minSub, minWrite });
}

describe('test 6: managed-mode wire has no unkeyed hash', () => {
  const phrase = makeTagger('phrase');
  const bundle = makeTagger('bundle');
  const rememberTagSets: Record<string, string[][]> = { phrase: [], bundle: [] };

  beforeAll(async () => {
    clearReadBlock(SA);
    subCaps = []; writeCaps = [];
    installFetch(() => subCaps);

    for (const [which, tagger] of [['phrase', phrase], ['bundle', bundle]] as const) {
      await runOp(`remember (${which})`, 1, 1, async () => {
        for (const t of TEXTS) { const p = await driveRemember(tagger, t, writeCaps); rememberTagSets[which].push(p.blindIndices); }
      });
    }

    // Debrief (crystal and legacy): same builder, no subgraph request by design.
    await runOp('debrief crystal', 0, 1, async () => {
      const text = TEXTS[0];
      const tags = phrase.factTags(text, embeddingFor(text), []);
      writeCaps.push(encodeFactProtobufKeyed(buildFactPayload({ tagger: phrase, id: crypto.randomUUID(), owner: SA, source: 'mcp_debrief', text, encryptedBlobBase64: encrypt('{}', keyed.encryptionKey), encryptedEmbedding: 'ZQ==', tags })));
    });
    await runOp('debrief legacy facts', 0, 1, async () => {
      for (const text of TEXTS.slice(-2)) {
        const tags = phrase.factTags(text, embeddingFor(text), []);
        writeCaps.push(encodeFactProtobufKeyed(buildFactPayload({ tagger: phrase, id: crypto.randomUUID(), owner: SA, source: 'mcp_debrief', text, encryptedBlobBase64: encrypt('{}', keyed.encryptionKey), tags })));
      }
    });

    // Recall (MCP always has a query embedding) plus the broadened pass.
    answer = () => ({ data: { blindIndexes: [], facts: [] } });
    await runOp('recall', 1, 0, async () => {
      for (const q of TEXTS) {
        await searchSubgraph(SA, phrase.queryTrapdoors(q, embeddingFor(q), []), 50, RELAY, AUTH);
      }
      await searchSubgraphBroadened(SA, 50, RELAY, AUTH);
    });

    // Contradiction lookup.
    await runOp('contradiction lookup', 1, 0, async () => {
      await collectCandidatesForEntities(
        { t: 'x', c: 'fact', cf: 1, i: 5, sa: 'a', ea: '2026-01-01T00:00:00Z', e: NAMES.map((n) => ({ n, tp: 'tool' })) } as never,
        'new-id', SA, AUTH, keyed.encryptionKey,
        {
          searchSubgraph: (o, t, m, a) => searchSubgraph(o, t, m, RELAY, a),
          decryptFromHex: () => '',
          entityTag: (n) => phrase.entityTag(n),
        },
        { info: () => {}, warn: () => {} },
      );
    });

    // Forget: lookup, then the keyed tombstone.
    answer = () => ({ data: { fact: { id: OWN_ID, owner: SA, isActive: true, encryptedBlob: claimHex() } } });
    await runOp('forget', 1, 1, async () => {
      await fetchFactForForget(SA, OWN_ID, RELAY, AUTH);
      writeCaps.push(encodeTombstoneProtobufKeyed(OWN_ID, SA));
    });
    // Dedup / contradiction supersede tombstones.
    await runOp('supersede tombstone', 0, 1, async () => { writeCaps.push(encodeTombstoneProtobufKeyed(SEEDED_ID, SA)); });

    // Pin, unpin, retype, set_scope: one fact this process wrote (guard skipped), one seeded only in the mock.
    for (const [kind, id] of [['written here', OWN_ID], ['seeded', SEEDED_ID]] as const) {
      const ops: Array<[string, boolean, (d: PinOpDeps & MetadataOpDeps) => Promise<unknown>]> = [
        ['pin', false, (d) => executePinOperation(id, 'pinned', d)],
        ['unpin', true, (d) => executePinOperation(id, 'active', d, undefined, confirmed('unpin', id))],
        ['retype', false, (d) => executeRetype(id, 'preference', d)],
        ['set_scope', false, (d) => executeSetScope(id, 'personal', d)],
      ];
      for (const [name, pinned, run] of ops) {
        const cache = new RecentWrites();
        if (id === OWN_ID) recordOwn(cache, pinned);
        const deps = pinHarness(phrase, cache, pinned);
        await runOp(`${name} (${kind})`, 1, 1, async () => { const r = (await run(deps)) as { success: boolean }; expect(r.success).toBe(true); });
      }
    }
  });

  test('every op left its minimum captures (so an empty capture cannot pass)', () => {
    expect(results.length).toBeGreaterThanOrEqual(16);
    for (const r of results) {
      expect([r.op, r.subs.length >= r.minSub]).toEqual([r.op, true]);
      expect([r.op, r.writes.length >= r.minWrite]).toEqual([r.op, true]);
    }
    // by design: no subgraph request for debrief
    for (const r of results.filter((x) => x.op.startsWith('debrief'))) expect(r.subs).toHaveLength(0);
  });

  test('1. no banned unkeyed value in any captured request body or write payload', () => {
    const everything = results.flatMap((r) => [...r.subs.map((s) => s.body), ...r.writes]);
    expect(everything.length).toBeGreaterThan(50);
    const hits = everything.flatMap((b) => scan(b));
    expect(hits).toEqual([]);
  });

  test('2. every subgraph request carries X-TotalReclaw-Index-Scheme: 1', () => {
    const subs = results.flatMap((r) => r.subs);
    expect(subs.length).toBeGreaterThan(20);
    for (const s of subs) expect(s.headers['X-TotalReclaw-Index-Scheme']).toBe('1');
  });

  test('3. every write payload: field 14 = 1 once and last, version 4, tags strictly ascending, tombstones in scheme-1 layout', () => {
    const writes = results.flatMap((r) => r.writes);
    expect(writes.length).toBeGreaterThan(30);
    let tombstones = 0, facts = 0;
    for (const w of writes) {
      const fields = parse(w);
      expect(fields[fields.length - 1]).toMatchObject({ n: 14, value: 1 });
      expect(fields.filter((f) => f.n === 14)).toHaveLength(1);
      expect(fields.find((f) => f.n === 8)!.value).toBe(4);
      const tags = fields.filter((f) => f.n === 5).map((f) => f.bytes!.toString('utf8'));
      for (let i = 1; i < tags.length; i++) expect(tags[i] > tags[i - 1]).toBe(true);
      expect(tags.every((t) => /^[0-9a-f]{64}$/.test(t))).toBe(true);
      expect(fields.some((f) => f.n === 9 || f.n === 11)).toBe(false);
      const isTombstone = fields.find((f) => f.n === 4)!.bytes!.length === 0;
      if (isTombstone) {
        tombstones++;
        expect(fields.find((f) => f.n === 6)!.bytes!.readDoubleLE(0)).toBe(0);
        expect(fields.find((f) => f.n === 7)!.value).toBe(0);
        expect(tags).toHaveLength(0);
      } else {
        facts++;
        expect(fields.find((f) => f.n === 7)!.value).toBe(1);
      }
    }
    expect(tombstones).toBeGreaterThanOrEqual(10); // forget, supersede, 8 pin/unpin/retype/set_scope
    expect(facts).toBeGreaterThanOrEqual(20);
  });

  test('4. only the guard query body contains indexScheme', () => {
    const subs = results.flatMap((r) => r.subs);
    const withScheme = subs.filter((s) => s.body.includes('indexScheme'));
    expect(withScheme.length).toBeGreaterThanOrEqual(4); // pin/unpin/retype/set_scope on the seeded fact
    expect(withScheme.every((s) => s.body.includes('FactIndexScheme'))).toBe(true);
    // the fact written by this process never triggered a guard query
    const ownGuard = results.filter((r) => r.op.endsWith('(written here)')).flatMap((r) => r.subs).filter((s) => s.body.includes('FactIndexScheme'));
    expect(ownGuard).toHaveLength(0);
  });

  test('5. phrase path and bundle path write the same tag set for the same text', () => {
    expect(rememberTagSets.phrase.length).toBe(TEXTS.length);
    expect(rememberTagSets.bundle).toEqual(rememberTagSets.phrase);
  });
});

describe('positive controls (separate captures, same pipeline, same scan helper)', () => {
  const controlSubs: SubCap[] = [];
  const controlWrites: Buffer[] = [];
  const legacyTag = fx.legacy_sha256.stems[0] as string;

  test('raw form: a legacy value sent as a trapdoor through the real read path is reported as raw', async () => {
    answer = () => ({ data: { blindIndexes: [], facts: [] } });
    installFetch(() => controlSubs);
    await searchSubgraph(SA, [legacyTag], 10, RELAY, AUTH);
    expect(controlSubs.length).toBeGreaterThan(0);
    const hits = controlSubs.flatMap((c) => scan(c.body));
    expect(hits).toContainEqual([legacyTag, 'raw']);
  });

  test('hex form: a payload built by the legacy core encoder through the capturing submit seam is reported as hex, not raw', async () => {
    const legacyPayload = core.encodeFactProtobuf(JSON.stringify({
      id: 'control', timestamp: '2026-11-25T12:00:00.000Z', owner: SA, encrypted_blob_hex: '00ff',
      blind_indices: [legacyTag], decay_score: 1, source: 's', content_fp: '', agent_id: 'a', encrypted_embedding: null, version: 4,
    }));
    await submit(() => controlWrites)([Buffer.from(legacyPayload)]);
    const hits = controlWrites.flatMap((w) => scan(w));
    expect(hits).toContainEqual([legacyTag, 'hex']);
    expect(hits).toContainEqual([legacyTag, 'raw']); // a protobuf carries the tag as plain ASCII bytes too
  });

  test('the scan reports hex-encoded calldata for a hex string body (UserOp JSON-RPC shape)', () => {
    const calldata = '0x' + Buffer.from(legacyTag, 'ascii').toString('hex');
    expect(scan(JSON.stringify({ params: [{ callData: calldata }] }))).toContainEqual([legacyTag, 'hex']);
  });

  test('a keyed value is never reported', () => {
    const t = makeTagger('phrase').factTags(TEXTS[0], embeddingFor(TEXTS[0]), []);
    expect(scan(JSON.stringify({ trapdoors: t }))).toEqual([]);
  });
});

// ── protobuf walker ──────────────────────────────────────────────────────────

interface Field { n: number; value?: number; bytes?: Buffer }
function parse(buf: Buffer): Field[] {
  const out: Field[] = [];
  let i = 0;
  const varint = (): number => { let v = 0, s = 0; for (;;) { const b = buf[i++]; v += (b & 0x7f) * 2 ** s; if (!(b & 0x80)) return v; s += 7; } };
  while (i < buf.length) {
    const key = varint();
    const n = key >> 3, wire = key & 7;
    if (wire === 0) out.push({ n, value: varint() });
    else if (wire === 1) { out.push({ n, bytes: buf.subarray(i, i + 8) }); i += 8; }
    else if (wire === 2) { const len = varint(); out.push({ n, bytes: buf.subarray(i, i + len) }); i += len; }
    else throw new Error(`wire ${wire}`);
  }
  return out;
}
