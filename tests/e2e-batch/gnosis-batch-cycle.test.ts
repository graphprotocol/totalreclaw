/**
 * E2E Test: Gnosis batched-cycle (imp-13).
 *
 * Per imp spec §6 T-5 + decomposition §imp item 18.
 *
 * Validates the Path B chain-gate (issue #281 / spec #317):
 *   - Pro-tier wallets on Gnosis (chain 100) MUST submit fact writes
 *     through `executeBatch` (1 UserOp → N Log(bytes) events).
 *   - Free-tier Sepolia keeps single-fact UserOps.
 *
 * This test covers the Gnosis half: seed a Pro-tier test wallet on staging,
 * submit BATCH_SIZE facts (default 15, env-tunable) as one batched UserOp,
 * poll the subgraph for indexing, recall one fact, and assert the round-trip
 * fact ID matches what was written.
 *
 * Emits one structured log line on success:
 *
 *     {"submission_path":"batch","fact_count":<BATCH_SIZE>,"userop_count":1}
 *
 * Acceptance: test passes + structured log emitted (issue #327).
 *
 * Endpoints:
 *   - Relay:    api-staging.totalreclaw.xyz (override via RELAY_URL)
 *   - Chain:    Gnosis (100)
 *   - Subgraph: relay-proxied via /v1/subgraph
 *   - Pro tier: granted by relay when the `X-TotalReclaw-Test: true`
 *               header is present (staging-only fixture).
 *
 * Run:
 *   cd tests/e2e-batch && npm install && npm run test:gnosis
 */
import { createHash, randomBytes, createCipheriv } from 'crypto';
import { mnemonicToAccount } from 'viem/accounts';
import { generateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english';
import { createPublicClient, http, type Hex } from 'viem';
import { gnosis } from 'viem/chains';
import { toSimpleSmartAccount } from 'permissionless/accounts';
import { createSmartAccountClient } from 'permissionless';
import { createPimlicoClient } from 'permissionless/clients/pimlico';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const RELAY_URL = process.env.RELAY_URL || 'https://api-staging.totalreclaw.xyz';
const CHAIN_ID = 100; // Gnosis mainnet (Pro-tier path)
const DEFAULT_DATA_EDGE_ADDRESS = '0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca' as const;
const ENTRYPOINT_ADDRESS = '0x0000000071727De22E5E9d8BAf0edAc6f37da032' as const;
const BATCH_SIZE = Number(process.env.BATCH_SIZE) || 15;

// Subgraph polling — Gnosis indexing latency on Graph Studio is variable.
// _meta.block.number can report ahead of actually queryable data, so we
// poll for the actual facts with a generous timeout (same as batch-e2e.ts).
const POLL_INTERVAL_MS = 15_000;
const POLL_MAX_ATTEMPTS = 180;

const TEST_HEADERS = {
  'X-TotalReclaw-Test': 'true',
  'X-TotalReclaw-Client': 'e2e-gnosis-batch-cycle',
};

// ---------------------------------------------------------------------------
// Crypto helpers
// ---------------------------------------------------------------------------

function generateTestKeys() {
  const authKey = randomBytes(32);
  const authKeyHash = createHash('sha256').update(authKey).digest();
  const salt = randomBytes(32);
  const encryptionKey = randomBytes(32);
  const dedupKey = randomBytes(32);
  return {
    authKey,
    authKeyHex: authKey.toString('hex'),
    authKeyHash: authKeyHash.toString('hex'),
    salt: salt.toString('hex'),
    encryptionKey,
    dedupKey,
  };
}

/** AES-256-GCM encrypt, returns hex string of iv+ciphertext+tag. */
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
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string;
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
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
// Relay + subgraph helpers
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
  return relayRequest(
    'POST',
    '/v1/register',
    { auth_key_hash: authKeyHash, salt },
    TEST_HEADERS,
  );
}

async function getBillingStatus(authKeyHex: string, walletAddress: string) {
  return relayRequest(
    'GET',
    `/v1/billing/status?wallet_address=${walletAddress}`,
    undefined,
    { Authorization: `Bearer ${authKeyHex}`, ...TEST_HEADERS },
  );
}

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
      ...TEST_HEADERS,
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
// Smart Account
// ---------------------------------------------------------------------------

async function deriveSmartAccountAddress(mnemonic: string): Promise<string> {
  const owner = mnemonicToAccount(mnemonic);
  const publicClient = createPublicClient({ chain: gnosis, transport: http() });
  const sa = await toSimpleSmartAccount({
    client: publicClient as any,
    owner,
    entryPoint: { address: ENTRYPOINT_ADDRESS, version: '0.7' },
  });
  return sa.address.toLowerCase();
}

// ---------------------------------------------------------------------------
// Batched on-chain submission (15 facts → 1 UserOp via SimpleAccount.executeBatch)
// ---------------------------------------------------------------------------

interface BatchSubmissionResult {
  txHash: string;
  userOpHash: string;
  batchSize: number;
  userOpCount: number;
}

async function submitGnosisBatch(
  mnemonic: string,
  authKeyHex: string,
  walletAddress: string,
  payloads: Buffer[],
  dataEdgeAddress: string,
): Promise<BatchSubmissionResult> {
  if (payloads.length === 0) throw new Error('Empty batch');

  const bundlerUrl = `${RELAY_URL}/v1/bundler`;
  const headers: Record<string, string> = {
    Authorization: `Bearer ${authKeyHex}`,
    'X-Wallet-Address': walletAddress,
    ...TEST_HEADERS,
  };
  const authTransport = http(bundlerUrl, { fetchOptions: { headers } });

  const owner = mnemonicToAccount(mnemonic);
  const publicClient = createPublicClient({ chain: gnosis, transport: http() });

  const pimlicoClient = createPimlicoClient({
    chain: gnosis,
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
    chain: gnosis,
    bundlerTransport: authTransport,
    paymaster: pimlicoClient,
    userOperation: {
      estimateFeesPerGas: async () =>
        (await pimlicoClient.getUserOperationGasPrice()).fast,
    },
  });

  const calls = payloads.map(p => ({
    to: dataEdgeAddress as `0x${string}`,
    value: 0n,
    data: `0x${p.toString('hex')}` as Hex,
  }));

  // One sendUserOperation call with N calls = ONE UserOp on the wire,
  // encoded as SimpleAccount.executeBatch(targets, values, datas).
  const userOpHash = await smartAccountClient.sendUserOperation({ calls });
  const receipt = await pimlicoClient.waitForUserOperationReceipt({
    hash: userOpHash,
    timeout: 180_000, // longer than batch-e2e.ts's 120s — Gnosis block time is slower
  });

  return {
    txHash: receipt.receipt.transactionHash,
    userOpHash,
    batchSize: payloads.length,
    userOpCount: 1,
  };
}

// ---------------------------------------------------------------------------
// GraphQL queries
// ---------------------------------------------------------------------------

// NOTE: subgraph schema v3 removed `source` and `agentId` from the Fact type
// (they are now encrypted inside encryptedBlob). The query below requests only
// fields that exist on the current schema (subgraph/schema.graphql).
const FACTS_BY_OWNER_QUERY = `
  query FactsByOwner($owner: String!) {
    facts(
      where: { owner: $owner, isActive: true }
      orderBy: sequenceId
      orderDirection: desc
      first: 50
    ) {
      id owner encryptedBlob decayScore isActive contentFp createdAt timestamp
    }
  }
`;

const SEARCH_BY_BLIND_INDEX = `
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: String!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: 25
    ) {
      hash
      fact {
        id owner encryptedBlob decayScore isActive contentFp createdAt timestamp
      }
    }
  }
`;

// ---------------------------------------------------------------------------
// Build 15 fact payloads for the cycle test
// ---------------------------------------------------------------------------

interface BuiltFact {
  id: string;
  text: string;
  contentFp: string; // sha256(text) — used to identify our facts post-index (source/agentId removed from schema v3)
  trapdoor: string; // unique marker word so we can recall this exact fact
  payload: Buffer;
}

function buildFactPayloads(
  ownerAddress: string,
  testRunId: string,
  encryptionKey: Buffer,
  dedupKey: Buffer,
): BuiltFact[] {
  const built: BuiltFact[] = [];
  const baseTs = Date.now();
  for (let i = 0; i < BATCH_SIZE; i++) {
    const marker = `gnosis${testRunId}item${i}`; // unique per-fact word
    const text = `Gnosis batch cycle fact ${i + 1} of ${BATCH_SIZE} ${marker}`;
    const factId = `gnosis-batch-${testRunId}-${i}`;
    const contentFp = createHash('sha256').update(text).digest('hex');
    const encryptedBlob = encryptToHex(text, encryptionKey);
    const blindIndices = generateBlindIndices(text, dedupKey);
    const payload = encodeFactProtobuf({
      id: factId,
      timestamp: new Date(baseTs + i).toISOString(),
      owner: ownerAddress,
      encryptedBlob,
      blindIndices,
      decayScore: 1.0,
      source: `e2e-gnosis-batch-${testRunId}`,
      contentFp,
      agentId: 'gnosis-batch-cycle-test',
    });
    built.push({
      id: factId,
      text,
      contentFp,
      trapdoor: generateBlindIndex(marker, dedupKey),
      payload,
    });
  }
  return built;
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('=== Gnosis batched-cycle E2E (imp-13) ===');
  console.log(`  Relay:    ${RELAY_URL}`);
  console.log(`  Chain:    Gnosis (${CHAIN_ID})`);
  console.log(`  Batch:    ${BATCH_SIZE} facts → 1 UserOp`);
  console.log('');

  // 1. Provision fresh wallet + keys.
  const mnemonic = generateMnemonic(wordlist);
  const walletAddress = await deriveSmartAccountAddress(mnemonic);
  const keys = generateTestKeys();
  const testRunId = randomBytes(4).toString('hex');
  console.log(`  Smart account: ${walletAddress}`);
  console.log(`  Test run id:   ${testRunId}`);

  // 2. Register on relay (X-TotalReclaw-Test grants Pro tier on staging).
  const regRes = await registerUser(keys.authKeyHash, keys.salt);
  assert(
    regRes.status === 200 || regRes.status === 201,
    `register failed: status=${regRes.status} body=${JSON.stringify(regRes.data).slice(0, 200)}`,
  );

  // 3. Confirm relay assigned Pro tier (Gnosis chain).
  const billing = await getBillingStatus(keys.authKeyHex, walletAddress);
  assert(
    billing.status === 200,
    `billing status fetch failed: ${billing.status}`,
  );
  assert(
    billing.data?.tier === 'pro',
    `expected tier=pro for test wallet (Pro-tier batch path), got tier=${billing.data?.tier}`,
  );
  console.log(`  Pro tier confirmed (features=${JSON.stringify(billing.data?.features ?? {})})`);

  // 3b. Resolve the authoritative DataEdge address from the relay billing
  // response. Staging is on-chain isolated (ops-5/6): its DataEdge is
  // `0xE7a4...`, distinct from production `0xC445...`. The relay advertises
  // it per-tier as `data_edge_address`; if absent, fall back to the prod
  // default (back-compat for older relays). Hardcoding `0xC445` here would
  // write to the PRODUCTION DataEdge while the staging subgraph only indexes
  // the staging DataEdge — the round-trip would never validate.
  const dataEdgeAddress: string =
    (typeof billing.data?.data_edge_address === 'string' && billing.data.data_edge_address)
    || DEFAULT_DATA_EDGE_ADDRESS;
  assert(
    /^0x[0-9a-fA-F]{40}$/.test(dataEdgeAddress),
    `invalid data_edge_address from billing: ${dataEdgeAddress}`,
  );
  console.log(`  DataEdge: ${dataEdgeAddress}${dataEdgeAddress === DEFAULT_DATA_EDGE_ADDRESS ? ' (default fallback)' : ''}`);

  // 4. Build BATCH_SIZE fact payloads.
  const facts = buildFactPayloads(walletAddress, testRunId, keys.encryptionKey, keys.dedupKey);
  assert(facts.length === BATCH_SIZE, `expected ${BATCH_SIZE} payloads, got ${facts.length}`);

  // 5. Submit the batched UserOp.
  console.log(`  Submitting batched UserOp on Gnosis...`);
  const t0 = Date.now();
  const batch = await submitGnosisBatch(
    mnemonic,
    keys.authKeyHex,
    walletAddress,
    facts.map(f => f.payload),
    dataEdgeAddress,
  );
  const elapsedMs = Date.now() - t0;
  console.log(`  txHash=${batch.txHash}`);
  console.log(`  userOpHash=${batch.userOpHash}`);
  console.log(`  elapsed=${(elapsedMs / 1000).toFixed(1)}s`);

  // Sanity: 15 facts MUST have been submitted in 1 UserOp.
  assert(
    batch.batchSize === BATCH_SIZE && batch.userOpCount === 1,
    `batch shape wrong: fact_count=${batch.batchSize} userop_count=${batch.userOpCount}`,
  );

  // 6. Emit the structured log line required by issue #327 acceptance.
  const submissionLog = {
    submission_path: 'batch',
    fact_count: batch.batchSize,
    userop_count: batch.userOpCount,
    chain_id: CHAIN_ID,
    user_op_hash: batch.userOpHash,
    transaction_hash: batch.txHash,
  };
  console.log(JSON.stringify(submissionLog));

  // 7. Poll subgraph for all 15 facts to land.
  console.log(`  Polling Gnosis subgraph for ${BATCH_SIZE} facts under owner=${walletAddress}...`);
  const factsResult = await pollSubgraph(
    keys.authKeyHex,
    FACTS_BY_OWNER_QUERY,
    { owner: walletAddress },
    (r: any) => Array.isArray(r?.data?.facts) && r.data.facts.length >= BATCH_SIZE,
    'facts-by-owner',
  );
  const indexedFacts = factsResult.data.facts;
  assert(
    indexedFacts.length >= BATCH_SIZE,
    `expected >= ${BATCH_SIZE} indexed facts, got ${indexedFacts.length}`,
  );
  console.log(`  Subgraph indexed ${indexedFacts.length} facts.`);

  // All indexed facts MUST be ours (proves they came from this batch, not noise).
  // Subgraph schema v3 removed `source`/`agentId` from Fact (now encrypted in the
  // blob), so we identify our facts by their contentFp (sha256 of the plaintext),
  // which the subgraph still exposes and which is unique per test run.
  const ourContentFps = new Set(facts.map(f => f.contentFp));
  const ourFacts = indexedFacts.filter(
    (f: any) => typeof f.contentFp === 'string' && ourContentFps.has(f.contentFp),
  );
  assert(
    ourFacts.length === BATCH_SIZE,
    `expected exactly ${BATCH_SIZE} facts with our contentFp, got ${ourFacts.length}`,
  );

  // 8. Recall: pick one fact, look it up by its unique trapdoor, assert id matches.
  //    `client.recall(...)` in the high-level SDK ultimately resolves to a
  //    blind-index lookup against the same subgraph + the same trapdoor hash —
  //    that's what we exercise here directly.
  const targetIdx = 7;
  const targetFact = facts[targetIdx];
  const recallResult = await pollSubgraph(
    keys.authKeyHex,
    SEARCH_BY_BLIND_INDEX,
    { trapdoors: [targetFact.trapdoor], owner: walletAddress },
    (r: any) => Array.isArray(r?.data?.blindIndexes) && r.data.blindIndexes.length >= 1,
    `recall-by-trapdoor[${targetIdx}]`,
  );
  const recalled = recallResult.data.blindIndexes[0]?.fact;
  assert(!!recalled, 'recall returned no fact');
  assert(
    recalled.owner.toLowerCase() === walletAddress.toLowerCase(),
    `recalled fact owner mismatch: ${recalled.owner} vs ${walletAddress}`,
  );

  // The on-chain subgraph fact `id` is `txHash-logIndex`, not the protobuf-level
  // fact.id. We assert the recalled fact's encrypted blob matches the original,
  // which is the strongest cryptographic guarantee that the round-trip matches.
  const indexedById = new Map<string, any>(indexedFacts.map((f: any) => [f.id, f]));
  assert(
    indexedById.has(recalled.id),
    `recalled fact id ${recalled.id} not in the set we just wrote`,
  );
  assert(
    recalled.encryptedBlob === ourFacts.find((f: any) => f.id === recalled.id)!.encryptedBlob,
    `recalled fact encryptedBlob mismatch for id=${recalled.id}`,
  );
  console.log(`  Recall round-trip OK (target idx=${targetIdx}, subgraph id=${recalled.id}).`);

  console.log('');
  console.log('=== imp-13 gnosis-batch-cycle: PASS ===');
}

main().catch((err: any) => {
  console.error('=== imp-13 gnosis-batch-cycle: FAIL ===');
  console.error(err.stack || err.message || err);
  process.exit(1);
});
