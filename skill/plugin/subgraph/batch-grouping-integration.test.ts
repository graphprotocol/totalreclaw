/**
 * batch-grouping-integration.test.ts — internal#449
 *
 * Drives the REAL submitFactBatchOnChain (mock WASM + fetch — no live network,
 * per the pin-unpin lesson: stub the bundler/RPC, never reach a real one) to
 * prove the batch-sizing module is wired into the submit path:
 *   (1) a batch larger than the count cap groups into multiple executeBatch
 *       UserOps (one per group), all stored;
 *   (2) a -32500 simulation revert on a multi-fact group triggers adaptive
 *       halving — the group splits and retries until every fact is stored.
 *
 * The halving LOGIC itself is pinned in batch-sizing.test.ts (stub storeFn,
 * no WASM); this file proves subgraph-store.ts orchestrates it against the real
 * submitFactBatchOnChainLocked (AA25 mutex + AA10 handling left intact).
 *
 * Run with: npx tsx batch-grouping-integration.test.ts   (TAP-style)
 */

import {
  __resetDeployedAccountsForTests,
  __resetRpcProbeCountForTests,
  __setWasmForTests,
  __clearWasmForTests,
  submitFactBatchOnChain,
} from './subgraph-store.js';

const core = await import('@totalreclaw/core');

let passed = 0;
let failed = 0;
function check(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) { console.log(`ok ${n} - ${name}`); passed++; }
  else { console.error(`not ok ${n} - ${name}`); failed++; process.exitCode = 1; }
}

interface Ctl { requests: Array<{ method: string; params: any[] }>; queue: Map<string, any[]>; _restore: () => void; }
function installFetchMock(): Ctl {
  const ctl: Ctl = { requests: [], queue: new Map(), _restore: () => {} };
  const original = globalThis.fetch;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const bodyStr = typeof init?.body === 'string' ? init.body : '';
    let parsed: any = {};
    try { parsed = JSON.parse(bodyStr); } catch { /* not JSON-RPC */ }
    if (parsed.method) ctl.requests.push({ method: parsed.method, params: parsed.params ?? [] });
    let result: any = null; let error: any = undefined;
    const q = ctl.queue.get(parsed.method);
    if (q && q.length > 0) { const staged = q.shift(); result = staged?.result ?? null; error = staged?.error; }
    return new Response(JSON.stringify({ jsonrpc: '2.0', id: parsed.id ?? 1, result, error }), { status: 200, headers: { 'Content-Type': 'application/json' } }) as any;
  }) as typeof globalThis.fetch;
  ctl._restore = () => { globalThis.fetch = original; };
  return ctl;
}
function stage(ctl: Ctl, method: string, result: any, error?: { message: string }): void {
  if (!ctl.queue.has(method)) ctl.queue.set(method, []);
  ctl.queue.get(method)!.push({ result, error });
}
function stageN(ctl: Ctl, method: string, result: any, n: number): void {
  for (let i = 0; i < n; i++) stage(ctl, method, result);
}

function makeMockWasm() {
  return {
    deriveEoa: () => ({ private_key: '0x' + 'a'.repeat(64), address: '0xbbbb' + 'b'.repeat(36) }),
    encodeSingleCall: (p: Uint8Array) => core.encodeSingleCall(p),
    encodeSingleCallTo: (p: Uint8Array, addr: string) => core.encodeSingleCallTo(p, addr),
    encodeBatchCall: (j: string) => core.encodeBatchCall(j),
    encodeBatchCallTo: (j: string, addr: string) => core.encodeBatchCallTo(j, addr),
    getEntryPointAddress: () => '0x5FF137D4b0FD9490299197e9fB6f7936EF32a416',
    getDataEdgeAddress: () => core.getDataEdgeAddress(),
    getSimpleAccountFactory: () => '0x9406Cc6185a346906296840746125A0E4493a848',
    // Delegate to the REAL installed core so the count cap tracks whatever the
    // installed @totalreclaw/core enforces (stale 2.5.3 → 15; ≥2.5.5 → 30).
    getMaxBatchSize: () => (core as any).getMaxBatchSize(),
    hashUserOp: () => '0x' + 'c'.repeat(64),
    signUserOp: () => 'd'.repeat(128),
  };
}

// One successful submit of a single group: deployed sender → no initCode/AA10.
function stageHappyGroup(ctl: Ctl): void {
  stage(ctl, 'eth_getCode', '0x1234');
  stage(ctl, 'pimlico_getUserOperationGasPrice', { fast: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' } });
  stage(ctl, 'eth_call', '0x0');
  stage(ctl, 'eth_estimateUserOperationGas', { callGasLimit: '0x10000', verificationGasLimit: '0x10000', preVerificationGas: '0x10000' });
  stage(ctl, 'pm_sponsorUserOperation', { callGasLimit: '0x10000', verificationGasLimit: '0x10000', preVerificationGas: '0x10000', paymaster: '0x' + 'f'.repeat(40), paymasterData: '0x' });
  stage(ctl, 'eth_sendUserOperation', '0x' + 'e'.repeat(64));
  stage(ctl, 'eth_getUserOperationReceipt', { success: true, receipt: { transactionHash: '0xabc' } });
}

const baseConfig = {
  relayUrl: 'http://dummy-relay/v1',
  mnemonic: 'test test test test test test test test test test test junk',
  walletAddress: '0xaaaa' + 'a'.repeat(36),
  chainId: 100,
  entryPointAddress: '0x5FF137D4b0FD9490299197e9fB6f7936EF32a416',
  cachePath: '',
  dataEdgeAddress: '0x' + 'e'.repeat(40),
};

// ---------------------------------------------------------------------------
// (1) count-cap grouping: 31 payloads > the installed core's count cap →
//     ceil(31/cap) executeBatch UserOps, all stored. Cap is read at runtime
//     from getMaxBatchSize (stale core=15 → 3 groups; ≥2.5.5 core=30 → 2 groups).
// ---------------------------------------------------------------------------
{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());
  const cap = (core as any).getMaxBatchSize();
  const expectedGroups = Math.ceil(31 / cap);
  for (let g = 0; g < expectedGroups; g++) stageHappyGroup(ctl);

  try {
    const result = await submitFactBatchOnChain(
      Array.from({ length: 31 }, () => Buffer.from([1])),
      baseConfig,
    );
    const sends = ctl.requests.filter(r => r.method === 'eth_sendUserOperation');
    check(result.success === true, 'count-cap: all groups succeed');
    check(result.batchSize === 31, `count-cap: batchSize totals all 31 payloads (got ${result.batchSize})`);
    check(sends.length === expectedGroups, `count-cap: 31 payloads split into ${expectedGroups} UserOps at cap=${cap} (got ${sends.length} sends)`);
    check((result.groupResults ?? []).length === expectedGroups, `count-cap: groupResults has ${expectedGroups} entries (got ${(result.groupResults ?? []).length})`);
  } catch (err: any) {
    check(false, `count-cap: submit threw: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// ---------------------------------------------------------------------------
// (2) halve-on-simfail: a 4-payload group sim-reverts at sponsorship → halves
//     to 2+2, both succeed → all stored, no error.
// ---------------------------------------------------------------------------
{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());

  // Attempt 1 (group of 4): everything up to sponsorship, then -32500 sim revert.
  stage(ctl, 'eth_getCode', '0x1234');
  stage(ctl, 'pimlico_getUserOperationGasPrice', { fast: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' } });
  stage(ctl, 'eth_call', '0x0');
  stage(ctl, 'eth_estimateUserOperationGas', { callGasLimit: '0x10000', verificationGasLimit: '0x10000', preVerificationGas: '0x10000' });
  stage(ctl, 'pm_sponsorUserOperation', null, { message: '-32500 Sender does not implement validateUserOp or factory is not deployed (reverted during simulation)' });
  // Attempts 2 & 3 (halved groups of 2): both succeed.
  stageHappyGroup(ctl);
  stageHappyGroup(ctl);

  try {
    const result = await submitFactBatchOnChain(
      Array.from({ length: 4 }, () => Buffer.from([1])),
      baseConfig,
    );
    const sends = ctl.requests.filter(r => r.method === 'eth_sendUserOperation');
    const sponsors = ctl.requests.filter(r => r.method === 'pm_sponsorUserOperation');
    check(result.success === true, 'halve: sim-reverted group halved and stored all (success=true)');
    check((result.errors ?? []).length === 0, `halve: no errors surfaced (got ${(result.errors ?? []).length})`);
    check(sends.length === 2, `halve: 2 successful sends after splitting 4→2+2 (got ${sends.length})`);
    check(sponsors.length === 3, `halve: 3 sponsor attempts (1 revert + 2 success) (got ${sponsors.length})`);
  } catch (err: any) {
    check(false, `halve: submit threw instead of halving: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// ---------------------------------------------------------------------------
// (3) single payload still uses the unchanged fast-path (1 send, no grouping)
// ---------------------------------------------------------------------------
{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());
  stageHappyGroup(ctl);
  try {
    const result = await submitFactBatchOnChain([Buffer.from([1])], baseConfig);
    const sends = ctl.requests.filter(r => r.method === 'eth_sendUserOperation');
    check(result.success === true, 'single: fast-path unchanged, succeeds');
    check(sends.length === 1, `single: exactly 1 UserOp (no grouping overhead) (got ${sends.length})`);
  } catch (err: any) {
    check(false, `single: threw: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// silence unused warning for stageN if the compiler keeps strict noUnusedLocals off
void stageN;

console.log(`\n# batch-grouping-integration — ${passed} passed, ${failed} failed (of ${passed + failed})`);
if (failed > 0) process.exit(1);
