/**
 * TotalReclaw E2E OMBH Validation — Subgraph Pipeline
 *
 * Ingests 415 benchmark facts through the full subgraph pipeline
 * (encrypt -> blind indices -> embedding -> LSH -> protobuf -> on-chain tx -> Graph Node)
 * and validates recall against 140 ground-truth queries.
 *
 * Prerequisites:
 *   - dev.sh running in another terminal (Hardhat + Graph Node)
 *   - ONNX model will auto-download on first run (~33.8MB)
 *
 * Run with (from subgraph/): npx tsx --tsconfig tsconfig.node.json tests/e2e-ombh-validation.ts
 *
 * Note: Like pocv2-e2e-test.ts, this script inlines crypto functions with
 * .js import paths that work under npx tsx (OpenClaw's bundler uses bare paths).
 */

import { ethers } from 'ethers';
import * as fs from 'node:fs';
import * as path from 'node:path';
import crypto from 'node:crypto';

// Noble hashes (with .js extensions for tsx compatibility)
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { hmac } from '@noble/hashes/hmac.js';
import { mnemonicToSeedSync, generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { stemmer } from 'porter-stemmer';

// Local modules (relative, .js extension for ESM resolution)
import { LSHHasher } from '../../skill/plugin/lsh.js';
import { rerank, type RerankerCandidate } from '../../skill/plugin/reranker.js';
import { encodeFactProtobuf, type FactPayload } from '../../skill/plugin/subgraph-store.js';

// @ts-ignore - @huggingface/transformers types
import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HARDHAT_RPC = 'http://127.0.0.1:8545';
const SUBGRAPH_ENDPOINT = 'http://localhost:8000/subgraphs/name/totalreclaw';

/** Hardhat default account #0 — used as deployer AND contract owner in local dev. */
// Note: We use provider.getSigner(0) instead of a hardcoded key to ensure we get
// the actual Hardhat account that deployed (and owns) the contracts.

/** Path to OMBH ground truth data (in totalreclaw-internal repo, private, maintainers only). */
const OMBH_FACTS_PATH = path.resolve(
  __dirname, '..', '..', '..', 'totalreclaw-internal',
  'ombh', 'synthetic-benchmark', 'ground-truth', 'facts-ingested.json',
);
const OMBH_QUERIES_PATH = path.resolve(
  __dirname, '..', '..', '..', 'totalreclaw-internal',
  'ombh', 'synthetic-benchmark', 'ground-truth', 'queries-ingested.json',
);

/** Output directory for results. */
const RESULTS_DIR = path.resolve(__dirname, 'e2e-results');

/** Deterministic test mnemonic — 12 words derived from a fixed seed. */
const TEST_MNEMONIC = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about';

// NOTE: This test still uses the legacy bge-small-en-v1.5 model for local Hardhat/Graph Node testing.
// Production uses Harrier-OSS-v1-270M (640d). Update when test infra supports the larger model.
const EMBEDDING_MODEL_ID = 'Xenova/bge-small-en-v1.5';
const EMBEDDING_DIM = 384;
const QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';

// ---------------------------------------------------------------------------
// Crypto (inlined for tsx compatibility — matches crypto.ts exactly)
// ---------------------------------------------------------------------------

const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';
const LSH_SEED_INFO = 'openmemory-lsh-seed-v1';

const IV_LENGTH = 12;
const TAG_LENGTH = 16;
const KEY_LENGTH = 32;

interface DerivedKeys {
  encryptionKey: Buffer;
  dedupKey: Buffer;
  salt: Buffer;
  lshSeed: Uint8Array;
}

function deriveKeysFromMnemonic(mnemonic: string): DerivedKeys {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = Buffer.from(seed.slice(0, 32));
  const seedBuf = Buffer.from(seed);
  const enc = (s: string) => Buffer.from(s, 'utf8');

  const encryptionKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(ENCRYPTION_KEY_INFO), 32),
  );
  const dedupKey = Buffer.from(
    hkdf(sha256, seedBuf, salt, enc(DEDUP_KEY_INFO), 32),
  );
  const lshSeed = new Uint8Array(
    hkdf(sha256, seedBuf, salt, enc(LSH_SEED_INFO), 32),
  );

  return { encryptionKey, dedupKey, salt, lshSeed };
}

function encrypt(plaintext: string, encryptionKey: Buffer): string {
  const iv = crypto.randomBytes(IV_LENGTH);
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey, iv, {
    authTagLength: TAG_LENGTH,
  });
  const ciphertext = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, ciphertext]).toString('base64');
}

function decrypt(encryptedBase64: string, encryptionKey: Buffer): string {
  const combined = Buffer.from(encryptedBase64, 'base64');
  const iv = combined.subarray(0, IV_LENGTH);
  const tag = combined.subarray(IV_LENGTH, IV_LENGTH + TAG_LENGTH);
  const ciphertext = combined.subarray(IV_LENGTH + TAG_LENGTH);
  const decipher = crypto.createDecipheriv('aes-256-gcm', encryptionKey, iv, {
    authTagLength: TAG_LENGTH,
  });
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}

function encryptToHex(plaintext: string, key: Buffer): string {
  return Buffer.from(encrypt(plaintext, key), 'base64').toString('hex');
}

function decryptFromHex(hexBlob: string, key: Buffer): string {
  return decrypt(Buffer.from(hexBlob, 'hex').toString('base64'), key);
}

function generateBlindIndices(text: string): string[] {
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
        sha256(Buffer.from(`stem:${stem}`, 'utf8')),
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
  return text.normalize('NFC').toLowerCase().replace(/\s+/g, ' ').trim();
}

function generateContentFingerprint(plaintext: string, dedupKey: Buffer): string {
  const normalized = normalizeText(plaintext);
  return Buffer.from(
    hmac(sha256, dedupKey, Buffer.from(normalized, 'utf8')),
  ).toString('hex');
}

// ---------------------------------------------------------------------------
// Embedding (local ONNX bge-small-en-v1.5)
// ---------------------------------------------------------------------------

let extractor: FeatureExtractionPipeline | null = null;

async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!extractor) {
    extractor = await pipeline('feature-extraction', EMBEDDING_MODEL_ID, {
      dtype: 'q8',
    });
  }
  const input = options?.isQuery ? QUERY_PREFIX + text : text;
  const output = await extractor(input, { pooling: 'mean', normalize: true });
  return Array.from(output.data as Float32Array);
}

// ---------------------------------------------------------------------------
// OMBH Data Types
// ---------------------------------------------------------------------------

interface OmbhFact {
  id: string;
  text: string;
  type: string;
  importance: number;
  source_conversations: string[];
  first_mentioned: string;
}

interface OmbhQueryRelevant {
  fact_id: string;
  relevance: number;
}

interface OmbhQuery {
  id: string;
  text: string;
  category: string;
  relevant_facts: OmbhQueryRelevant[];
  source_fact_batch: string[];
}

interface OmbhFactsFile {
  metadata: { total_facts: number };
  facts: OmbhFact[];
}

interface OmbhQueriesFile {
  metadata: {
    total_queries: number;
    generation_stats: {
      category_distribution: Record<string, number>;
    };
  };
  queries: OmbhQuery[];
}

// ---------------------------------------------------------------------------
// Subgraph GraphQL Client
// ---------------------------------------------------------------------------

async function querySubgraph(query: string, variables: Record<string, unknown> = {}): Promise<any> {
  const response = await fetch(SUBGRAPH_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, variables }),
  });
  if (!response.ok) {
    throw new Error(`Subgraph query failed: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function getIndexedFactCount(): Promise<number> {
  const result = await querySubgraph(`{
    globalStates(first: 1) { totalFacts }
  }`);
  const states = result?.data?.globalStates;
  if (!states || states.length === 0) return 0;
  return parseInt(states[0].totalFacts, 10);
}

/** Page size for GraphQL queries — must not exceed GRAPH_GRAPHQL_MAX_FIRST (default 5000). */
const SEARCH_PAGE_SIZE = parseInt(process.env.TOTALRECLAW_SUBGRAPH_PAGE_SIZE ?? '5000', 10);

async function searchByBlindIndices(
  owner: string,
  trapdoors: string[],
  maxCandidates: number = 5000,
): Promise<Array<{
  id: string;
  encryptedBlob: string;
  encryptedEmbedding: string | null;
  decayScore: string;
  isActive: boolean;
}>> {
  const BATCH_SIZE = 500;
  const allResults = new Map<string, any>();

  for (let i = 0; i < trapdoors.length; i += BATCH_SIZE) {
    const batch = trapdoors.slice(i, i + BATCH_SIZE);

    // Initial query for this batch
    const result = await querySubgraph(`
      query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
        blindIndexes(
          where: { hash_in: $trapdoors, owner: $owner }
          first: $first
          orderBy: id
          orderDirection: asc
        ) {
          id
          fact {
            id
            encryptedBlob
            encryptedEmbedding
            decayScore
            isActive
          }
        }
      }
    `, {
      trapdoors: batch,
      owner,
      first: SEARCH_PAGE_SIZE,
    });

    const entries = result?.data?.blindIndexes ?? [];
    for (const entry of entries) {
      if (entry.fact && !allResults.has(entry.fact.id)) {
        allResults.set(entry.fact.id, entry.fact);
      }
    }

    // Cursor-based pagination if this batch was saturated
    if (entries.length >= SEARCH_PAGE_SIZE && allResults.size < maxCandidates) {
      let lastId = entries[entries.length - 1].id;

      while (allResults.size < maxCandidates) {
        const pageResult = await querySubgraph(`
          query PaginateBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!, $lastId: String!) {
            blindIndexes(
              where: { hash_in: $trapdoors, owner: $owner, id_gt: $lastId }
              first: $first
              orderBy: id
              orderDirection: asc
            ) {
              id
              fact {
                id
                encryptedBlob
                encryptedEmbedding
                decayScore
                isActive
              }
            }
          }
        `, {
          trapdoors: batch,
          owner,
          first: SEARCH_PAGE_SIZE,
          lastId,
        });

        const pageEntries = pageResult?.data?.blindIndexes ?? [];
        if (pageEntries.length === 0) break;

        for (const entry of pageEntries) {
          if (entry.fact && !allResults.has(entry.fact.id)) {
            allResults.set(entry.fact.id, entry.fact);
          }
        }

        if (pageEntries.length < SEARCH_PAGE_SIZE) break;
        lastId = pageEntries[pageEntries.length - 1].id;
      }
    }

    if (allResults.size >= maxCandidates) break;
  }

  return Array.from(allResults.values());
}

// ---------------------------------------------------------------------------
// Timing Utilities
// ---------------------------------------------------------------------------

function hrMs(start: [number, number]): number {
  const [s, ns] = process.hrtime(start);
  return s * 1000 + ns / 1e6;
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.ceil((p / 100) * sorted.length) - 1;
  return sorted[Math.max(0, idx)];
}

function median(sorted: number[]): number {
  return percentile(sorted, 50);
}

// ---------------------------------------------------------------------------
// Main E2E Validation
// ---------------------------------------------------------------------------

async function main() {
  console.log('');
  console.log('========================================================');
  console.log('  TotalReclaw E2E OMBH Validation — Subgraph Pipeline');
  console.log('========================================================');
  console.log('');

  // ---- Step 0: Verify dev environment ----
  console.log('[0/6] Verifying dev environment...');

  try {
    const meta = await querySubgraph('{ _meta { block { number } } }');
    console.log(`  Graph Node is running. Current block: ${meta?.data?._meta?.block?.number}`);
  } catch (err) {
    console.error('ERROR: Graph Node not reachable at', SUBGRAPH_ENDPOINT);
    console.error('Make sure dev.sh is running: cd subgraph && ./scripts/dev.sh');
    process.exit(1);
  }

  // ---- Step 1: Load OMBH data ----
  console.log('[1/6] Loading OMBH ground truth data...');

  if (!fs.existsSync(OMBH_FACTS_PATH)) {
    console.error(`ERROR: Facts file not found: ${OMBH_FACTS_PATH}`);
    console.error('Make sure totalreclaw-internal repo (private, maintainers only) is cloned alongside this repo.');
    process.exit(1);
  }
  if (!fs.existsSync(OMBH_QUERIES_PATH)) {
    console.error(`ERROR: Queries file not found: ${OMBH_QUERIES_PATH}`);
    process.exit(1);
  }

  const factsFile: OmbhFactsFile = JSON.parse(fs.readFileSync(OMBH_FACTS_PATH, 'utf-8'));
  const queriesFile: OmbhQueriesFile = JSON.parse(fs.readFileSync(OMBH_QUERIES_PATH, 'utf-8'));

  const facts = factsFile.facts;
  const queries = queriesFile.queries;

  console.log(`  Facts: ${facts.length} (expected ${factsFile.metadata.total_facts})`);
  console.log(`  Queries: ${queries.length} (expected ${queriesFile.metadata.total_queries})`);
  console.log(`  Categories: ${JSON.stringify(queriesFile.metadata.generation_stats.category_distribution)}`);

  if (facts.length !== factsFile.metadata.total_facts) {
    console.error(`ERROR: Expected ${factsFile.metadata.total_facts} facts, got ${facts.length}`);
    process.exit(1);
  }

  // ---- Step 2: Initialize crypto + embedding ----
  console.log('[2/6] Initializing crypto, embedding model, and LSH hasher...');

  const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
  console.log(`  Encryption key: ${keys.encryptionKey.length * 8}-bit`);
  console.log(`  LSH seed: ${keys.lshSeed.length} bytes`);

  // Initialize embedding model (downloads ~33.8MB on first run)
  console.log('  Loading embedding model (bge-small-en-v1.5 ONNX, may download on first run)...');
  const testEmb = await generateEmbedding('initialization test');
  console.log(`  Embedding model ready: ${testEmb.length}-dim vectors`);

  const lshHasher = new LSHHasher(keys.lshSeed, EMBEDDING_DIM);
  console.log(`  LSH hasher: ${lshHasher.tables} tables, ${lshHasher.bits} bits/table`);

  // ---- Step 3: Connect to Hardhat node ----
  console.log('[3/6] Connecting to Hardhat node...');

  const provider = new ethers.JsonRpcProvider(HARDHAT_RPC);
  const deployer = await provider.getSigner(0);

  // Read deployed contract address
  const addressesPath = path.resolve(__dirname, '..', '..', 'contracts', 'deployed-addresses.json');
  if (!fs.existsSync(addressesPath)) {
    console.error(`ERROR: deployed-addresses.json not found at ${addressesPath}`);
    console.error('Run dev.sh first to deploy contracts.');
    process.exit(1);
  }
  const addresses = JSON.parse(fs.readFileSync(addressesPath, 'utf-8'));
  const dataEdgeAddress = addresses.eventfulDataEdge;

  console.log(`  Deployer: ${deployer.address}`);
  console.log(`  DataEdge: ${dataEdgeAddress}`);
  console.log(`  EntryPoint: ${addresses.entryPoint}`);

  // The EventfulDataEdge fallback checks `require(msg.sender == entryPoint)`.
  // On localhost, deploy.ts sets entryPoint to the canonical ERC-4337 address.
  // Use setEntryPoint() to update it to the deployer address for local testing.
  if (deployer.address.toLowerCase() !== addresses.entryPoint.toLowerCase()) {
    console.log('  EntryPoint != deployer — calling setEntryPoint() to update...');
    const abi = ['function setEntryPoint(address _newEntryPoint) external'];
    const dataEdge = new ethers.Contract(dataEdgeAddress, abi, deployer);
    const tx = await dataEdge.setEntryPoint(deployer.address);
    await tx.wait();
    console.log(`  EntryPoint updated to deployer: ${deployer.address}`);
  }
  const txSigner = deployer;

  // ---- Step 4: Ingest 415 facts ----
  console.log('[4/6] Ingesting facts on-chain...');
  console.log(`  Sending ${facts.length} transactions to DataEdge contract...`);
  console.log('');

  const ingestTimings: number[] = [];
  const ingestStart = process.hrtime();
  let txErrors = 0;

  // Build a fact ID -> text map for recall evaluation
  const factIdToText = new Map<string, string>();
  // Build a fact ID -> on-chain fact ID map (on-chain IDs may differ)
  const factIdMap = new Map<string, string>();

  for (let i = 0; i < facts.length; i++) {
    const fact = facts[i];
    const factStart = process.hrtime();

    try {
      // 1. Create document JSON
      const doc = {
        text: fact.text,
        metadata: {
          type: fact.type,
          importance: fact.importance,
          source: 'benchmark',
        },
      };

      // 2. Encrypt the document
      const encryptedBlob = encryptToHex(JSON.stringify(doc), keys.encryptionKey);

      // 3. Generate blind indices (word + stem)
      const wordIndices = generateBlindIndices(fact.text);

      // 4. Generate embedding
      const embedding = await generateEmbedding(fact.text);

      // 5. Generate LSH bucket hashes
      const lshBuckets = lshHasher.hash(embedding);

      // 6. Merge blind indices (word + LSH)
      const allBlindIndices = [...wordIndices, ...lshBuckets];

      // 7. Encrypt embedding
      const encryptedEmbedding = encryptToHex(JSON.stringify(embedding), keys.encryptionKey);

      // 8. Content fingerprint
      const contentFp = generateContentFingerprint(fact.text, keys.dedupKey);

      // 9. Build Protobuf payload
      const factPayload: FactPayload = {
        id: fact.id,
        timestamp: new Date().toISOString(),
        owner: deployer.address,
        encryptedBlob,
        blindIndices: allBlindIndices,
        decayScore: 1.0,
        source: 'benchmark',
        contentFp,
        agentId: 'e2e-ombh-validation',
        encryptedEmbedding,
      };

      const protobuf = encodeFactProtobuf(factPayload);

      // 10. Send as transaction from the EntryPoint signer (or deployer if they're the same)
      const tx = await txSigner.sendTransaction({
        to: dataEdgeAddress,
        data: '0x' + protobuf.toString('hex'),
        gasLimit: 3_000_000,
      });
      await tx.wait();

      const elapsed = hrMs(factStart);
      ingestTimings.push(elapsed);

      factIdToText.set(fact.id, fact.text);
      factIdMap.set(fact.id, fact.id);

      // Progress indicator every 50 facts
      if ((i + 1) % 50 === 0 || i === facts.length - 1) {
        const totalElapsed = hrMs(ingestStart) / 1000;
        const rate = (i + 1) / totalElapsed;
        console.log(`  [${i + 1}/${facts.length}] ${elapsed.toFixed(0)}ms last | ${rate.toFixed(1)} facts/s | ${totalElapsed.toFixed(1)}s elapsed`);
      }
    } catch (err) {
      txErrors++;
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`  ERROR on fact ${fact.id}: ${msg}`);
      if (txErrors > 10) {
        console.error('Too many transaction errors. Aborting.');
        process.exit(1);
      }
    }
  }

  const totalIngestTime = hrMs(ingestStart);
  ingestTimings.sort((a, b) => a - b);

  console.log('');
  console.log('  Ingest complete:');
  console.log(`    Total time: ${(totalIngestTime / 1000).toFixed(1)}s`);
  console.log(`    Per-fact avg: ${(totalIngestTime / facts.length).toFixed(0)}ms`);
  console.log(`    Per-fact median: ${median(ingestTimings).toFixed(0)}ms`);
  console.log(`    Per-fact p95: ${percentile(ingestTimings, 95).toFixed(0)}ms`);
  console.log(`    Per-fact p99: ${percentile(ingestTimings, 99).toFixed(0)}ms`);
  console.log(`    Tx errors: ${txErrors}`);

  // ---- Step 5: Wait for Graph Node indexing ----
  console.log('[5/6] Waiting for Graph Node to index all facts...');

  const expectedCount = facts.length - txErrors;
  const indexStart = process.hrtime();
  const INDEX_TIMEOUT_MS = 120_000; // 2 minutes
  const INDEX_POLL_MS = 2_000;

  let indexedCount = 0;
  while (true) {
    indexedCount = await getIndexedFactCount();
    const elapsed = hrMs(indexStart);

    if (indexedCount >= expectedCount) {
      console.log(`  Indexed ${indexedCount} facts in ${(elapsed / 1000).toFixed(1)}s`);
      break;
    }

    if (elapsed > INDEX_TIMEOUT_MS) {
      console.error(`  TIMEOUT: Only ${indexedCount}/${expectedCount} facts indexed after ${(elapsed / 1000).toFixed(0)}s`);
      console.error('  Continuing with partial data...');
      break;
    }

    process.stdout.write(`  Waiting... ${indexedCount}/${expectedCount} (${(elapsed / 1000).toFixed(0)}s)\r`);
    await new Promise((r) => setTimeout(r, INDEX_POLL_MS));
  }

  // Get blind index count
  const blindIndexResult = await querySubgraph(`{
    globalStates(first: 1) { totalFacts }
  }`);

  // Count total blind index entities (sample first 1000)
  const blindIndexCountResult = await querySubgraph(`{
    blindIndexes(first: 1000) { id }
  }`);
  const blindIndexSample = blindIndexCountResult?.data?.blindIndexes?.length ?? 0;
  console.log(`  BlindIndex entities (sampled): >= ${blindIndexSample}`);

  // ---- Step 6: Run 140 queries ----
  console.log('[6/6] Running queries and evaluating recall...');
  console.log('');

  interface QueryResult {
    queryId: string;
    category: string;
    recall8: number;
    precision8: number;
    mrr: number;
    candidateCount: number;
    timeMs: number;
    prepTimeMs: number;
    graphqlTimeMs: number;
    rerankTimeMs: number;
    topKIds: string[];
    expectedIds: string[];
  }

  const queryResults: QueryResult[] = [];
  const queryTimings: number[] = [];

  // Owner address for subgraph queries (lowercase hex as Bytes type)
  const ownerHex = deployer.address.toLowerCase();

  for (let i = 0; i < queries.length; i++) {
    const query = queries[i];
    const queryStart = process.hrtime();

    try {
      const prepStart = process.hrtime();

      // 1. Generate word trapdoors
      const wordTrapdoors = generateBlindIndices(query.text);

      // 2. Generate query embedding + LSH trapdoors
      const queryEmbedding = await generateEmbedding(query.text, { isQuery: true });
      const lshTrapdoors = lshHasher.hash(queryEmbedding);

      // 3. Merge all trapdoors
      const allTrapdoors = [...wordTrapdoors, ...lshTrapdoors];

      const prepTimeMs = hrMs(prepStart);
      const graphqlStart = process.hrtime();

      // 4. Query subgraph via GraphQL hash_in
      const candidates = await searchByBlindIndices(ownerHex, allTrapdoors);

      const graphqlTimeMs = hrMs(graphqlStart);
      const rerankStart = process.hrtime();

      // 5. Decrypt returned facts and build reranker input
      const rerankerCandidates: RerankerCandidate[] = [];

      for (const candidate of candidates) {
        try {
          // The subgraph returns encryptedBlob as hex-encoded Bytes
          let blobHex = candidate.encryptedBlob;
          // Strip 0x prefix if present
          if (blobHex.startsWith('0x')) blobHex = blobHex.slice(2);

          const docJson = decryptFromHex(blobHex, keys.encryptionKey);
          const doc = JSON.parse(docJson) as { text: string };

          let decryptedEmbedding: number[] | undefined;
          if (candidate.encryptedEmbedding) {
            try {
              let embHex = candidate.encryptedEmbedding;
              if (embHex.startsWith('0x')) embHex = embHex.slice(2);
              decryptedEmbedding = JSON.parse(decryptFromHex(embHex, keys.encryptionKey));
            } catch {
              // Skip bad embedding
            }
          }

          rerankerCandidates.push({
            id: candidate.id,
            text: doc.text,
            embedding: decryptedEmbedding,
          });
        } catch {
          // Skip un-decryptable candidates
        }
      }

      // 6. Rerank with BM25 + cosine + RRF
      const reranked = rerank(query.text, queryEmbedding, rerankerCandidates, 8);

      const rerankTimeMs = hrMs(rerankStart);

      const elapsed = hrMs(queryStart);
      queryTimings.push(elapsed);

      // 7. Evaluate against ground truth
      const expectedIds = query.relevant_facts.map((rf) => rf.fact_id);
      const topKIds = reranked.map((r) => r.id);

      // For recall: how many of the expected facts appear in our top-8?
      // We match by fact text since on-chain IDs are the protobuf IDs
      const expectedTexts = new Set(expectedIds.map((id) => factIdToText.get(id)).filter(Boolean));
      const retrievedTexts = new Set(reranked.map((r) => r.text));

      let hits = 0;
      for (const text of expectedTexts) {
        if (text && retrievedTexts.has(text)) hits++;
      }

      const recall8 = expectedTexts.size > 0 ? hits / expectedTexts.size : 0;
      const precision8 = reranked.length > 0 ? hits / reranked.length : 0;

      // MRR: reciprocal rank of the first relevant result
      let mrr = 0;
      for (let rank = 0; rank < reranked.length; rank++) {
        if (expectedTexts.has(reranked[rank].text)) {
          mrr = 1 / (rank + 1);
          break;
        }
      }

      queryResults.push({
        queryId: query.id,
        category: query.category,
        recall8,
        precision8,
        mrr,
        candidateCount: candidates.length,
        timeMs: elapsed,
        prepTimeMs,
        graphqlTimeMs,
        rerankTimeMs,
        topKIds,
        expectedIds,
      });

      // Progress indicator every 20 queries
      if ((i + 1) % 20 === 0 || i === queries.length - 1) {
        console.log(`  [${i + 1}/${queries.length}] ${elapsed.toFixed(0)}ms | recall@8=${recall8.toFixed(2)} | candidates=${candidates.length}`);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`  ERROR on query ${query.id}: ${msg}`);
      queryResults.push({
        queryId: query.id,
        category: query.category,
        recall8: 0,
        precision8: 0,
        mrr: 0,
        candidateCount: 0,
        timeMs: hrMs(queryStart),
        prepTimeMs: 0,
        graphqlTimeMs: 0,
        rerankTimeMs: 0,
        topKIds: [],
        expectedIds: query.relevant_facts.map((rf) => rf.fact_id),
      });
    }
  }

  queryTimings.sort((a, b) => a - b);

  // ---- Generate Report ----
  console.log('');
  console.log('========================================================');
  console.log('  RESULTS');
  console.log('========================================================');

  // Overall metrics
  const overallRecall = queryResults.reduce((s, r) => s + r.recall8, 0) / queryResults.length;
  const overallPrecision = queryResults.reduce((s, r) => s + r.precision8, 0) / queryResults.length;
  const overallMRR = queryResults.reduce((s, r) => s + r.mrr, 0) / queryResults.length;

  console.log('');
  console.log('  Overall Metrics:');
  console.log(`    Recall@8:    ${(overallRecall * 100).toFixed(1)}%`);
  console.log(`    Precision@8: ${(overallPrecision * 100).toFixed(1)}%`);
  console.log(`    MRR:         ${overallMRR.toFixed(3)}`);

  // Per-category metrics
  const categories = ['factual', 'semantic', 'cross_conversation', 'negative'];
  console.log('');
  console.log('  Per-Category Recall@8:');
  const categoryMetrics: Record<string, { recall: number; precision: number; mrr: number; count: number }> = {};

  for (const cat of categories) {
    const catResults = queryResults.filter((r) => r.category === cat);
    if (catResults.length === 0) continue;

    const catRecall = catResults.reduce((s, r) => s + r.recall8, 0) / catResults.length;
    const catPrecision = catResults.reduce((s, r) => s + r.precision8, 0) / catResults.length;
    const catMRR = catResults.reduce((s, r) => s + r.mrr, 0) / catResults.length;

    categoryMetrics[cat] = { recall: catRecall, precision: catPrecision, mrr: catMRR, count: catResults.length };
    console.log(`    ${cat.padEnd(20)} ${(catRecall * 100).toFixed(1).padStart(6)}% recall | ${(catPrecision * 100).toFixed(1).padStart(6)}% precision | MRR ${catMRR.toFixed(3)} (n=${catResults.length})`);
  }

  // Timing statistics
  console.log('');
  console.log('  Ingest Timing:');
  console.log(`    Total:  ${(totalIngestTime / 1000).toFixed(1)}s for ${facts.length} facts`);
  console.log(`    Avg:    ${(totalIngestTime / facts.length).toFixed(0)}ms/fact`);
  console.log(`    Median: ${median(ingestTimings).toFixed(0)}ms`);
  console.log(`    p95:    ${percentile(ingestTimings, 95).toFixed(0)}ms`);
  console.log(`    p99:    ${percentile(ingestTimings, 99).toFixed(0)}ms`);

  console.log('');
  console.log('  Query Timing:');
  console.log(`    Total:  ${(queryTimings.reduce((a, b) => a + b, 0) / 1000).toFixed(1)}s for ${queries.length} queries`);
  console.log(`    Avg:    ${(queryTimings.reduce((a, b) => a + b, 0) / queries.length).toFixed(0)}ms/query`);
  console.log(`    Median: ${median(queryTimings).toFixed(0)}ms`);
  console.log(`    p95:    ${percentile(queryTimings, 95).toFixed(0)}ms`);
  console.log(`    p99:    ${percentile(queryTimings, 99).toFixed(0)}ms`);

  // Latency breakdown
  const prepTimes = queryResults.filter(r => r.prepTimeMs > 0).map(r => r.prepTimeMs).sort((a, b) => a - b);
  const gqlTimes = queryResults.filter(r => r.graphqlTimeMs > 0).map(r => r.graphqlTimeMs).sort((a, b) => a - b);
  const rerankTimes = queryResults.filter(r => r.rerankTimeMs > 0).map(r => r.rerankTimeMs).sort((a, b) => a - b);

  console.log('');
  console.log('  Query Latency Breakdown:');
  if (prepTimes.length > 0) {
    const avgPrep = prepTimes.reduce((a, b) => a + b, 0) / prepTimes.length;
    console.log(`    Client prep (avg/p95):   ${avgPrep.toFixed(0)}ms / ${percentile(prepTimes, 95).toFixed(0)}ms  (embed + blind indices + LSH)`);
  }
  if (gqlTimes.length > 0) {
    const avgGql = gqlTimes.reduce((a, b) => a + b, 0) / gqlTimes.length;
    console.log(`    GraphQL (avg/p95):       ${avgGql.toFixed(0)}ms / ${percentile(gqlTimes, 95).toFixed(0)}ms  (network + Graph Node)`);
  }
  if (rerankTimes.length > 0) {
    const avgRerank = rerankTimes.reduce((a, b) => a + b, 0) / rerankTimes.length;
    console.log(`    Reranking (avg/p95):     ${avgRerank.toFixed(0)}ms / ${percentile(rerankTimes, 95).toFixed(0)}ms  (decrypt + BM25 + cosine + RRF)`);
  }

  // Subgraph entity counts
  console.log('');
  console.log('  Subgraph State:');
  console.log(`    Facts indexed:      ${indexedCount}`);
  console.log(`    BlindIndex sample:  >= ${blindIndexSample}`);
  console.log(`    Tx errors:          ${txErrors}`);

  // PoC v2 baseline comparison
  console.log('');
  console.log('  Comparison with PoC v2 Baseline:');
  console.log('    PoC v2 (PostgreSQL):  98.1% Recall@8 (32-bit x 20 tables, in-memory simulation)');
  console.log(`    Subgraph (on-chain):  ${(overallRecall * 100).toFixed(1)}% Recall@8 (full E2E pipeline)`);
  console.log(`    Delta:                ${((overallRecall * 100) - 98.1).toFixed(1)} percentage points`);

  // ---- Save Results to JSON ----
  const report = {
    timestamp: new Date().toISOString(),
    mnemonic: TEST_MNEMONIC,
    factCount: facts.length,
    queryCount: queries.length,
    txErrors,
    indexedCount,
    blindIndexSample,
    ingest: {
      totalMs: totalIngestTime,
      avgMs: totalIngestTime / facts.length,
      medianMs: median(ingestTimings),
      p95Ms: percentile(ingestTimings, 95),
      p99Ms: percentile(ingestTimings, 99),
    },
    query: {
      totalMs: queryTimings.reduce((a, b) => a + b, 0),
      avgMs: queryTimings.reduce((a, b) => a + b, 0) / queries.length,
      medianMs: median(queryTimings),
      p95Ms: percentile(queryTimings, 95),
      p99Ms: percentile(queryTimings, 99),
      prepAvgMs: prepTimes.length > 0 ? prepTimes.reduce((a, b) => a + b, 0) / prepTimes.length : 0,
      prepP95Ms: prepTimes.length > 0 ? percentile(prepTimes, 95) : 0,
      graphqlAvgMs: gqlTimes.length > 0 ? gqlTimes.reduce((a, b) => a + b, 0) / gqlTimes.length : 0,
      graphqlP95Ms: gqlTimes.length > 0 ? percentile(gqlTimes, 95) : 0,
      rerankAvgMs: rerankTimes.length > 0 ? rerankTimes.reduce((a, b) => a + b, 0) / rerankTimes.length : 0,
      rerankP95Ms: rerankTimes.length > 0 ? percentile(rerankTimes, 95) : 0,
    },
    overall: {
      recall8: overallRecall,
      precision8: overallPrecision,
      mrr: overallMRR,
    },
    categories: categoryMetrics,
    perQuery: queryResults.map((r) => ({
      id: r.queryId,
      category: r.category,
      recall8: r.recall8,
      precision8: r.precision8,
      mrr: r.mrr,
      candidateCount: r.candidateCount,
      timeMs: r.timeMs,
      prepTimeMs: r.prepTimeMs,
      graphqlTimeMs: r.graphqlTimeMs,
      rerankTimeMs: r.rerankTimeMs,
    })),
  };

  const reportPath = path.join(RESULTS_DIR, `e2e-results-${Date.now()}.json`);
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  console.log('');
  console.log(`  Results saved to: ${reportPath}`);

  // Also write a latest symlink/copy
  const latestPath = path.join(RESULTS_DIR, 'e2e-results-latest.json');
  fs.writeFileSync(latestPath, JSON.stringify(report, null, 2));
  console.log(`  Latest results:   ${latestPath}`);

  console.log('');
  console.log('========================================================');

  // Exit with error if recall is below threshold
  if (overallRecall < 0.50) {
    console.log('  RESULT: FAIL (recall@8 < 50%)');
    process.exit(1);
  } else {
    console.log('  RESULT: PASS');
  }
}

main().catch((err) => {
  console.error('');
  console.error('FATAL ERROR:', err instanceof Error ? err.message : String(err));
  if (err instanceof Error && err.stack) {
    console.error(err.stack);
  }
  process.exit(1);
});
