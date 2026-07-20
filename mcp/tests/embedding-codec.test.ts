/**
 * Universal embedding codec — MCP-side cross-client format parity
 * (internal#479 Part B).
 *
 * Locks the local UNIVERSAL decoder against the golden vectors committed by
 * Part A (`rust/totalreclaw-core/tests/fixtures/embedding_codec_vectors.json`),
 * so the MCP server reads every format ever written — canonical f16, legacy
 * JSON array (TS plugin), and legacy f32 binary (old Python / the MCP write
 * itself). Companion to `skill/plugin/embedding/embedding-codec.test.ts`.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import {
  decodeEmbeddingLocal,
  decodeEmbeddingUniversal,
  decodeEmbeddingWithCore,
  encodeEmbeddingPayload,
  encodeEmbeddingPayloadWithCore,
  LEGACY_F32_MARKER,
  EMBEDDING_DIMS,
  type CoreEmbeddingCodec,
} from '../src/embedding-codec';
import { encrypt, decrypt } from '../src/subgraph/crypto';

const FIXTURE_PATH = resolve(
  __dirname,
  '../../rust/totalreclaw-core/tests/fixtures/embedding_codec_vectors.json',
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

// 32-byte test key — XChaCha20-Poly1305 only needs key bytes.
const TEST_KEY = Buffer.alloc(32, 0x42);

function cosine(a: number[], b: number[]): number {
  expect(a.length).toBe(b.length);
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

/** Spy that records the vector passed to encode and returns a sentinel. Throws
 * on non-finite input to model core's fail-closed validation. */
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

describe('embedding-codec local universal decoder (golden vectors)', () => {
  it('decodes an f16 payload to cosine >= 0.9999 vs the fixture vector', () => {
    const decoded = decodeEmbeddingLocal(FIXTURE.canonical_f16_base64);
    expect(decoded.length).toBe(EMBEDDING_DIMS);
    expect(cosine(decoded, FIXTURE.unit_vector)).toBeGreaterThanOrEqual(0.9999);
  });

  it('decodes a JSON-array payload exactly', () => {
    const decoded = decodeEmbeddingLocal(FIXTURE.legacy_json_640);
    expect(decoded).toEqual(FIXTURE.unit_vector);
  });

  it('decodes an f32-binary payload exactly', () => {
    const decoded = decodeEmbeddingLocal(FIXTURE.legacy_f32_base64_640);
    expect(decoded).toEqual(FIXTURE.unit_vector);
  });

  it('decodes a non-canonical-dim f32 payload exactly', () => {
    const decoded = decodeEmbeddingLocal(FIXTURE.non_canonical.f32_base64);
    expect(decoded).toEqual(FIXTURE.non_canonical.vector);
  });

  it('trims whitespace then parses JSON', () => {
    expect(decodeEmbeddingLocal('   [1.5, 2.5, -3.25]\n')).toEqual([1.5, 2.5, -3.25]);
  });

  it('decodes an empty JSON array to an empty vector', () => {
    expect(decodeEmbeddingLocal('[]')).toEqual([]);
  });

  it('throws on a buffer of bad length (not 1280, not %4)', () => {
    const oneByte = Buffer.from([0xaa]).toString('base64');
    expect(() => decodeEmbeddingLocal(oneByte)).toThrow(/length|invalid/i);
  });

  it('throws on malformed JSON', () => {
    expect(() => decodeEmbeddingLocal('[1.0, not-json')).toThrow(/json|parse/i);
  });

  it('throws on invalid base64', () => {
    expect(() => decodeEmbeddingLocal('!!!! not base64 &&&')).toThrow();
  });
});

describe('embedding-codec write-path selection', () => {
  it('core-absent returns the legacy-f32 marker (MCP packs f32 itself)', () => {
    expect(encodeEmbeddingPayloadWithCore(FIXTURE.unit_vector, null)).toBe(LEGACY_F32_MARKER);
  });

  it('core-present delegates to encodeEmbeddingCanonical', () => {
    const spy = makeSpyCore();
    const payload = encodeEmbeddingPayloadWithCore(FIXTURE.unit_vector, spy);
    expect(payload).toBe('CORE-CANONICAL-PAYLOAD');
    expect(spy.seen).toEqual(FIXTURE.unit_vector);
  });

  it('core-present encode aborts (throws) on NaN — fail-closed', () => {
    const spy = makeSpyCore();
    const bad = FIXTURE.unit_vector.slice();
    bad[7] = NaN;
    expect(() => encodeEmbeddingPayloadWithCore(bad, spy)).toThrow(/fail-closed|finite/i);
  });

  it('default write-path returns the legacy marker when core codec unavailable', () => {
    // Published core (2.5.x) predates the codec -> marker, not canonical.
    expect(encodeEmbeddingPayload(FIXTURE.unit_vector)).toBe(LEGACY_F32_MARKER);
  });
});

describe('embedding-codec decode routing', () => {
  it('decodeEmbeddingWithCore delegates to the core binding when present', () => {
    const spy = makeSpyCore();
    expect(decodeEmbeddingWithCore('AAAA', spy)).toEqual([0.5, 0.25]);
  });

  it('decodeEmbeddingWithCore falls back to local decode when core absent', () => {
    expect(decodeEmbeddingWithCore(FIXTURE.legacy_json_640, null)).toEqual(FIXTURE.unit_vector);
  });

  it('decodeEmbeddingUniversal (default) reads the f16 fixture payload', () => {
    const out = decodeEmbeddingUniversal(FIXTURE.canonical_f16_base64);
    expect(out.length).toBe(EMBEDDING_DIMS);
    expect(cosine(out, FIXTURE.unit_vector)).toBeGreaterThanOrEqual(0.9999);
  });
});

describe('embedding-codec cross-format recall path (MCP encrypt/decrypt)', () => {
  // The exact composition index.ts decryptEmbedding uses:
  //   decodeEmbeddingUniversal(decrypt(ciphertext, key)).
  it('decodes an f16 (foreign-format) fact at cosine >= 0.9999', () => {
    const ciphertext = encrypt(FIXTURE.canonical_f16_base64, TEST_KEY);
    const decoded = decodeEmbeddingUniversal(decrypt(ciphertext, TEST_KEY));
    expect(decoded.length).toBe(EMBEDDING_DIMS);
    expect(cosine(decoded, FIXTURE.unit_vector)).toBeGreaterThanOrEqual(0.9999);
  });

  it('decodes a legacy JSON (TS-plugin) fact exactly', () => {
    const ciphertext = encrypt(FIXTURE.legacy_json_640, TEST_KEY);
    const decoded = decodeEmbeddingUniversal(decrypt(ciphertext, TEST_KEY));
    expect(decoded).toEqual(FIXTURE.unit_vector);
  });

  it('decodes a legacy f32 (MCP/Python) fact exactly', () => {
    const ciphertext = encrypt(FIXTURE.legacy_f32_base64_640, TEST_KEY);
    const decoded = decodeEmbeddingUniversal(decrypt(ciphertext, TEST_KEY));
    expect(decoded).toEqual(FIXTURE.unit_vector);
  });
});

describe('strict base64 (coordinator review)', () => {
  it('rejects non-base64 garbage instead of silently decoding', () => {
    // Buffer.from(s,'base64') is lax (skips invalid chars); the local decoder
    // must match core/Python's strict rejection so a corrupted payload can
    // never decode to a plausible-length garbage vector.
    expect(() => decodeEmbeddingLocal('!!not base64 at all!!')).toThrow();
    expect(() => decodeEmbeddingLocal('QUJD$Q==')).toThrow();
  });
});
