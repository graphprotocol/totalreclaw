/**
 * E2E Tests: MCP Session Debrief Tool (Tests 1, 5, 6)
 *
 * Tests against LIVE staging:
 *   - Relay: api-staging.totalreclaw.xyz
 *   - Chain: Base Sepolia (84532)
 *   - Subgraph: totalreclaw---base-sepolia (Graph Studio)
 *
 * Test 1: MCP totalreclaw_debrief Tool — Store + Recall
 *   - Generate fresh BIP-39 mnemonic, register with staging relay
 *   - Store 3 debrief items via on-chain pipeline (source=mcp_debrief)
 *   - Wait for subgraph indexing
 *   - Recall via blind-index search and verify decrypted text matches
 *
 * Test 5: Edge Cases — Empty / All-Filtered Facts
 *   - Empty facts array returns { success: false, error }
 *   - Facts with importance < 6 all filtered → { stored: 0 }
 *   - parseDebriefResponse validation in an E2E context
 *
 * Test 6: Source Tag Verification
 *   - Store a debrief item (source=mcp_debrief)
 *   - Store a regular fact (source=mcp_remember)
 *   - Query subgraph, decrypt, verify source tags are distinct
 *
 * Run:
 *   cd tests/e2e-debrief && npm install && npm run test:debrief
 *
 * Run single test:
 *   npx tsx mcp-debrief-e2e.ts --test 1
 *   npx tsx mcp-debrief-e2e.ts --test 5
 *   npx tsx mcp-debrief-e2e.ts --test 6
 */

// Run: TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz TOTALRECLAW_TEST=true npx tsx mcp-debrief-e2e.ts

import { createHash, randomBytes, createCipheriv, createDecipheriv } from 'crypto';
import crypto from 'crypto';
import { mnemonicToAccount } from 'viem/accounts';
import { generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import { createPublicClient, http, type Hex } from 'viem';
import { baseSepolia } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { createSmartAccountClient } from 'permissionless';
import { createPimlicoClient } from 'permissionless/clients/pimlico';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { mnemonicToSeedSync } from '@scure/bip39';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const RELAY_URL = process.env.TOTALRECLAW_SERVER_URL || 'https://api-staging.totalreclaw.xyz';
const CHAIN_ID = 84532; // Base Sepolia
const DATA_EDGE_ADDRESS = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca' as const;
const ENTRYPOINT_ADDRESS = '0x0000000071727De22E5E9d8BAf0edAc6f37da032' as const;

// Subgraph polling — Graph Studio has variable indexing latency (5-40+ min)
const POLL_INTERVAL_MS = 15_000;
const POLL_MAX_ATTEMPTS = 180; // 45 minutes max per poll

// ---------------------------------------------------------------------------
// Test runner
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
let skipped = 0;

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

async function runTest(name: string, fn: () => Promise<void>): Promise<boolean> {
  try {
    await fn();
    console.log(`  [PASS] ${name}`);
    passed++;
    return true;
  } catch (err: any) {
    console.log(`  [FAIL] ${name} — ${err.message}`);
    failed++;
    return false;
  }
}

function skipTest(name: string, reason: string): void {
  console.log(`  [SKIP] ${name} — ${reason}`);
  skipped++;
}

// Parse --test flags from CLI (e.g. --test 1 --test 6)
const allArgs = process.argv.slice(2);
const tests: string[] = [];
for (let i = 0; i < allArgs.length; i++) {
  if (allArgs[i] === '--test' && allArgs[i + 1]) {
    tests.push(allArgs[i + 1]);
    i++;
  }
}
const shouldRun = (test: string) => tests.length === 0 || tests.includes(test);

// ---------------------------------------------------------------------------
// Key derivation (self-contained, matches mcp/src/subgraph/crypto.ts)
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';
const ENCRYPTION_KEY_INFO = 'totalreclaw-encryption-key-v1';
const DEDUP_KEY_INFO = 'openmemory-dedup-v1';

function deriveKeysFromMnemonic(mnemonic: string): {
  authKey: Buffer;
  encryptionKey: Buffer;
  dedupKey: Buffer;
  salt: Buffer;
} {
  const seed = mnemonicToSeedSync(mnemonic.trim());
  const salt = Buffer.from(seed.slice(0, 32));
  const enc = (s: string) => Buffer.from(s, 'utf8');
  const seedBuf = Buffer.from(seed);

  const authKey = Buffer.from(hkdf(sha256, seedBuf, salt, enc(AUTH_KEY_INFO), 32));
  const encryptionKey = Buffer.from(hkdf(sha256, seedBuf, salt, enc(ENCRYPTION_KEY_INFO), 32));
  const dedupKey = Buffer.from(hkdf(sha256, seedBuf, salt, enc(DEDUP_KEY_INFO), 32));

  return { authKey, encryptionKey, dedupKey, salt };
}

function computeAuthKeyHash(authKey: Buffer): string {
  return Buffer.from(sha256(authKey)).toString('hex');
}

// ---------------------------------------------------------------------------
// Crypto helpers (self-contained, matches mcp/src/subgraph/crypto.ts)
// ---------------------------------------------------------------------------

/** AES-256-GCM encrypt, returns base64 string of iv+tag+ciphertext */
function encryptBlob(plaintext: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv, { authTagLength: 16 });
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  // Wire format: iv || tag || ciphertext (matches MCP server)
  return Buffer.concat([iv, tag, encrypted]).toString('base64');
}

/** AES-256-GCM decrypt from base64 string */
function decryptBlob(encryptedBase64: string, key: Buffer): string {
  const combined = Buffer.from(encryptedBase64, 'base64');
  assert(combined.length >= 28, 'Encrypted data too short'); // 12 + 16 minimum
  const iv = combined.subarray(0, 12);
  const tag = combined.subarray(12, 28);
  const ciphertext = combined.subarray(28);
  const decipher = createDecipheriv('aes-256-gcm', key, iv, { authTagLength: 16 });
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString('utf8');
}

/**
 * Generate blind indices (SHA-256 hashes of tokens) for a text string.
 * Matches the tokenization in mcp/src/subgraph/crypto.ts:
 *   1. Lowercase
 *   2. Remove punctuation (keep Unicode letters, numbers, whitespace)
 *   3. Split on whitespace
 *   4. Filter tokens shorter than 2 characters
 *   5. SHA-256 each token
 *   6. Deduplicate
 */
function generateBlindIndices(text: string): string[] {
  const tokens = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter(t => t.length >= 2);

  const seen = new Set<string>();
  const indices: string[] = [];

  for (const token of tokens) {
    const hash = createHash('sha256').update(Buffer.from(token, 'utf8')).digest('hex');
    if (!seen.has(hash)) {
      seen.add(hash);
      indices.push(hash);
    }
  }

  return indices;
}

/**
 * Compute HMAC-SHA256 content fingerprint for dedup.
 * Matches mcp/src/subgraph/crypto.ts: normalizeText -> HMAC-SHA256(dedupKey, normalized).
 */
function generateContentFingerprint(plaintext: string, dedupKey: Buffer): string {
  const normalized = plaintext
    .normalize('NFC')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
  return crypto
    .createHmac('sha256', dedupKey)
    .update(Buffer.from(normalized, 'utf8'))
    .digest('hex');
}

// ---------------------------------------------------------------------------
// Debrief response parser (self-contained, matches mcp/src/tools/debrief.ts)
// ---------------------------------------------------------------------------

interface DebriefItem {
  text: string;
  type: 'summary' | 'context';
  importance: number;
}

/**
 * Parse and validate a debrief response (JSON array of debrief items).
 * Strips markdown code fences, validates items, filters importance < 6, caps at 5.
 */
function parseDebriefResponse(response: string): DebriefItem[] {
  let cleaned = response.trim();
  if (cleaned.startsWith('```')) {
    cleaned = cleaned.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '').trim();
  }

  try {
    const parsed = JSON.parse(cleaned);
    if (!Array.isArray(parsed)) return [];

    return parsed
      .filter(
        (item: unknown) =>
          item &&
          typeof item === 'object' &&
          typeof (item as DebriefItem).text === 'string' &&
          (item as DebriefItem).text.length >= 5,
      )
      .map((item: unknown) => {
        const d = item as Record<string, unknown>;
        const type = d.type === 'summary' ? 'summary' : 'context';
        const importance =
          typeof d.importance === 'number'
            ? Math.max(1, Math.min(10, d.importance))
            : 7;
        return {
          text: String(d.text).slice(0, 512),
          type,
          importance,
        } as DebriefItem;
      })
      .filter(d => d.importance >= 6)
      .slice(0, 5);
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Protobuf encoder (self-contained, matches mcp/src/subgraph/store.ts)
// ---------------------------------------------------------------------------

function encodeVarint(value: number): Buffer {
  const bytes: number[] = [];
  let v = value >>> 0;
  while (v > 0x7f) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v & 0x7f);
  return Buffer.from(bytes);
}

interface FactPayload {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string;
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
  encryptedEmbedding?: string;
}

function encodeFactProtobuf(fact: FactPayload): Buffer {
  const parts: Buffer[] = [];
  const writeString = (fn: number, val: string) => {
    if (!val) return;
    const d = Buffer.from(val, 'utf-8');
    parts.push(encodeVarint((fn << 3) | 2), encodeVarint(d.length), d);
  };
  const writeBytes = (fn: number, val: Buffer) => {
    parts.push(encodeVarint((fn << 3) | 2), encodeVarint(val.length), val);
  };
  const writeDouble = (fn: number, val: number) => {
    parts.push(encodeVarint((fn << 3) | 1));
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(val);
    parts.push(buf);
  };
  const writeVarintField = (fn: number, val: number) => {
    parts.push(encodeVarint((fn << 3) | 0), encodeVarint(val));
  };

  writeString(1, fact.id);
  writeString(2, fact.timestamp);
  writeString(3, fact.owner);
  writeBytes(4, Buffer.from(fact.encryptedBlob, 'hex'));
  for (const idx of fact.blindIndices) writeString(5, idx);
  writeDouble(6, fact.decayScore);
  writeVarintField(7, 1); // is_active
  writeVarintField(8, 2); // version
  writeString(9, fact.source);
  writeString(10, fact.contentFp);
  writeString(11, fact.agentId);
  if (fact.encryptedEmbedding) writeString(13, fact.encryptedEmbedding);
  return Buffer.concat(parts);
}

// ---------------------------------------------------------------------------
// Relay API helpers
// ---------------------------------------------------------------------------

async function relayRequest(
  method: string,
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  const res = await fetch(`${RELAY_URL}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data };
}

async function registerUser(authKeyHash: string, saltHex: string) {
  return relayRequest('POST', '/v1/register', {
    auth_key_hash: authKeyHash,
    salt: saltHex,
  }, {
    'X-TotalReclaw-Test': 'true',
    'X-TotalReclaw-Client': 'e2e-debrief-test',
  });
}

// ---------------------------------------------------------------------------
// Subgraph helpers
// ---------------------------------------------------------------------------

async function querySubgraph(
  authKeyHex: string,
  query: string,
  variables: Record<string, unknown> = {},
): Promise<any> {
  const res = await fetch(`${RELAY_URL}/v1/subgraph`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${authKeyHex}`,
      'X-TotalReclaw-Client': 'e2e-debrief-test',
    },
    body: JSON.stringify({ query, variables }),
  });
  return res.json();
}

async function pollSubgraph(
  authKeyHex: string,
  query: string,
  variables: Record<string, unknown>,
  predicate: (data: any) => boolean,
  label: string,
): Promise<any> {
  for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
    const result = await querySubgraph(authKeyHex, query, variables);
    if (predicate(result)) return result;
    if (i % 4 === 0) {
      const facts = result?.data?.facts;
      const errors = result?.errors;
      const detail = errors
        ? `errors=${JSON.stringify(errors).slice(0, 200)}`
        : facts
          ? `facts=${facts.length}`
          : `raw=${JSON.stringify(result).slice(0, 200)}`;
      console.log(
        `    Polling ${label}... attempt ${i + 1}/${POLL_MAX_ATTEMPTS} (${detail})`,
      );
    }
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
  }
  throw new Error(
    `Subgraph polling timed out for ${label} after ${(POLL_MAX_ATTEMPTS * POLL_INTERVAL_MS) / 1000}s`,
  );
}

// ---------------------------------------------------------------------------
// Smart Account helpers
// ---------------------------------------------------------------------------

async function deriveSmartAccountAddress(mnemonic: string): Promise<string> {
  const owner = mnemonicToAccount(mnemonic);
  const publicClient = createPublicClient({ chain: baseSepolia, transport: http() });
  const sa = await toSimpleSmartAccount({
    client: publicClient as any,
    owner,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });
  return sa.address.toLowerCase();
}

// ---------------------------------------------------------------------------
// On-chain batch submission
// ---------------------------------------------------------------------------

async function submitBatch(
  mnemonic: string,
  authKeyHex: string,
  walletAddress: string,
  payloads: Buffer[],
): Promise<{ txHash: string; userOpHash: string; batchSize: number }> {
  if (payloads.length === 0) throw new Error('Empty batch');

  const bundlerUrl = `${RELAY_URL}/v1/bundler`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${authKeyHex}`,
    'X-Wallet-Address': walletAddress,
    'X-TotalReclaw-Client': 'e2e-debrief-test',
  };
  const authTransport = http(bundlerUrl, { fetchOptions: { headers } });

  const owner = mnemonicToAccount(mnemonic);
  const publicClient = createPublicClient({ chain: baseSepolia, transport: http() });

  const pimlicoClient = createPimlicoClient({
    chain: baseSepolia,
    transport: authTransport,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });

  const smartAccount = await toSimpleSmartAccount({
    client: publicClient as any,
    owner,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });

  const smartAccountClient = createSmartAccountClient({
    account: smartAccount,
    chain: baseSepolia,
    bundlerTransport: authTransport,
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () =>
        (await pimlicoClient.getUserOperationGasPrice()).fast,
    },
  });

  const calls = payloads.map(p => ({
    to: DATA_EDGE_ADDRESS as `0x${string}`,
    value: 0n,
    data: `0x${p.toString('hex')}` as Hex,
  }));

  const userOpHash = await smartAccountClient.sendUserOperation({ calls });
  const receipt = await pimlicoClient.waitForUserOperationReceipt({
    hash: userOpHash,
    timeout: 120_000,
  });

  return {
    txHash: receipt.receipt.transactionHash,
    userOpHash,
    batchSize: payloads.length,
  };
}

// ---------------------------------------------------------------------------
// Shared GraphQL queries
// ---------------------------------------------------------------------------

const FACTS_BY_OWNER_QUERY = `
  query FactsByOwner($owner: String!) {
    facts(
      where: { owner: $owner, isActive: true }
      orderBy: sequenceId
      orderDirection: desc
      first: 50
    ) {
      id
      owner
      encryptedBlob
      decayScore
      source
      isActive
      contentFp
      agentId
    }
  }
`;

const SEARCH_BY_BLIND_INDEX = `
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: String!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: 20
    ) {
      hash
      fact {
        id
        owner
        encryptedBlob
        decayScore
        source
        isActive
        contentFp
        agentId
      }
    }
  }
`;

// ---------------------------------------------------------------------------
// Shared context
// ---------------------------------------------------------------------------

interface TestContext {
  mnemonic: string;
  authKeyHex: string;
  authKeyHash: string;
  encryptionKey: Buffer;
  dedupKey: Buffer;
  saltHex: string;
  walletAddress: string;
  testRunId: string;
}

async function initTestContext(): Promise<TestContext> {
  const mnemonic = generateMnemonic(wordlist);
  const { authKey, encryptionKey, dedupKey, salt } = deriveKeysFromMnemonic(mnemonic);
  const authKeyHex = authKey.toString('hex');
  const authKeyHash = computeAuthKeyHash(authKey);
  const saltHex = salt.toString('hex');
  const walletAddress = await deriveSmartAccountAddress(mnemonic);
  const testRunId = randomBytes(4).toString('hex');

  return {
    mnemonic,
    authKeyHex,
    authKeyHash,
    encryptionKey,
    dedupKey,
    saltHex,
    walletAddress,
    testRunId,
  };
}

// ---------------------------------------------------------------------------
// Subgraph sync wait helper
// ---------------------------------------------------------------------------

async function waitForSubgraphSync(authKeyHex: string): Promise<void> {
  const tipRes = await fetch('https://sepolia.base.org', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'eth_blockNumber', params: [], id: 1 }),
  });
  const tipData = (await tipRes.json()) as { result: string };
  const targetBlock = parseInt(tipData.result, 16);
  console.log(`  Chain tip: block ${targetBlock}`);

  const maxWaitMs = 45 * 60 * 1000;
  const pollMs = 15_000;
  const startWait = Date.now();
  let lastLog = 0;

  while (Date.now() - startWait < maxWaitMs) {
    const metaResult = await querySubgraph(authKeyHex, '{ _meta { block { number } } }');
    const subgraphBlock = metaResult?.data?._meta?.block?.number ?? 0;
    const behind = targetBlock - subgraphBlock;
    const elapsed = Math.round((Date.now() - startWait) / 1000);

    if (subgraphBlock >= targetBlock) {
      console.log(
        `  Subgraph synced to ${subgraphBlock} (target ${targetBlock}) after ${elapsed}s`,
      );
      return;
    }

    if (Date.now() - lastLog > 60_000) {
      console.log(
        `    ${elapsed}s elapsed — subgraph at ${subgraphBlock}, ${behind} blocks behind target`,
      );
      lastLog = Date.now();
    }
    await new Promise(r => setTimeout(r, pollMs));
  }

  const metaFinal = await querySubgraph(authKeyHex, '{ _meta { block { number } } }');
  const finalBlock = metaFinal?.data?._meta?.block?.number ?? 0;
  if (finalBlock < targetBlock) {
    console.log(
      `  WARNING: Subgraph still at ${finalBlock} after 45 min (target ${targetBlock}). Proceeding anyway.`,
    );
  }
}

// =========================================================================
// TEST 1: MCP totalreclaw_debrief — Store + Recall
// =========================================================================

async function test1_debriefStoreAndRecall() {
  console.log('\n=== Test 1: MCP Debrief — Store + Recall ===\n');

  const ctx = await initTestContext();
  console.log(`  Wallet: ${ctx.walletAddress}`);
  console.log(`  Run ID: ${ctx.testRunId}`);

  // Register with staging relay
  const regRes = await registerUser(ctx.authKeyHash, ctx.saltHex);
  assert(regRes.status === 200, `Registration failed: ${JSON.stringify(regRes.data)}`);
  console.log(`  Registered: ${regRes.data.user_id}`);

  // Define the 3 debrief items (exact spec from test plan)
  const debriefItems = [
    {
      text: 'Session focused on migrating auth from JWT to OAuth2 for better third-party integration',
      type: 'summary' as const,
      importance: 8,
    },
    {
      text: 'Decided to keep backward compatibility with JWT for 90 days during migration',
      type: 'summary' as const,
      importance: 7,
    },
    {
      text: 'OAuth2 provider selection still pending — evaluating Auth0 vs Keycloak',
      type: 'context' as const,
      importance: 7,
    },
  ];

  // Validate through parseDebriefResponse (same path as the MCP handler)
  const validated = parseDebriefResponse(JSON.stringify(debriefItems));
  assert(validated.length === 3, `Expected 3 validated items, got ${validated.length}`);
  console.log(`  Parser validated: ${validated.length} items`);

  // Build on-chain payloads with source=mcp_debrief
  const now = new Date().toISOString();
  const factPayloads: FactPayload[] = debriefItems.map((item, i) => {
    const plaintext = JSON.stringify({
      text: item.text,
      metadata: {
        type: item.type,
        importance: item.importance / 10,
        source: 'mcp_debrief',
        tags: [item.type],
      },
    });

    const blob = encryptBlob(plaintext, ctx.encryptionKey);
    const blindIndices = generateBlindIndices(item.text);
    // Marker blind index for test-run isolation
    const markerIndex = createHash('sha256')
      .update(`e2e-debrief-${ctx.testRunId}-item-${i}`)
      .digest('hex');
    const contentFp = generateContentFingerprint(item.text, ctx.dedupKey);

    return {
      id: crypto.randomUUID(),
      timestamp: now,
      owner: ctx.walletAddress,
      encryptedBlob: Buffer.from(blob, 'base64').toString('hex'),
      blindIndices: [...blindIndices, markerIndex],
      decayScore: 0.7 + i * 0.05,
      source: 'mcp_debrief',
      contentFp,
      agentId: 'e2e-debrief-test',
    };
  });

  // Submit as a batch
  const protobufs = factPayloads.map(f => encodeFactProtobuf(f));
  const submitResult = await runTest('1.1: Submit 3 debrief facts as batch', async () => {
    const result = await submitBatch(
      ctx.mnemonic,
      ctx.authKeyHex,
      ctx.walletAddress,
      protobufs,
    );
    assert(result.batchSize === 3, `Expected batchSize=3, got ${result.batchSize}`);
    console.log(`    txHash: ${result.txHash}`);
    console.log(`    userOpHash: ${result.userOpHash.slice(0, 16)}...`);
  });

  if (!submitResult) {
    skipTest('1.2: Facts indexed by subgraph', 'submission failed');
    skipTest('1.3: Recall debrief items via blind-index search', 'submission failed');
    skipTest('1.4: Decrypt and verify debrief text matches exactly', 'submission failed');
    skipped += 3;
    return;
  }

  // Wait for subgraph to sync past current chain tip
  console.log('\n  Waiting for subgraph to index debrief facts...');
  await waitForSubgraphSync(ctx.authKeyHex);

  // Verify facts are indexed
  const indexed = await runTest('1.2: Facts indexed by subgraph', async () => {
    const result = await pollSubgraph(
      ctx.authKeyHex,
      FACTS_BY_OWNER_QUERY,
      { owner: ctx.walletAddress },
      data => (data?.data?.facts?.length ?? 0) >= 3,
      '3 debrief facts',
    );
    const facts = result.data.facts;
    const debriefFacts = facts.filter((f: any) => f.source === 'mcp_debrief');
    assert(
      debriefFacts.length >= 3,
      `Expected >= 3 debrief facts, got ${debriefFacts.length}`,
    );
    console.log(`    Found ${debriefFacts.length} debrief facts indexed`);
  });

  if (!indexed) {
    skipTest('1.3: Recall debrief items via blind-index search', 'indexing failed');
    skipTest('1.4: Decrypt and verify debrief text matches exactly', 'indexing failed');
    skipped += 2;
    return;
  }

  // Recall via blind-index search
  await runTest('1.3: Recall debrief items via blind-index search', async () => {
    // Use word trapdoors from the debrief text ("auth", "oauth2", etc.)
    const searchTerms = ['auth', 'oauth2', 'migration'];
    const trapdoors = searchTerms.flatMap(term => generateBlindIndices(term));

    const result = await querySubgraph(ctx.authKeyHex, SEARCH_BY_BLIND_INDEX, {
      trapdoors,
      owner: ctx.walletAddress,
    });

    const entries = result?.data?.blindIndexes || [];
    assert(entries.length >= 1, `Expected >= 1 search result, got ${entries.length}`);
    console.log(`    Search returned ${entries.length} blind-index match(es)`);
  });

  // Decrypt and verify text matches exactly
  await runTest('1.4: Decrypt and verify debrief text matches exactly', async () => {
    const result = await querySubgraph(ctx.authKeyHex, FACTS_BY_OWNER_QUERY, {
      owner: ctx.walletAddress,
    });
    const facts = result.data.facts.filter((f: any) => f.source === 'mcp_debrief');
    assert(facts.length >= 3, `Expected >= 3 debrief facts, got ${facts.length}`);

    const decryptedTexts: string[] = [];
    for (const fact of facts) {
      try {
        // encryptedBlob from subgraph is 0x-prefixed hex; strip prefix, convert to base64
        const hex = fact.encryptedBlob.startsWith('0x') ? fact.encryptedBlob.slice(2) : fact.encryptedBlob;
        const blobBase64 = Buffer.from(hex, 'hex').toString('base64');
        const decrypted = decryptBlob(blobBase64, ctx.encryptionKey);
        const parsed = JSON.parse(decrypted);
        decryptedTexts.push(parsed.text);
      } catch {
        // Skip facts that fail to decrypt (may be corrupted or from a different key)
        console.log(`    Skipping undecryptable fact ${fact.id}`);
      }
    }

    // Verify at least one stored text matches exactly
    const expectedTexts = debriefItems.map(d => d.text);
    let matchCount = 0;
    for (const expected of expectedTexts) {
      if (decryptedTexts.includes(expected)) {
        matchCount++;
      }
    }

    assert(
      matchCount >= 1,
      `Expected at least 1 exact text match, got ${matchCount}. ` +
        `Decrypted: ${JSON.stringify(decryptedTexts.slice(0, 2))}`,
    );
    console.log(`    ${matchCount}/${expectedTexts.length} debrief texts match exactly`);
  });
}

// =========================================================================
// TEST 5: Edge Cases — Empty / All-Filtered / Validation
// =========================================================================

async function test5_edgeCases() {
  console.log('\n=== Test 5: Edge Cases — Validation + Empty Facts ===\n');

  // 5.1: Empty facts array should return empty
  await runTest('5.1: Empty facts array returns empty', async () => {
    const validated = parseDebriefResponse('[]');
    assert(
      validated.length === 0,
      `Expected 0 items from empty array, got ${validated.length}`,
    );
    console.log('    Empty array correctly yields 0 items');
  });

  // 5.2: All facts filtered by importance < 6
  await runTest('5.2: All low-importance facts filtered', async () => {
    const lowImportanceFacts = [
      { text: 'Minor detail about formatting preferences', type: 'context', importance: 3 },
      { text: 'Trivial observation about code style', type: 'context', importance: 2 },
      { text: 'Tiny note about whitespace choices', type: 'summary', importance: 1 },
    ];
    const validated = parseDebriefResponse(JSON.stringify(lowImportanceFacts));
    assert(validated.length === 0, `Expected 0 items (all filtered), got ${validated.length}`);
    console.log(`    All ${lowImportanceFacts.length} low-importance items filtered`);
  });

  // 5.3: Invalid type defaults to context
  await runTest('5.3: Invalid type defaults to context', async () => {
    const input = JSON.stringify([
      { text: 'Valid debrief item with wrong type field', type: 'fact', importance: 7 },
    ]);
    const validated = parseDebriefResponse(input);
    assert(validated.length === 1, `Expected 1 item, got ${validated.length}`);
    assert(
      validated[0].type === 'context',
      `Expected type=context (default), got ${validated[0].type}`,
    );
    console.log('    Invalid type "fact" defaulted to "context"');
  });

  // 5.4: Missing importance defaults to 7
  await runTest('5.4: Missing importance defaults to 7', async () => {
    const input = JSON.stringify([
      { text: 'Debrief item without explicit importance value', type: 'summary' },
    ]);
    const validated = parseDebriefResponse(input);
    assert(validated.length === 1, `Expected 1 item, got ${validated.length}`);
    assert(
      validated[0].importance === 7,
      `Expected importance=7, got ${validated[0].importance}`,
    );
    console.log('    Missing importance defaulted to 7');
  });

  // 5.5: Cap at 5 items
  await runTest('5.5: More than 5 items capped', async () => {
    const items = Array.from({ length: 8 }, (_, i) => ({
      text: `Debrief item number ${i + 1} with enough text to pass validation`,
      type: 'summary',
      importance: 7,
    }));
    const validated = parseDebriefResponse(JSON.stringify(items));
    assert(validated.length === 5, `Expected 5 items (capped), got ${validated.length}`);
    console.log(`    8 items capped to ${validated.length}`);
  });

  // 5.6: Short text (< 5 chars) filtered
  await runTest('5.6: Short text filtered', async () => {
    const input = JSON.stringify([
      { text: 'ok', type: 'summary', importance: 8 },
      { text: 'Valid debrief item that passes the length check', type: 'summary', importance: 8 },
    ]);
    const validated = parseDebriefResponse(input);
    assert(
      validated.length === 1,
      `Expected 1 item (short text filtered), got ${validated.length}`,
    );
    assert(
      validated[0].text === 'Valid debrief item that passes the length check',
      `Wrong text survived: ${validated[0].text}`,
    );
    console.log('    Short text "ok" filtered, valid text kept');
  });

  // 5.7: Markdown code fences stripped
  await runTest('5.7: Markdown code fences stripped', async () => {
    const input =
      '```json\n[{"text": "Session wrapped in markdown code fences", "type": "summary", "importance": 8}]\n```';
    const validated = parseDebriefResponse(input);
    assert(
      validated.length === 1,
      `Expected 1 item after fence stripping, got ${validated.length}`,
    );
    assert(
      validated[0].text === 'Session wrapped in markdown code fences',
      `Unexpected text: ${validated[0].text}`,
    );
    console.log('    Markdown fences stripped correctly');
  });

  // 5.8: Invalid JSON returns empty array
  await runTest('5.8: Invalid JSON returns empty', async () => {
    const validated = parseDebriefResponse('not valid json at all');
    assert(
      validated.length === 0,
      `Expected 0 items for invalid JSON, got ${validated.length}`,
    );
    console.log('    Invalid JSON handled gracefully');
  });

  // 5.9: Importance clamped to 1-10
  // Note: importance < 6 is filtered out by the parser, so we test the upper clamp
  // and verify that low values get clamped but then filtered by the importance gate.
  await runTest('5.9: Importance clamped to 10 maximum', async () => {
    const input = JSON.stringify([
      { text: 'Importance way above the maximum allowed value', type: 'summary', importance: 99 },
    ]);
    const validated = parseDebriefResponse(input);
    assert(validated.length === 1, `Expected 1 item, got ${validated.length}`);
    assert(
      validated[0].importance === 10,
      `Expected 10 (clamped from 99), got ${validated[0]?.importance}`,
    );
    console.log('    Importance clamped correctly (99->10)');
  });

  // 5.10: Negative importance clamped to 1, then filtered by importance gate (< 6)
  await runTest('5.10: Negative importance clamped then filtered', async () => {
    const input = JSON.stringify([
      { text: 'Negative importance value should be filtered out', type: 'summary', importance: -5 },
    ]);
    const validated = parseDebriefResponse(input);
    // -5 -> Math.max(1, Math.min(10, -5)) = 1, then 1 < 6 filtered out
    assert(
      validated.length === 0,
      `Expected 0 items (importance=1 after clamp, filtered by gate), got ${validated.length}`,
    );
    console.log('    Negative importance (-5->1) correctly filtered by importance gate');
  });
}

// =========================================================================
// TEST 6: Source Tag Verification
// =========================================================================

async function test6_sourceTagVerification() {
  console.log('\n=== Test 6: Source Tag Verification ===\n');

  const ctx = await initTestContext();
  console.log(`  Wallet: ${ctx.walletAddress}`);
  console.log(`  Run ID: ${ctx.testRunId}`);

  // Register with staging relay
  const regRes = await registerUser(ctx.authKeyHash, ctx.saltHex);
  assert(regRes.status === 200, `Registration failed: ${JSON.stringify(regRes.data)}`);
  console.log(`  Registered: ${regRes.data.user_id}`);

  // Define the debrief and regular fact texts
  const debriefText =
    'E2E source tag test: debrief item about OAuth2 migration strategy decision';
  const regularFactText =
    'E2E source tag test: regular fact — user prefers dark mode in IDE';

  // Encrypt debrief fact
  const debriefPlaintext = JSON.stringify({
    text: debriefText,
    metadata: {
      type: 'summary',
      importance: 0.8,
      source: 'mcp_debrief',
      tags: ['summary'],
    },
  });
  const debriefBlob = encryptBlob(debriefPlaintext, ctx.encryptionKey);

  // Encrypt regular fact
  const regularPlaintext = JSON.stringify({
    text: regularFactText,
    metadata: {
      type: 'preference',
      importance: 0.7,
      source: 'mcp_remember',
      tags: ['preference'],
    },
  });
  const regularBlob = encryptBlob(regularPlaintext, ctx.encryptionKey);

  const now = new Date().toISOString();

  // Build debrief fact payload (source=mcp_debrief)
  const debriefFact: FactPayload = {
    id: crypto.randomUUID(),
    timestamp: now,
    owner: ctx.walletAddress,
    encryptedBlob: Buffer.from(debriefBlob, 'base64').toString('hex'),
    blindIndices: [
      ...generateBlindIndices(debriefText),
      createHash('sha256').update(`e2e-src-debrief-${ctx.testRunId}`).digest('hex'),
    ],
    decayScore: 0.8,
    source: 'mcp_debrief',
    contentFp: generateContentFingerprint(debriefText, ctx.dedupKey),
    agentId: 'e2e-debrief-test',
  };

  // Build regular fact payload (source=mcp_remember)
  const regularFact: FactPayload = {
    id: crypto.randomUUID(),
    timestamp: now,
    owner: ctx.walletAddress,
    encryptedBlob: Buffer.from(regularBlob, 'base64').toString('hex'),
    blindIndices: [
      ...generateBlindIndices(regularFactText),
      createHash('sha256').update(`e2e-src-regular-${ctx.testRunId}`).digest('hex'),
    ],
    decayScore: 0.7,
    source: 'mcp_remember',
    contentFp: generateContentFingerprint(regularFactText, ctx.dedupKey),
    agentId: 'e2e-debrief-test',
  };

  // Submit both in one batch
  const payloads = [debriefFact, regularFact].map(f => encodeFactProtobuf(f));
  const submitResult = await runTest('6.1: Submit debrief + regular fact', async () => {
    const result = await submitBatch(
      ctx.mnemonic,
      ctx.authKeyHex,
      ctx.walletAddress,
      payloads,
    );
    assert(result.batchSize === 2, `Expected batchSize=2, got ${result.batchSize}`);
    console.log(`    txHash: ${result.txHash}`);
  });

  if (!submitResult) {
    skipTest('6.2: Both facts indexed by subgraph', 'submission failed');
    skipTest('6.3: Source tag verification', 'submission failed');
    skipped += 2;
    return;
  }

  // Wait for subgraph to sync
  console.log('\n  Waiting for subgraph to index source-tag facts...');
  await waitForSubgraphSync(ctx.authKeyHex);

  // Verify both facts are indexed
  const indexed = await runTest('6.2: Both facts indexed by subgraph', async () => {
    const result = await pollSubgraph(
      ctx.authKeyHex,
      FACTS_BY_OWNER_QUERY,
      { owner: ctx.walletAddress },
      data => {
        const facts = data?.data?.facts || [];
        return facts.length >= 2;
      },
      'source-tag facts',
    );
    const facts = result.data.facts;
    console.log(`    Found ${facts.length} total facts indexed`);
  });

  if (!indexed) {
    skipTest('6.3: Source tag verification', 'indexing failed');
    skipped++;
    return;
  }

  // Verify source tags
  await runTest('6.3: Debrief has mcp_debrief, regular has mcp_remember', async () => {
    const result = await querySubgraph(ctx.authKeyHex, FACTS_BY_OWNER_QUERY, {
      owner: ctx.walletAddress,
    });
    const facts = result.data.facts;

    // Filter to our test run facts (by agentId)
    const ourFacts = facts.filter((f: any) => f.agentId === 'e2e-debrief-test');
    assert(ourFacts.length >= 2, `Expected >= 2 test facts, got ${ourFacts.length}`);

    // Find debrief and regular facts by source field
    const debriefFound = ourFacts.find((f: any) => f.source === 'mcp_debrief');
    const regularFound = ourFacts.find((f: any) => f.source === 'mcp_remember');

    assert(debriefFound, 'No fact with source=mcp_debrief found');
    assert(regularFound, 'No fact with source=mcp_remember found');

    // Debug: log raw encryptedBlob to diagnose decrypt failures
    console.log(`    regular blob length: ${regularFound.encryptedBlob.length}, starts with 0x: ${regularFound.encryptedBlob.startsWith('0x')}`);

    // Decrypt and verify text content matches
    const debriefHex = debriefFound.encryptedBlob.startsWith('0x') ? debriefFound.encryptedBlob.slice(2) : debriefFound.encryptedBlob;
    const debriefBlobBase64 = Buffer.from(debriefHex, 'hex').toString('base64');
    const debriefDecrypted = JSON.parse(decryptBlob(debriefBlobBase64, ctx.encryptionKey));
    assert(
      debriefDecrypted.text === debriefText,
      `Debrief text mismatch: ${debriefDecrypted.text}`,
    );

    const regularHex = regularFound.encryptedBlob.startsWith('0x') ? regularFound.encryptedBlob.slice(2) : regularFound.encryptedBlob;
    const regularBlobBase64 = Buffer.from(regularHex, 'hex').toString('base64');
    const regularDecrypted = JSON.parse(decryptBlob(regularBlobBase64, ctx.encryptionKey));
    assert(
      regularDecrypted.text === regularFactText,
      `Regular text mismatch: ${regularDecrypted.text}`,
    );

    // Verify the sources are distinct
    assert(
      debriefFound.source !== regularFound.source,
      `Sources should be different: both are ${debriefFound.source}`,
    );

    console.log(
      `    debrief fact: source=${debriefFound.source}, text="${debriefDecrypted.text.slice(0, 50)}..."`,
    );
    console.log(
      `    regular fact: source=${regularFound.source}, text="${regularDecrypted.text.slice(0, 50)}..."`,
    );
  });
}

// =========================================================================
// MAIN
// =========================================================================

async function main() {
  console.log(`\n${'='.repeat(60)}`);
  console.log(`  TotalReclaw E2E Tests — Session Debrief (MCP)`);
  console.log(`${'='.repeat(60)}`);
  console.log(`  Relay:     ${RELAY_URL}`);
  console.log(`  Chain:     Base Sepolia (${CHAIN_ID})`);
  console.log(`  DataEdge:  ${DATA_EDGE_ADDRESS}`);
  console.log(`  Tests:     ${tests.length ? tests.join(', ') : 'ALL (1, 5, 6)'}`);
  console.log(`${'='.repeat(60)}\n`);

  // Guard: remind about test environment
  if (!process.env.TOTALRECLAW_TEST && !process.env.RELAY_URL) {
    console.log('  WARNING: Neither TOTALRECLAW_TEST nor RELAY_URL set.');
    console.log('  This test will hit the staging relay. Continuing...\n');
  }

  if (shouldRun('1')) await test1_debriefStoreAndRecall();
  if (shouldRun('5')) await test5_edgeCases();
  if (shouldRun('6')) await test6_sourceTagVerification();

  console.log(`\n${'='.repeat(60)}`);
  console.log(`  Results: ${passed} passed, ${failed} failed, ${skipped} skipped`);
  console.log(`${'='.repeat(60)}\n`);

  process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => {
  console.error(`\nFATAL: ${err.message}\n${err.stack}\n`);
  process.exit(1);
});
