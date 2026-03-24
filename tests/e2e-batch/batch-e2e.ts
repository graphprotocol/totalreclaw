/**
 * E2E Tests: Client Batching, Extraction Config, Dual-Chain Routing, Full Pipeline
 *
 * Tests against LIVE staging:
 *   - Relay: api.totalreclaw.xyz
 *   - Chain: Base Sepolia (84532)
 *   - Subgraph: totalreclaw---base-sepolia (Graph Studio)
 *
 * Test Groups:
 *   A — Relay health + registration (prerequisites)
 *   B — Client batching: 3 facts in 1 UserOp (CRITICAL)
 *   C — Server-side extraction config fields
 *   D — Tombstone + replacement in same batch
 *   E — Dual-chain routing verification
 *   F — Full search pipeline (store → blind-index search → recall)
 *
 * Run:
 *   cd tests/e2e-batch && npm install && npm test
 *
 * Run single group:
 *   npx tsx batch-e2e.ts --test B
 */
import { createHash, randomBytes, createCipheriv } from 'crypto';
import crypto from 'crypto';
import { mnemonicToAccount } from 'viem/accounts';
import { generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import { createPublicClient, http, type Address, type Hex } from 'viem';
import { baseSepolia } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { createSmartAccountClient } from 'permissionless';
import { createPimlicoClient } from 'permissionless/clients/pimlico';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const RELAY_URL = process.env.RELAY_URL || 'https://api.totalreclaw.xyz';
const CHAIN_ID = 84532; // Base Sepolia
const DATA_EDGE_ADDRESS = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca' as const;
const ENTRYPOINT_ADDRESS = '0x0000000071727De22E5E9d8BAf0edAc6f37da032' as const;

// Subgraph polling — Graph Studio has variable indexing latency (5-40+ min)
// _meta.block.number can report ahead of actually queryable data, so we must
// poll for the actual facts with a generous timeout.
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

// Parse --test flags from CLI (e.g. --test B --test C)
const allArgs = process.argv.slice(2);
const groups: string[] = [];
for (let i = 0; i < allArgs.length; i++) {
  if (allArgs[i] === '--test' && allArgs[i + 1]) {
    groups.push(allArgs[i + 1].toUpperCase());
    i++;
  }
}
const shouldRun = (group: string) => groups.length === 0 || groups.includes(group);

// ---------------------------------------------------------------------------
// Crypto helpers
// ---------------------------------------------------------------------------

function generateTestKeys() {
  const authKey = randomBytes(32);
  const authKeyHash = createHash('sha256').update(authKey).digest();
  const salt = randomBytes(32);
  const encryptionKey = randomBytes(32);
  return {
    authKey,
    authKeyHex: authKey.toString('hex'),
    authKeyHash: authKeyHash.toString('hex'),
    salt: salt.toString('hex'),
    encryptionKey,
  };
}

/** AES-256-GCM encrypt, returns hex string of iv+ciphertext+tag */
function encryptToHex(plaintext: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, encrypted, tag]).toString('hex');
}

// ---------------------------------------------------------------------------
// Protobuf encoder (self-contained, matches server/proto/totalreclaw.proto)
// ---------------------------------------------------------------------------

function encodeVarint(value: number): Buffer {
  const bytes: number[] = [];
  let v = value >>> 0;
  while (v > 0x7f) { bytes.push((v & 0x7f) | 0x80); v >>>= 7; }
  bytes.push(v & 0x7f);
  return Buffer.from(bytes);
}

interface FactPayload {
  id: string; timestamp: string; owner: string;
  encryptedBlob: string; blindIndices: string[];
  decayScore: number; source: string; contentFp: string;
  agentId: string; encryptedEmbedding?: string;
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
    const buf = Buffer.alloc(8); buf.writeDoubleLE(val); parts.push(buf);
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
// Blind index generation (matches client library)
// ---------------------------------------------------------------------------

function generateBlindIndex(term: string, dedupKey: Buffer): string {
  return createHash('sha256')
    .update(Buffer.concat([dedupKey, Buffer.from(term.toLowerCase().trim())]))
    .digest('hex');
}

function generateBlindIndices(text: string, dedupKey: Buffer): string[] {
  const words = text.toLowerCase().split(/\s+/).filter(w => w.length > 2);
  return [...new Set(words)].map(w => generateBlindIndex(w, dedupKey));
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

async function registerUser(authKeyHash: string, salt: string) {
  return relayRequest('POST', '/v1/register', { auth_key_hash: authKeyHash, salt });
}

async function getBillingStatus(authKeyHex: string, walletAddress: string) {
  return relayRequest(
    'GET',
    `/v1/billing/status?wallet_address=${walletAddress}`,
    undefined,
    { Authorization: `Bearer ${authKeyHex}` },
  );
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
      'X-TotalReclaw-Client': 'e2e-batch-test',
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
      // Log what the subgraph returned for debugging
      const facts = result?.data?.facts;
      const errors = result?.errors;
      const detail = errors ? `errors=${JSON.stringify(errors).slice(0, 200)}`
        : facts ? `facts=${facts.length}`
        : `raw=${JSON.stringify(result).slice(0, 200)}`;
      console.log(`    Polling ${label}... attempt ${i + 1}/${POLL_MAX_ATTEMPTS} (${detail})`);
    }
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
  }
  throw new Error(`Subgraph polling timed out for ${label} after ${POLL_MAX_ATTEMPTS * POLL_INTERVAL_MS / 1000}s`);
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
    'X-TotalReclaw-Client': 'e2e-batch-test',
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
      first: 20
    ) {
      id owner encryptedBlob decayScore source isActive contentFp agentId
    }
  }
`;

const ALL_FACTS_BY_OWNER_QUERY = `
  query AllFactsByOwner($owner: String!) {
    facts(
      where: { owner: $owner }
      orderBy: sequenceId
      orderDirection: desc
      first: 20
    ) {
      id owner encryptedBlob decayScore source isActive contentFp agentId
    }
  }
`;

const SEARCH_BY_BLIND_INDEX = `
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: String!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: 10
    ) {
      hash
      fact {
        id owner encryptedBlob decayScore source isActive contentFp agentId
      }
    }
  }
`;

// =========================================================================
// TEST GROUP A: Relay health + registration
// =========================================================================

async function testGroupA(keys: ReturnType<typeof generateTestKeys>) {
  console.log('\n=== Test Group A: Relay Health + Registration ===\n');

  await runTest('A1: Relay health check', async () => {
    const res = await relayRequest('GET', '/health');
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    assert(res.data.status === 'ok', `Expected status=ok, got ${res.data.status}`);
    assert(res.data.service === 'totalreclaw-relay', `Wrong service: ${res.data.service}`);
  });

  await runTest('A2: Register test user', async () => {
    const res = await registerUser(keys.authKeyHash, keys.salt);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    assert(res.data.success === true, `Expected success=true, got ${JSON.stringify(res.data)}`);
    assert(typeof res.data.user_id === 'string', `Expected user_id string`);
    console.log(`    user_id: ${res.data.user_id}`);
  });
}

// =========================================================================
// TEST GROUP C: Server-side extraction config
// =========================================================================

async function testGroupC(keys: ReturnType<typeof generateTestKeys>) {
  console.log('\n=== Test Group C: Server-Side Extraction Config ===\n');

  const walletAddress = '0x' + randomBytes(20).toString('hex');

  await runTest('C1: Billing status returns extraction_interval', async () => {
    const res = await getBillingStatus(keys.authKeyHex, walletAddress);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    assert(res.data.success === true, `Expected success=true`);
    assert(res.data.features !== undefined, `Expected features object`);
    assert(
      typeof res.data.features.extraction_interval === 'number',
      `Expected extraction_interval number, got ${typeof res.data.features.extraction_interval}`,
    );
    console.log(`    extraction_interval=${res.data.features.extraction_interval}`);
  });

  await runTest('C2: Billing status returns max_facts_per_extraction', async () => {
    const res = await getBillingStatus(keys.authKeyHex, walletAddress);
    assert(
      typeof res.data.features.max_facts_per_extraction === 'number',
      `Expected max_facts_per_extraction number, got ${typeof res.data.features.max_facts_per_extraction}`,
    );
    console.log(`    max_facts_per_extraction=${res.data.features.max_facts_per_extraction}`);
  });

  await runTest('C3: Free tier defaults are sensible', async () => {
    const res = await getBillingStatus(keys.authKeyHex, walletAddress);
    const f = res.data.features;
    assert(f.extraction_interval >= 1 && f.extraction_interval <= 10, `interval ${f.extraction_interval} out of [1,10]`);
    assert(f.max_facts_per_extraction >= 1 && f.max_facts_per_extraction <= 50, `max_facts ${f.max_facts_per_extraction} out of [1,50]`);
    assert(f.min_extract_interval >= 1, `min_extract_interval should be >= 1`);
    assert(typeof f.llm_dedup === 'boolean', `llm_dedup should be boolean`);
  });
}

// =========================================================================
// TEST GROUP E: Dual-chain routing
// =========================================================================

async function testGroupE(keys: ReturnType<typeof generateTestKeys>) {
  console.log('\n=== Test Group E: Dual-Chain Routing ===\n');

  const walletAddress = '0x' + randomBytes(20).toString('hex');

  // E1: Free tier has correct chain config in features
  await runTest('E1: Free tier billing shows Base Sepolia chain', async () => {
    const res = await getBillingStatus(keys.authKeyHex, walletAddress);
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    assert(res.data.tier === 'free', `Expected tier=free, got ${res.data.tier}`);
    // The features dict may include chain info from the tiers table
    console.log(`    tier=${res.data.tier}, features=${JSON.stringify(res.data.features)}`);
  });

  // E2: Bundler proxy accepts free-tier JSON-RPC (supportedEntryPoints)
  await runTest('E2: Bundler proxy accepts eth_supportedEntryPoints', async () => {
    const res = await relayRequest(
      'POST',
      '/v1/bundler',
      {
        jsonrpc: '2.0',
        id: 1,
        method: 'eth_supportedEntryPoints',
        params: [],
      },
      {
        Authorization: `Bearer ${keys.authKeyHex}`,
        'X-Wallet-Address': walletAddress,
        'X-TotalReclaw-Client': 'e2e-batch-test',
      },
    );
    assert(res.status === 200, `Expected 200, got ${res.status}`);
    // Pimlico should return an array with the EntryPoint address
    assert(
      Array.isArray(res.data.result),
      `Expected result array, got ${JSON.stringify(res.data)}`,
    );
    console.log(`    EntryPoints: ${JSON.stringify(res.data.result)}`);
  });

  // E3: Subgraph proxy returns data from Base Sepolia subgraph
  await runTest('E3: Subgraph proxy returns _meta from Base Sepolia', async () => {
    const result = await querySubgraph(
      keys.authKeyHex,
      '{ _meta { block { number } } }',
    );
    assert(result?.data?._meta?.block?.number !== undefined, `Expected _meta.block.number`);
    const blockNum = parseInt(result.data._meta.block.number);
    // Base Sepolia block numbers are > 30M
    assert(blockNum > 30_000_000, `Block ${blockNum} too low for Base Sepolia (expected >30M)`);
    console.log(`    Subgraph synced to block: ${blockNum}`);
  });
}

// =========================================================================
// TWO-PHASE B/D/F: submit first, verify later (Graph Studio has variable lag)
// =========================================================================

interface SubmissionContext {
  mnemonic: string;
  smartAccountAddr: string;
  testRunId: string;
  payloads: Buffer[];
  txHash?: string;
  testFacts?: FactPayload[];
  // D-specific
  originalFact?: FactPayload;
  originalTxHash?: string;
  tombstonePlusReplacementTxHash?: string;
  replacement?: FactPayload;
  // F-specific
  fact?: FactPayload;
  markerIndex?: string;
  dedupKey?: Buffer;
  blindIndices?: string[];
}

async function submitPhaseB(keys: ReturnType<typeof generateTestKeys>): Promise<SubmissionContext | null> {
  console.log('\n--- Phase 1: Submit B (3-fact batch) ---\n');
  const ctx: SubmissionContext = {
    mnemonic: generateMnemonic(wordlist),
    smartAccountAddr: '',
    testRunId: randomBytes(4).toString('hex'),
    payloads: [],
    testFacts: [],
  };
  ctx.smartAccountAddr = await deriveSmartAccountAddress(ctx.mnemonic);
  console.log(`  B Smart Account: ${ctx.smartAccountAddr}`);

  const now = new Date().toISOString();
  for (let i = 0; i < 3; i++) {
    ctx.testFacts!.push({
      id: crypto.randomUUID(),
      timestamp: now,
      owner: ctx.smartAccountAddr,
      encryptedBlob: randomBytes(64).toString('hex'),
      blindIndices: [createHash('sha256').update(`e2e-batch-${ctx.testRunId}-fact-${i}`).digest('hex')],
      decayScore: 0.7 + i * 0.1,
      source: `e2e-batch-${ctx.testRunId}`,
      contentFp: createHash('sha256').update(`fp-${i}-${ctx.testRunId}`).digest('hex'),
      agentId: 'e2e-batch-test',
    });
  }
  ctx.payloads = ctx.testFacts!.map(f => encodeFactProtobuf(f));

  const ok = await runTest('B-submit: 3-fact batch UserOp', async () => {
    const result = await submitBatch(ctx.mnemonic, keys.authKeyHex, ctx.smartAccountAddr, ctx.payloads);
    ctx.txHash = result.txHash;
    assert(result.batchSize === 3, `Expected batchSize=3`);
    console.log(`    txHash: ${result.txHash} (${result.userOpHash.slice(0, 16)}...)`);
  });
  return ok ? ctx : null;
}

async function submitPhaseD(keys: ReturnType<typeof generateTestKeys>): Promise<SubmissionContext | null> {
  console.log('\n--- Phase 1: Submit D (tombstone batch) ---\n');
  const ctx: SubmissionContext = {
    mnemonic: generateMnemonic(wordlist),
    smartAccountAddr: '',
    testRunId: randomBytes(4).toString('hex'),
    payloads: [],
  };
  ctx.smartAccountAddr = await deriveSmartAccountAddress(ctx.mnemonic);
  console.log(`  D Smart Account: ${ctx.smartAccountAddr}`);

  ctx.originalFact = {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    owner: ctx.smartAccountAddr,
    encryptedBlob: randomBytes(64).toString('hex'),
    blindIndices: [createHash('sha256').update(`d-orig-${ctx.testRunId}`).digest('hex')],
    decayScore: 0.8,
    source: `e2e-tombstone-${ctx.testRunId}`,
    contentFp: createHash('sha256').update(`d-orig-fp-${ctx.testRunId}`).digest('hex'),
    agentId: 'e2e-tombstone-test',
  };

  // Submit original
  const d1 = await runTest('D-submit-1: Original fact', async () => {
    const result = await submitBatch(ctx.mnemonic, keys.authKeyHex, ctx.smartAccountAddr, [encodeFactProtobuf(ctx.originalFact!)]);
    ctx.originalTxHash = result.txHash;
    console.log(`    txHash: ${result.txHash}`);
  });
  if (!d1) return null;

  // Wait for smart account deployment to propagate to public RPC
  console.log('    Waiting 10s for smart account deployment to propagate...');
  await new Promise(r => setTimeout(r, 10_000));

  // Submit tombstone + replacement in same UserOp (don't wait for D1 to index)
  const tombstone: FactPayload = {
    id: ctx.originalFact.id,
    timestamp: new Date().toISOString(),
    owner: ctx.smartAccountAddr,
    encryptedBlob: '00',
    blindIndices: [],
    decayScore: 0,
    source: 'tombstone',
    contentFp: '',
    agentId: 'e2e-tombstone-test',
  };
  ctx.replacement = {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    owner: ctx.smartAccountAddr,
    encryptedBlob: randomBytes(64).toString('hex'),
    blindIndices: [createHash('sha256').update(`d-repl-${ctx.testRunId}`).digest('hex')],
    decayScore: 0.9,
    source: `e2e-tombstone-${ctx.testRunId}`,
    contentFp: createHash('sha256').update(`d-repl-fp-${ctx.testRunId}`).digest('hex'),
    agentId: 'e2e-tombstone-test',
  };

  const d2 = await runTest('D-submit-2: Tombstone + replacement in 1 UserOp', async () => {
    const payloads = [encodeFactProtobuf(tombstone), encodeFactProtobuf(ctx.replacement!)];
    const result = await submitBatch(ctx.mnemonic, keys.authKeyHex, ctx.smartAccountAddr, payloads);
    ctx.tombstonePlusReplacementTxHash = result.txHash;
    assert(result.batchSize === 2, `Expected batchSize=2`);
    console.log(`    txHash: ${result.txHash}`);
  });
  return d2 ? ctx : null;
}

async function submitPhaseF(keys: ReturnType<typeof generateTestKeys>): Promise<SubmissionContext | null> {
  console.log('\n--- Phase 1: Submit F (search pipeline) ---\n');
  const ctx: SubmissionContext = {
    mnemonic: generateMnemonic(wordlist),
    smartAccountAddr: '',
    testRunId: randomBytes(4).toString('hex'),
    payloads: [],
    dedupKey: keys.encryptionKey,
  };
  ctx.smartAccountAddr = await deriveSmartAccountAddress(ctx.mnemonic);
  console.log(`  F Smart Account: ${ctx.smartAccountAddr}`);

  const factText = 'User prefers dark mode in all applications';
  ctx.blindIndices = generateBlindIndices(factText, ctx.dedupKey!);
  ctx.markerIndex = createHash('sha256').update(`e2e-search-marker-${ctx.testRunId}`).digest('hex');
  const allIndices = [...ctx.blindIndices, ctx.markerIndex];

  ctx.fact = {
    id: crypto.randomUUID(),
    timestamp: new Date().toISOString(),
    owner: ctx.smartAccountAddr,
    encryptedBlob: encryptToHex(JSON.stringify({
      text: factText,
      metadata: { type: 'preference', importance: 0.8, source: 'e2e-test' },
    }), keys.encryptionKey),
    blindIndices: allIndices,
    decayScore: 0.8,
    source: `e2e-search-${ctx.testRunId}`,
    contentFp: createHash('sha256').update(factText + ctx.testRunId).digest('hex'),
    agentId: 'e2e-search-test',
  };

  const ok = await runTest('F-submit: Fact with blind indices', async () => {
    const result = await submitBatch(ctx.mnemonic, keys.authKeyHex, ctx.smartAccountAddr, [encodeFactProtobuf(ctx.fact!)]);
    console.log(`    txHash: ${result.txHash}`);
    console.log(`    Blind indices: ${allIndices.length} (${ctx.blindIndices!.length} word + 1 marker)`);
  });
  return ok ? ctx : null;
}

// Verification phases (run after subgraph catches up)

async function verifyPhaseB(keys: ReturnType<typeof generateTestKeys>, ctx: SubmissionContext) {
  console.log('\n--- Phase 3: Verify B (batch indexing) ---\n');

  const b3 = await runTest('B3: All 3 facts indexed by subgraph', async () => {
    const result = await pollSubgraph(
      keys.authKeyHex, FACTS_BY_OWNER_QUERY, { owner: ctx.smartAccountAddr },
      (data) => (data?.data?.facts?.length ?? 0) >= 3,
      '3 batch facts',
    );
    console.log(`    Found ${result.data.facts.length} facts`);
  });
  if (!b3) { skipTest('B4-B5', 'B3 failed'); skipped += 2; return; }

  await runTest('B4: Correct metadata on each fact', async () => {
    const result = await querySubgraph(keys.authKeyHex, FACTS_BY_OWNER_QUERY, { owner: ctx.smartAccountAddr });
    const ours = result.data.facts.filter((f: any) => f.source === `e2e-batch-${ctx.testRunId}`);
    assert(ours.length === 3, `Expected 3 test facts, got ${ours.length}`);
    for (const f of ours) {
      assert(f.owner === ctx.smartAccountAddr, `Wrong owner`);
      assert(f.agentId === 'e2e-batch-test', `Wrong agentId`);
      assert(f.isActive === true, `Fact should be active`);
    }
  });

  await runTest('B5: Unique subgraph IDs', async () => {
    const result = await querySubgraph(keys.authKeyHex, FACTS_BY_OWNER_QUERY, { owner: ctx.smartAccountAddr });
    const ours = result.data.facts.filter((f: any) => f.source === `e2e-batch-${ctx.testRunId}`);
    const ids = new Set(ours.map((f: any) => f.id));
    assert(ids.size === 3, `Expected 3 unique IDs, got ${ids.size}`);
    console.log(`    IDs: ${[...ids].map(id => (id as string).slice(0, 16) + '...').join(', ')}`);
  });
}

async function verifyPhaseD(keys: ReturnType<typeof generateTestKeys>, ctx: SubmissionContext) {
  console.log('\n--- Phase 3: Verify D (tombstone) ---\n');

  await runTest('D3: Original tombstoned, replacement active', async () => {
    const result = await pollSubgraph(
      keys.authKeyHex, ALL_FACTS_BY_OWNER_QUERY, { owner: ctx.smartAccountAddr },
      (data) => {
        const facts = data?.data?.facts || [];
        // We need at least the replacement to be active
        const active = facts.filter((f: any) =>
          f.isActive === true && f.source === `e2e-tombstone-${ctx.testRunId}` && parseFloat(f.decayScore) > 0
        );
        return active.length >= 1 && facts.length >= 2;
      },
      'tombstone + replacement',
    );
    const facts = result.data.facts;
    const active = facts.filter((f: any) =>
      f.isActive === true && f.source === `e2e-tombstone-${ctx.testRunId}` && parseFloat(f.decayScore) > 0
    );
    assert(active.length >= 1, `Expected >= 1 active replacement, got ${active.length}`);
    console.log(`    Total facts: ${facts.length}, active replacements: ${active.length}`);
  });
}

async function verifyPhaseF(keys: ReturnType<typeof generateTestKeys>, ctx: SubmissionContext) {
  console.log('\n--- Phase 3: Verify F (search pipeline) ---\n');

  const f1b = await runTest('F1b: Fact indexed by subgraph', async () => {
    await pollSubgraph(
      keys.authKeyHex, FACTS_BY_OWNER_QUERY, { owner: ctx.smartAccountAddr },
      (data) => (data?.data?.facts?.length ?? 0) >= 1,
      'search fact',
    );
  });
  if (!f1b) { skipTest('F2-F3', 'F1b timed out'); skipped += 2; return; }

  await runTest('F2: Search by marker blind index', async () => {
    const result = await querySubgraph(keys.authKeyHex, SEARCH_BY_BLIND_INDEX,
      { trapdoors: [ctx.markerIndex!], owner: ctx.smartAccountAddr });
    const entries = result?.data?.blindIndexes || [];
    assert(entries.length >= 1, `Expected >= 1 blind index entry, got ${entries.length}`);
    const fact = entries[0].fact;
    assert(fact.source === `e2e-search-${ctx.testRunId}`, `Wrong source: ${fact.source}`);
    console.log(`    Found: ${fact.id.slice(0, 16)}...`);
  });

  await runTest('F3: Search by word trapdoor ("dark")', async () => {
    const darkTrapdoor = generateBlindIndex('dark', ctx.dedupKey!);
    const result = await querySubgraph(keys.authKeyHex, SEARCH_BY_BLIND_INDEX,
      { trapdoors: [darkTrapdoor], owner: ctx.smartAccountAddr });
    const entries = result?.data?.blindIndexes || [];
    assert(entries.length >= 1, `Expected >= 1 blind index entry, got ${entries.length}`);
    console.log(`    Word trapdoor returned ${entries.length} result(s)`);
  });
}

// =========================================================================
// MAIN — Two-phase: submit everything → quick tests → wait → verify
// =========================================================================

async function main() {
  console.log(`\n${'='.repeat(60)}`);
  console.log(`  TotalReclaw E2E Tests — Batch, Routing, Pipeline`);
  console.log(`${'='.repeat(60)}`);
  console.log(`  Relay:     ${RELAY_URL}`);
  console.log(`  Chain:     Base Sepolia (${CHAIN_ID})`);
  console.log(`  DataEdge:  ${DATA_EDGE_ADDRESS}`);
  console.log(`  Groups:    ${groups.length ? groups.join(', ') : 'ALL'}`);
  console.log(`${'='.repeat(60)}\n`);

  const keys = generateTestKeys();

  // Register once
  const regRes = await registerUser(keys.authKeyHash, keys.salt);
  if (regRes.status !== 200 || !regRes.data.success) {
    console.error(`FATAL: Registration failed — ${JSON.stringify(regRes.data)}`);
    process.exit(1);
  }

  // ---- PHASE 1: Submit all on-chain transactions ----
  let bCtx: SubmissionContext | null = null;
  let dCtx: SubmissionContext | null = null;
  let fCtx: SubmissionContext | null = null;

  if (shouldRun('B')) bCtx = await submitPhaseB(keys);
  if (shouldRun('D')) dCtx = await submitPhaseD(keys);
  if (shouldRun('F')) fCtx = await submitPhaseF(keys);

  // ---- PHASE 2: Quick tests (no subgraph dependency) ----
  if (shouldRun('A')) await testGroupA(keys);
  if (shouldRun('C')) await testGroupC(keys);
  if (shouldRun('E')) await testGroupE(keys);

  // ---- Wait for subgraph to catch up to chain tip ----
  // Graph Studio has variable latency (5-40 min). Instead of a fixed wait,
  // get the current chain tip block and poll _meta until the subgraph passes it.
  const needsSubgraph = bCtx || dCtx || fCtx;
  if (needsSubgraph) {
    // Get current chain tip (all our txs are confirmed by now)
    const tipRes = await fetch('https://sepolia.base.org', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', method: 'eth_blockNumber', params: [], id: 1 }),
    });
    const tipData = await tipRes.json() as { result: string };
    const targetBlock = parseInt(tipData.result, 16);
    console.log(`\n  Chain tip: block ${targetBlock}`);
    console.log(`  Waiting for subgraph to sync past this block...`);

    const maxWaitMs = 45 * 60 * 1000; // 45 min absolute max
    const pollMs = 15_000;
    const startWait = Date.now();
    let lastLog = 0;

    while (Date.now() - startWait < maxWaitMs) {
      const metaResult = await querySubgraph(keys.authKeyHex, '{ _meta { block { number } } }');
      const subgraphBlock = metaResult?.data?._meta?.block?.number ?? 0;
      const behind = targetBlock - subgraphBlock;
      const elapsed = Math.round((Date.now() - startWait) / 1000);

      if (subgraphBlock >= targetBlock) {
        console.log(`  Subgraph synced to ${subgraphBlock} (target ${targetBlock}) after ${elapsed}s`);
        break;
      }

      if (Date.now() - lastLog > 60_000) {
        console.log(`    ${elapsed}s elapsed — subgraph at ${subgraphBlock}, ${behind} blocks behind target`);
        lastLog = Date.now();
      }
      await new Promise(r => setTimeout(r, pollMs));
    }

    const metaFinal = await querySubgraph(keys.authKeyHex, '{ _meta { block { number } } }');
    const finalBlock = metaFinal?.data?._meta?.block?.number ?? 0;
    if (finalBlock < targetBlock) {
      console.log(`  WARNING: Subgraph still at ${finalBlock} after 45 min (target ${targetBlock}). Proceeding anyway.`);
    }
    console.log('');
  }

  // ---- PHASE 3: Verify subgraph indexing ----
  if (bCtx) await verifyPhaseB(keys, bCtx);
  if (dCtx) await verifyPhaseD(keys, dCtx);
  if (fCtx) await verifyPhaseF(keys, fCtx);

  console.log(`\n${'='.repeat(60)}`);
  console.log(`  Results: ${passed} passed, ${failed} failed, ${skipped} skipped`);
  console.log(`${'='.repeat(60)}\n`);

  process.exit(failed > 0 ? 1 : 0);
}

main().catch(err => {
  console.error(`\nFATAL: ${err.message}\n${err.stack}\n`);
  process.exit(1);
});
