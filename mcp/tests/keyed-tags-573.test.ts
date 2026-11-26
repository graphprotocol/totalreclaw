/**
 * internal#573 (M1): the MCP keyed-tag module against the frozen vectors.
 *
 * Everything goes through the client's own module (`KeyedTagger`, `LSHHasher`),
 * never through core directly. Keys and the public test mnemonic come from the
 * fixture; nothing is printed.
 */

import { KeyedTagger } from '../src/subgraph/keyed-tags';
import { LSHHasher } from '../src/subgraph/lsh';
import { deriveKeys, deriveLshSeed } from '../src/subgraph/crypto';
import { bundleVaultToBuffers, type DerivedBundleV1 } from '../src/subgraph/bundle';
import { fx, makeFixtureLsh, makeFixtureTagger, encryptionKey, dedupKey, lshSeed } from './helpers/keyed-fixture';

const HEX64 = /^[0-9a-f]{64}$/;
const sorted = (a: string[]) => [...a].sort();

describe('keys reach the tagger without client-side derivation', () => {
  test('phrase path: deriveKeys + deriveLshSeed equal the fixture keys', () => {
    const { encryptionKey: ek, dedupKey: dk, salt } = deriveKeys(fx.meta.mnemonic);
    expect(ek.toString('hex')).toBe(fx.meta.encryption_key_hex);
    expect(dk.toString('hex')).toBe(fx.meta.dedup_key_hex);
    expect(Buffer.from(deriveLshSeed(fx.meta.mnemonic, salt)).toString('hex')).toBe(fx.meta.lsh_seed_hex);
  });
});

describe('stem tags', () => {
  const tagger = makeFixtureTagger();
  for (const v of fx.stem_vectors) {
    test(`factTags(text) equals the frozen tags: ${v.name}`, () => {
      expect(tagger.factTags(v.text)).toEqual(v.tags);
    });
  }
});

describe('entity tags', () => {
  const tagger = makeFixtureTagger();
  for (const v of fx.entity_vectors) {
    test(`entityTag(raw name) equals the frozen tag: ${v.name}`, () => {
      expect(tagger.entityTag(v.name)).toBe(v.tag);
    });
  }
  test('factTags adds entity tags into the one sorted list', () => {
    const names = fx.entity_vectors.map((v: { name: string }) => v.name);
    const tags = tagger.factTags('prefers dark mode', undefined, names);
    expect(tags).toEqual(sorted([...new Set<string>([
      ...tagger.factTags('prefers dark mode'),
      ...fx.entity_vectors.map((v: { tag: string }) => v.tag),
    ])]));
  });
});

describe('keyed LSH', () => {
  const lsh = makeFixtureLsh();
  const tagger = makeFixtureTagger(lsh);
  test('hashKeyed equals the frozen keyed tags, in table order', () => {
    expect(lsh.hashKeyed(fx.lsh.embedding)).toEqual(fx.lsh.keyed_tags);
  });
  test('factTags with an embedding merges keyed_tags_sorted into the sorted list', () => {
    const text = 'prefers dark mode';
    const merged = sorted([...new Set<string>([...tagger.factTags(text), ...fx.lsh.keyed_tags_sorted])]);
    expect(tagger.factTags(text, fx.lsh.embedding)).toEqual(merged);
  });
  test('LSHHasher has no unkeyed hash method left', () => {
    expect((lsh as unknown as Record<string, unknown>).hash).toBeUndefined();
    expect(typeof lsh.hashKeyed).toBe('function');
  });
});

describe('content fingerprint v2', () => {
  const tagger = makeFixtureTagger();
  for (const v of fx.fp_v2) {
    test(`contentFp equals the frozen fp: ${JSON.stringify(v.text).slice(0, 30)}`, () => {
      expect(tagger.contentFp(v.text)).toBe(v.fp);
    });
  }
});

describe('query trapdoors', () => {
  const lsh = makeFixtureLsh();
  const tagger = makeFixtureTagger(lsh);
  const names = ['PostgreSQL', 'Café'];
  const query = 'prefers dark mode';

  test('equal the sorted union of stem, entity and keyed LSH tags', () => {
    const expected = sorted([...new Set<string>([
      ...tagger.factTags(query),
      ...names.map((n) => tagger.entityTag(n)),
      ...fx.lsh.keyed_tags,
    ])]);
    expect(tagger.queryTrapdoors(query, fx.lsh.embedding, names)).toEqual(expected);
  });

  test('are 64-hex, sorted, and share nothing with the legacy unkeyed values', () => {
    const t = tagger.queryTrapdoors(query, fx.lsh.embedding, names);
    expect(t.every((x) => HEX64.test(x))).toBe(true);
    expect(t).toEqual(sorted(t));
    const banned = new Set<string>([
      ...fx.legacy_sha256.tokens, ...fx.legacy_sha256.stems, ...fx.legacy_sha256.entities,
      fx.legacy_sha256.marker, ...fx.legacy_sha256.lsh,
    ]);
    expect(t.filter((x) => banned.has(x))).toEqual([]);
  });
});

describe('phrase path and bundle path produce identical tags', () => {
  const phrase = deriveKeys(fx.meta.mnemonic);
  const phraseTagger = new KeyedTagger(
    phrase.encryptionKey, phrase.dedupKey,
    new LSHHasher(deriveLshSeed(fx.meta.mnemonic, phrase.salt), fx.lsh.dims, fx.lsh.n_tables, fx.lsh.n_bits),
  );
  const bundle = {
    vault: {
      encryption_key: fx.meta.encryption_key_hex,
      dedup_key: fx.meta.dedup_key_hex,
      auth_key: '00'.repeat(32),
      lsh_seed: fx.meta.lsh_seed_hex,
    },
  } as unknown as DerivedBundleV1;
  const vault = bundleVaultToBuffers(bundle);
  const bundleTagger = new KeyedTagger(
    vault.encryptionKey, vault.dedupKey,
    new LSHHasher(vault.lshSeed, fx.lsh.dims, fx.lsh.n_tables, fx.lsh.n_bits),
  );

  test('fact tags, trapdoors, entity tag and fingerprint match', () => {
    const text = 'User prefers PostgreSQL over MySQL at Acme in 2024';
    expect(bundleTagger.factTags(text, fx.lsh.embedding, ['Acme'])).toEqual(phraseTagger.factTags(text, fx.lsh.embedding, ['Acme']));
    expect(bundleTagger.queryTrapdoors(text, fx.lsh.embedding, ['Acme'])).toEqual(phraseTagger.queryTrapdoors(text, fx.lsh.embedding, ['Acme']));
    expect(bundleTagger.entityTag('Acme')).toBe(phraseTagger.entityTag('Acme'));
    expect(bundleTagger.contentFp(text)).toBe(phraseTagger.contentFp(text));
  });

  test('stem vectors match the frozen tags through the bundle path', () => {
    for (const v of fx.stem_vectors) expect(bundleTagger.factTags(v.text)).toEqual(v.tags);
  });
});

describe('nothing produced equals a legacy unkeyed value', () => {
  const tagger = makeFixtureTagger();
  test('across every stem vector text, entity name and the LSH embedding', () => {
    const banned = new Set<string>([
      ...fx.legacy_sha256.tokens, ...fx.legacy_sha256.stems, ...fx.legacy_sha256.entities,
      fx.legacy_sha256.marker, ...fx.legacy_sha256.lsh,
    ]);
    const produced: string[] = [];
    for (const v of fx.stem_vectors) produced.push(...tagger.factTags(v.text, fx.lsh.embedding, fx.entity_vectors.map((e: { name: string }) => e.name)));
    expect(produced.length).toBeGreaterThan(0);
    expect(produced.filter((x) => banned.has(x))).toEqual([]);
  });
});

describe('the tagger leaks no key material', () => {
  test('JSON.stringify and inspect show no key or hasher state', () => {
    const tagger = new KeyedTagger(encryptionKey, dedupKey, new LSHHasher(lshSeed, fx.lsh.dims));
    expect(JSON.stringify(tagger)).toBe('{}');
    expect(Object.keys(tagger)).toEqual([]);
    const shown = require('node:util').inspect(tagger, { showHidden: true, depth: 4 });
    expect(shown).not.toContain(fx.meta.encryption_key_hex);
    expect(shown).not.toContain(fx.meta.dedup_key_hex);
  });
});
