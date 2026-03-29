/**
 * Parity Tests: OpenClaw Plugin vs MCP Server Crypto
 *
 * Verifies that the @totalreclaw/mcp-server (mcp/src/index.ts) produces
 * byte-identical outputs to the OpenClaw plugin (skill/plugin/*.ts)
 * for all shared cryptographic, LSH, and reranker operations.
 *
 * NOTE: NanoClaw now spawns @totalreclaw/mcp-server via npx, so there is
 * only ONE MCP implementation. These parity tests validate that the plugin
 * and MCP server share identical crypto primitives.
 *
 * Run with: cd tests/parity && npm install && npx tsx parity-test.ts
 *
 * Test vectors use fixed, deterministic inputs:
 *   - Password: "test-password-for-parity"
 *   - Salt: 32 bytes of 0xAB
 *   - Known plaintext strings
 *   - Synthetic embedding vectors (deterministic from seed)
 *
 * Architecture:
 *   - Plugin functions: imported from plugin-adapter.ts (mirrors skill/plugin/*.ts
 *     with corrected import specifiers for raw tsx execution)
 *   - MCP functions: imported from nanoclaw-adapter.ts (standalone re-implementation
 *     of crypto primitives using @noble/hashes)
 *
 * Both adapters contain functions implementing the same algorithms. The parity
 * test verifies they produce byte-identical outputs for the same inputs.
 */

// ---------------------------------------------------------------------------
// Plugin imports (adapter mirroring skill/plugin/*.ts)
// ---------------------------------------------------------------------------
import {
  deriveKeys as pluginDeriveKeys,
  deriveLshSeed as pluginDeriveLshSeed,
  computeAuthKeyHash as pluginComputeAuthKeyHash,
  encrypt as pluginEncrypt,
  decrypt as pluginDecrypt,
  generateBlindIndices as pluginGenerateBlindIndices,
  generateContentFingerprint as pluginGenerateContentFingerprint,
  LSHHasher as PluginLSHHasher,
  tokenize as pluginTokenize,
  bm25Score as pluginBm25Score,
  cosineSimilarity as pluginCosineSimilarity,
  rrfFuse as pluginRrfFuse,
  type RankedItem as PluginRankedItem,
} from './plugin-adapter.js';

// ---------------------------------------------------------------------------
// NanoClaw imports (adapter extracted from the monolith)
// ---------------------------------------------------------------------------
import {
  deriveKeys as nanoDeriveKeys,
  deriveLshSeed as nanoDeriveLshSeed,
  computeAuthKeyHash as nanoComputeAuthKeyHash,
  encrypt as nanoEncrypt,
  decrypt as nanoDecrypt,
  generateBlindIndices as nanoGenerateBlindIndices,
  generateContentFingerprint as nanoGenerateContentFingerprint,
  LSHHasher as NanoLSHHasher,
  tokenize as nanoTokenize,
  bm25Score as nanoBm25Score,
  cosineSimilarity as nanoCosineSimilarity,
  rrfFuse as nanoRrfFuse,
  type RankedItem as NanoRankedItem,
} from './nanoclaw-adapter.js';

import { sha256 } from '@noble/hashes/sha2.js';

// ---------------------------------------------------------------------------
// Test harness (TAP-style, matching existing plugin tests)
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
let testNum = 0;

function assert(condition: boolean, message: string): void {
  testNum++;
  if (condition) {
    passed++;
    console.log(`ok ${testNum} - ${message}`);
  } else {
    failed++;
    console.log(`not ok ${testNum} - ${message}`);
  }
}

function assertStrictEqual<T>(actual: T, expected: T, message: string): void {
  assert(actual === expected, `${message} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
}

function assertArrayEqual(actual: string[], expected: string[], message: string): void {
  const ok =
    actual.length === expected.length &&
    actual.every((v, i) => v === expected[i]);
  assert(ok, `${message} (lengths: ${actual.length} vs ${expected.length})`);
  if (!ok && actual.length <= 30) {
    // Show first mismatch for debugging
    for (let i = 0; i < Math.max(actual.length, expected.length); i++) {
      if (actual[i] !== expected[i]) {
        console.log(`  # first mismatch at index ${i}: plugin="${expected[i]}" vs nano="${actual[i]}"`);
        break;
      }
    }
  }
}

function assertNumberEqual(actual: number, expected: number, message: string, epsilon = 1e-15): void {
  const diff = Math.abs(actual - expected);
  assert(diff <= epsilon, `${message} (diff=${diff}, epsilon=${epsilon})`);
}

function assertBufferEqual(actual: Buffer, expected: Buffer, message: string): void {
  assert(actual.equals(expected), `${message} (${actual.toString('hex').slice(0, 16)}... vs ${expected.toString('hex').slice(0, 16)}...)`);
}

// ---------------------------------------------------------------------------
// Fixed test vectors
// ---------------------------------------------------------------------------

const TEST_PASSWORD = 'test-password-for-parity';
const TEST_SALT = Buffer.alloc(32, 0xab);

const TEST_PLAINTEXT_1 = 'Alex works at Nexus Labs as a senior engineer';
const TEST_PLAINTEXT_2 = 'The user prefers dark mode and Vim keybindings';
const TEST_PLAINTEXT_3 = 'Meeting notes: discussed Q3 roadmap with the team';

/**
 * Create a deterministic pseudo-embedding from a numeric seed.
 * Uses SHA-256 chain to fill the vector, then normalizes to unit length.
 */
function makeEmbedding(seed: number, dims: number): number[] {
  const vec: number[] = new Array(dims);
  let hash = sha256(Buffer.from(`embedding_${seed}`, 'utf8'));
  let offset = 0;
  const view = new DataView(new ArrayBuffer(4));

  for (let i = 0; i < dims; i++) {
    if (offset + 4 > hash.length) {
      hash = sha256(hash);
      offset = 0;
    }
    view.setUint8(0, hash[offset]);
    view.setUint8(1, hash[offset + 1]);
    view.setUint8(2, hash[offset + 2]);
    view.setUint8(3, hash[offset + 3]);
    vec[i] = (view.getUint32(0, true) / 0xFFFFFFFF) * 2 - 1;
    offset += 4;
  }

  // Normalize to unit vector
  let norm = 0;
  for (let i = 0; i < dims; i++) norm += vec[i] * vec[i];
  norm = Math.sqrt(norm);
  for (let i = 0; i < dims; i++) vec[i] /= norm;

  return vec;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

async function runTests(): Promise<void> {
  // =========================================================================
  // Test 1: Key Derivation (deriveKeys)
  // =========================================================================
  console.log('# 1. Key Derivation Parity');

  const pluginKeys = pluginDeriveKeys(TEST_PASSWORD, TEST_SALT);
  const nanoKeys = nanoDeriveKeys(TEST_PASSWORD, TEST_SALT);

  assertBufferEqual(nanoKeys.authKey, pluginKeys.authKey, 'authKey matches');
  assertBufferEqual(nanoKeys.encryptionKey, pluginKeys.encryptionKey, 'encryptionKey matches');
  assertBufferEqual(nanoKeys.dedupKey, pluginKeys.dedupKey, 'dedupKey matches');
  assertBufferEqual(nanoKeys.salt, pluginKeys.salt, 'salt matches');

  // =========================================================================
  // Test 2: Auth Key Hash (computeAuthKeyHash)
  // =========================================================================
  console.log('# 2. Auth Key Hash Parity');

  const pluginAuthHash = pluginComputeAuthKeyHash(pluginKeys.authKey);
  const nanoAuthHash = nanoComputeAuthKeyHash(nanoKeys.authKey);

  assertStrictEqual(nanoAuthHash, pluginAuthHash, 'computeAuthKeyHash produces identical hex');
  assert(/^[0-9a-f]{64}$/.test(pluginAuthHash), 'auth key hash is valid 64-char hex');

  // =========================================================================
  // Test 3: Blind Indices (generateBlindIndices)
  // =========================================================================
  console.log('# 3. Blind Indices Parity');

  for (const text of [TEST_PLAINTEXT_1, TEST_PLAINTEXT_2, TEST_PLAINTEXT_3]) {
    const pluginIndices = pluginGenerateBlindIndices(text);
    const nanoIndices = nanoGenerateBlindIndices(text);
    assertArrayEqual(nanoIndices, pluginIndices, `blind indices match for "${text.slice(0, 40)}..."`);
  }

  // Edge cases
  {
    const pluginEmpty = pluginGenerateBlindIndices('');
    const nanoEmpty = nanoGenerateBlindIndices('');
    assertArrayEqual(nanoEmpty, pluginEmpty, 'blind indices match for empty string');
  }

  {
    const pluginPunct = pluginGenerateBlindIndices('Hello, World! How are you?');
    const nanoPunct = nanoGenerateBlindIndices('Hello, World! How are you?');
    assertArrayEqual(nanoPunct, pluginPunct, 'blind indices match with punctuation');
  }

  {
    const pluginUnicode = pluginGenerateBlindIndices('cafe\u0301 naive\u0308');
    const nanoUnicode = nanoGenerateBlindIndices('cafe\u0301 naive\u0308');
    assertArrayEqual(nanoUnicode, pluginUnicode, 'blind indices match with Unicode combining chars');
  }

  // =========================================================================
  // Test 4: Content Fingerprint (generateContentFingerprint)
  // =========================================================================
  console.log('# 4. Content Fingerprint Parity');

  for (const text of [TEST_PLAINTEXT_1, TEST_PLAINTEXT_2, TEST_PLAINTEXT_3]) {
    const pluginFp = pluginGenerateContentFingerprint(text, pluginKeys.dedupKey);
    const nanoFp = nanoGenerateContentFingerprint(text, nanoKeys.dedupKey);
    assertStrictEqual(nanoFp, pluginFp, `content fingerprint matches for "${text.slice(0, 40)}..."`);
    assert(/^[0-9a-f]{64}$/.test(pluginFp), 'content fingerprint is valid 64-char hex');
  }

  // Whitespace normalization
  {
    const text1 = '  Hello   world  \n  test  ';
    const text2 = 'Hello world test';
    const pluginFp1 = pluginGenerateContentFingerprint(text1, pluginKeys.dedupKey);
    const pluginFp2 = pluginGenerateContentFingerprint(text2, pluginKeys.dedupKey);
    assertStrictEqual(pluginFp1, pluginFp2, 'content fingerprint normalizes whitespace (plugin)');

    const nanoFp1 = nanoGenerateContentFingerprint(text1, nanoKeys.dedupKey);
    assertStrictEqual(nanoFp1, pluginFp1, 'whitespace-normalized fingerprint matches cross-impl');
  }

  // =========================================================================
  // Test 5: LSH Seed Derivation (deriveLshSeed)
  // =========================================================================
  console.log('# 5. LSH Seed Derivation Parity');

  const pluginLshSeed = pluginDeriveLshSeed(TEST_PASSWORD, TEST_SALT);
  const nanoLshSeed = nanoDeriveLshSeed(TEST_PASSWORD, TEST_SALT);

  assert(pluginLshSeed.length === 32, 'plugin LSH seed is 32 bytes');
  assert(nanoLshSeed.length === 32, 'nano LSH seed is 32 bytes');

  let lshSeedMatch = true;
  for (let i = 0; i < 32; i++) {
    if (pluginLshSeed[i] !== nanoLshSeed[i]) {
      lshSeedMatch = false;
      break;
    }
  }
  assert(lshSeedMatch, 'LSH seeds are byte-identical');

  // =========================================================================
  // Test 6: LSH Bucket Hashes (LSHHasher.hash)
  // =========================================================================
  console.log('# 6. LSH Bucket Hashes Parity');

  const DIMS = 1024; // Qwen3-Embedding-0.6B dimension
  const N_TABLES = 20;
  const N_BITS = 32;

  const pluginHasher = new PluginLSHHasher(pluginLshSeed, DIMS, N_TABLES, N_BITS);
  const nanoHasher = new NanoLSHHasher(nanoLshSeed, DIMS, N_TABLES, N_BITS);

  // Test with multiple embeddings
  for (const seed of [1, 42, 100, 999]) {
    const emb = makeEmbedding(seed, DIMS);
    const pluginBuckets = pluginHasher.hash(emb);
    const nanoBuckets = nanoHasher.hash(emb);

    assertArrayEqual(nanoBuckets, pluginBuckets, `LSH buckets match for embedding seed=${seed}`);
  }

  // Verify correct count and format
  {
    const emb = makeEmbedding(42, DIMS);
    const buckets = pluginHasher.hash(emb);
    assert(buckets.length === N_TABLES, `LSH produces ${N_TABLES} bucket hashes`);
    assert(buckets.every((h: string) => /^[0-9a-f]{64}$/.test(h)), 'all bucket hashes are valid SHA-256 hex');
  }

  // Small dimensions edge case
  {
    const smallSeed = sha256(Buffer.from('small-dim-test', 'utf8'));
    const pluginSmall = new PluginLSHHasher(smallSeed, 3, 2, 4);
    const nanoSmall = new NanoLSHHasher(smallSeed, 3, 2, 4);
    const smallEmb = [0.5, 0.5, 0.7071];

    const pluginSmallBuckets = pluginSmall.hash(smallEmb);
    const nanoSmallBuckets = nanoSmall.hash(smallEmb);
    assertArrayEqual(nanoSmallBuckets, pluginSmallBuckets, 'LSH buckets match for small dims (3d, 2 tables, 4 bits)');
  }

  // =========================================================================
  // Test 7: Tokenization (tokenize)
  // =========================================================================
  console.log('# 7. Tokenization Parity');

  const tokenTestCases = [
    'Alex works at Nexus Labs as a senior engineer',
    'The quick brown fox jumps over the lazy dog',
    'Hello, World! How are you?',
    'gaming games gamer gameplay',
    'running ran runner runs',
    '',
    'I am a test!',
    'cafe\u0301 naive\u0308 re\u0301sume\u0301',
  ];

  for (const text of tokenTestCases) {
    const pluginTokens = pluginTokenize(text);
    const nanoTokens = nanoTokenize(text);
    assertArrayEqual(nanoTokens, pluginTokens, `tokenize matches for "${text.slice(0, 40)}${text.length > 40 ? '...' : ''}"`);
  }

  // With stop words preserved
  {
    const pluginTokensNoStop = pluginTokenize('I am a test!', false);
    const nanoTokensNoStop = nanoTokenize('I am a test!', false);
    assertArrayEqual(nanoTokensNoStop, pluginTokensNoStop, 'tokenize matches with removeStopWords=false');
  }

  // =========================================================================
  // Test 8: BM25 Scores
  // =========================================================================
  console.log('# 8. BM25 Score Parity');

  {
    const queryTerms = ['alex', 'work'];
    const docTerms = ['alex', 'work', 'nexus', 'lab'];
    const avgDocLen = 4;
    const docCount = 10;
    const termDocFreqs = new Map<string, number>([
      ['alex', 3], ['work', 5], ['nexus', 1], ['lab', 2],
    ]);

    const pluginScore = pluginBm25Score(queryTerms, docTerms, avgDocLen, docCount, termDocFreqs);
    const nanoScore = nanoBm25Score(queryTerms, docTerms, avgDocLen, docCount, termDocFreqs);
    assertNumberEqual(nanoScore, pluginScore, 'BM25 scores match for standard case');
  }

  {
    // No overlap
    const pluginScore = pluginBm25Score(['python'], ['alex', 'work'], 4, 10, new Map([['alex', 3], ['work', 5]]));
    const nanoScore = nanoBm25Score(['python'], ['alex', 'work'], 4, 10, new Map([['alex', 3], ['work', 5]]));
    assertNumberEqual(nanoScore, pluginScore, 'BM25 scores match for no-overlap case');
  }

  {
    // Empty inputs
    const pluginEmpty = pluginBm25Score(['hello'], [], 4, 10, new Map());
    const nanoEmpty = nanoBm25Score(['hello'], [], 4, 10, new Map());
    assertNumberEqual(nanoEmpty, pluginEmpty, 'BM25 scores match for empty doc');
  }

  {
    // Multiple terms with varying frequencies
    const queryTerms = ['test', 'data', 'analysi'];
    const docTerms = ['test', 'test', 'data', 'analysi', 'report', 'test'];
    const avgDocLen = 8;
    const docCount = 100;
    const termDocFreqs = new Map<string, number>([
      ['test', 50], ['data', 30], ['analysi', 5], ['report', 80],
    ]);

    const pluginScore = pluginBm25Score(queryTerms, docTerms, avgDocLen, docCount, termDocFreqs);
    const nanoScore = nanoBm25Score(queryTerms, docTerms, avgDocLen, docCount, termDocFreqs);
    assertNumberEqual(nanoScore, pluginScore, 'BM25 scores match for multi-term case');
  }

  // =========================================================================
  // Test 9: Cosine Similarity
  // =========================================================================
  console.log('# 9. Cosine Similarity Parity');

  const cosineTestCases: Array<[number[], number[], string]> = [
    [[1, 2, 3], [2, 4, 6], 'parallel vectors'],
    [[1, 0], [0, 1], 'orthogonal vectors'],
    [[1, 2, 3], [-1, -2, -3], 'opposite vectors'],
    [[3, 4], [3, 4], 'identical vectors'],
    [[0, 0, 0], [1, 2, 3], 'zero vector a'],
    [[1, 2], [0, 0], 'zero vector b'],
    [[], [1, 2], 'empty vector a'],
    [[1, 2], [], 'empty vector b'],
    [[1, 1], [1, 0], '45-degree angle'],
  ];

  for (const [a, b, label] of cosineTestCases) {
    const pluginCos = pluginCosineSimilarity(a, b);
    const nanoCos = nanoCosineSimilarity(a, b);
    assertNumberEqual(nanoCos, pluginCos, `cosine similarity matches for ${label}`);
  }

  // High-dimensional vectors (384-dim, like real embeddings)
  {
    const embA = makeEmbedding(42, 384);
    const embB = makeEmbedding(43, 384);
    const pluginCos = pluginCosineSimilarity(embA, embB);
    const nanoCos = nanoCosineSimilarity(embA, embB);
    assertNumberEqual(nanoCos, pluginCos, 'cosine similarity matches for 384-dim embeddings');
  }

  // =========================================================================
  // Test 10: RRF Fusion
  // =========================================================================
  console.log('# 10. RRF Fusion Parity');

  {
    const ranking1: PluginRankedItem[] = [
      { id: 'A', score: 10 },
      { id: 'B', score: 8 },
      { id: 'C', score: 6 },
    ];
    const ranking2: PluginRankedItem[] = [
      { id: 'C', score: 10 },
      { id: 'A', score: 8 },
      { id: 'B', score: 6 },
    ];

    const pluginFused = pluginRrfFuse([ranking1, ranking2], 60);
    const nanoFused = nanoRrfFuse(
      [ranking1 as NanoRankedItem[], ranking2 as NanoRankedItem[]], 60,
    );

    assert(pluginFused.length === nanoFused.length, 'RRF output lengths match');
    for (let i = 0; i < pluginFused.length; i++) {
      assertStrictEqual(nanoFused[i].id, pluginFused[i].id, `RRF rank ${i} id matches`);
      assertNumberEqual(nanoFused[i].score, pluginFused[i].score, `RRF rank ${i} score matches`);
    }
  }

  // Items in only one ranking
  {
    const ranking1: PluginRankedItem[] = [
      { id: 'X', score: 10 },
      { id: 'Y', score: 5 },
    ];
    const ranking2: PluginRankedItem[] = [
      { id: 'Z', score: 10 },
      { id: 'X', score: 5 },
    ];

    const pluginFused = pluginRrfFuse([ranking1, ranking2], 60);
    const nanoFused = nanoRrfFuse(
      [ranking1 as NanoRankedItem[], ranking2 as NanoRankedItem[]], 60,
    );

    assert(pluginFused.length === nanoFused.length, 'RRF with partial overlap: lengths match');
    for (let i = 0; i < pluginFused.length; i++) {
      assertStrictEqual(nanoFused[i].id, pluginFused[i].id, `RRF partial rank ${i} id matches`);
      assertNumberEqual(nanoFused[i].score, pluginFused[i].score, `RRF partial rank ${i} score matches`);
    }
  }

  // Empty input
  {
    const pluginEmpty = pluginRrfFuse([], 60);
    const nanoEmpty = nanoRrfFuse([], 60);
    assert(pluginEmpty.length === 0 && nanoEmpty.length === 0, 'RRF empty input produces empty output');
  }

  // =========================================================================
  // Test 11: Encryption Round-Trip (cross-implementation)
  // =========================================================================
  console.log('# 11. Encryption Round-Trip (Cross-Implementation)');

  // Encrypt with plugin, decrypt with NanoClaw (same keys)
  for (const text of [TEST_PLAINTEXT_1, TEST_PLAINTEXT_2, TEST_PLAINTEXT_3]) {
    const encrypted = pluginEncrypt(text, pluginKeys.encryptionKey);
    const decrypted = nanoDecrypt(encrypted, nanoKeys.encryptionKey);
    assertStrictEqual(decrypted, text, `plugin-encrypt -> nano-decrypt for "${text.slice(0, 30)}..."`);
  }

  // Encrypt with NanoClaw, decrypt with plugin
  for (const text of [TEST_PLAINTEXT_1, TEST_PLAINTEXT_2, TEST_PLAINTEXT_3]) {
    const encrypted = nanoEncrypt(text, nanoKeys.encryptionKey);
    const decrypted = pluginDecrypt(encrypted, pluginKeys.encryptionKey);
    assertStrictEqual(decrypted, text, `nano-encrypt -> plugin-decrypt for "${text.slice(0, 30)}..."`);
  }

  // Edge case: empty string
  {
    const encrypted = pluginEncrypt('', pluginKeys.encryptionKey);
    const decrypted = nanoDecrypt(encrypted, nanoKeys.encryptionKey);
    assertStrictEqual(decrypted, '', 'cross-impl round-trip for empty string');
  }

  // Edge case: Unicode text
  {
    const unicode = 'cafe\u0301 \u{1F600} \u4E16\u754C Hello \u{1F310}';
    const encrypted = pluginEncrypt(unicode, pluginKeys.encryptionKey);
    const decrypted = nanoDecrypt(encrypted, nanoKeys.encryptionKey);
    assertStrictEqual(decrypted, unicode, 'cross-impl round-trip for Unicode text');
  }

  // Edge case: large text
  {
    const large = 'A'.repeat(10000);
    const encrypted = nanoEncrypt(large, nanoKeys.encryptionKey);
    const decrypted = pluginDecrypt(encrypted, pluginKeys.encryptionKey);
    assertStrictEqual(decrypted, large, 'cross-impl round-trip for large text (10KB)');
  }

  // =========================================================================
  // Test 12: End-to-End Workflow Parity
  // =========================================================================
  console.log('# 12. End-to-End Workflow Parity');

  // Simulate a full store+search cycle to verify all pieces work together:
  // 1. Derive keys with same password/salt
  // 2. Generate blind indices for the same text
  // 3. Generate content fingerprint
  // 4. Generate LSH buckets for the same embedding
  // 5. All outputs should be identical

  {
    const factText = 'Alex prefers TypeScript over Python for backend development';
    const emb = makeEmbedding(42, DIMS);

    // Plugin pipeline
    const pKeys = pluginDeriveKeys(TEST_PASSWORD, TEST_SALT);
    const pLshSeed = pluginDeriveLshSeed(TEST_PASSWORD, TEST_SALT);
    const pHasher = new PluginLSHHasher(pLshSeed, DIMS, N_TABLES, N_BITS);

    const pBlind = pluginGenerateBlindIndices(factText);
    const pFp = pluginGenerateContentFingerprint(factText, pKeys.dedupKey);
    const pLsh = pHasher.hash(emb);
    const pAuthHash = pluginComputeAuthKeyHash(pKeys.authKey);

    // NanoClaw pipeline
    const nKeys = nanoDeriveKeys(TEST_PASSWORD, TEST_SALT);
    const nLshSeed = nanoDeriveLshSeed(TEST_PASSWORD, TEST_SALT);
    const nHasher = new NanoLSHHasher(nLshSeed, DIMS, N_TABLES, N_BITS);

    const nBlind = nanoGenerateBlindIndices(factText);
    const nFp = nanoGenerateContentFingerprint(factText, nKeys.dedupKey);
    const nLsh = nHasher.hash(emb);
    const nAuthHash = nanoComputeAuthKeyHash(nKeys.authKey);

    // Verify all outputs match
    assertArrayEqual(nBlind, pBlind, 'E2E: blind indices identical');
    assertStrictEqual(nFp, pFp, 'E2E: content fingerprint identical');
    assertArrayEqual(nLsh, pLsh, 'E2E: LSH bucket hashes identical');
    assertStrictEqual(nAuthHash, pAuthHash, 'E2E: auth key hash identical');

    // Merged indices (blind + LSH) should be identical
    const pAllIndices = [...pBlind, ...pLsh];
    const nAllIndices = [...nBlind, ...nLsh];
    assertArrayEqual(nAllIndices, pAllIndices, 'E2E: merged blind+LSH indices identical');
  }

  // =========================================================================
  // Summary
  // =========================================================================

  console.log(`\n1..${testNum}`);
  console.log(`# pass: ${passed}`);
  console.log(`# fail: ${failed}`);

  if (failed > 0) {
    console.log('\nFAILED');
    process.exit(1);
  } else {
    console.log('\nALL PARITY TESTS PASSED');
    process.exit(0);
  }
}

runTests().catch((err) => {
  console.error('Test runner failed:', err);
  process.exit(1);
});
