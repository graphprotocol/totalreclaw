/**
 * E2E (internal#449): byte-capped adaptive grouping through the PLUGIN's
 * real submit path against STAGING.
 *
 * Unlike gnosis-batch-cycle.test.ts (which builds its own viem UserOps to
 * validate the relay/bundler/subgraph pipeline), this drives the plugin's
 * `submitFactBatchOnChain` — the code #449 changed — end-to-end:
 *
 *   1. Fresh mnemonic + register on staging (X-TotalReclaw-Test).
 *   2. Read chain_id + data_edge_address from billing (client-consistency).
 *   3. Encode FACT_COUNT payloads with realistic index counts via the
 *      plugin's own `encodeFactProtobuf`, sized so the 32KB byte cap forces
 *      MULTIPLE groups (count cap alone would allow 30).
 *   4. Submit through `submitFactBatchOnChain`; assert multi-group success
 *      with distinct UserOp hashes and per-group size ≤ the count cap.
 *   5. Poll the staging subgraph until all facts index; then tombstone-skip
 *      (facts are test-vault junk on the isolated staging DataEdge).
 *
 * Run:  npx tsx e2e-449-grouping.ts       (from tests/e2e-batch, needs net)
 */
import { createHash, randomBytes, createCipheriv } from 'crypto';
import {
  encodeFactProtobuf,
  submitFactBatchOnChain,
  PROTOBUF_VERSION_V4,
  type FactPayload,
  type SubgraphStoreConfig,
} from '../../skill/plugin/subgraph/subgraph-store.js';

const RELAY_URL = process.env.RELAY_URL || 'https://api-staging.totalreclaw.xyz';
if (RELAY_URL.includes('api.totalreclaw.xyz')) {
  throw new Error('E2E must hit staging, never production');
}
const FACT_COUNT = Number(process.env.FACT_COUNT || 35);

const TEST_HEADERS = {
  'X-TotalReclaw-Test': 'true',
  'X-TotalReclaw-Client': 'e2e-449-grouping',
};

let passed = 0;
let failed = 0;
function check(cond: boolean, name: string): void {
  if (cond) { console.log(`ok ${++passed + failed} - ${name}`); }
  else { console.error(`not ok ${passed + ++failed} - ${name}`); process.exitCode = 1; }
}

function encryptToHex(plaintext: string, key: Buffer): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  return Buffer.concat([iv, encrypted, cipher.getAuthTag()]).toString('hex');
}

async function relayJson(method: string, path: string, body?: unknown, headers: Record<string, string> = {}) {
  const res = await fetch(`${RELAY_URL}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...TEST_HEADERS, ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, data: await res.json().catch(() => ({})) };
}

async function main() {
  // 1. Identity: 12-word test mnemonic (throwaway) + relay auth keys.
  const { generateMnemonic } = await import('@scure/bip39');
  const { wordlist } = await import('@scure/bip39/wordlists/english');
  const mnemonic = generateMnemonic(wordlist);
  const authKey = randomBytes(32);
  const authKeyHex = authKey.toString('hex');
  const authKeyHash = createHash('sha256').update(authKey).digest('hex');
  const salt = randomBytes(32).toString('hex');
  const encryptionKey = randomBytes(32);

  // 2. Register (retry on the staging rate-limit window).
  let reg = await relayJson('POST', '/v1/register', { auth_key_hash: authKeyHash, salt });
  for (let i = 0; reg.status === 429 && i < 3; i++) {
    console.log('register 429 — waiting 60s');
    await new Promise((r) => setTimeout(r, 60_000));
    reg = await relayJson('POST', '/v1/register', { auth_key_hash: authKeyHash, salt });
  }
  check(reg.status === 200 && reg.data.success === true, `registered on staging (status=${reg.status})`);

  // 3. Billing → authoritative chain + DataEdge (client-consistency rule).
  const billing = await relayJson('GET', '/v1/billing/status?wallet_address=0x0000000000000000000000000000000000000001', undefined, {
    Authorization: `Bearer ${authKeyHex}`,
  });
  const chainId = billing.data.chain_id ?? 100;
  const dataEdge = billing.data.data_edge_address ?? '';
  check(chainId === 100, `billing chain_id=100 (got ${chainId})`);
  check(/^0x[0-9a-fA-F]{40}$/.test(dataEdge), `billing data_edge_address present (${dataEdge})`);

  // 4. Payloads: realistic index load (1 word-index + 20 LSH buckets) and a
  //    ~600-char blob → ~2.2KB each encoded, so 35 facts ≈ 77KB total —
  //    the 32KB byte cap must split this into ≥3 groups even though the
  //    count cap (30) alone would allow a 30+5 split.
  const payloads: Buffer[] = [];
  const ids: string[] = [];
  for (let i = 0; i < FACT_COUNT; i++) {
    const id = `e2e449-${Date.now()}-${i}-${randomBytes(4).toString('hex')}`;
    ids.push(id);
    const fact: FactPayload = {
      id,
      timestamp: new Date().toISOString(),
      owner: '',
      encryptedBlob: encryptToHex(
        JSON.stringify({ text: `e2e449 grouping fact ${i} ` + 'x'.repeat(500), type: 'claim', source: 'derived', created_at: new Date().toISOString(), schema_version: '1.0' }),
        encryptionKey,
      ),
      blindIndices: Array.from({ length: 21 }, (_, j) =>
        createHash('sha256').update(`e2e449:${id}:${j}`).digest('hex'),
      ),
      decayScore: 100,
      source: 'e2e-449',
      contentFp: createHash('sha256').update(id).digest('hex'),
      agentId: 'e2e-449',
      version: PROTOBUF_VERSION_V4,
    };
    payloads.push(encodeFactProtobuf(fact));
  }
  const totalBytes = payloads.reduce((n, b) => n + b.length, 0);
  console.log(`# ${FACT_COUNT} payloads, ${totalBytes} bytes total (expect >2 groups under the 32KB cap)`);

  // 5. Submit through the REAL plugin path.
  const config: SubgraphStoreConfig = {
    relayUrl: RELAY_URL,
    mnemonic,
    cachePath: '/tmp/e2e449-cache.json',
    chainId,
    dataEdgeAddress: dataEdge,
    entryPointAddress: '',
    authKeyHex,
  };
  const t0 = Date.now();
  const result = await submitFactBatchOnChain(payloads, config);
  console.log(`# submit took ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  console.log(`# groups: ${result.groupResults.map((g) => g.batchSize).join(', ')} | errors: ${result.errors.length}`);

  check(result.success === true, `overall success (errors: ${result.errors.join('; ') || 'none'})`);
  check(result.batchSize === FACT_COUNT, `all ${FACT_COUNT} facts stored (got ${result.batchSize})`);
  check(result.groupResults.length >= 2, `byte cap forced multiple groups (${result.groupResults.length})`);
  check(result.groupResults.every((g) => g.batchSize <= 30), 'every group within the count cap');
  const hashes = new Set(result.groupResults.map((g) => g.userOpHash));
  check(hashes.size === result.groupResults.length, 'distinct UserOp hash per group');
  check(result.groupResults.every((g) => g.success), 'every group receipt success=true');

  // 6. Subgraph: poll until all FACT_COUNT facts index for this owner.
  //    Owner = the SA the plugin derived; recover it from any receipt via the
  //    subgraph facts query on our unique agent-run source ids.
  const query = `query($ids: [String!]) { facts(where: { id_in: $ids }) { id } }`;
  let indexed = 0;
  for (let i = 0; i < 30 && indexed < FACT_COUNT; i++) {
    await new Promise((r) => setTimeout(r, 10_000));
    const res = await fetch(`${RELAY_URL}/v1/subgraph`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${authKeyHex}`, ...TEST_HEADERS },
      body: JSON.stringify({ query, variables: { ids } }),
    });
    const json = (await res.json().catch(() => ({}))) as { data?: { facts?: Array<{ id: string }> } };
    indexed = json.data?.facts?.length ?? 0;
    console.log(`# poll ${i + 1}: ${indexed}/${FACT_COUNT} indexed`);
  }
  check(indexed === FACT_COUNT, `staging subgraph indexed ${indexed}/${FACT_COUNT}`);

  console.log(`\n# e2e-449-grouping — ${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

main().catch((e) => {
  console.error('FATAL:', e);
  process.exit(1);
});
