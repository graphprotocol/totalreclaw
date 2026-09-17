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
 *      plugin's own `encodeFactProtobuf`, EACH carrying a realistic
 *      640-d `encryptedEmbedding` (#548) built exactly like the plugin's
 *      write path: `encryptToHex(encodeEmbeddingPayload(vec))` — canonical
 *      f16 (base64 little-endian, 1280 B for 640-d) wrapped in
 *      XChaCha20-Poly1305 (~3.4 KB hex). Legacy-JSON embedding writes are
 *      RETIRED (core 2.6.0 / plugin 3.4.0); canonical f16 is the only
 *      format, and this test fails loudly if the codec ever falls back to
 *      the legacy JSON array (~27 KB — the size distortion that let #498
 *      through undetected).
 *   4. Submit through `submitFactBatchOnChain`; with realistic sizes
 *      (~5-6 KB per fact) the 32 KB byte cap must force MULTIPLE groups
 *      (the count cap alone would allow 30 per group).
 *   5. Poll the staging subgraph until all facts index; then tombstone-skip
 *      (facts are test-vault junk on the isolated staging DataEdge).
 *
 * Run:  npx tsx e2e-449-grouping.ts       (from tests/e2e-batch, needs net)
 */
import { createHash, randomBytes } from 'crypto';
import {
  encodeFactProtobuf,
  submitFactBatchOnChain,
  PROTOBUF_VERSION_V4,
  type FactPayload,
  type SubgraphStoreConfig,
} from '../../skill/plugin/subgraph/subgraph-store.js';
import { MAX_BATCH_BYTES } from '../../skill/plugin/subgraph/batch-sizing.js';
import {
  encodeEmbeddingPayload,
  decodeEmbeddingUniversal,
  EMBEDDING_DIMS,
} from '../../skill/plugin/embedding/embedding-codec.js';
// The plugin's write path encrypts both the claim blob and the embedding
// with XChaCha20-Poly1305 via `runtime/format-helpers.ts::encryptToHex`
// (index.ts: `encryptToHex(encodeEmbeddingPayload(embedding), key)`), so we
// use the real helper rather than a local AES stand-in (#548 fidelity).
import { encryptToHex } from '../../skill/plugin/runtime/format-helpers.js';

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

// ---------------------------------------------------------------------------
// Deterministic 640-d test embeddings (#548)
// ---------------------------------------------------------------------------

/** FNV-1a 32-bit hash — seeds the PRNG from the fact id (reproducible runs). */
function fnv1a(str: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** mulberry32 PRNG — tiny, deterministic, no deps. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Harrier-shaped embedding: EMBEDDING_DIMS (640) finite floats in (-1, 1),
 * deterministic per fact id. Only the SIZE and wire FORMAT matter for the
 * grouping this test exercises — the values just have to be finite and
 * inside f16 range so `encodeEmbeddingCanonical` (fail-closed) accepts them.
 */
function buildTestEmbedding(seed: string): number[] {
  const rand = mulberry32(fnv1a(seed));
  return Array.from({ length: EMBEDDING_DIMS }, () => rand() * 2 - 1);
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

  // 4. Payloads: realistic index load (1 word-index + 20 LSH buckets), a
  //    ~600-char blob, and — since #548 — a realistic 640-d embedding per
  //    fact, encoded exactly like the plugin's write path:
  //    encryptToHex(encodeEmbeddingPayload(vec)) = XChaCha20-Poly1305 over
  //    base64(LE f16) ≈ 3.4KB hex. Each fact therefore encodes to ~5-6KB,
  //    so 35 facts ≈ 200KB total — the 32KB byte cap must split this into
  //    ~7 groups even though the count cap (30) alone would allow a 30+5
  //    split.
  //
  //    Canary first: legacy-JSON embedding writes are RETIRED (canonical
  //    f16 is the only format since core 2.6.0 / plugin 3.4.0). If the
  //    installed core predates `encodeEmbeddingCanonical`, the codec
  //    silently falls back to JSON.stringify(number[]) (~27KB) — fail
  //    loudly instead of letting the #548 size-fidelity gap back in.
  const canaryVec = buildTestEmbedding('e2e449-canary');
  const canaryPayload = encodeEmbeddingPayload(canaryVec);
  check(
    !canaryPayload.startsWith('['),
    `embedding codec produced canonical f16, not legacy JSON (got ${canaryPayload.slice(0, 24)}... — is @totalreclaw/core >= 2.6.0 resolvable from this test?)`,
  );
  check(
    Buffer.from(canaryPayload, 'base64').length === EMBEDDING_DIMS * 2,
    `canonical f16 payload is ${EMBEDDING_DIMS * 2} bytes (640-d)`,
  );
  check(
    decodeEmbeddingUniversal(canaryPayload).length === EMBEDDING_DIMS,
    'universal decoder round-trips the canonical payload to 640 dims',
  );

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
      // Realistic embedding (protobuf field 13): dominates the payload size
      // the way real Harrier embeddings dominate production writes.
      encryptedEmbedding: encryptToHex(
        encodeEmbeddingPayload(buildTestEmbedding(id)),
        encryptionKey,
      ),
      version: PROTOBUF_VERSION_V4,
    };
    payloads.push(encodeFactProtobuf(fact));
  }
  const totalBytes = payloads.reduce((n, b) => n + b.length, 0);
  const minPayload = Math.min(...payloads.map((b) => b.length));
  const maxPayload = Math.max(...payloads.map((b) => b.length));
  console.log(`# ${FACT_COUNT} payloads, ${totalBytes} bytes total (${minPayload}-${maxPayload} bytes each; expect >2 groups under the ${MAX_BATCH_BYTES}-byte cap)`);
  check(totalBytes > MAX_BATCH_BYTES, `realistic payloads exceed one group (${totalBytes} > ${MAX_BATCH_BYTES} bytes)`);
  check(
    payloads.every((p) => p.length < MAX_BATCH_BYTES),
    `every individual fact fits under the byte cap (max ${maxPayload} < ${MAX_BATCH_BYTES})`,
  );
  check(
    minPayload >= 4_000 && maxPayload <= 8_000,
    `per-fact size in the realistic ~5-6KB band (${minPayload}-${maxPayload} bytes; a missing embedding would drop this to ~2KB, a legacy-JSON one would push it past ~27KB)`,
  );

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
  // Pigeonhole lower bound: groups are flushed before exceeding
  // MAX_BATCH_BYTES, so ceil(total/cap) real-size groups are unavoidable —
  // with realistic ~5-6KB facts that is ~7 groups, not the 2 the old
  // embedding-less payloads produced.
  const minGroups = Math.ceil(totalBytes / MAX_BATCH_BYTES);
  check(
    result.groupResults.length >= Math.max(2, minGroups),
    `byte cap forced multiple groups (${result.groupResults.length} >= ${Math.max(2, minGroups)} = ceil(${totalBytes}/${MAX_BATCH_BYTES}))`,
  );
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
