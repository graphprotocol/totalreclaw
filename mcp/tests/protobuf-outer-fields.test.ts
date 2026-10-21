/**
 * PRD-04 F8 / DEP-6 — the MCP outer protobuf carries no plaintext provenance.
 *
 * Until DEP-6, `encodeFactProtobuf` wrote outer field 9 (`source`, e.g.
 * "mcp_remember") and field 11 (`agent_id`, "mcp-server") as plaintext
 * calldata. Core (`protobuf.rs`) and Python (`protobuf.py`) removed both in
 * v3. This suite pins the MCP encoder to the shared cross-language fixture
 * (`tests/parity/fixtures/outer-protobuf-v1.json`, Rust-generated), to the
 * installed `@totalreclaw/core` WASM encoder, and statically guards against
 * a second encoder appearing anywhere in `mcp/src`.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { encodeFactProtobuf, type FactPayload } from '../src/subgraph/store.js';

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
}

interface Fixture {
  meta: { forbidden_fields: number[] };
  vectors: FixtureVector[];
}

interface LegacyFixture {
  vectors: Array<{ name: string; expected_field_numbers: number[]; protobuf_hex: string }>;
}

const FIXTURE_DIR = join(__dirname, '../../tests/parity/fixtures');
const fixture = JSON.parse(
  readFileSync(join(FIXTURE_DIR, 'outer-protobuf-v1.json'), 'utf8'),
) as Fixture;
const legacy = JSON.parse(
  readFileSync(join(FIXTURE_DIR, 'legacy', 'outer-protobuf-mcp-pre-dep6.json'), 'utf8'),
) as LegacyFixture;
const FORBIDDEN = fixture.meta.forbidden_fields;
const SRC_DIR = join(__dirname, '..', 'src');

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

function toMcpPayload(input: FixtureInput): FactPayload {
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

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...listTsFiles(full));
    } else if (entry.endsWith('.ts') && !entry.endsWith('.d.ts')) {
      out.push(full);
    }
  }
  return out;
}

describe('MCP outer protobuf — fields 9/11 never written (PRD-04 F8 / DEP-6)', () => {
  it('fixture declares fields 9 and 11 forbidden', () => {
    expect(FORBIDDEN).toEqual([9, 11]);
  });

  for (const v of fixture.vectors) {
    it(`${v.name}: omits fields 9 and 11 even though source/agentId are set`, () => {
      expect(v.input.source).not.toBe('');
      expect(v.input.agent_id).not.toBe('');
      const fields = fieldNumbers(encodeFactProtobuf(toMcpPayload(v.input)));
      for (const f of FORBIDDEN) {
        expect(fields).not.toContain(f);
      }
      expect(fields).toEqual(v.expected_field_numbers);
    });

    it(`${v.name}: byte-identical to the shared Rust-generated fixture`, () => {
      const hex = encodeFactProtobuf(toMcpPayload(v.input)).toString('hex');
      expect(hex).toBe(v.expected_protobuf_hex);
    });

    it(`${v.name}: byte-identical to the installed @totalreclaw/core WASM encoder`, () => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const core = require('@totalreclaw/core') as { encodeFactProtobuf: (json: string) => Uint8Array };
      const wasmHex = Buffer.from(core.encodeFactProtobuf(JSON.stringify(v.input))).toString('hex');
      const mcpHex = encodeFactProtobuf(toMcpPayload(v.input)).toString('hex');
      expect(mcpHex).toBe(wasmHex);
    });
  }

  it('negative control: the pre-DEP-6 legacy vectors DO contain fields 9 and 11', () => {
    expect(legacy.vectors.length).toBeGreaterThan(0);
    for (const lv of legacy.vectors) {
      const fields = fieldNumbers(Buffer.from(lv.protobuf_hex, 'hex'));
      expect(fields).toContain(9);
      expect(fields).toContain(11);
      expect(fields).toEqual(lv.expected_field_numbers);
    }
  });

  it('static guard: one encoder in mcp/src, and nothing writes outer field 9 or 11', () => {
    const files = listTsFiles(SRC_DIR);
    const toRel = (f: string) => relative(SRC_DIR, f).split(sep).join('/');
    const definers = files
      .filter((f) => /export function encodeFactProtobuf\b/.test(readFileSync(f, 'utf8')))
      .map(toRel);
    expect(definers).toEqual(['subgraph/protobuf.ts']);
    const writers = files
      .filter((f) => /writeString\(\s*(9|11)\s*,|\(\s*(9|11)\s*<<\s*3\s*\)/.test(readFileSync(f, 'utf8')))
      .map(toRel);
    expect(writers).toEqual([]);
  });
});
