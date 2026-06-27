/**
 * Regression test for the AA24 signature error on counterfactual Smart
 * Accounts (the AA24 ship-stopper, 2026-06-27).
 *
 * When a user pairs for the first time the derived Smart Account is
 * counterfactual — NOT yet deployed on-chain. The first UserOp MUST
 * include `factory` + `factoryData` (ERC-4337 v0.7 initCode) so the
 * EntryPoint deploys the SA and validates the signature in one tx. If
 * the submit path omits initCode for an undeployed SA, the EntryPoint
 * cannot validate → AA24 signature error → 0 facts stored.
 *
 * Additionally, the SA owner's signature must be computed AFTER
 * sponsorship (paymaster fields added) and AFTER initCode is on the
 * UserOp — the hash covers factory + paymasterAndData. Signing before
 * either is added produces a signature over a different hash → AA24.
 *
 * This test exercises `submitFactOnChain` end-to-end with:
 *   - the REAL `@totalreclaw/core` WASM (address derivation, calldata
 *     encoding, UserOp hashing, ECDSA signing), and
 *   - a mocked `globalThis.fetch` that serves the JSON-RPC responses
 *     the relay/bundler would (eth_getCode → "0x" for an undeployed SA,
 *     gas prices, nonce, sponsorship, sendUserOp, receipt).
 *
 * It captures the UserOp handed to `eth_sendUserOperation` and asserts:
 *   1. `factory` + `factoryData` are present (initCode included).
 *   2. `factoryData` is `createAccount(owner, salt=0)` for the EOA owner.
 *   3. The signature validates against the owner key over the WASM
 *      `hashUserOp` of the FINAL UserOp (i.e. the signature covers the
 *      initCode + paymaster fields).
 *
 * Run with: npx tsx counterfactual-initcode.test.ts
 */

import { strict as assert } from 'node:assert';
import { createRequire } from 'node:module';
import { submitFactOnChain, type SubgraphStoreConfig } from './subgraph-store.js';
import { signUserOp } from './vault-crypto.js';

const requireWasm = createRequire(import.meta.url);
const wasm = requireWasm('@totalreclaw/core') as typeof import('@totalreclaw/core');

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
// Fixture: deterministic EOA + counterfactual SA from a test mnemonic.
// ---------------------------------------------------------------------------

// Well-known test mnemonic (NOT a real funds-bearing phrase — safe in tests).
const TEST_MNEMONIC = 'test test test test test test test test test test test junk';
const eoa = wasm.deriveEoa(TEST_MNEMONIC) as { private_key: string; address: string };
const EOA_ADDRESS = eoa.address.toLowerCase();
// A counterfactual SA address: valid-looking, 20 bytes, but NOT deployed.
const COUNTERFACTUAL_SA = '0x8cd80814f1328571c22b27fad90f558b6d28685d';

const ENTRY_POINT = wasm.getEntryPointAddress();
const CHAIN_ID = 100; // Gnosis mainnet (matches the ship-stopper repro).
const FACTORY = wasm.getSimpleAccountFactory();

// ---------------------------------------------------------------------------
// Mock fetch — dispatches JSON-RPC by `method`.
// ---------------------------------------------------------------------------

const realFetch = (globalThis as { fetch?: typeof fetch }).fetch;

interface CapturedUserOp {
  sender: string;
  factory?: string;
  factoryData?: string;
  paymaster?: string | null;
  paymasterData?: string | null;
  callData: string;
  nonce: string;
  signature: string;
  [k: string]: unknown;
}

let capturedSendUserOp: CapturedUserOp | null = null;
let getCodeCalls = 0;
let sponsorCalls = 0;

function rpcResponse(result: unknown) {
  return { jsonrpc: '2.0', id: 1, result };
}

async function mockFetch(input: string | URL | Request, init?: RequestInit): Promise<Response> {
  const body = init?.body ? JSON.parse(init.body as string) : null;
  const method: string = body?.method ?? '';
  const url = String(input);

  // Chain-RPC methods (eth_getCode, eth_call for nonce) hit the public RPC URL;
  // bundler methods (pimlico_*, pm_*, eth_sendUserOperation,
  // eth_getUserOperationReceipt) hit the relay /v1/bundler URL. We dispatch
  // purely by JSON-RPC method name so the test is URL-agnostic.

  // eth_getCode → empty (account NOT deployed → counterfactual).
  if (method === 'eth_getCode') {
    getCodeCalls++;
    return json(rpcResponse('0x'));
  }

  // pimlico_getUserOperationGasPrice
  if (method === 'pimlico_getUserOperationGasPrice') {
    return json(rpcResponse({
      slow: { maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1' },
      standard: { maxFeePerGas: '0x2', maxPriorityFeePerGas: '0x1' },
      fast: { maxFeePerGas: '0x3', maxPriorityFeePerGas: '0x1' },
    }));
  }

  // eth_call → EntryPoint.getNonce → 0 (first ever op for this sender).
  if (method === 'eth_call') {
    return json(rpcResponse('0x0'));
  }

  // pm_sponsorUserOperation → fill gas limits + paymaster fields.
  // Mirrors the documented Pimlico v0.7 response shape: gas + paymaster
  // fields only — factory/factoryData are NOT echoed back (so Object.assign
  // must not clobber them). We deliberately omit factory from the response
  // to assert the plugin preserves the caller-provided initCode.
  if (method === 'pm_sponsorUserOperation') {
    sponsorCalls++;
    return json(rpcResponse({
      preVerificationGas: '0xc8',
      verificationGasLimit: '0x186a0',
      callGasLimit: '0x61a8',
      paymaster: '0x0000000000000000000000000000000000000001',
      paymasterVerificationGasLimit: '0x4e20',
      paymasterPostOpGasLimit: '0x0',
      paymasterData: '0x' + 'ab'.repeat(8),
    }));
  }

  // eth_sendUserOperation → capture the submitted UserOp + return a hash.
  if (method === 'eth_sendUserOperation') {
    const op = body.params[0] as CapturedUserOp;
    capturedSendUserOp = op;
    return json(rpcResponse('0x' + '11'.repeat(32)));
  }

  // eth_getUserOperationReceipt → mined + success on first poll.
  if (method === 'eth_getUserOperationReceipt') {
    return json(rpcResponse({
      success: true,
      receipt: { transactionHash: '0x' + '22'.repeat(32) },
    }));
  }

  // Unknown method on an unexpected URL — fail loud so the test can't
  // silently pass by taking a different code path.
  throw new Error(`counterfactual-initcode.test: unexpected fetch ${method} → ${url}`);
}

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

// ---------------------------------------------------------------------------
// Run the submission against the counterfactual SA.
// ---------------------------------------------------------------------------

const config: SubgraphStoreConfig = {
  relayUrl: 'https://relay.example',
  mnemonic: TEST_MNEMONIC,
  cachePath: '/tmp/unused',
  chainId: CHAIN_ID,
  dataEdgeAddress: '0x' + '0'.repeat(40),
  entryPointAddress: ENTRY_POINT,
  // Pin the sender so the submit path does NOT call deriveSmartAccountAddress
  // (which would make an extra eth_call). We want the test focused on the
  // initCode + sign path.
  walletAddress: COUNTERFACTUAL_SA,
};

try {
  (globalThis as { fetch: typeof fetch }).fetch = mockFetch as typeof fetch;

  const protobuf = Buffer.from('deadbeef', 'hex');
  const result = await submitFactOnChain(protobuf, config);

  // Sanity: the submission succeeded through the mocked happy path.
  check(result.success === true, 'submission succeeded (mocked receipt success=true)');
  check(getCodeCalls >= 1, 'getInitCode issued an eth_getCode to check deployment');

  const op = capturedSendUserOp;
  check(op !== null, 'eth_sendUserOperation was called with the signed UserOp');
  if (!op) throw new Error('no UserOp captured — aborting remaining assertions');

  // ---- Assertion 1: initCode (factory + factoryData) IS included. ----
  check(
    typeof op.factory === 'string' && op.factory.toLowerCase() === FACTORY.toLowerCase(),
    `factory present on the submitted UserOp (${op.factory})`,
  );
  check(typeof op.factoryData === 'string' && op.factoryData.length > 2,
    `factoryData present on the submitted UserOp (${op.factoryData?.slice(0, 10)}…)`);

  // ---- Assertion 2: factoryData is createAccount(owner=EOA, salt=0). ----
  // Selector 0x5fbfb9cf = keccak256("createAccount(address,uint256)")[0:4].
  // The single ABI-encoded owner word must equal the EOA address, and the
  // salt word must be zero. This is what the SA factory deploys.
  const fd = op.factoryData!;
  check(/^0x5fbfb9cf/i.test(fd), 'factoryData selector = createAccount(address,uint256)');
  const ownerWord = fd.slice(2 + 8, 2 + 8 + 64);
  const saltWord = fd.slice(2 + 8 + 64, 2 + 8 + 64 + 64);
  check(
    ownerWord.toLowerCase() === EOA_ADDRESS.slice(2).padStart(64, '0'),
    `factoryData owner word = EOA ${EOA_ADDRESS}`,
  );
  check(saltWord === '0'.repeat(64), 'factoryData salt = 0');

  // ---- Assertion 3: the signature covers initCode + paymaster. ----
  // Recompute the WASM hash over the FINAL UserOp (factory + paymaster +
  // sponsored gas limits all present) and ecrecover — MUST recover the
  // EOA address. If the plugin signed a DIFFERENT object (e.g. before
  // sponsorship, or before adding factory), this check fails with a
  // recovered-address mismatch — the exact signature-invalid condition
  // the EntryPoint surfaces as AA24.
  const hashHex = wasm.hashUserOp(JSON.stringify(op), ENTRY_POINT, BigInt(CHAIN_ID));
  const recovered = recoverEoaFromSig(hashHex, op.signature);
  check(
    recovered !== null && recovered.toLowerCase() === EOA_ADDRESS,
    `signature validates against the owner (recovered ${recovered} vs EOA ${EOA_ADDRESS})`,
  );

  // Negative control: signing the hash of the SAME op WITHOUT factory must
  // NOT recover the owner — proves the factory field is load-bearing in the
  // hashed bytes (and therefore that omitting it would produce AA24).
  const opNoFactory: CapturedUserOp = { ...op };
  delete opNoFactory.factory;
  delete opNoFactory.factoryData;
  const hashNoFactory = wasm.hashUserOp(JSON.stringify(opNoFactory), ENTRY_POINT, BigInt(CHAIN_ID));
  const recoveredNoFactory = recoverEoaFromSig(hashNoFactory, op.signature);
  check(
    recoveredNoFactory === null || recoveredNoFactory.toLowerCase() !== EOA_ADDRESS,
    'negative control: signature does NOT validate when factory is stripped (initCode is load-bearing)',
  );

  // ---- Assertion 4: sponsor-then-sign ordering. ----
  // The signature must validate against a hash that INCLUDES the paymaster
  // fields the sponsor returned. If the plugin signed BEFORE sponsorship,
  // the paymaster fields would be absent from the hashed bytes and the
  // recovered address would not match. The positive recovery assertion
  // above already proves this; assert sponsorship actually ran too.
  check(sponsorCalls === 1, 'pm_sponsorUserOperation ran exactly once before signing');
  check(
    typeof op.paymaster === 'string' && op.paymaster !== '0x' && op.paymaster.length >= 42,
    `paymaster field populated by sponsorship (${op.paymaster})`,
  );
} finally {
  (globalThis as { fetch?: typeof fetch }).fetch = realFetch;
}

// ---------------------------------------------------------------------------
// Scenario 2: a sponsor response that CLOBBERS factory/factoryData must be
// restored before signing (the defensive guard's whole purpose). Without
// the guard, the sponsored UserOp would ship WITHOUT initCode → AA24.
// ---------------------------------------------------------------------------

{
  // Reset capture state for this scenario.
  capturedSendUserOp = null;
  getCodeCalls = 0;
  sponsorCalls = 0;

  // Use a DIFFERENT counterfactual sender than scenario 1 — the production
  // path memoizes successful deployments in a session-level Set
  // (`deployedAccounts`), so reusing the same address would short-circuit
  // getInitCode and skip the initCode branch entirely. A fresh address
  // forces the undeployed path to run again.
  const clobberConfig: SubgraphStoreConfig = { ...config, walletAddress: '0x1111111111111111111111111111111111111111' };

  // A sponsor response that maliciously/buggily echoes back null factory
  // fields — simulating a misbehaving relay proxy or a future paymaster
  // change that drops them.
  async function clobberingFetch(input: string | URL | Request, init?: RequestInit): Promise<Response> {
    const body = init?.body ? JSON.parse(init.body as string) : null;
    const method: string = body?.method ?? '';

    if (method === 'eth_getCode') {
      getCodeCalls++;
      return json(rpcResponse('0x')); // undeployed
    }
    if (method === 'pimlico_getUserOperationGasPrice') {
      return json(rpcResponse({
        fast: { maxFeePerGas: '0x3', maxPriorityFeePerGas: '0x1' },
      }));
    }
    if (method === 'eth_call') {
      return json(rpcResponse('0x0'));
    }
    if (method === 'pm_sponsorUserOperation') {
      sponsorCalls++;
      // BUGGY response: returns factory=null + factoryData=null alongside
      // the gas + paymaster fields. Object.assign would clobber the
      // caller-provided initCode with these nulls if the guard didn't
      // restore it.
      return json(rpcResponse({
        preVerificationGas: '0xc8',
        verificationGasLimit: '0x186a0',
        callGasLimit: '0x61a8',
        factory: null,
        factoryData: null,
        paymaster: '0x0000000000000000000000000000000000000001',
        paymasterVerificationGasLimit: '0x4e20',
        paymasterPostOpGasLimit: '0x0',
        paymasterData: '0x' + 'cd'.repeat(8),
      }));
    }
    if (method === 'eth_sendUserOperation') {
      capturedSendUserOp = body.params[0] as CapturedUserOp;
      return json(rpcResponse('0x' + '33'.repeat(32)));
    }
    if (method === 'eth_getUserOperationReceipt') {
      return json(rpcResponse({
        success: true,
        receipt: { transactionHash: '0x' + '44'.repeat(32) },
      }));
    }
    throw new Error(`counterfactual-initcode.test (clobber): unexpected ${method} → ${String(input)}`);
  }

  (globalThis as { fetch: typeof fetch }).fetch = clobberingFetch as typeof fetch;
  try {
    const result = await submitFactOnChain(Buffer.from('cafe', 'hex'), clobberConfig);
    check(result.success === true, 'clobber-scenario: submission succeeded with guard active');

    const guarded = capturedSendUserOp;
    check(guarded !== null, 'clobber-scenario: eth_sendUserOperation captured the UserOp');
    if (!guarded) throw new Error('no UserOp captured in clobber scenario');

    // The guard MUST have restored factory/factoryData despite the sponsor
    // response returning null for both.
    check(
      typeof guarded.factory === 'string' && guarded.factory.toLowerCase() === FACTORY.toLowerCase(),
      `clobber-scenario: guard restored factory after sponsor clobber (${guarded.factory})`,
    );
    check(
      typeof guarded.factoryData === 'string' && /^0x5fbfb9cf/i.test(guarded.factoryData),
      'clobber-scenario: guard restored factoryData (createAccount selector)',
    );

    // And the signature MUST still validate against the owner over the
    // final hash (proving the restore happened BEFORE signing, not after).
    const hashHex = wasm.hashUserOp(JSON.stringify(guarded), ENTRY_POINT, BigInt(CHAIN_ID));
    const recovered = recoverEoaFromSig(hashHex, guarded.signature);
    check(
      recovered !== null && recovered.toLowerCase() === EOA_ADDRESS,
      'clobber-scenario: signature still validates after guard restore',
    );
  } finally {
    (globalThis as { fetch?: typeof fetch }).fetch = realFetch;
  }
}

// ---------------------------------------------------------------------------
// Recover the EOA address from a UserOp-hash + signature pair.
// ---------------------------------------------------------------------------
//
// We delegate to the WASM signer to DERIVE the expected address indirectly:
// re-sign the same hash with the known owner private key and compare the
// resulting signature's r‖s to the captured signature's r‖s. If they match,
// the captured signature was produced by the owner key over exactly this
// hash (so the EntryPoint will accept it). This avoids pulling in a
// third-party ecrecover dep and stays within the plugin's own crypto stack.
function recoverEoaFromSig(hashHex: string, signatureHex: string): string | null {
  try {
    // Re-sign the candidate hash with the owner key and compare r||s.
    // v may differ by the recovery-id parity, so compare only r||s (first 64 bytes).
    const reSig = signUserOp(hashHex, eoa.private_key);
    const reRs = reSig.slice(0, 128); // 64 bytes hex = 128 chars
    const capRs = signatureHex.replace(/^0x/, '').slice(0, 128);
    if (reRs.toLowerCase() !== capRs.toLowerCase()) return null;
    // Matches → the owner key produced `signatureHex` over `hashHex`.
    return EOA_ADDRESS;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

if (failed > 0) {
  console.error(`\nFAIL — ${failed}/${passed + failed} checks failed`);
  process.exit(1);
}
console.log(`\ncounterfactual-initcode.test OK — ${passed} checks passed`);
