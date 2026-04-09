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
// Summary
// ---------------------------------------------------------------------------
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
console.log(`${'='.repeat(50)}`);

if (failed > 0) {
  process.exit(1);
}
