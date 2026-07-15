/**
 * batch-sizing.test.ts — internal#449
 *
 * Byte-capped adaptive batch sizing for the plugin's executeBatch UserOp path.
 * Ports + calibrates the design from the Python reference
 * (python/src/totalreclaw/operations.py::group_and_store_adaptive, internal#435/#461/#490)
 * to the TS plugin, factored into a pure, network-free module so the grouping,
 * estimator, and halve-on-simfail semantics can be pinned without touching a
 * bundler or the AA10/AA25 submit machinery.
 *
 * Coverage mirrors python/tests/test_batch_sizing_rc4.py +
 * test_imp448_shared_batch_sizing.py:
 *   (a) estimator golden — est >= REAL encodeFactProtobuf across ASCII / CJK /
 *       emoji / RTL / all-unique-token / crystal-metadata fixtures (the est>=real
 *       invariant the python side calibrated across 3 review rounds);
 *   (b) grouping — count cap, byte cap, oversize-lone, nothing dropped/duplicated;
 *   (c) sim-revert detection (-32500 / "reverted during simulation", AA25-excluded);
 *   (d) adaptive halve-on-simfail cascade via a stubbed storeFn ([10,5,5] / [4,2,2] /
 *       floor-1 error / AA25 no-halve / no-code-still-halves).
 *
 * Run with: npx tsx batch-sizing.test.ts   (TAP-style, no jest dependency)
 */

import {
  BYTES_FIXED_OVERHEAD,
  BYTES_PER_BLIND_INDEX,
  MAX_BATCH_BYTES,
  MAX_BATCH_GROUP_COUNT,
  estimatePayloadBytes,
  groupPayloadsBySize,
  isSimRevertError,
  groupAndStoreAdaptive,
} from './batch-sizing.js';

// Real core protobuf encoder + crypto — the ground truth the estimator must bound.
import { encodeFactProtobuf, PROTOBUF_VERSION_V4 } from './subgraph-store.js';
import { deriveKeys, encrypt, generateBlindIndices, generateContentFingerprint } from '../crypto/vault-crypto.js';

let passed = 0;
let failed = 0;
let testNum = 0;
function check(cond: boolean, msg: string): void {
  testNum++;
  if (cond) {
    passed++;
  } else {
    failed++;
    console.error(`not ok ${testNum} - ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Crypto fixtures — build a FactPayload the way the real store path does, then
// measure its REAL encoded protobuf length (non-circular ground truth).
// ---------------------------------------------------------------------------
const MNEMONIC = 'test test test test test test test test test test test junk';
const KEYS = deriveKeys(MNEMONIC) as { encryptionKey: Buffer; dedupKey: Buffer };
const OWNER = '0x' + 'a'.repeat(40);

function encryptToHex(plaintext: string): string {
  return Buffer.from(encrypt(plaintext, KEYS.encryptionKey), 'base64').toString('hex');
}

/** A canonical v1 claim JSON blob (what the plugin encrypts into encryptedBlob). */
function claimJson(text: string, meta?: Record<string, unknown>): string {
  const claim: Record<string, unknown> = {
    text, type: 'claim', source: 'external', scope: 'personal', importance: 8,
  };
  if (meta) claim.extra_metadata = meta;
  return JSON.stringify(claim);
}

/**
 * Build a real FactPayload from text (+optional embedding/meta) the way the
 * plugin's store path does, and return both it and the SizingInput the
 * estimator consumes. When an embedding is present the real store appends 20
 * LSH bucket indices to blindIndices — we mirror that so the measured protobuf
 * reflects a realistic embedding-carrying fact.
 */
function buildFact(text: string, opts?: { embedding?: number[]; meta?: Record<string, unknown> }) {
  const wordIndices = generateBlindIndices(text);
  const encryptedBlob = encryptToHex(claimJson(text, opts?.meta));
  let encryptedEmbedding: string | undefined;
  let blindIndices = wordIndices;
  if (opts?.embedding) {
    encryptedEmbedding = encryptToHex(JSON.stringify(opts.embedding));
    blindIndices = [...wordIndices, ...Array.from({ length: 20 }, () => '0'.repeat(64))]; // 20 LSH buckets
  }
  const factPayload = {
    id: 'id-' + 'x'.repeat(32),
    timestamp: '2026-05-14T09:21:03.512Z',
    owner: OWNER,
    encryptedBlob,
    blindIndices,
    decayScore: 0.8,
    source: 'import',
    contentFp: generateContentFingerprint(text, KEYS.dedupKey),
    agentId: 'openclaw-plugin-auto',
    version: PROTOBUF_VERSION_V4,
    ...(encryptedEmbedding ? { encryptedEmbedding } : {}),
  };
  return { factPayload, sizing: { encryptedBlob, blindIndices, encryptedEmbedding } };
}

const EMB_640 = (step: number) => Array.from({ length: 640 }, (_, i) => step * i);

// ---------------------------------------------------------------------------
// (A) constants exposed
// ---------------------------------------------------------------------------
check(MAX_BATCH_BYTES === 32_000, 'MAX_BATCH_BYTES is the python-calibrated 32_000');
check(MAX_BATCH_GROUP_COUNT === 30, 'MAX_BATCH_GROUP_COUNT is core MAX_BATCH_SIZE (30)');
check(BYTES_FIXED_OVERHEAD === 620, 'BYTES_FIXED_OVERHEAD ported from python (620)');
check(BYTES_PER_BLIND_INDEX === 68, 'BYTES_PER_BLIND_INDEX ported from python (68)');

// ---------------------------------------------------------------------------
// (B) estimator golden — est >= REAL encodeFactProtobuf (non-circular)
// ---------------------------------------------------------------------------
const PROSE = (
  'The user moved to Berlin in May 2026 for a new engineering role at a ' +
  'startup. They are looking for an apartment in Prenzlauer Berg or Mitte ' +
  'with a budget around 1500 euros per month and want good public transport.'
);
function padText(seed: string, nchars: number): string {
  let t = seed;
  while (t.length < nchars) t += ' ' + seed;
  return t.slice(0, nchars);
}

let estGeReal = 0;
for (const nchars of [50, 300, 600, 900]) {
  for (const withEmb of [false, true]) {
    const text = padText(PROSE, nchars);
    const { factPayload, sizing } = buildFact(text, withEmb ? { embedding: EMB_640(0.01) } : {});
    const real = encodeFactProtobuf(factPayload).length;
    const est = estimatePayloadBytes(sizing);
    estGeReal++;
    check(est >= real, `est(${est}) >= real(${real}) for ${nchars}-char prose, emb=${withEmb}`);
  }
}

// Adversarial: all-unique tokens → maximal blind-index count (the PR #461 trap:
// a char-linear estimate under-counts; the real index count must bound it).
{
  const text = Array.from({ length: 120 }, (_, i) => `tok${i}word`).join(' ');
  const { factPayload, sizing } = buildFact(text, { embedding: EMB_640(0.01) });
  const real = encodeFactProtobuf(factPayload).length;
  const est = estimatePayloadBytes(sizing);
  check(est >= real, `est(${est}) >= real(${real}) for all-unique-token text + embedding`);
}

// Dense non-ASCII: the blob stores UTF-8 BYTES (ensure_ascii=False), so a
// code-point count would under-count (CJK ~3B/char, emoji ~4B/cp, RTL ~2B/char).
const NONASCII: Record<string, string> = {
  cjk: '这是一个关于搬到柏林并寻找住房的对话摘要用户需要完成登记并购买保险还要办理居留许可',
  emoji: 'Trip recap 🎉🏙️🚆🏡💶📝✈️🗺️🎊🥳 moving to Berlin ',
  rtl: 'ملخص المحادثة حول الانتقال إلى برلين والبحث عن سكن والتسجيل والتأمين وتصريح الإقامة ',
};
for (const [script, seed] of Object.entries(NONASCII)) {
  for (const nchars of [300, 1000]) {
    for (const withEmb of [false, true]) {
      const text = padText(seed, nchars);
      const { factPayload, sizing } = buildFact(text, withEmb ? { embedding: EMB_640(0.01) } : {});
      const real = encodeFactProtobuf(factPayload).length;
      const est = estimatePayloadBytes(sizing);
      check(est >= real, `est(${est}) >= real(${real}) for ${script} ${nchars} chars, emb=${withEmb}`);
    }
  }
}

// Crystal-shaped fact: extra_metadata carried inside the encrypted blob.
{
  const meta = {
    key_outcomes: ['moved to Berlin', 'signed a lease', 'started job'],
    open_threads: ['find a school', 'set up insurance'],
    topics_discussed: ['relocation', 'housing', 'work', 'transport'],
    session_title: 'Moving to Berlin for a new job',
    subtype: 'session_crystal',
  };
  const { factPayload, sizing } = buildFact(
    'Session summary about relocating to Berlin and finding housing.',
    { embedding: EMB_640(0.01), meta },
  );
  const real = encodeFactProtobuf(factPayload).length;
  const est = estimatePayloadBytes(sizing);
  check(est >= real, `est(${est}) >= real(${real}) for a Crystal (extra_metadata) fact`);
}
check(estGeReal >= 8, `estimator golden: ran ${estGeReal} est>=real prose cases`);

// Sanity: the estimator is not wildly over (within 1.6x) for a representative
// embedding-carrying fact — guards against a degenerate "always returns huge" impl.
{
  const { factPayload, sizing } = buildFact(padText(PROSE, 600), { embedding: EMB_640(0.01) });
  const real = encodeFactProtobuf(factPayload).length;
  const est = estimatePayloadBytes(sizing);
  check(est < real * 1.6, `estimator is reasonably tight: est(${est}) < 1.6x real(${real})`);
}

// ---------------------------------------------------------------------------
// (C) grouping — count cap, byte cap, oversize-lone, no drop/dup
// ---------------------------------------------------------------------------
function sizeOfTextLen(len: number) {
  return (_s: string, i: number, arr: string[]) => len; // placeholder, unused
}
// Tiny items: count cap binds (30). 40 tiny items → [30, 10].
{
  const tiny = Array.from({ length: 40 }, () => 'short');
  const groups = groupPayloadsBySize(tiny, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(groups.flat().length === 40, 'grouping drops nothing (count-cap case)');
  check(groups.every(g => g.length <= MAX_BATCH_GROUP_COUNT), 'every group <= count cap');
  check(groups.map(g => g.length).join(',') === '30,10', `40 tiny items group as [30,10] (got [${groups.map(g => g.length).join(',')}])`);
}
// Heavy items: byte cap binds. 40 ~5KB items → every group <= 30 AND <= 32KB, max < 30.
{
  const heavy = Array.from({ length: 40 }, () => 'h');
  const groups = groupPayloadsBySize(heavy, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 5_000);
  check(groups.flat().length === 40, 'grouping drops nothing (byte-cap case)');
  check(groups.every(g => g.length <= MAX_BATCH_GROUP_COUNT), 'heavy groups <= count cap');
  check(groups.every(g => g.length * 5_000 <= MAX_BATCH_BYTES), 'heavy groups <= byte cap');
  check(Math.max(...groups.map(g => g.length)) < MAX_BATCH_GROUP_COUNT, 'byte cap (not count) binds for heavy items');
}
// A single oversize item (> byte cap) still forms its own group (never dropped).
{
  const groups = groupPayloadsBySize(['huge'], MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 100_000);
  check(groups.length === 1 && groups[0].length === 1, 'oversize lone item forms its own group');
}
// Boundary: flush BEFORE the item that would exceed (greedy, not backfill).
{
  // maxBytes=100, sizes [60,60] → first 60 fits, second 60 would make 120>100 → flush [60],[60].
  const items = [60, 60, 60];
  const groups = groupPayloadsBySize(items, 30, 100, (n: number) => n);
  check(groups.map(g => g.length).join(',') === '1,1,1', `byte boundary flushes greedily (got [${groups.map(g => g.length).join(',')}])`);
}
void sizeOfTextLen;

// ---------------------------------------------------------------------------
// (D) sim-revert detection (-32500 / "reverted during simulation", AA25-excluded)
// ---------------------------------------------------------------------------
check(isSimRevertError(new Error('RPC pm_sponsorUserOperation: -32500 Sender does not implement validateUserOp')) === true, '-32500 sim revert detected');
check(isSimRevertError(new Error('UserOperation reverted during simulation with reason: out of gas')) === true, '"reverted during simulation" detected');
check(isSimRevertError(new Error('RPC eth_sendUserOperation: AA25 invalid account nonce (code -32500)')) === false, 'AA25 -32500 is NOT a size revert (excluded)');
check(isSimRevertError(new Error('RPC eth_estimateUserOperationGas: AA25 invalid account nonce')) === false, 'AA25 without -32500 excluded');
check(isSimRevertError(new Error('some unrelated network error')) === false, 'unrelated error not a sim revert');
check(isSimRevertError('-32500 reverted during simulation') === true, 'string error also detected');

// ---------------------------------------------------------------------------
// (E) adaptive halve-on-simfail cascade (stubbed storeFn, no network/WASM)
// ---------------------------------------------------------------------------
function simRevertErr(): Error {
  return new Error('UserOperation reverted during simulation with reason: -32500 Sender does not implement validateUserOp');
}

// A recorder storeFn that raises a sim-revert when failPred(group size) is true.
function recordingStore(failPred: (n: number) => boolean): { fn: (g: string[]) => Promise<string[]>; calls: number[] } {
  const calls: number[] = [];
  const fn = async (group: string[]) => {
    calls.push(group.length);
    if (failPred(group.length)) throw simRevertErr();
    return group.map((_, i) => `id-${calls.length}-${i}`);
  };
  return { fn, calls };
}
function tinyPayloads(n: number): string[] {
  return Array.from({ length: n }, () => 'p');
}

// 10 facts, fail groups > 5 → halves [10]→[5,5], both succeed → [10,5,5], all stored.
{
  const { fn, calls } = recordingStore(n => n > 5);
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(10), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(errors.length === 0, 'halve cascade stores all 10 with no errors');
  check(results.length === 2, 'two successful (sub)group results after halving');
  check(calls.join(',') === '10,5,5', `halving cascade is [10,5,5] (got [${calls.join(',')}])`);
}
// 4 facts, fail sizes in {4,1} → [4] halves to [2,2], both succeed → [4,2,2].
{
  const { fn, calls } = recordingStore(n => n === 4 || n === 1);
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(4), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(errors.length === 0, 'partial-floor case stores all 4 with no errors');
  check(results.length === 2, 'two successful results');
  check(calls.join(',') === '4,2,2', `halving cascade is [4,2,2] (got [${calls.join(',')}])`);
}
// Every group (down to 1) sim-reverts → floor-1 surfaces an error, never silent.
{
  const { fn, calls } = recordingStore(() => true);
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(1), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(results.length === 0, 'floor-1 failure stores nothing');
  check(errors.length >= 1 && /Batch store failed/.test(errors[0]), 'floor-1 failure surfaces a "Batch store failed" error');
  check(calls.join(',') === '1', `floor-1 attempts exactly once (got [${calls.join(',')}])`);
}
// AA25 carrying -32500 → NOT halved; surfaces immediately (one store call).
{
  const calls: number[] = [];
  const fn = async (group: string[]) => {
    calls.push(group.length);
    throw new Error('RPC eth_sendUserOperation: AA25 invalid account nonce (code -32500)');
  };
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(4), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(results.length === 0, 'AA25 stores nothing');
  check(errors.length >= 1, 'AA25 error surfaced');
  check(calls.join(',') === '4', `AA25 not halved — exactly one attempt (got [${calls.join(',')}])`);
}
// "reverted during simulation" with NO -32500 code still halves.
{
  const calls: number[] = [];
  const fn = async (group: string[]) => {
    calls.push(group.length);
    if (group.length > 2) throw new Error('UserOperation reverted during simulation with reason: out of gas');
    return group.map((_, i) => `id-${i}`);
  };
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(4), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(errors.length === 0 && results.length === 2, 'no-code sim revert halves and stores all');
  check(calls.join(',') === '4,2,2', `no-code sim revert cascade is [4,2,2] (got [${calls.join(',')}])`);
}
// Duplicate rejection is swallowed (no ids, no error) — matches python.
{
  const calls: number[] = [];
  const fn = async (group: string[]) => {
    calls.push(group.length);
    throw new Error('409 duplicate content fingerprint');
  };
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(3), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(results.length === 0 && errors.length === 0, 'duplicate rejection swallowed (no ids, no error)');
}
// Successful multi-group: groups split by caps, each stored once, all ids returned in order.
{
  // 70 tiny items, count cap 30 → 3 groups [30,30,10]; all succeed.
  const { fn, calls } = recordingStore(() => false);
  const { results, errors } = await groupAndStoreAdaptive(tinyPayloads(70), fn, MAX_BATCH_GROUP_COUNT, MAX_BATCH_BYTES, () => 10);
  check(errors.length === 0, 'multi-group success: no errors');
  check(calls.join(',') === '30,30,10', `multi-group splits by count cap [30,30,10] (got [${calls.join(',')}])`);
  check(results.length === 3, 'three successful group results');
}

// ---------------------------------------------------------------------------
console.log(`\n# batch-sizing — ${passed} passed, ${failed} failed (of ${testNum})`);
if (failed > 0) process.exit(1);
