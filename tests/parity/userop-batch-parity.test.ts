/**
 * Cross-language ERC-4337 SimpleAccount.executeBatch calldata parity test
 * (TypeScript / WASM side).
 *
 * Loads the shared fixture ``fixtures/userop-batch-v1.json`` — generated
 * from the Rust source-of-truth encoder
 * ``totalreclaw_core::userop::encode_batch_call`` — and asserts that the
 * WASM binding ``encodeBatchCall`` produces byte-identical
 * ``executeBatch`` calldata for the same input payloads.
 *
 * The Python sibling test
 * ``python/tests/test_userop_batch.py::TestSharedParityFixture`` loads
 * the same fixture and asserts the same byte-identity from the Python
 * encoder. Both tests passing CI is the cross-client guarantee.
 *
 * Companion to:
 *   - tests/parity/fixtures/generate-userop-batch-fixture.py (regenerator)
 *   - python/tests/test_userop_batch.py::TestSharedParityFixture
 *   - skill/plugin/pin-batch-cross-impl-parity.test.ts (pin-scenario sibling)
 *
 * Run:
 *   cd tests/parity && npx tsx userop-batch-parity.test.ts
 *   -- or --
 *   cd tests/parity && node --experimental-strip-types userop-batch-parity.test.ts
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// Load the WASM core via createRequire (the CJS module produced by
// wasm-pack at ``rust/totalreclaw-core/pkg``). Mirror of the lazy-load
// pattern in skill/plugin/pin-batch-cross-impl-parity.test.ts.
const wasmPath = join(
  __dirname,
  '..',
  '..',
  'rust',
  'totalreclaw-core',
  'pkg',
  'totalreclaw_core.js',
);
const wasm = require(wasmPath) as {
  encodeBatchCall: (payloadsHexJson: string) => Uint8Array;
};

interface Fixture {
  meta: {
    version: number;
    description: string;
    selector_executeBatch: string;
    count: number;
  };
  payloads_hex: string[];
  expected_calldata_hex: string;
  expected_calldata_bytes: number;
}

const fixturePath = join(
  __dirname,
  'fixtures',
  'userop-batch-v1.json',
);
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as Fixture;

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function assertEqHex(actual: string, expected: string, name: string): void {
  const ok = actual.toLowerCase() === expected.toLowerCase();
  if (!ok) {
    console.log(`  actual[0..60]:   ${actual.slice(0, 60)}…`);
    console.log(`  expected[0..60]: ${expected.slice(0, 60)}…`);
    if (actual.length !== expected.length) {
      console.log(
        `  length mismatch: actual ${actual.length}, expected ${expected.length}`,
      );
    }
  }
  assert(ok, name);
}

function runTests(): void {
  // 1. Sanity-check the fixture itself.
  assert(
    fixture.payloads_hex.length === fixture.meta.count,
    `fixture payloads count matches meta (${fixture.meta.count})`,
  );
  assert(
    fixture.meta.count === 15,
    'fixture batch size is the spec-mandated 15 (MAX_BATCH_SIZE)',
  );
  assert(
    fixture.expected_calldata_hex.startsWith('47e1da2a'),
    `fixture expected_calldata_hex routes through SimpleAccount.executeBatch (selector 0x47e1da2a)`,
  );

  // 2. THE cross-language parity claim: TS/WASM encoder produces the
  // byte-identical executeBatch calldata that the Rust core does (and
  // by transitivity, what the Python encoder does — see
  // python/tests/test_userop_batch.py::TestSharedParityFixture).
  const payloadsBytes = fixture.payloads_hex.map((h) => Buffer.from(h, 'hex'));
  const payloadsHexArg = JSON.stringify(payloadsBytes.map((b) => b.toString('hex')));
  const calldataBytes = wasm.encodeBatchCall(payloadsHexArg);
  const calldataHex = Buffer.from(calldataBytes).toString('hex');

  assert(
    calldataBytes.length === fixture.expected_calldata_bytes,
    `calldata byte length matches fixture (${fixture.expected_calldata_bytes})`,
  );
  assertEqHex(
    calldataHex,
    fixture.expected_calldata_hex,
    'TS/WASM encodeBatchCall byte-identical to shared fixture (Rust-generated, Python-verified)',
  );

  console.log(`\n# ${passed}/${passed + failed} passed`);
  if (failed > 0) {
    console.log('\nSOME TESTS FAILED');
    process.exit(1);
  } else {
    console.log('\nALL TESTS PASSED');
  }
}

runTests();
