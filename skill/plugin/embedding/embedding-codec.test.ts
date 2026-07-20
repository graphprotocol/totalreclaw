/**
 * Universal embedding codec — cross-client format parity (internal#479 Part B).
 *
 * Locks the local UNIVERSAL decoder against the golden vectors committed by
 * Part A (`rust/totalreclaw-core/tests/fixtures/embedding_codec_vectors.json`),
 * so the plugin reads every format ever written — canonical f16 (Hermes /
 * core), legacy JSON array (this plugin), and legacy f32 binary (old Python /
 * MCP). Before this, a plugin vault that ingested a foreign-format fact
 * silently degraded to word-index matching for that fact.
 *
 * The local fallback is the actual parity fix; the core-binding preference
 * (exercised via the injected `CoreEmbeddingCodec` seam) is byte-exact parity
 * with core when the installed `@totalreclaw/core` exposes the codec.
 *
 * Run with: npx tsx embedding-codec.test.ts
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  decodeEmbeddingLocal,
  decodeEmbeddingUniversal,
  decodeEmbeddingWithCore,
  encodeEmbeddingPayload,
  encodeEmbeddingPayloadWithCore,
  decodeEmbeddingFromHex,
  EMBEDDING_DIMS,
  type CoreEmbeddingCodec,
} from './embedding-codec.js';
import { encryptToHex } from '../runtime/format-helpers.js';

const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../rust/totalreclaw-core/tests/fixtures/embedding_codec_vectors.json',
);

interface Fixture {
  dims: number;
  unit_vector: number[];
  canonical_f16_base64: string;
  legacy_json_640: string;
  legacy_f32_base64_640: string;
  non_canonical: { dims: number; vector: number[]; f32_base64: string };
}

const FIXTURE = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as Fixture;

// 32-byte test key — XChaCha20-Poly1305 only needs key bytes; value is
// arbitrary as long as encrypt + decrypt use the same key.
const TEST_KEY = Buffer.alloc(32, 0x42);

function cosine(a: number[], b: number[]): number {
  assert.equal(a.length, b.length, 'cosine: length mismatch');
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

let passed = 0;
function check(name: string, fn: () => void): void {
  fn();
  passed++;
  console.log(`ok ${passed} - ${name}`);
}

// ---------------------------------------------------------------------------
// Local universal decoder — the actual parity fix (golden vectors).
// ---------------------------------------------------------------------------

check('f16 payload decodes to cosine >= 0.9999 vs fixture vector', () => {
  const decoded = decodeEmbeddingLocal(FIXTURE.canonical_f16_base64);
  assert.equal(decoded.length, EMBEDDING_DIMS);
  const cos = cosine(decoded, FIXTURE.unit_vector);
  assert.ok(cos >= 0.9999, `f16 round-trip cosine ${cos} below 0.9999`);
});

check('JSON-array payload decodes exactly to fixture vector', () => {
  const decoded = decodeEmbeddingLocal(FIXTURE.legacy_json_640);
  assert.equal(decoded.length, EMBEDDING_DIMS);
  assert.deepEqual(decoded, FIXTURE.unit_vector);
});

check('f32-binary payload decodes exactly to fixture vector', () => {
  const decoded = decodeEmbeddingLocal(FIXTURE.legacy_f32_base64_640);
  assert.equal(decoded.length, EMBEDDING_DIMS);
  assert.deepEqual(decoded, FIXTURE.unit_vector);
});

check('non-canonical dim f32 payload decodes exactly', () => {
  const decoded = decodeEmbeddingLocal(FIXTURE.non_canonical.f32_base64);
  assert.equal(decoded.length, FIXTURE.non_canonical.dims);
  assert.deepEqual(decoded, FIXTURE.non_canonical.vector);
});

check('JSON path ignores leading/trailing whitespace', () => {
  assert.deepEqual(decodeEmbeddingLocal('   [1.5, 2.5, -3.25]\n'), [1.5, 2.5, -3.25]);
});

check('empty JSON array decodes to empty vector', () => {
  assert.deepEqual(decodeEmbeddingLocal('[]'), []);
});

check('buffer of bad length (not 1280, not %4) throws', () => {
  // 1 byte base64 -> neither f16(1280) nor f32(%4).
  const oneByte = Buffer.from([0xaa]).toString('base64');
  assert.throws(() => decodeEmbeddingLocal(oneByte), /length|invalid/i);
});

check('malformed JSON array throws', () => {
  assert.throws(() => decodeEmbeddingLocal('[1.0, not-json'), /json|parse/i);
});

check('invalid base64 throws', () => {
  assert.throws(() => decodeEmbeddingLocal('!!!! not base64 &&&'));
});

// ---------------------------------------------------------------------------
// Write-path selection (core-present -> canonical; core-absent -> JSON).
// The core binding is injected, so no f16 reimplementation lives in the test.
// ---------------------------------------------------------------------------

/** Spy that records the vector passed to encode and returns a sentinel string.
 * Simulates core: returns whatever it wants for valid input, throws on NaN to
 * model core's fail-closed validation. */
function makeSpyCore(): CoreEmbeddingCodec & { seen: number[] | null } {
  const spy: CoreEmbeddingCodec & { seen: number[] | null } = {
    seen: null,
    encodeEmbeddingCanonical(embedding: number[]): string {
      for (const v of embedding) {
        if (!Number.isFinite(v)) {
          throw new Error('embedding has non-finite component (fail-closed)');
        }
      }
      spy.seen = embedding.slice();
      return 'CORE-CANONICAL-PAYLOAD';
    },
    decodeEmbeddingUniversal(_payload: string): number[] {
      return [0.5, 0.25];
    },
  };
  return spy;
}

check('write-path: core-absent emits legacy JSON array', () => {
  const payload = encodeEmbeddingPayloadWithCore(FIXTURE.unit_vector, null);
  assert.ok(payload.trimStart().startsWith('['), 'legacy payload must be a JSON array');
  assert.deepEqual(JSON.parse(payload), FIXTURE.unit_vector);
});

check('write-path: core-present delegates to core.encodeEmbeddingCanonical', () => {
  const spy = makeSpyCore();
  const payload = encodeEmbeddingPayloadWithCore(FIXTURE.unit_vector, spy);
  assert.equal(payload, 'CORE-CANONICAL-PAYLOAD');
  assert.deepEqual(spy.seen, FIXTURE.unit_vector, 'core must receive the raw vector');
});

check('write-path: core-present encode aborts (throws) on NaN — fail-closed', () => {
  const spy = makeSpyCore();
  const bad = FIXTURE.unit_vector.slice();
  bad[7] = NaN;
  assert.throws(() => encodeEmbeddingPayloadWithCore(bad, spy), /fail-closed|finite/i);
});

check('default write-path falls back to JSON when core codec unavailable', () => {
  // Installed published core (2.5.6) predates the codec -> resolveCoreCodec()
  // returns null in this environment -> legacy JSON.
  const payload = encodeEmbeddingPayload(FIXTURE.unit_vector);
  assert.ok(payload.trimStart().startsWith('['));
});

// ---------------------------------------------------------------------------
// Decode prefers core binding when present.
// ---------------------------------------------------------------------------

check('decodeEmbeddingWithCore delegates to the core binding when present', () => {
  const spy = makeSpyCore();
  const out = decodeEmbeddingWithCore('AAAA', spy);
  assert.deepEqual(out, [0.5, 0.25], 'core decode result must pass through');
});

check('decodeEmbeddingWithCore falls back to local decode when core absent', () => {
  const out = decodeEmbeddingWithCore(FIXTURE.legacy_json_640, null);
  assert.deepEqual(out, FIXTURE.unit_vector);
});

check('decodeEmbeddingUniversal (default) reads the f16 fixture payload', () => {
  const out = decodeEmbeddingUniversal(FIXTURE.canonical_f16_base64);
  assert.equal(out.length, EMBEDDING_DIMS);
  assert.ok(cosine(out, FIXTURE.unit_vector) >= 0.9999);
});

// ---------------------------------------------------------------------------
// Cross-format integration: an f16 fact (fixture payload — what Hermes /
// canonical core writes) is decodable through the plugin's hex-wrapped recall
// read path. This is the parity bug's acceptance test: foreign-format facts
// no longer degrade to word-index matching.
// ---------------------------------------------------------------------------

check('recall read path decodes an f16 (foreign-format) fact at cosine >= 0.9999', () => {
  // Simulate a Hermes/core-written f16 fact landing in the plugin vault:
  // the on-chain blob is encryptToHex(canonical_f16_payload, key).
  const hexBlob = encryptToHex(FIXTURE.canonical_f16_base64, TEST_KEY);
  const decoded = decodeEmbeddingFromHex(hexBlob, TEST_KEY);
  assert.equal(decoded.length, EMBEDDING_DIMS);
  assert.ok(cosine(decoded, FIXTURE.unit_vector) >= 0.9999);
});

check('recall read path still decodes a legacy JSON fact exactly', () => {
  const hexBlob = encryptToHex(FIXTURE.legacy_json_640, TEST_KEY);
  const decoded = decodeEmbeddingFromHex(hexBlob, TEST_KEY);
  assert.deepEqual(decoded, FIXTURE.unit_vector);
});

console.log(`\n${passed} checks passed.`);

check('strict base64: non-base64 garbage throws instead of silently decoding', () => {
  // Buffer.from(s,'base64') is lax (skips invalid chars); the local decoder
  // must match core/Python's strict rejection so a corrupted payload can
  // never decode to a plausible-length garbage vector.
  assert.throws(() => decodeEmbeddingLocal('!!not base64 at all!!'));
  assert.throws(() => decodeEmbeddingLocal('QUJD$Q=='));
});
