/**
 * internal#573 (M2): keyed encoders against the frozen wire vectors and the
 * WASM core keyed encoders.
 *
 * Valid-hex payloads are compared byte for byte with core. Invalid blob hex is
 * checked separately: core skips field 4, the client throws (a payload with no
 * blob must never reach the chain), and the message never echoes the value.
 */

import {
  encodeFactProtobufKeyed,
  encodeTombstoneProtobufKeyed,
  type FactPayload,
} from '../src/subgraph/store';
import { fx } from './helpers/keyed-fixture';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const core = require('@totalreclaw/core') as {
  encodeFactProtobufKeyed: (json: string) => Uint8Array;
  encodeTombstoneProtobufKeyed: (factId: string, owner: string) => Uint8Array;
};

interface Field { n: number; wire: number; bytes?: Buffer; value?: number }

function readVarint(buf: Buffer, at: number): [number, number] {
  let v = 0, shift = 0, i = at;
  for (;;) {
    const b = buf[i++];
    v += (b & 0x7f) * 2 ** shift;
    if (!(b & 0x80)) return [v, i];
    shift += 7;
  }
}

function parse(buf: Buffer): Field[] {
  const out: Field[] = [];
  let i = 0;
  while (i < buf.length) {
    const [key, afterKey] = readVarint(buf, i);
    const n = key >> 3, wire = key & 7;
    i = afterKey;
    if (wire === 0) { const [v, j] = readVarint(buf, i); out.push({ n, wire, value: v }); i = j; }
    else if (wire === 1) { out.push({ n, wire, bytes: buf.subarray(i, i + 8) }); i += 8; }
    else if (wire === 2) { const [len, j] = readVarint(buf, i); out.push({ n, wire, bytes: buf.subarray(j, j + len) }); i = j + len; }
    else throw new Error(`wire type ${wire}`);
  }
  return out;
}

function toFact(input: Record<string, unknown>): FactPayload {
  return {
    id: input.id as string,
    timestamp: input.timestamp as string,
    owner: input.owner as string,
    encryptedBlob: input.encrypted_blob_hex as string,
    blindIndices: [...(input.blind_indices as string[])],
    decayScore: input.decay_score as number,
    source: (input.source as string) ?? '',
    contentFp: (input.content_fp as string) ?? '',
    agentId: (input.agent_id as string) ?? '',
    encryptedEmbedding: (input.encrypted_embedding as string | null) ?? undefined,
  };
}

// Deterministic PRNG (mulberry32).
function rng(seed: number) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const hexOf = (r: () => number, nBytes: number) => Array.from({ length: nBytes }, () => Math.floor(r() * 256).toString(16).padStart(2, '0')).join('');

describe('frozen wire vectors', () => {
  test('wire.fact: byte-exact', () => {
    const buf = encodeFactProtobufKeyed(toFact(fx.wire.fact.input));
    expect(buf.toString('hex')).toBe(fx.wire.fact.expected_hex);
  });

  test('wire.tombstone: byte-exact at the fixture timestamp', () => {
    const t = fx.wire.tombstone;
    const buf = encodeTombstoneProtobufKeyed(t.fact_id, t.owner, t.timestamp);
    expect(buf.toString('hex')).toBe(t.expected_hex);
  });

  test('core keyed tombstone matches prefix/suffix and the client matches at the same timestamp', () => {
    const t = fx.wire.tombstone;
    const coreHex = Buffer.from(core.encodeTombstoneProtobufKeyed(t.fact_id, t.owner)).toString('hex');
    expect(coreHex.startsWith(t.prefix_hex)).toBe(true);
    expect(coreHex.endsWith(t.suffix_hex)).toBe(true);
    // Take the timestamp core wrote (field 2) and encode the client's with it.
    const f2 = parse(Buffer.from(coreHex, 'hex')).find((f) => f.n === 2)!;
    const mine = encodeTombstoneProtobufKeyed(t.fact_id, t.owner, f2.bytes!.toString('utf8'));
    expect(mine.toString('hex')).toBe(coreHex);
  });
});

describe('200 random payloads equal core byte for byte', () => {
  const r = rng(573);
  for (let k = 0; k < 200; k++) {
    const nTags = Math.floor(r() * 12);
    const pool = Array.from({ length: Math.max(1, Math.ceil(nTags / 2)) }, () => hexOf(r, 32));
    const tags = Array.from({ length: nTags }, () => pool[Math.floor(r() * pool.length)]); // duplicates, unsorted
    const blobBytes = k % 25 === 0 ? 0 : Math.floor(r() * 80); // includes the empty blob
    const input = {
      id: `id-${k}`,
      timestamp: '2026-11-25T12:00:00.000Z',
      owner: '0x' + hexOf(r, 20),
      encrypted_blob_hex: hexOf(r, blobBytes),
      blind_indices: tags,
      decay_score: [0, 1, 0.5, 0.123][k % 4],
      source: 's', content_fp: k % 3 ? hexOf(r, 32) : '', agent_id: 'a',
      encrypted_embedding: k % 2 ? 'ZW1iZWRkaW5n' : null,
      version: [0, 3, 4, 9][k % 4],
    };
    test(`payload ${k}`, () => {
      const mine = encodeFactProtobufKeyed(toFact(input));
      expect(mine.toString('hex')).toBe(Buffer.from(core.encodeFactProtobufKeyed(JSON.stringify(input))).toString('hex'));
      const fields = parse(mine);
      const f5 = fields.filter((f) => f.n === 5).map((f) => f.bytes!.toString('utf8'));
      for (let i = 1; i < f5.length; i++) expect(f5[i] > f5[i - 1]).toBe(true); // strictly ascending
      expect(fields[fields.length - 1]).toMatchObject({ n: 14, value: 1 });
      expect(fields.filter((f) => f.n === 14)).toHaveLength(1);
      expect(fields.find((f) => f.n === 8)!.value).toBe(4);
    });
  }
});

describe('tombstone layout', () => {
  test('empty blob, decay 0, is_active 0, version 4, field 14 = 1, nothing else', () => {
    const fields = parse(encodeTombstoneProtobufKeyed('id-1', '0xabc', '2026-01-01T00:00:00.000Z'));
    expect(fields.map((f) => f.n)).toEqual([1, 2, 3, 4, 6, 7, 8, 14]);
    expect(fields.find((f) => f.n === 4)!.bytes!.length).toBe(0);
    expect(fields.find((f) => f.n === 6)!.bytes!.readDoubleLE(0)).toBe(0);
    expect(fields.find((f) => f.n === 7)!.value).toBe(0);
    expect(fields.find((f) => f.n === 8)!.value).toBe(4);
    expect(fields.find((f) => f.n === 14)!.value).toBe(1);
  });
  test('default timestamp is an ISO string', () => {
    const f2 = parse(encodeTombstoneProtobufKeyed('id-1', '0xabc')).find((f) => f.n === 2)!;
    expect(f2.bytes!.toString('utf8')).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  });
});

describe('rejections never echo the value', () => {
  const base = toFact(fx.wire.fact.input);
  const secretWord = 'secretword';
  const badTags: Array<[string, string]> = [
    ['uppercase hex', 'A'.repeat(64)],
    ['63 chars', 'a'.repeat(63)],
    ['a word', secretWord],
    ['65 chars', 'a'.repeat(65)],
    ['empty', ''],
  ];
  for (const [label, tag] of badTags) {
    test(`bad tag (${label}) throws`, () => {
      let msg = '';
      try { encodeFactProtobufKeyed({ ...base, blindIndices: [...base.blindIndices, tag] }); } catch (e) { msg = (e as Error).message; }
      expect(msg).not.toBe('');
      if (tag) expect(msg).not.toContain(tag);
    });
  }
  const badBlobs: Array<[string, string]> = [
    ['odd length', 'abc'],
    ['non-hex character', '00zz11'],
    ['embedded space', '00 11'],
    ['0x prefix', '0x0011'],
  ];
  for (const [label, blob] of badBlobs) {
    test(`bad blob hex (${label}) throws`, () => {
      let msg = '';
      try { encodeFactProtobufKeyed({ ...base, encryptedBlob: blob }); } catch (e) { msg = (e as Error).message; }
      expect(msg).not.toBe('');
      expect(msg).not.toContain(blob);
    });
  }
  test('an empty blob is valid and writes 22 00 like core', () => {
    const buf = encodeFactProtobufKeyed({ ...base, encryptedBlob: '' });
    expect(parse(buf).find((f) => f.n === 4)!.bytes!.length).toBe(0);
  });
});

describe('only keyed encoders are exported from the store', () => {
  test('no legacy encoder remains', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const store = require('../src/subgraph/store') as Record<string, unknown>;
    expect(store.encodeFactProtobuf).toBeUndefined();
    expect(typeof store.encodeFactProtobufKeyed).toBe('function');
    expect(typeof store.encodeTombstoneProtobufKeyed).toBe('function');
  });
});
