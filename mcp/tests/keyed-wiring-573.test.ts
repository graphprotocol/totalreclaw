/**
 * internal#573 (M3): the call sites use the keyed tagger.
 *
 * `index.ts` boots a stdio server on import, so the handlers cannot be driven
 * directly. The seams they use are: `KeyedTagger` (tags, trapdoors, fp),
 * `buildFactPayload`, the real `searchSubgraph` batching, and
 * `collectCandidatesForEntities`. The source checks pin that index.ts calls
 * those seams and nothing else. The end-to-end wire scan is
 * `wire-no-unkeyed-573.test.ts`.
 */

import fs from 'node:fs';
import path from 'node:path';
import { searchSubgraph } from '../src/subgraph/search';
import { buildFactPayload } from '../src/subgraph/keyed-tags';
import { collectCandidatesForEntities } from '../src/contradiction-sync';
import { encodeFactProtobufKeyed } from '../src/subgraph/store';
import { fx, makeFixtureLsh, makeFixtureTagger, encryptionKey } from './helpers/keyed-fixture';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const core = require('@totalreclaw/core') as typeof import('@totalreclaw/core');

const RELAY = 'https://relay.test';
const SA = '0xAbC0000000000000000000000000000000000001';
const EMB: number[] = fx.lsh.embedding;

let bodies: Array<{ query: string; variables: Record<string, unknown> }> = [];
const realFetch = globalThis.fetch;
beforeEach(() => {
  bodies = [];
  delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  globalThis.fetch = (async (_url: unknown, init: { body?: string }) => {
    bodies.push(JSON.parse(init?.body ?? '{}'));
    return new Response(JSON.stringify({ data: { blindIndexes: [], facts: [] } }), { status: 200 });
  }) as typeof fetch;
});
afterEach(() => { globalThis.fetch = realFetch; });

const sentTrapdoors = () =>
  bodies.filter((b) => b.query.includes('SearchByBlindIndex')).flatMap((b) => b.variables.trapdoors as string[]);

describe('recall (M-9)', () => {
  const lsh = makeFixtureLsh();
  const tagger = makeFixtureTagger(lsh);
  const query = 'prefers dark mode';

  test('queryTrapdoors equals core generateSearchTrapdoorsKeyed for the same inputs', () => {
    const indexer = new core.WasmBlindIndexer(encryptionKey.toString('hex'));
    const wasmLsh = core.WasmLshHasher.withParams(fx.meta.lsh_seed_hex, fx.lsh.dims, fx.lsh.n_tables, fx.lsh.n_bits);
    const expected = core.generateSearchTrapdoorsKeyed(query, [], new Float32Array(EMB), indexer, wasmLsh);
    expect(tagger.queryTrapdoors(query, EMB, [])).toEqual(expected);
  });

  test('searchSubgraph sends the trapdoor list in order, in batches of 5, unfiltered', async () => {
    const trapdoors = tagger.queryTrapdoors(query, EMB, []);
    await searchSubgraph(SA, trapdoors, 10, RELAY, 'ab'.repeat(32));
    const batches = bodies.filter((b) => b.query.includes('SearchByBlindIndex')).map((b) => b.variables.trapdoors as string[]);
    expect(batches.every((b) => b.length <= 5)).toBe(true);
    expect(batches.length).toBe(Math.ceil(trapdoors.length / 5));
    expect(batches.flat()).toEqual(trapdoors);
  });
});

describe('store-time dedup (M-3)', () => {
  test('the dedup search sends exactly the sorted write tags', async () => {
    const tagger = makeFixtureTagger();
    const text = 'User prefers PostgreSQL over MySQL at Acme in 2024';
    const writeTags = tagger.factTags(text, EMB, []);
    expect(writeTags).toEqual([...writeTags].sort());
    expect(new Set(writeTags).size).toBe(writeTags.length);
    await searchSubgraph(SA, writeTags, 10, RELAY, 'ab'.repeat(32));
    expect(sentTrapdoors()).toEqual(writeTags);
  });
});

describe('buildFactPayload (M-5, M-6)', () => {
  test('tags are written as given, the fingerprint is fp v2, decay is live, no field 9/11', () => {
    const tagger = makeFixtureTagger();
    const text = fx.fp_v2[0].text;
    const tags = tagger.factTags(text, EMB, []);
    const p = buildFactPayload({
      tagger, id: 'f-1', owner: SA, source: 'mcp_remember', text,
      encryptedBlobBase64: Buffer.from('00ff11', 'hex').toString('base64'),
      encryptedEmbedding: 'ZW1i', tags,
    });
    expect(p.blindIndices).toBe(tags);
    expect(p.contentFp).toBe(fx.fp_v2[0].fp);
    expect(p.decayScore).toBe(1);
    expect(p.encryptedBlob).toBe('00ff11');
    expect(() => encodeFactProtobufKeyed(p)).not.toThrow();
  });
});

describe('contradiction lookup (M-13)', () => {
  test('queries the keyed entity tag, one per entity', async () => {
    const tagger = makeFixtureTagger();
    const asked: string[][] = [];
    const out = await collectCandidatesForEntities(
      { t: 'x', c: 'fact', cf: 1, i: 5, sa: 'a', ea: '2026-01-01T00:00:00Z', e: [{ n: 'PostgreSQL', tp: 'tool' }, { n: 'Acme', tp: 'org' }] } as never,
      'new-id', SA, 'ab'.repeat(32), encryptionKey,
      {
        searchSubgraph: async (_o, trapdoors) => { asked.push(trapdoors); return []; },
        decryptFromHex: () => '',
        entityTag: (n) => tagger.entityTag(n),
      },
      { info: () => {}, warn: () => {} },
    );
    expect(out).toEqual([]);
    expect(asked).toEqual([[tagger.entityTag('PostgreSQL')], [tagger.entityTag('Acme')]]);
    const legacy = new Set<string>(fx.legacy_sha256.entities);
    expect(asked.flat().filter((t) => legacy.has(t))).toEqual([]);
  });
});

describe('index.ts call sites (source check)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.ts'), 'utf8');
  test('remember builds one tag list and reuses it for the dedup search and the payload', () => {
    expect(src).toContain('const allIndices = state.tagger.factTags(item.text, embedding, []);');
    const dedup = src.slice(src.indexOf('Store-time dedup: search for near-duplicates'));
    expect(dedup.slice(0, 600)).toContain('allIndices,');
    expect(src).toContain('tags: allIndices,');
  });
  test('recall uses the keyed trapdoors as returned', () => {
    expect(src).toContain('state.tagger.queryTrapdoors(query.trim(), queryEmbedding, [])');
  });
  test('pin / retype tags come from the tagger with the entity names', () => {
    expect(src).toContain('state.tagger.factTags(text, embedding, entityNames)');
  });
  test('tombstones use the keyed tombstone encoder at every site', () => {
    expect((src.match(/encodeTombstoneProtobufKeyed\(/g) ?? []).length).toBe(3); // dedup, contradiction, forget
  });
  test('the tagger is built in both init paths', () => {
    expect(src).toContain('new KeyedTagger(encryptionKey, dedupKey, lshHasher)');
    expect(src).toContain('new KeyedTagger(vault.encryptionKey, vault.dedupKey, lshHasher)');
  });
});
