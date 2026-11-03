/**
 * PRD-04 F8 / DEP-6 — outer-protobuf calldata parity (TypeScript leg).
 *
 * Loads the shared fixture `fixtures/outer-protobuf-v1.json` (generated from
 * the Rust core; see `fixtures/generate-outer-protobuf-fixture.py`) and
 * asserts, for the same inputs:
 *
 *   1. the MCP server encoder (`mcp/src/subgraph/protobuf.ts`) and the WASM
 *      core encoder (`encodeFactProtobuf`, used by the OpenClaw plugin and
 *      the SPA) are byte-identical to the fixture;
 *   2. SimpleAccount calldata built the way the MCP runtime builds it
 *      (viem `encodeFunctionData` with the SimpleAccount v0.7 ABI) and the
 *      way core builds it (WASM `encodeSingleCallTo` / `encodeBatchCallTo`)
 *      is byte-identical to the fixture; and
 *   3. ABI-decoding that calldata yields outer protobufs WITHOUT field 9
 *      (`source`) or field 11 (`agent_id`), addressed to the fixture DataEdge.
 *
 * Negative control: the walker MUST find fields 9 and 11 in
 * `fixtures/legacy/outer-protobuf-mcp-pre-dep6.json` (pre-fix MCP output).
 *
 * Siblings: tests/parity/test_outer_protobuf_parity.py (Python),
 * rust/totalreclaw-core/tests/outer_protobuf_parity.rs (Rust),
 * mcp/tests/protobuf-outer-fields.test.ts (MCP unit guard).
 *
 * Run (needs rust/totalreclaw-core/pkg from
 * `wasm-pack build --target nodejs --out-dir pkg --features wasm`):
 *   cd tests/parity && npx tsx outer-protobuf-parity.test.ts
 * Use tsx, not `node --experimental-strip-types`: the MCP encoder is a
 * TypeScript file inside the CommonJS `mcp/` package and is loaded through
 * tsx's require hook.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { decodeFunctionData, encodeFunctionData, parseAbi, type Hex } from 'viem';

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

// WASM core (CJS module produced by wasm-pack) — same loading pattern as
// userop-batch-parity.test.ts.
const wasm = require(
  join(__dirname, '..', '..', 'rust', 'totalreclaw-core', 'pkg', 'totalreclaw_core.js'),
) as {
  encodeFactProtobuf: (json: string) => Uint8Array;
  encodeSingleCallTo: (payload: Uint8Array, dataEdgeAddress: string) => Uint8Array;
  encodeBatchCallTo: (payloadsHexJson: string, dataEdgeAddress: string) => Uint8Array;
};

interface McpFactPayload {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: string;
  blindIndices: string[];
  decayScore: number;
  source: string;
  contentFp: string;
  agentId: string;
  encryptedEmbedding?: string;
}

// The MCP server's own encoder, loaded from source (dependency-free module).
const mcp = require(
  join(__dirname, '..', '..', 'mcp', 'src', 'subgraph', 'protobuf.ts'),
) as { encodeFactProtobuf: (fact: McpFactPayload) => Buffer };

interface FixtureInput {
  id: string;
  timestamp: string;
  owner: string;
  encrypted_blob_hex: string;
  blind_indices: string[];
  decay_score: number;
  source: string;
  content_fp: string;
  agent_id: string;
  encrypted_embedding: string | null;
  version: number;
}

interface FixtureVector {
  name: string;
  input: FixtureInput;
  expected_field_numbers: number[];
  expected_protobuf_hex: string;
  expected_execute_calldata_hex: string;
}

interface Fixture {
  meta: { data_edge_address: string; forbidden_fields: number[] };
  vectors: FixtureVector[];
  batch: { vector_names: string[]; expected_calldata_hex: string };
}

interface LegacyFixture {
  vectors: Array<{ name: string; expected_field_numbers: number[]; protobuf_hex: string }>;
}

const fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'outer-protobuf-v1.json'), 'utf8'),
) as Fixture;
const legacy = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'legacy', 'outer-protobuf-mcp-pre-dep6.json'), 'utf8'),
) as LegacyFixture;

const FORBIDDEN = new Set(fixture.meta.forbidden_fields);
// viem validates mixed-case addresses as EIP-55 checksums; lowercase is
// always accepted and encodes to the same 20 bytes.
const DATA_EDGE = fixture.meta.data_edge_address.toLowerCase() as Hex;

// SimpleAccount v0.7 — the ABI permissionless' toSimpleSmartAccount encodes
// for the MCP write path (execute for 1 call, executeBatch for >1).
const SIMPLE_ACCOUNT_ABI = parseAbi([
  'function execute(address dest, uint256 value, bytes func)',
  'function executeBatch(address[] dest, uint256[] value, bytes[] func)',
]);

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
      console.log(`  length mismatch: actual ${actual.length}, expected ${expected.length}`);
    }
  }
  assert(ok, name);
}

function readVarint(buf: Buffer, offset: number): [number, number] {
  let value = 0;
  let shift = 0;
  let i = offset;
  while (i < buf.length) {
    const byte = buf[i];
    value += (byte & 0x7f) * 2 ** shift;
    i += 1;
    if ((byte & 0x80) === 0) return [value, i];
    shift += 7;
  }
  throw new Error('truncated varint');
}

/** Outer protobuf field numbers of `buf`, in wire order. */
function fieldNumbers(buf: Buffer): number[] {
  const out: number[] = [];
  let i = 0;
  while (i < buf.length) {
    const [key, afterKey] = readVarint(buf, i);
    i = afterKey;
    out.push(Math.floor(key / 8));
    const wireType = key % 8;
    if (wireType === 0) {
      i = readVarint(buf, i)[1];
    } else if (wireType === 1) {
      i += 8;
    } else if (wireType === 2) {
      const [len, afterLen] = readVarint(buf, i);
      i = afterLen + len;
    } else if (wireType === 5) {
      i += 4;
    } else {
      throw new Error(`unsupported wire type ${wireType}`);
    }
  }
  if (i !== buf.length) throw new Error('protobuf walk overran the buffer');
  return out;
}

function hasForbidden(fields: number[]): boolean {
  return fields.some((f) => FORBIDDEN.has(f));
}

function toMcpPayload(input: FixtureInput): McpFactPayload {
  return {
    id: input.id,
    timestamp: input.timestamp,
    owner: input.owner,
    encryptedBlob: input.encrypted_blob_hex,
    blindIndices: [...input.blind_indices],
    decayScore: input.decay_score,
    source: input.source,
    contentFp: input.content_fp,
    agentId: input.agent_id,
    encryptedEmbedding: input.encrypted_embedding ?? undefined,
  };
}

function runTests(): void {
  const mcpBytesByName = new Map<string, Buffer>();

  for (const v of fixture.vectors) {
    // 1. Encoder parity (MCP + WASM) against the Rust-generated fixture.
    const mcpBytes = mcp.encodeFactProtobuf(toMcpPayload(v.input));
    mcpBytesByName.set(v.name, mcpBytes);
    assertEqHex(mcpBytes.toString('hex'), v.expected_protobuf_hex, `${v.name}: MCP encoder byte-identical to fixture`);
    const wasmBytes = Buffer.from(wasm.encodeFactProtobuf(JSON.stringify(v.input)));
    assertEqHex(wasmBytes.toString('hex'), v.expected_protobuf_hex, `${v.name}: WASM core encoder byte-identical to fixture`);
    const fields = fieldNumbers(mcpBytes);
    assert(!hasForbidden(fields), `${v.name}: MCP outer protobuf has no field 9/11 (got [${fields.join(',')}])`);
    assert(
      fields.join(',') === v.expected_field_numbers.join(','),
      `${v.name}: MCP field layout is [${v.expected_field_numbers.join(',')}]`,
    );

    // 2. execute() calldata parity + decode.
    const tsCalldata = encodeFunctionData({
      abi: SIMPLE_ACCOUNT_ABI,
      functionName: 'execute',
      args: [DATA_EDGE, 0n, `0x${mcpBytes.toString('hex')}`],
    });
    assertEqHex(tsCalldata.slice(2), v.expected_execute_calldata_hex, `${v.name}: viem execute() calldata byte-identical to fixture`);
    const wasmCalldata = Buffer.from(wasm.encodeSingleCallTo(new Uint8Array(mcpBytes), DATA_EDGE)).toString('hex');
    assertEqHex(wasmCalldata, v.expected_execute_calldata_hex, `${v.name}: WASM encodeSingleCallTo byte-identical to fixture`);

    const decoded = decodeFunctionData({ abi: SIMPLE_ACCOUNT_ABI, data: `0x${v.expected_execute_calldata_hex}` });
    assert(decoded.functionName === 'execute', `${v.name}: calldata decodes as execute()`);
    const [dest, value, func] = decoded.args as unknown as readonly [Hex, bigint, Hex];
    assert(dest.toLowerCase() === DATA_EDGE, `${v.name}: execute() targets the fixture DataEdge`);
    assert(value === 0n, `${v.name}: execute() value is 0`);
    const inner = Buffer.from(func.slice(2), 'hex');
    assert(inner.equals(mcpBytes), `${v.name}: decoded calldata payload equals the MCP protobuf`);
    assert(!hasForbidden(fieldNumbers(inner)), `${v.name}: decoded execute() payload has no field 9/11`);
  }

  // 3. executeBatch() calldata parity + decode (MCP remember-with-supersede shape).
  const batchPayloads = fixture.batch.vector_names.map((n) => {
    const b = mcpBytesByName.get(n);
    if (!b) throw new Error(`batch references unknown vector ${n}`);
    return b;
  });
  const tsBatch = encodeFunctionData({
    abi: SIMPLE_ACCOUNT_ABI,
    functionName: 'executeBatch',
    args: [
      batchPayloads.map(() => DATA_EDGE),
      batchPayloads.map(() => 0n),
      batchPayloads.map((b) => `0x${b.toString('hex')}` as Hex),
    ],
  });
  assertEqHex(tsBatch.slice(2), fixture.batch.expected_calldata_hex, 'batch: viem executeBatch() calldata byte-identical to fixture');
  const wasmBatch = Buffer.from(
    wasm.encodeBatchCallTo(JSON.stringify(batchPayloads.map((b) => b.toString('hex'))), DATA_EDGE),
  ).toString('hex');
  assertEqHex(wasmBatch, fixture.batch.expected_calldata_hex, 'batch: WASM encodeBatchCallTo byte-identical to fixture');

  const decodedBatch = decodeFunctionData({ abi: SIMPLE_ACCOUNT_ABI, data: `0x${fixture.batch.expected_calldata_hex}` });
  assert(decodedBatch.functionName === 'executeBatch', 'batch: calldata decodes as executeBatch()');
  const [dests, values, funcs] = decodedBatch.args as unknown as readonly [readonly Hex[], readonly bigint[], readonly Hex[]];
  assert(funcs.length === batchPayloads.length, `batch: decodes to ${batchPayloads.length} calls`);
  funcs.forEach((func, k) => {
    const inner = Buffer.from(func.slice(2), 'hex');
    assert(dests[k].toLowerCase() === DATA_EDGE, `batch call ${k}: targets the fixture DataEdge`);
    assert(values[k] === 0n, `batch call ${k}: value is 0`);
    assert(inner.equals(batchPayloads[k]), `batch call ${k}: payload equals the MCP protobuf`);
    assert(!hasForbidden(fieldNumbers(inner)), `batch call ${k}: payload has no field 9/11`);
  });

  // 4. Negative control — the walker must see the defect in the legacy vectors.
  for (const lv of legacy.vectors) {
    const fields = fieldNumbers(Buffer.from(lv.protobuf_hex, 'hex'));
    assert(
      fields.includes(9) && fields.includes(11),
      `legacy ${lv.name}: walker detects fields 9 and 11 (negative control)`,
    );
    assert(
      fields.join(',') === lv.expected_field_numbers.join(','),
      `legacy ${lv.name}: field layout is [${lv.expected_field_numbers.join(',')}]`,
    );
  }

  console.log(`\n# ${passed}/${passed + failed} passed`);
  if (failed > 0) {
    console.log('\nSOME TESTS FAILED');
    process.exit(1);
  } else {
    console.log('\nALL TESTS PASSED');
  }
}

runTests();
