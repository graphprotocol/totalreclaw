/**
 * Shared loader for the frozen keyed-blind-index vectors
 * (`tests/parity/fixtures/keyed-blind-indices-v1.json`, internal#573).
 *
 * Keys come from the fixture's `meta` (the public test mnemonic's derived
 * vault keys). They are never copied into a source file, logged or printed.
 */

import fs from 'node:fs';
import path from 'node:path';
import { KeyedTagger } from '../../src/subgraph/keyed-tags';
import { LSHHasher } from '../../src/subgraph/lsh';

export const KEYED_FIXTURE_PATH = path.join(
  __dirname, '..', '..', '..', 'tests', 'parity', 'fixtures', 'keyed-blind-indices-v1.json',
);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const fx: any = JSON.parse(fs.readFileSync(KEYED_FIXTURE_PATH, 'utf8'));

export const encryptionKey = Buffer.from(fx.meta.encryption_key_hex, 'hex');
export const dedupKey = Buffer.from(fx.meta.dedup_key_hex, 'hex');
export const lshSeed = Buffer.from(fx.meta.lsh_seed_hex, 'hex');

/** The fixture's LSH parameters (dims 8, 20 tables, 32 bits). */
export function makeFixtureLsh(): LSHHasher {
  return new LSHHasher(lshSeed, fx.lsh.dims, fx.lsh.n_tables, fx.lsh.n_bits);
}

export function makeFixtureTagger(lsh: LSHHasher = makeFixtureLsh()): KeyedTagger {
  return new KeyedTagger(encryptionKey, dedupKey, lsh);
}

/** A valid keyed tag list for harness stubs that need `generateIndices` to return something encodable. */
export const MOCK_KEYED_TAGS: string[] = makeFixtureTagger().factTags('mock text', undefined, []);
