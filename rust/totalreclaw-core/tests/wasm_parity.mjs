/**
 * WASM parity tests for @totalreclaw/core.
 *
 * Verifies that the WASM bindings produce byte-for-byte identical output
 * to the TypeScript and Rust native implementations, using the shared
 * crypto_vectors.json test fixtures.
 *
 * Run: node tests/wasm_parity.mjs
 * (from rust/totalreclaw-core/)
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Load the WASM module (Node.js CJS output from wasm-pack)
const wasm = await import(join(__dirname, '..', 'pkg', 'totalreclaw_core.js'));

// Load test vectors
const vectors = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'crypto_vectors.json'), 'utf8')
);

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (!condition) {
    console.error(`  FAIL: ${message}`);
    failed++;
  } else {
    console.log(`  PASS: ${message}`);
    passed++;
  }
}

function assertEqual(actual, expected, message) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    console.error(`  FAIL: ${message}`);
    console.error(`    expected: ${JSON.stringify(expected)}`);
    console.error(`    actual:   ${JSON.stringify(actual)}`);
    failed++;
  } else {
    console.log(`  PASS: ${message}`);
    passed++;
  }
}

// ---------------------------------------------------------------------------
// 1. Key derivation
// ---------------------------------------------------------------------------
console.log('\n=== Key Derivation ===');

const kd = vectors.key_derivation;
const keys = wasm.deriveKeysFromMnemonic(kd.mnemonic);

assertEqual(keys.auth_key, kd.auth_key_hex, 'auth_key matches');
assertEqual(keys.encryption_key, kd.encryption_key_hex, 'encryption_key matches');
assertEqual(keys.dedup_key, kd.dedup_key_hex, 'dedup_key matches');
assertEqual(keys.salt, kd.salt_hex, 'salt matches');

// Auth key hash
const authKeyHash = wasm.computeAuthKeyHash(kd.auth_key_hex);
assertEqual(authKeyHash, kd.auth_key_hash, 'auth_key_hash matches');

// Lenient mode produces same result for valid mnemonic
const keysLenient = wasm.deriveKeysFromMnemonicLenient(kd.mnemonic);
assertEqual(keysLenient.auth_key, kd.auth_key_hex, 'lenient auth_key matches strict');
assertEqual(keysLenient.encryption_key, kd.encryption_key_hex, 'lenient encryption_key matches strict');

// Strict mode rejects bad checksum
try {
  wasm.deriveKeysFromMnemonic(
    'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon'
  );
  assert(false, 'strict should reject bad checksum');
} catch (e) {
  assert(e.message.includes('invalid'), 'strict rejects bad checksum: ' + e.message.slice(0, 60));
}

// Lenient accepts bad checksum
try {
  wasm.deriveKeysFromMnemonicLenient(
    'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon'
  );
  assert(true, 'lenient accepts bad checksum');
} catch (e) {
  assert(false, 'lenient should accept bad checksum: ' + e.message);
}

// ---------------------------------------------------------------------------
// 2. LSH seed derivation
// ---------------------------------------------------------------------------
console.log('\n=== LSH Seed ===');

const lshSeedHex = wasm.deriveLshSeed(kd.mnemonic, kd.salt_hex);
assertEqual(lshSeedHex, vectors.lsh.lsh_seed_hex, 'LSH seed matches');

// ---------------------------------------------------------------------------
// 3. Encrypt / Decrypt round-trip (XChaCha20-Poly1305)
// ---------------------------------------------------------------------------
console.log('\n=== XChaCha20-Poly1305 ===');

const xc = vectors.xchacha20;
const encrypted = wasm.encrypt(xc.plaintext, xc.encryption_key_hex);
const decrypted = wasm.decrypt(encrypted, xc.encryption_key_hex);
assertEqual(decrypted, xc.plaintext, 'encrypt/decrypt round-trip');

// Decrypt the known fixed-nonce ciphertext
const decryptedFixed = wasm.decrypt(xc.fixed_nonce_encrypted_base64, xc.encryption_key_hex);
assertEqual(decryptedFixed, xc.plaintext, 'decrypt fixed-nonce ciphertext from vectors');

// Wrong key fails
try {
  const wrongKey = '0'.repeat(64);
  wasm.decrypt(encrypted, wrongKey);
  assert(false, 'decrypt with wrong key should fail');
} catch (e) {
  assert(true, 'decrypt with wrong key fails');
}

// ---------------------------------------------------------------------------
// 4. Blind indices
// ---------------------------------------------------------------------------
console.log('\n=== Blind Indices ===');

for (const tc of vectors.blind_indices.test_cases) {
  const indices = wasm.generateBlindIndices(tc.text);
  assertEqual(indices, tc.indices, `blind indices for "${tc.text.slice(0, 40)}..."`);
}

// ---------------------------------------------------------------------------
// 5. Content fingerprint
// ---------------------------------------------------------------------------
console.log('\n=== Content Fingerprint ===');

const fp = vectors.content_fingerprint;
for (const tc of fp.test_cases) {
  const fingerprint = wasm.generateContentFingerprint(tc.text, fp.dedup_key_hex);
  assertEqual(fingerprint, tc.fingerprint, `fingerprint for "${tc.text.slice(0, 40)}..."`);
}

// normalizeText
const normalized = wasm.normalizeText('  Hello   WORLD  ');
assertEqual(normalized, 'hello world', 'normalizeText collapses whitespace');

// ---------------------------------------------------------------------------
// 6. LSH hashing
// ---------------------------------------------------------------------------
console.log('\n=== LSH Hashing ===');

// Small (4d, 3 tables, 4 bits)
const small = vectors.lsh.small;
const hasherSmall = wasm.WasmLshHasher.withParams(
  vectors.lsh.lsh_seed_hex,
  small.dims,
  small.n_tables,
  small.n_bits
);
const smallHashes = hasherSmall.hash(new Float64Array(small.embedding));
assertEqual(smallHashes, small.hashes, 'small LSH hashes match');
assertEqual(hasherSmall.tables, small.n_tables, 'small tables getter');
assertEqual(hasherSmall.bits, small.n_bits, 'small bits getter');
assertEqual(hasherSmall.dimensions, small.dims, 'small dimensions getter');
hasherSmall.free();

// Real (1024d, 20 tables, 32 bits)
const real = vectors.lsh.real;
const embedding1024 = new Float64Array(real.dims);
for (let i = 0; i < real.dims; i++) {
  embedding1024[i] = Math.sin(i * 0.1) * 0.5;
}
// Verify first 10 match
for (let i = 0; i < 10; i++) {
  assert(
    Math.abs(embedding1024[i] - real.embedding_first_10[i]) < 1e-14,
    `embedding[${i}] matches fixture`
  );
}

const hasherReal = new wasm.WasmLshHasher(vectors.lsh.lsh_seed_hex, real.dims);
const realHashes = hasherReal.hash(embedding1024);
assertEqual(realHashes, real.hashes, 'real (1024d) LSH hashes match');
hasherReal.free();

// Dimension mismatch error
try {
  const hasher3 = new wasm.WasmLshHasher(vectors.lsh.lsh_seed_hex, 4);
  hasher3.hash(new Float64Array([1.0, 2.0])); // wrong dims
  assert(false, 'should reject dimension mismatch');
} catch (e) {
  assert(true, 'rejects dimension mismatch: ' + e.message.slice(0, 60));
}

// ---------------------------------------------------------------------------
// 7. Protobuf encoding
// ---------------------------------------------------------------------------
console.log('\n=== Protobuf ===');

const factPayload = {
  id: 'test-fact-id',
  timestamp: '2026-01-01T00:00:00Z',
  owner: '0xABCD1234',
  encrypted_blob_hex: 'deadbeef',
  blind_indices: ['hash1', 'hash2'],
  decay_score: 0.8,
  source: 'test_source',
  content_fp: 'fp_test',
  agent_id: 'test_agent',
};

const protobufBytes = wasm.encodeFactProtobuf(JSON.stringify(factPayload));
assert(protobufBytes instanceof Uint8Array, 'encodeFactProtobuf returns Uint8Array');
assert(protobufBytes.length > 0, 'protobuf is non-empty');
// Verify the fact ID appears in the wire format
const bytesStr = new TextDecoder().decode(protobufBytes);
assert(bytesStr.includes('test-fact-id'), 'protobuf contains fact ID');

const tombstoneBytes = wasm.encodeTombstoneProtobuf('fact-to-delete', '0xOwner');
assert(tombstoneBytes instanceof Uint8Array, 'encodeTombstoneProtobuf returns Uint8Array');
assert(tombstoneBytes.length > 0, 'tombstone is non-empty');
const tombStr = new TextDecoder().decode(tombstoneBytes);
assert(tombStr.includes('fact-to-delete'), 'tombstone contains fact ID');

// With optional encrypted_embedding
const factWithEmb = { ...factPayload, encrypted_embedding: 'base64encembedding' };
const protobufWithEmb = wasm.encodeFactProtobuf(JSON.stringify(factWithEmb));
assert(protobufWithEmb.length > protobufBytes.length, 'protobuf with embedding is larger');

// ---------------------------------------------------------------------------
// 8. Debrief
// ---------------------------------------------------------------------------
console.log('\n=== Debrief ===');

// parseDebriefResponse
const debriefInput = JSON.stringify([
  { text: 'Session was about refactoring auth', type: 'summary', importance: 8 },
  { text: 'Migration to new API still pending', type: 'context', importance: 7 },
  { text: 'Low importance item should be filtered', type: 'summary', importance: 3 },
]);
const debriefItems = wasm.parseDebriefResponse(debriefInput);
assertEqual(debriefItems.length, 2, 'debrief filters low importance');
assertEqual(debriefItems[0].type, 'summary', 'first item is summary');
assertEqual(debriefItems[0].importance, 8, 'first item importance');
assertEqual(debriefItems[1].type, 'context', 'second item is context');

// Empty response
const emptyDebrief = wasm.parseDebriefResponse('[]');
assertEqual(emptyDebrief.length, 0, 'empty debrief response');

// Invalid JSON
const invalidDebrief = wasm.parseDebriefResponse('not json');
assertEqual(invalidDebrief.length, 0, 'invalid JSON returns empty');

// Code fences
const fencedDebrief = wasm.parseDebriefResponse(
  '```json\n[{"text":"Fenced debrief item with enough text","type":"summary","importance":8}]\n```'
);
assertEqual(fencedDebrief.length, 1, 'strips code fences');

// Cap at 5 items
const manyItems = Array.from({ length: 8 }, (_, i) => ({
  text: `Debrief item number ${i + 1} with sufficient text`,
  type: 'summary',
  importance: 7,
}));
const capped = wasm.parseDebriefResponse(JSON.stringify(manyItems));
assertEqual(capped.length, 5, 'caps at 5 items');

// getDebriefSystemPrompt
const prompt = wasm.getDebriefSystemPrompt();
assert(prompt.includes('{already_stored_facts}'), 'prompt has placeholder');
assert(prompt.includes('Maximum 5 items'), 'prompt mentions cap');

// buildDebriefPrompt
const builtPrompt = wasm.buildDebriefPrompt(JSON.stringify(['Fact A', 'Fact B']));
assert(builtPrompt.includes('- Fact A'), 'built prompt includes fact A');
assert(builtPrompt.includes('- Fact B'), 'built prompt includes fact B');
assert(!builtPrompt.includes('(none)'), 'built prompt does not say none');

const emptyPrompt = wasm.buildDebriefPrompt('[]');
assert(emptyPrompt.includes('(none)'), 'empty facts shows (none)');

// Constants
assertEqual(wasm.getMinDebriefMessages(), 8, 'MIN_DEBRIEF_MESSAGES = 8');
assertEqual(wasm.getMaxDebriefItems(), 5, 'MAX_DEBRIEF_ITEMS = 5');
assertEqual(wasm.getDebriefSource(), 'zeroclaw_debrief', 'DEBRIEF_SOURCE');

// ---------------------------------------------------------------------------
// 9. Embedding codec (canonical f16 + universal decoder) — internal#479
// ---------------------------------------------------------------------------
console.log('\n=== Embedding Codec ===');

const embVectors = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'embedding_codec_vectors.json'), 'utf8')
);
const unitVec = embVectors.unit_vector; // 640-d, exact-f32 values

// (a) encode -> canonical f16 base64 must match the Python struct '<e' output.
const canonicalB64 = wasm.encodeEmbeddingCanonical(new Float32Array(unitVec));
assertEqual(canonicalB64, embVectors.canonical_f16_base64, 'encodeEmbeddingCanonical matches Python f16 base64');

// decode(canonical f16) -> Float32Array, length 640, cosine >= 0.9999.
const decF16 = wasm.decodeEmbeddingUniversal(canonicalB64);
assert(decF16 instanceof Float32Array, 'decodeEmbeddingUniversal returns Float32Array');
assertEqual(decF16.length, 640, 'decoded f16 length is 640');
{
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < 640; i++) {
    dot += decF16[i] * unitVec[i];
    na += unitVec[i] * unitVec[i];
    nb += decF16[i] * decF16[i];
  }
  const cos = dot / (Math.sqrt(na) * Math.sqrt(nb));
  assert(cos >= 0.9999, `f16 round-trip cosine ${cos} >= 0.9999`);
}

// (b) legacy JSON array path (TS plugin) -> exact recovery.
const decJson = Array.from(wasm.decodeEmbeddingUniversal(embVectors.legacy_json_640));
assertEqual(decJson.length, 640, 'decoded JSON length is 640');
assert(decJson.every((v, i) => v === unitVec[i]), 'JSON legacy path recovers input exactly');

// (c) legacy f32 binary path (old Python) -> exact recovery.
const decF32 = Array.from(wasm.decodeEmbeddingUniversal(embVectors.legacy_f32_base64_640));
assertEqual(decF32.length, 640, 'decoded f32 length is 640');
assert(decF32.every((v, i) => v === unitVec[i]), 'f32 legacy path recovers input exactly');

// (d) non-canonical 1024-dim: encode uses f32 (640 guard), lossless round trip.
const ncVec = embVectors.non_canonical.vector;
const ncEnc = wasm.encodeEmbeddingCanonical(new Float32Array(ncVec));
const ncDec = Array.from(wasm.decodeEmbeddingUniversal(ncEnc));
assertEqual(ncDec.length, 1024, '1024-d round-trip length');
assert(ncDec.every((v, i) => v === ncVec[i]), '1024-d f32 round trip is lossless');
// decode the committed 1024-d f32 fixture directly.
const ncDecFix = Array.from(wasm.decodeEmbeddingUniversal(embVectors.non_canonical.f32_base64));
assert(ncDecFix.every((v, i) => v === ncVec[i]), 'decode committed 1024-d f32 fixture exact');

// Error paths -> JsError, never a silent wrong-dim vector.
try {
  wasm.decodeEmbeddingUniversal(Buffer.from([0xaa]).toString('base64')); // 1 byte
  assert(false, 'bad-length buffer should error');
} catch (e) {
  assert(true, 'rejects bad-length buffer: ' + e.message.slice(0, 60));
}
try {
  wasm.decodeEmbeddingUniversal('[1.0, not-json');
  assert(false, 'malformed JSON should error');
} catch (e) {
  assert(true, 'rejects malformed JSON: ' + e.message.slice(0, 60));
}

// Fail-closed encode (#479 review): NaN / Infinity / finite-f16-overflow must
// THROW at the WASM surface; the F16_MAX boundary (65504) must encode.
function assertThrows(fn, message) {
  try { fn(); assert(false, message + ' (did not throw)'); }
  catch (e) { assert(true, message); }
}
{
  const bad = new Float32Array(640).fill(0.5);
  bad[3] = NaN;
  assertThrows(() => wasm.encodeEmbeddingCanonical(bad), 'encode throws on NaN component');
  bad[3] = Infinity;
  assertThrows(() => wasm.encodeEmbeddingCanonical(bad), 'encode throws on Infinity component');
  bad[3] = 1e10;
  assertThrows(() => wasm.encodeEmbeddingCanonical(bad), 'encode throws on finite f16 overflow');
  bad[3] = 65504;
  const okB64 = wasm.encodeEmbeddingCanonical(bad);
  const back = wasm.decodeEmbeddingUniversal(okB64);
  assertEqual(back[3], 65504, 'F16_MAX boundary encodes and round-trips');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
console.log(`${'='.repeat(50)}`);

if (failed > 0) {
  process.exit(1);
}
