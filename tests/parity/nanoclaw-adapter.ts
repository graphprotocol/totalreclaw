/**
 * MCP Crypto Adapter for Parity Tests
 *
 * Standalone re-implementation of TotalReclaw's crypto, LSH, and reranker
 * primitives using @noble/hashes. Used to verify parity with the OpenClaw
 * plugin (skill/plugin/*.ts).
 *
 * NOTE: The old NanoClaw monolith (totalreclaw-mcp.ts) has been removed.
 * NanoClaw now spawns @totalreclaw/mcp-server via npx. This adapter
 * remains as a standalone reference implementation for parity testing.
 *
 * IMPORTANT: Do NOT modify the function logic here. If you need to change
 * behavior, change the source of truth (mcp/src/index.ts and the plugin
 * files) and then update this adapter to match.
 */

import { argon2id } from '@noble/hashes/argon2.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { hmac } from '@noble/hashes/hmac.js';
import { mnemonicToSeedSync, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { stemmer } from 'porter-stemmer';
import { xchacha20poly1305 } from '@noble/ciphers/chacha.js';
import crypto from 'node:crypto';

// =========================================================================
// Crypto primitives (Argon2id + HKDF key derivation, XChaCha20-Poly1305 encryption)
// =========================================================================

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';
const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

const ARGON2_TIME_COST = 3;
const ARGON2_MEMORY_COST = 65536;
const ARGON2_PARALLELISM = 4;
const ARGON2_DK_LEN = 32;

const NONCE_LENGTH = 24;
const TAG_LENGTH = 16;
const KEY_LENGTH = 32;

function isBip39Mnemonic(input: string): boolean {
  const words = input.trim().split(/\s+/);
  if (words.length !== 12 && words.length !== 24) return false;
  return validateMnemonic(input.trim(), wordlist);
}

function deriveKeysFromMnemonic(
  mnemonic: string,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = Buffer.from(seed.slice(0, 32));
  const seedBuf = Buffer.from(seed);
  const enc = (s: string) => new Uint8Array(Buffer.from(s, 'utf8'));

  const authKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(AUTH_KEY_INFO), 32),
  );
  const encryptionKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(DEDUP_KEY_INFO), 32),
  );

  return { authKey, encryptionKey, dedupKey, salt };
}

export function deriveKeys(
  password: string,
  existingSalt?: Buffer,
): { authKey: Buffer; encryptionKey: Buffer; dedupKey: Buffer; salt: Buffer } {
  if (isBip39Mnemonic(password)) {
    return deriveKeysFromMnemonic(password);
  }

  const salt = existingSalt ?? crypto.randomBytes(32);

  const masterKey = argon2id(
    Buffer.from(password, 'utf8'),
    salt,
    { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
  );

  // NanoClaw uses Buffer.from(s, 'utf8') for info (not Uint8Array wrapper)
  const enc = (s: string) => Buffer.from(s, 'utf8');
  const authKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(AUTH_KEY_INFO), 32),
  );
  const encryptionKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, masterKey, salt, enc(DEDUP_KEY_INFO), 32),
  );

  return { authKey, encryptionKey, dedupKey, salt: Buffer.from(salt) };
}

export function computeAuthKeyHash(authKey: Buffer): string {
  return Buffer.from(sha256(authKey)).toString('hex');
}

export function encrypt(plaintext: string, encryptionKey: Buffer): string {
  if (encryptionKey.length !== KEY_LENGTH) {
    throw new Error(`Invalid key length: expected ${KEY_LENGTH}, got ${encryptionKey.length}`);
  }

  const nonce = crypto.randomBytes(NONCE_LENGTH);
  const cipher = xchacha20poly1305(new Uint8Array(encryptionKey), nonce);
  const sealed = cipher.encrypt(new Uint8Array(Buffer.from(plaintext, 'utf8')));

  // sealed = ciphertext || tag (tag is last 16 bytes)
  const ctLen = sealed.length - TAG_LENGTH;
  const ciphertext = sealed.subarray(0, ctLen);
  const tag = sealed.subarray(ctLen);

  // Wire format: nonce(24) || tag(16) || ciphertext
  const combined = Buffer.concat([nonce, tag, ciphertext]);
  return combined.toString('base64');
}

export function decrypt(encryptedBase64: string, encryptionKey: Buffer): string {
  if (encryptionKey.length !== KEY_LENGTH) {
    throw new Error(`Invalid key length: expected ${KEY_LENGTH}, got ${encryptionKey.length}`);
  }

  const combined = Buffer.from(encryptedBase64, 'base64');

  if (combined.length < NONCE_LENGTH + TAG_LENGTH) {
    throw new Error('Encrypted data too short');
  }

  const nonce = combined.subarray(0, NONCE_LENGTH);
  const tag = combined.subarray(NONCE_LENGTH, NONCE_LENGTH + TAG_LENGTH);
  const ciphertext = combined.subarray(NONCE_LENGTH + TAG_LENGTH);

  // @noble/ciphers expects: ciphertext || tag
  const ctWithTag = Buffer.concat([ciphertext, tag]);
  const cipher = xchacha20poly1305(new Uint8Array(encryptionKey), new Uint8Array(nonce));
  const plaintext = cipher.decrypt(new Uint8Array(ctWithTag));

  return Buffer.from(plaintext).toString('utf8');
}

export function generateBlindIndices(text: string): string[] {
  const tokens = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter((t) => t.length >= 2);

  const seen = new Set<string>();
  const indices: string[] = [];

  for (const token of tokens) {
    const hash = Buffer.from(sha256(Buffer.from(token, 'utf8'))).toString('hex');
    if (!seen.has(hash)) {
      seen.add(hash);
      indices.push(hash);
    }

    const stem = stemmer(token);
    if (stem.length >= 2 && stem !== token) {
      const stemHash = Buffer.from(
        sha256(Buffer.from(`stem:${stem}`, 'utf8'))
      ).toString('hex');
      if (!seen.has(stemHash)) {
        seen.add(stemHash);
        indices.push(stemHash);
      }
    }
  }

  return indices;
}

function normalizeText(text: string): string {
  return text
    .normalize('NFC')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

export function generateContentFingerprint(plaintext: string, dedupKey: Buffer): string {
  const normalized = normalizeText(plaintext);
  return Buffer.from(
    hmac(sha256, dedupKey, Buffer.from(normalized, 'utf8')),
  ).toString('hex');
}

// =========================================================================
// LSH Seed Derivation (HKDF with LSH_SEED_INFO)
// =========================================================================

const LSH_SEED_INFO = 'openmemory-lsh-seed-v1';

export function deriveLshSeed(password: string, salt: Buffer): Uint8Array {
  if (isBip39Mnemonic(password)) {
    const seed = mnemonicToSeedSync(password.trim());
    return new Uint8Array(
      hkdf(sha256, Buffer.from(seed), salt, Buffer.from(LSH_SEED_INFO, 'utf8'), 32),
    );
  }

  const masterKey = argon2id(
    Buffer.from(password, 'utf8'),
    salt,
    { t: ARGON2_TIME_COST, m: ARGON2_MEMORY_COST, p: ARGON2_PARALLELISM, dkLen: ARGON2_DK_LEN },
  );

  return new Uint8Array(
    hkdf(sha256, masterKey, salt, Buffer.from(LSH_SEED_INFO, 'utf8'), 32),
  );
}

// =========================================================================
// LSH Hasher (locality-sensitive hashing for blind search indices)
// =========================================================================

export class LSHHasher {
  private hyperplanes: Float64Array[];
  private readonly dims: number;
  private readonly nTables: number;
  private readonly nBits: number;

  constructor(seed: Uint8Array, dims: number, nTables: number = 20, nBits: number = 32) {
    if (seed.length < 16) throw new Error(`LSH seed too short: expected >= 16 bytes, got ${seed.length}`);
    if (dims < 1) throw new Error(`dims must be positive, got ${dims}`);
    if (nTables < 1) throw new Error(`nTables must be positive, got ${nTables}`);
    if (nBits < 1) throw new Error(`nBits must be positive, got ${nBits}`);

    this.dims = dims;
    this.nTables = nTables;
    this.nBits = nBits;
    this.hyperplanes = new Array(nTables);

    for (let t = 0; t < nTables; t++) {
      this.hyperplanes[t] = this.generateTableHyperplanes(seed, t);
    }
  }

  private generateTableHyperplanes(seed: Uint8Array, tableIndex: number): Float64Array {
    const BYTES_PER_FLOAT = 8;
    const totalFloats = this.dims * this.nBits;
    const totalBytes = totalFloats * BYTES_PER_FLOAT;

    const randomBytes = this.deriveRandomBytes(seed, `lsh_table_${tableIndex}`, totalBytes);
    const hyperplaneMatrix = new Float64Array(totalFloats);
    const view = new DataView(randomBytes.buffer, randomBytes.byteOffset, randomBytes.byteLength);

    for (let i = 0; i < totalFloats; i++) {
      const offset = i * BYTES_PER_FLOAT;
      const u1Raw = view.getUint32(offset, true);
      const u2Raw = view.getUint32(offset + 4, true);
      const u1 = (u1Raw + 1) / (0xFFFFFFFF + 2);
      const u2 = (u2Raw + 1) / (0xFFFFFFFF + 2);
      hyperplaneMatrix[i] = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
    }

    return hyperplaneMatrix;
  }

  private deriveRandomBytes(seed: Uint8Array, baseInfo: string, length: number): Uint8Array {
    const MAX_HKDF_OUTPUT = 255 * 32;
    const result = new Uint8Array(length);
    let offset = 0;
    let blockIndex = 0;

    while (offset < length) {
      const remaining = length - offset;
      const chunkLen = Math.min(remaining, MAX_HKDF_OUTPUT);
      const info = Buffer.from(`${baseInfo}_block_${blockIndex}`, 'utf8');
      const chunk = hkdf(sha256, seed, new Uint8Array(0), info, chunkLen);
      result.set(new Uint8Array(chunk), offset);
      offset += chunkLen;
      blockIndex++;
    }

    return result;
  }

  hash(embedding: number[]): string[] {
    if (embedding.length !== this.dims) {
      throw new Error(`Embedding dimension mismatch: expected ${this.dims}, got ${embedding.length}`);
    }

    const results: string[] = new Array(this.nTables);

    for (let t = 0; t < this.nTables; t++) {
      const matrix = this.hyperplanes[t];
      const bits = new Array<string>(this.nBits);

      for (let b = 0; b < this.nBits; b++) {
        const baseOffset = b * this.dims;
        let dot = 0;
        for (let d = 0; d < this.dims; d++) {
          dot += matrix[baseOffset + d] * embedding[d];
        }
        bits[b] = dot >= 0 ? '1' : '0';
      }

      const signature = bits.join('');
      const bucketId = `lsh_t${t}_${signature}`;
      const hashBytes = sha256(Buffer.from(bucketId, 'utf8'));
      results[t] = Buffer.from(hashBytes).toString('hex');
    }

    return results;
  }

  get tables(): number { return this.nTables; }
  get bits(): number { return this.nBits; }
  get dimensions(): number { return this.dims; }
}

// =========================================================================
// Reranker (BM25 + cosine + RRF fusion)
// =========================================================================

const STOP_WORDS = new Set([
  'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'do', 'for',
  'from', 'had', 'has', 'have', 'he', 'her', 'him', 'his', 'how', 'if',
  'in', 'into', 'is', 'it', 'its', 'me', 'my', 'no', 'not', 'of', 'on',
  'or', 'our', 'out', 'she', 'so', 'than', 'that', 'the', 'their', 'them',
  'then', 'there', 'these', 'they', 'this', 'to', 'up', 'us', 'was', 'we',
  'were', 'what', 'when', 'where', 'which', 'who', 'whom', 'why', 'will',
  'with', 'you', 'your',
]);

export function tokenize(text: string, removeStopWords: boolean = true): string[] {
  let tokens = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter((t) => t.length >= 2);

  if (removeStopWords) {
    tokens = tokens.filter((t) => !STOP_WORDS.has(t));
  }

  return tokens.map((t) => stemmer(t));
}

export function bm25Score(
  queryTerms: string[],
  docTerms: string[],
  avgDocLen: number,
  docCount: number,
  termDocFreqs: Map<string, number>,
  k1: number = 1.2,
  b: number = 0.75,
): number {
  if (docTerms.length === 0 || avgDocLen === 0 || docCount === 0) return 0;

  const docTf = new Map<string, number>();
  for (const term of docTerms) {
    docTf.set(term, (docTf.get(term) ?? 0) + 1);
  }

  const docLen = docTerms.length;
  let score = 0;

  for (const qi of queryTerms) {
    const freq = docTf.get(qi) ?? 0;
    if (freq === 0) continue;

    const nqi = termDocFreqs.get(qi) ?? 0;
    const idf = Math.log((docCount - nqi + 0.5) / (nqi + 0.5) + 1);
    const tfNorm = (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * docLen / avgDocLen));
    score += idf * tfNorm;
  }

  return score;
}

export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length === 0 || b.length === 0) return 0;

  const len = Math.min(a.length, b.length);
  let dot = 0;
  let normA = 0;
  let normB = 0;

  for (let i = 0; i < len; i++) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }

  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  if (denom === 0) return 0;
  return dot / denom;
}

export interface RankedItem {
  id: string;
  score: number;
}

export function rrfFuse(rankings: RankedItem[][], k: number = 60): RankedItem[] {
  const fusedScores = new Map<string, number>();

  for (const ranking of rankings) {
    for (let rank = 0; rank < ranking.length; rank++) {
      const item = ranking[rank];
      const contribution = 1 / (k + rank + 1);
      fusedScores.set(item.id, (fusedScores.get(item.id) ?? 0) + contribution);
    }
  }

  const fused: RankedItem[] = [];
  for (const [id, score] of fusedScores) {
    fused.push({ id, score });
  }

  fused.sort((a, b) => b.score - a.score);
  return fused;
}
