// scanner-sim: allow
/**
 * Tests for the initCode lifecycle (AA10 "sender already constructed" fix).
 *
 * Root cause this test locks in: after the first successful on-chain
 * submission deploys the counterfactual Smart Account via initCode, every
 * subsequent submission for the SAME sender MUST omit `factory` /
 * `factoryData`. If initCode is present on a UserOp whose sender is
 * already constructed, the EntryPoint rejects with AA10.
 *
 * The previous implementation cached "deployed" in a module-level Set.
 * That cache was the only thing preventing AA10 across submissions within
 * a session, and it was fragile:
 *   - A process restart emptied the cache → the next submission relied on
 *     a single eth_getCode that could return stale `0x` from a lagging
 *     RPC node → initCode was re-added → AA10.
 *   - A receipt poll that timed out (success undetected) left the cache
 *     empty → same staleness path.
 *
 * The fix: `getInitCode` re-checks `eth_getCode` on EVERY call — no
 * session cache. This test exercises the resulting contract by mocking
 * the RPC layer (`rpcRequest` via the relay module's fetch site) and
 * driving the real `getInitCode` through both states (undeployed →
 * deployed).
 *
 * The test also asserts the cache reset helper is a no-op-safe seam and
 * that repeated calls all hit the RPC layer (proving no caching).
 *
 * Run with: npx tsx initcode-lifecycle.test.ts
 */

import {
  __resetDeployedAccountsForTests,
  __getInitCodeForTests,
  __getRpcProbeCountForTests,
  __resetRpcProbeCountForTests,
} from './subgraph-store.js';

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
// RPC probe instrumentation
// ---------------------------------------------------------------------------
//
// The production `getInitCode` calls `rpcRequest({ method: 'eth_getCode', ... })`
// on every invocation. To observe (and control) those calls without pulling
// in the live network, we monkey-patch global fetch. The probe counter is
// exported test-only from subgraph-store.ts so we can assert how many RPC
// reads happened across a sequence of getInitCode calls.
//
// `rpcRequest` POSTs a JSON-RPC envelope and reads `res.json()`. We decode
// the request body to find the method + params, then return the canned
// `eth_getCode` result the caller configured.

interface FetchController {
  codeResponses: string[];   // queued eth_getCode results (consumed in order)
  requests: Array<{ method: string; params: unknown[] }>;
}

let controller: FetchController | null = null;

function installFetchMock(): FetchController {
  const ctl: FetchController = { codeResponses: [], requests: [] };
  controller = ctl;
  const original = globalThis.fetch;

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const bodyStr = typeof init?.body === 'string' ? init.body : '';
    let parsed: any = {};
    try { parsed = JSON.parse(bodyStr); } catch { /* not JSON-RPC */ }
    if (parsed.method) {
      ctl.requests.push({ method: parsed.method, params: parsed.params ?? [] });
    }
    // Look for an eth_getCode call and drain the next queued response.
    if (parsed.method === 'eth_getCode') {
      const code = ctl.codeResponses.shift() ?? '0x';
      const responseBody = JSON.stringify({ jsonrpc: '2.0', id: parsed.id ?? 1, result: code });
      return new Response(responseBody, {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }) as any;
    }
    // Default: empty success envelope for any other method.
    const responseBody = JSON.stringify({ jsonrpc: '2.0', id: parsed.id ?? 1, result: null });
    return new Response(responseBody, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }) as any;
  }) as typeof globalThis.fetch;

  // Attach a restore for teardown.
  (ctl as any)._restore = () => { globalThis.fetch = original; };
  return ctl;
}

function restoreFetch(ctl: FetchController): void {
  (ctl as any)._restore?.();
  controller = null;
}

// ---------------------------------------------------------------------------
// Scenario 1: undeployed → deployed transition omits initCode on the 2nd call
// ---------------------------------------------------------------------------
//
// First call  : eth_getCode → '0x'      → factory + factoryData returned.
// Second call : eth_getCode → '0x1234'  → factory + factoryData are NULL.
// This is the exact AA10 regression: if the second call still returned
// initCode, the entrypoint would reject with "sender already constructed".

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  // Queue two eth_getCode responses: first undeployed, second deployed.
  ctl.codeResponses.push('0x');            // 1st call: SA not yet on-chain
  ctl.codeResponses.push('0x1234abcd');    // 2nd call: SA constructed

  const sender = '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const eoa    = '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
  const rpcUrl = 'http://localhost:0/rpc';

  const first = await __getInitCodeForTests(sender, eoa, rpcUrl);
  check(
    first.factory !== null && first.factoryData !== null,
    '1st call (undeployed): returns factory + factoryData (initCode present)',
  );
  check(
    typeof first.factory === 'string' && first.factory.startsWith('0x'),
    '1st call: factory is a hex string',
  );
  check(
    typeof first.factoryData === 'string' && first.factoryData.startsWith('0x'),
    '1st call: factoryData is a hex string',
  );

  const second = await __getInitCodeForTests(sender, eoa, rpcUrl);
  check(
    second.factory === null && second.factoryData === null,
    '2nd call (deployed): returns null factory + factoryData (no initCode → no AA10)',
  );

  restoreFetch(ctl);
}

// ---------------------------------------------------------------------------
// Scenario 2: getInitCode re-checks eth_getCode on EVERY call (no caching)
// ---------------------------------------------------------------------------
//
// The session-cache optimization used to skip the RPC after a "deployed"
// result. That meant a process restart (empty cache) fell back to a single
// stale eth_getCode. After the fix, every call MUST hit eth_getCode — we
// assert this via the RPC probe counter.

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  // All deployed.
  ctl.codeResponses.push('0xdeadbeef', '0xdeadbeef', '0xdeadbeef');

  const sender = '0xcccccccccccccccccccccccccccccccccccccccc';
  const eoa    = '0xdddddddddddddddddddddddddddddddddddddddd';
  const rpcUrl = 'http://localhost:0/rpc';

  const probesBefore = __getRpcProbeCountForTests();
  check(probesBefore === 0, `probe counter starts at 0 (got ${probesBefore})`);

  await __getInitCodeForTests(sender, eoa, rpcUrl);
  await __getInitCodeForTests(sender, eoa, rpcUrl);
  await __getInitCodeForTests(sender, eoa, rpcUrl);

  const probesAfter = __getRpcProbeCountForTests();
  check(
    probesAfter === 3,
    `3 getInitCode calls → 3 eth_getCode RPC reads (no caching) (got ${probesAfter})`,
  );

  restoreFetch(ctl);
}

// ---------------------------------------------------------------------------
// Scenario 3: a stale-then-fresh sequence converges to "no initCode"
// ---------------------------------------------------------------------------
//
// Mirrors the field failure mode: receipt poll times out on the deploying
// UserOp, so the cache never gets marked. The next submission calls
// eth_getCode, which on a lagging node may STILL return '0x' once, then
// '0x<code>' once the node catches up. With the cache removed, the
// per-submission re-check self-heals as soon as the node reflects the
// deployment. This test asserts the second call returns no initCode even
// when the first call (post-deployment) saw stale empty code.

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  ctl.codeResponses.push('0x');          // stale: node hasn't seen the deploy tx
  ctl.codeResponses.push('0xcafe');      // fresh: node caught up

  const sender = '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
  const eoa    = '0xffffffffffffffffffffffffffffffffffffffff';
  const rpcUrl = 'http://localhost:0/rpc';

  const stale = await __getInitCodeForTests(sender, eoa, rpcUrl);
  // Stale read legitimately returns initCode (the plugin can't know better).
  check(
    stale.factory !== null,
    'stale eth_getCode=0x still returns initCode (plugin cannot detect staleness)',
  );

  const fresh = await __getInitCodeForTests(sender, eoa, rpcUrl);
  check(
    fresh.factory === null && fresh.factoryData === null,
    'fresh eth_getCode=0xcafe → no initCode (self-heals once node catches up)',
  );

  restoreFetch(ctl);
}

// ---------------------------------------------------------------------------
// Scenario 4: factory address is the canonical SimpleAccountFactory
// ---------------------------------------------------------------------------
//
// When initCode IS returned, the factory field must be the address the
// EntryPoint expects (from @totalreclaw/core getSimpleAccountFactory).
// We assert it is a 20-byte hex address — not the EOA, not the sender.

{
  __resetDeployedAccountsForTests();
  __resetRpcProbeCountForTests();
  const ctl = installFetchMock();
  ctl.codeResponses.push('0x');

  const sender = '0x1111111111111111111111111111111111111111';
  const eoa    = '0x2222222222222222222222222222222222222222';
  const rpcUrl = 'http://localhost:0/rpc';

  const { factory, factoryData } = await __getInitCodeForTests(sender, eoa, rpcUrl);

  check(factory !== null && factory!.length === 42, 'factory is a 0x + 20-byte address');
  check(
    factory!.toLowerCase() !== sender.toLowerCase() &&
      factory!.toLowerCase() !== eoa.toLowerCase(),
    'factory is neither the sender nor the EOA',
  );
  check(
    factoryData !== null && factoryData!.length > 10,
    'factoryData is non-empty calldata',
  );
  // createAccount(address,uint256) selector is 0x5fbfb9cf — first 10 chars.
  check(
    factoryData!.toLowerCase().startsWith('0x5fbfb9cf'),
    'factoryData encodes createAccount(address,uint256) selector',
  );

  restoreFetch(ctl);
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
