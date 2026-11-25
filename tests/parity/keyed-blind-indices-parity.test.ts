/**
 * Keyed blind indices (index scheme 1, internal#573) — WASM/TS leg.
 *
 * Reads `fixtures/keyed-blind-indices-v1.json` (written by the independent
 * stdlib-Python generator `fixtures/generate-keyed-blind-indices-v1.py`) and
 * asserts that the WASM core reproduces every tag, fingerprint and wire byte
 * from the raw inputs. It also re-derives the three keys with node:crypto
 * (a second independent HKDF) and checks the fixture, then computes the tags
 * for the fixture's vectors with node:crypto HMAC and checks the WASM output.
 *
 * Siblings: rust/totalreclaw-core/tests/keyed_blind_indices.rs (Rust),
 * tests/parity/test_keyed_blind_indices_parity.py (PyO3).
 *
 * Run (needs rust/totalreclaw-core/pkg from
 * `wasm-pack build --target nodejs --out-dir pkg --features wasm`):
 *   cd tests/parity && npx tsx keyed-blind-indices-parity.test.ts
 */
import { createHash, createHmac, hkdfSync } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const wasm = require(join(__dirname, '..', '..', 'rust', 'totalreclaw-core', 'pkg', 'totalreclaw_core.js'));
const fx = JSON.parse(readFileSync(join(__dirname, 'fixtures', 'keyed-blind-indices-v1.json'), 'utf8'));

let passed = 0;
let failed = 0;
function check(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) { console.log(`ok ${n} - ${name}`); passed++; } else { console.log(`not ok ${n} - ${name}`); failed++; }
}
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const hex = (b: Uint8Array | Buffer) => Buffer.from(b).toString('hex');
const sha256 = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex');

// 1. Keys: node's HKDF (empty salt, label) reproduces the fixture keys.
const hk = (ikmHex: string, label: string) =>
  hex(Buffer.from(hkdfSync('sha256', Buffer.from(ikmHex, 'hex'), Buffer.alloc(0), label, 32)));
const indexKey = hk(fx.meta.encryption_key_hex, fx.meta.labels.index_key);
const lshKey = hk(fx.meta.lsh_seed_hex, fx.meta.labels.lsh_bucket_key);
const fpKey = hk(fx.meta.dedup_key_hex, fx.meta.labels.fp_key_v2);
check(indexKey === fx.keys.index_key_hex, 'indexKey (node HKDF) matches fixture');
check(lshKey === fx.keys.lsh_bucket_key_hex, 'lshBucketKey (node HKDF) matches fixture');
check(fpKey === fx.keys.fp_key_v2_hex, 'fpKeyV2 (node HKDF) matches fixture');
const hmac = (keyHex: string, msg: string) => createHmac('sha256', Buffer.from(keyHex, 'hex')).update(msg, 'utf8').digest('hex');

// 2. Stem tags.
const ix = new wasm.WasmBlindIndexer(fx.meta.encryption_key_hex);
const produced: string[] = [];
for (const v of fx.stem_vectors) {
  const got = ix.stemTags(v.text) as string[];
  check(same(got, v.tags), `stem tags: ${v.name}`);
  check(same(got, [...new Set(v.stem_inputs.map((s: string) => hmac(indexKey, 'stem:' + s)))].sort()), `stem tags (node HMAC): ${v.name}`);
  produced.push(...got);
}

// 3. Entities, marker, separation.
for (const v of fx.entity_vectors) {
  check(ix.entityTag(v.normalized) === v.tag, `entity tag: ${v.normalized}`);
  produced.push(ix.entityTag(v.normalized));
}
check(ix.entityTag('PostgreSQL') === ix.entityTag('  postgresql '), 'entity case/whitespace variants agree');
check(ix.entityTag('Postgre   SQL') === ix.entityTag('postgre sql'), 'entity inner whitespace collapses');
check(ix.entityTag('Café') === ix.entityTag('Café'), 'entity NFC == NFD');
check(ix.digestMarkerTag() === fx.marker.tag, 'digest marker tag');
produced.push(ix.digestMarkerTag());
check(new Set([fx.separation.stem, fx.separation.entity, fx.separation.type]).size === 3, 'domains separate');
check(ix.stemTags('digest')[0] === fx.separation.stem && ix.entityTag('digest') === fx.separation.entity, 'separation vectors');
const other = new wasm.WasmBlindIndexer(fx.meta.second_encryption_key_hex);
check(ix.stemTags('prefers dark mode').every((t: string) => !(other.stemTags('prefers dark mode') as string[]).includes(t)), 'two vaults share no tag');
check(same(wasm.sortDedupTags(['b', 'a', 'b']), ['a', 'b']), 'sortDedupTags');

// 4. LSH.
const l = fx.lsh;
const hasher = wasm.WasmLshHasher.withParams(fx.meta.lsh_seed_hex, l.dims, l.n_tables, l.n_bits);
const emb = Float64Array.from(l.embedding);
const keyed = hasher.hashKeyed(emb) as string[];
check(same(keyed, l.keyed_tags), 'LSH keyed tags (tables 0..19)');
check(same(keyed, l.bucket_ids.map((id: string) => hmac(lshKey, id))), 'LSH keyed tags (node HMAC over bucket ids)');
check(same(hasher.hash(emb), l.legacy_sha256_tags), 'legacy LSH hash() unchanged');
produced.push(...keyed);

// 5. contentFp v2.
for (const v of fx.fp_v2) {
  check(wasm.generateContentFingerprintV2(v.text, fx.meta.dedup_key_hex) === v.fp, `fp v2: ${JSON.stringify(v.text)}`);
}

// 6. Wire bytes.
const wf = fx.wire.fact;
check(hex(wasm.encodeFactProtobufKeyed(JSON.stringify(wf.input))) === wf.expected_hex, 'keyed fact protobuf bytes');
check(hex(wasm.encodeFactProtobuf(JSON.stringify(wf.input))) !== wf.expected_hex, 'legacy encoder output differs (no field 14, unsorted)');
const wt = fx.wire.tombstone;
const tomb = hex(wasm.encodeTombstoneProtobufKeyed(wt.fact_id, wt.owner, wt.version));
const mid = tomb.slice(wt.prefix_hex.length, tomb.length - wt.suffix_hex.length);
check(tomb.startsWith(wt.prefix_hex) && tomb.endsWith(wt.suffix_hex) && mid.startsWith('12'), 'keyed tombstone: prefix, timestamp field, suffix with field 14');
check(tomb.endsWith('7001'), 'keyed tombstone ends with field 14 = 1');

// 7. No keyed output equals a legacy sha256 value.
const banned = new Set<string>([...fx.legacy_sha256.tokens, ...fx.legacy_sha256.stems, ...fx.legacy_sha256.entities, ...fx.legacy_sha256.lsh, fx.legacy_sha256.marker]);
check(sha256('type:digest') === fx.legacy_sha256.marker, 'legacy marker value reproduced');
check(produced.length > 0 && produced.every((t) => /^[0-9a-f]{64}$/.test(t) && !banned.has(t)), 'no keyed tag equals a legacy sha256 value');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
