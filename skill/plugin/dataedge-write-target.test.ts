/**
 * dataedge-write-target.test.ts — #460 (completes #462)
 *
 * #462 threaded the relay's authoritative `data_edge_address` into
 * `getSubgraphConfig().dataEdgeAddress`, but the submit paths still called the
 * legacy address-less WASM encoders (`encodeSingleCall` / `encodeBatchCall`),
 * which bake the PRODUCTION DataEdge (0xC445af1D…) into the calldata. So the
 * resolved address was computed and discarded — writes still mined on prod even
 * against the staging relay (rc.22 QA NO-GO, internal PR #453).
 *
 * This test drives the REAL submitFactOnChain / submitFactBatchOnChain with a
 * config whose `dataEdgeAddress` is the staging DataEdge, mocks the network,
 * and asserts the produced UserOp `callData` targets the STAGING address and
 * NOT the prod default. The mock WASM delegates the four calldata encoders to
 * the REAL @totalreclaw/core so the assertion checks the actual ABI-encoded
 * bytes (the address is the right-padded `execute(dataEdge, …)` target).
 *
 * Run with: npx tsx dataedge-write-target.test.ts
 * TAP-style output, no jest dependency.
 */

import {
  __resetDeployedAccountsForTests,
  __resetRpcProbeCountForTests,
  __setWasmForTests,
  __clearWasmForTests,
  submitFactOnChain,
  submitFactBatchOnChain,
} from './subgraph-store.js';

// Real core — the mock WASM below delegates the calldata encoders to it so the
// assertions inspect the genuine ABI-encoded target address, not a stub.
const core = await import('@totalreclaw/core');

const STAGING_DATA_EDGE = '0xE7a4D2677B686e13775Ba9092631089e35F0BB91';
// Address bytes as they appear in the calldata: lowercased, no 0x prefix.
const STAGING_BYTES = 'e7a4d2677b686e13775ba9092631089e35f0bb91';
const PROD_BYTES = 'c445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca';

let passed = 0;
let failed = 0;

function check(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.error(`not ok ${n} - ${name}`);
    failed++;
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
// Fetch mock — records every JSON-RPC request + returns staged responses.
// ---------------------------------------------------------------------------

interface Ctl {
  requests: Array<{ method: string; params: any[] }>;
  queue: Map<string, any[]>;
  _restore: () => void;
}

function installFetchMock(): Ctl {
  const ctl: Ctl = { requests: [], queue: new Map(), _restore: () => {} };
  const original = globalThis.fetch;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const bodyStr = typeof init?.body === 'string' ? init.body : '';
    let parsed: any = {};
    try { parsed = JSON.parse(bodyStr); } catch { /* not JSON-RPC */ }
    if (parsed.method) ctl.requests.push({ method: parsed.method, params: parsed.params ?? [] });
    let result: any = null;
    let error: any = undefined;
    const q = ctl.queue.get(parsed.method);
    if (q && q.length > 0) {
      const staged = q.shift();
      result = staged?.result ?? null;
      error = staged?.error;
    }
    const responseBody = JSON.stringify({ jsonrpc: '2.0', id: parsed.id ?? 1, result, error });
    return new Response(responseBody, { status: 200, headers: { 'Content-Type': 'application/json' } }) as any;
  }) as typeof globalThis.fetch;
  ctl._restore = () => { globalThis.fetch = original; };
  return ctl;
}

function stage(ctl: Ctl, method: string, result: any, error?: { message: string }): void {
  if (!ctl.queue.has(method)) ctl.queue.set(method, []);
  ctl.queue.get(method)!.push({ result, error });
}

// Mock WASM: real calldata encoders (so the target address is genuine), stub
// everything else the submit path touches. Both legacy + `*To` encoders are
// wired to real core so the assertion is meaningful whichever the code calls.
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
    hashUserOp: () => '0x' + 'c'.repeat(64),
    signUserOp: () => 'd'.repeat(128),
  };
}

// Stage a happy submit path (sender already deployed → no initCode).
function stageHappyPath(ctl: Ctl): void {
  stage(ctl, 'eth_getCode', '0x1234'); // deployed → no initCode / no AA10
  stage(ctl, 'pimlico_getUserOperationGasPrice', { fast: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' } });
  stage(ctl, 'eth_call', '0x0'); // nonce
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
  dataEdgeAddress: STAGING_DATA_EDGE,
};

// Pull the callData out of the last UserOp the bundler saw (sponsor + send both
// carry it — assert on both to be safe).
function callDataOf(ctl: Ctl, method: string): string {
  const reqs = ctl.requests.filter(r => r.method === method);
  const userOp = reqs[reqs.length - 1]?.params?.[0];
  const cd = userOp?.callData ?? '';
  return typeof cd === 'string' ? cd.toLowerCase() : '';
}

// ---------------------------------------------------------------------------
// Single-fact path
// ---------------------------------------------------------------------------

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());
  stageHappyPath(ctl);

  try {
    const result = await submitFactOnChain(Buffer.from([1, 2, 3]), baseConfig);
    check(result.success === true, 'single: submit succeeds on happy path');

    const sponsorCd = callDataOf(ctl, 'pm_sponsorUserOperation');
    const sendCd = callDataOf(ctl, 'eth_sendUserOperation');
    check(sponsorCd.length > 0, 'single: pm_sponsorUserOperation carried a callData');
    check(
      sponsorCd.includes(STAGING_BYTES),
      'single: sponsor callData targets the STAGING DataEdge (resolved address reached the calldata)',
    );
    check(
      !sponsorCd.includes(PROD_BYTES),
      'single: sponsor callData does NOT target the prod DataEdge (legacy encoder abandoned)',
    );
    check(
      sendCd.includes(STAGING_BYTES) && !sendCd.includes(PROD_BYTES),
      'single: eth_sendUserOperation callData targets STAGING, not prod',
    );
  } catch (err: any) {
    check(false, `single: submit threw: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// ---------------------------------------------------------------------------
// Batch path
// ---------------------------------------------------------------------------

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());
  stageHappyPath(ctl);

  try {
    const result = await submitFactBatchOnChain([Buffer.from([1]), Buffer.from([2])], baseConfig);
    check(result.success === true, 'batch: submit succeeds on happy path');

    const sponsorCd = callDataOf(ctl, 'pm_sponsorUserOperation');
    const sendCd = callDataOf(ctl, 'eth_sendUserOperation');
    check(sponsorCd.length > 0, 'batch: pm_sponsorUserOperation carried a callData');
    check(
      sponsorCd.includes(STAGING_BYTES),
      'batch: sponsor callData targets the STAGING DataEdge (resolved address reached the calldata)',
    );
    check(
      !sponsorCd.includes(PROD_BYTES),
      'batch: sponsor callData does NOT target the prod DataEdge (legacy encoder abandoned)',
    );
    check(
      sendCd.includes(STAGING_BYTES) && !sendCd.includes(PROD_BYTES),
      'batch: eth_sendUserOperation callData targets STAGING, not prod',
    );
  } catch (err: any) {
    check(false, `batch: submit threw: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// ---------------------------------------------------------------------------
// Guard: empty dataEdgeAddress falls back to the legacy encoder (prod default),
// behavior-identical to pre-fix — must NOT throw on an empty address.
// ---------------------------------------------------------------------------

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  __setWasmForTests(makeMockWasm());
  stageHappyPath(ctl);

  try {
    const result = await submitFactOnChain(Buffer.from([1, 2, 3]), { ...baseConfig, dataEdgeAddress: '' });
    check(result.success === true, 'guard: empty dataEdgeAddress still submits (legacy encoder fallback)');
    const sponsorCd = callDataOf(ctl, 'pm_sponsorUserOperation');
    check(
      sponsorCd.includes(PROD_BYTES),
      'guard: empty dataEdgeAddress falls back to legacy encoder (prod default in calldata)',
    );
  } catch (err: any) {
    check(false, `guard: empty dataEdgeAddress must not throw, got: ${err?.message}`);
  } finally {
    ctl._restore();
    __clearWasmForTests();
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('\nSOME TESTS FAILED');
  process.exit(1);
}
console.log('\nALL TESTS PASSED');
