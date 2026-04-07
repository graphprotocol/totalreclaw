/**
 * Generate crypto parity test fixtures from the canonical TypeScript implementation.
 *
 * Usage: cd mcp && npx tsx generate-fixtures.ts
 *
 * Outputs: ../rust/totalreclaw-memory/tests/fixtures/crypto_vectors.json
 */

import { mnemonicToSeedSync } from '@scure/bip39';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { hmac } from '@noble/hashes/hmac.js';
import { stemmer } from 'porter-stemmer';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import {
  deriveKeysFromMnemonic,
  deriveLshSeed,
  computeAuthKeyHash,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateContentFingerprint,
} from './src/subgraph/crypto.js';
import { LSHHasher } from './src/subgraph/lsh.js';

const TEST_MNEMONIC = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

// 1. Key Derivation
const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
const seed = mnemonicToSeedSync(TEST_MNEMONIC.trim());

const keyDerivation = {
  mnemonic: TEST_MNEMONIC,
  bip39_seed_hex: Buffer.from(seed).toString('hex'),
  salt_hex: keys.salt.toString('hex'),
  auth_key_hex: keys.authKey.toString('hex'),
  encryption_key_hex: keys.encryptionKey.toString('hex'),
  dedup_key_hex: keys.dedupKey.toString('hex'),
  auth_key_hash: computeAuthKeyHash(keys.authKey),
};

// 2. LSH Seed
const lshSeed = deriveLshSeed(TEST_MNEMONIC, keys.salt);
const lshSeedHex = Buffer.from(lshSeed).toString('hex');

// 3. AES-256-GCM
const testPlaintext = 'The user prefers dark mode in all applications.';
const fixedIv = Buffer.alloc(12, 0);

function encryptWithFixedIv(plaintext: string, encryptionKey: Buffer, iv: Buffer): string {
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey, iv, { authTagLength: 16 });
  const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  const combined = Buffer.concat([iv, tag, ciphertext]);
  return combined.toString('base64');
}

const aesFixedEncrypted = encryptWithFixedIv(testPlaintext, keys.encryptionKey, fixedIv);
const aesRandomEncrypted = encrypt(testPlaintext, keys.encryptionKey);
const aesDecrypted = decrypt(aesRandomEncrypted, keys.encryptionKey);
if (aesDecrypted !== testPlaintext) throw new Error('AES round-trip failed');

const aesVectors = {
  plaintext: testPlaintext,
  encryption_key_hex: keys.encryptionKey.toString('hex'),
  fixed_iv_hex: fixedIv.toString('hex'),
  fixed_iv_encrypted_base64: aesFixedEncrypted,
  round_trip_verified: true,
};

// 4. Blind Indices
const blindTestTexts = [
  'The user prefers dark mode in all applications.',
  'Project deadline is March 15th 2025.',
  'User chose Python over Rust because of team expertise.',
];

const blindIndices = blindTestTexts.map((text) => ({
  text,
  indices: generateBlindIndices(text),
}));

const tokenHashMappings: Record<string, string> = {};
const stemMappings: Record<string, { stem: string; hash: string }> = {};

for (const text of blindTestTexts) {
  const tokens = text.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter((t: string) => t.length >= 2);
  for (const token of tokens) {
    if (!tokenHashMappings[token]) {
      tokenHashMappings[token] = Buffer.from(sha256(Buffer.from(token, 'utf8'))).toString('hex');
    }
    const stem = stemmer(token);
    if (stem.length >= 2 && stem !== token && !stemMappings[token]) {
      stemMappings[token] = {
        stem,
        hash: Buffer.from(sha256(Buffer.from(`stem:${stem}`, 'utf8'))).toString('hex'),
      };
    }
  }
}

// 5. Content Fingerprint
const fingerprintTests = [
  'The user prefers dark mode in all applications.',
  '  The   USER prefers dark   mode in ALL applications.  ',
  'Project deadline is March 15th 2025.',
];

const fingerprints = fingerprintTests.map((text) => ({
  text,
  fingerprint: generateContentFingerprint(text, keys.dedupKey),
}));

// 6. LSH Hasher
const SMALL_DIMS = 4;
const REAL_DIMS = 640;

const lshSmall = new LSHHasher(lshSeed, SMALL_DIMS, 3, 4);
const lshReal = new LSHHasher(lshSeed, REAL_DIMS, 20, 32);

const smallEmbedding = [0.5, -0.3, 0.8, -0.1];
const smallHashes = lshSmall.hash(smallEmbedding);

const realEmbedding: number[] = new Array(REAL_DIMS);
for (let i = 0; i < REAL_DIMS; i++) {
  realEmbedding[i] = Math.sin(i * 0.1) * 0.5;
}
const realHashes = lshReal.hash(realEmbedding);

const lshSmallFirstHyperplanes: number[] = [];
const smallHP = (lshSmall as any).hyperplanes[0];
for (let i = 0; i < Math.min(16, smallHP.length); i++) {
  lshSmallFirstHyperplanes.push(smallHP[i]);
}

const lshVectors = {
  lsh_seed_hex: lshSeedHex,
  small: {
    dims: SMALL_DIMS,
    n_tables: 3,
    n_bits: 4,
    embedding: smallEmbedding,
    hashes: smallHashes,
    first_hyperplanes_table0: lshSmallFirstHyperplanes,
  },
  real: {
    dims: REAL_DIMS,
    n_tables: 20,
    n_bits: 32,
    embedding_first_10: realEmbedding.slice(0, 10),
    embedding_generation: 'sin(i * 0.1) * 0.5 for i in 0..1024',
    hashes: realHashes,
  },
};

// 7. Porter Stemmer Parity
const stemmerTestWords = [
  'applications', 'prefers', 'running', 'community', 'communities',
  'argued', 'argues', 'arguing', 'deadline', 'project',
  'expertise', 'chosen', 'because', 'python', 'user',
  'dark', 'mode', 'march', 'over', 'team',
];

const stemmerResults = stemmerTestWords.map((word) => ({
  word,
  stem: stemmer(word),
}));

// Assemble and write
const fixture = {
  _comment: 'Generated from TypeScript canonical implementation. Do not edit manually.',
  _generated_at: new Date().toISOString(),
  _mnemonic: TEST_MNEMONIC,
  key_derivation: keyDerivation,
  aes_gcm: aesVectors,
  blind_indices: {
    test_cases: blindIndices,
    token_hash_mappings: tokenHashMappings,
    stem_mappings: stemMappings,
  },
  content_fingerprint: {
    dedup_key_hex: keys.dedupKey.toString('hex'),
    test_cases: fingerprints,
  },
  lsh: lshVectors,
  porter_stemmer: stemmerResults,
};

const outDir = path.resolve(__dirname, '../rust/totalreclaw-memory/tests/fixtures');
fs.mkdirSync(outDir, { recursive: true });
const outPath = path.join(outDir, 'crypto_vectors.json');
fs.writeFileSync(outPath, JSON.stringify(fixture, null, 2) + '\n');

console.log(`Fixture written to: ${outPath}`);
console.log(`Key derivation: auth_key=${keyDerivation.auth_key_hex.slice(0, 16)}...`);
console.log(`LSH seed: ${lshSeedHex.slice(0, 16)}...`);
console.log(`Blind indices counts: ${blindIndices.map((b) => b.indices.length).join(', ')}`);
console.log(`Content fingerprints: ${fingerprints.map((f) => f.fingerprint.slice(0, 16)).join(', ')}`);
console.log(`LSH small hashes: ${smallHashes.length}, real hashes: ${realHashes.length}`);
console.log(`Porter stemmer tests: ${stemmerResults.length}`);
