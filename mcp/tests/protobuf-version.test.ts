/**
 * Regression test for QA-V1-VPS-20260418.md Bug #10.
 *
 * `encodeFactProtobuf` must stamp the outer protobuf wrapper field 8
 * (`version`, wire type 0) as `4` (PROTOBUF_VERSION_V4). Shipping `2`
 * or `3` silently breaks the Memory Taxonomy v1 cross-client contract —
 * the subgraph and peer clients (plugin, python, rust) all expect the
 * v4 sentinel when the inner blob is a v1 JSON claim.
 */

import {
  encodeFactProtobuf,
  encodeVarint,
  PROTOBUF_VERSION_V4,
  type FactPayload,
} from '../src/subgraph/store.js';

/**
 * Read a protobuf varint starting at `offset`. Returns [value, bytesConsumed].
 */
function readVarint(buf: Buffer, offset: number): [number, number] {
  let value = 0;
  let shift = 0;
  let i = offset;
  while (i < buf.length) {
    const byte = buf[i];
    value |= (byte & 0x7f) << shift;
    i += 1;
    if ((byte & 0x80) === 0) {
      return [value >>> 0, i - offset];
    }
    shift += 7;
  }
  throw new Error('truncated varint');
}

/**
 * Walk the protobuf wire format and return the varint value for a given
 * (fieldNumber, wireType=0) pair, or `undefined` if the field is absent.
 */
function readVarintField(buf: Buffer, fieldNumber: number): number | undefined {
  const wantKey = (fieldNumber << 3) | 0;
  let i = 0;
  while (i < buf.length) {
    const [key, keyLen] = readVarint(buf, i);
    i += keyLen;
    const wireType = key & 0x07;
    if (key === wantKey) {
      const [value] = readVarint(buf, i);
      return value;
    }
    // Skip this field based on wire type.
    if (wireType === 0) {
      const [, len] = readVarint(buf, i);
      i += len;
    } else if (wireType === 1) {
      i += 8; // fixed64
    } else if (wireType === 2) {
      const [len, lenLen] = readVarint(buf, i);
      i += lenLen + len; // length-delimited
    } else if (wireType === 5) {
      i += 4; // fixed32
    } else {
      throw new Error(`unsupported wire type ${wireType}`);
    }
  }
  return undefined;
}

function makeFact(overrides: Partial<FactPayload> = {}): FactPayload {
  return {
    id: 'test-id-00000000',
    timestamp: '2026-04-18T00:00:00Z',
    owner: '0x2c0CF74B2b76110708CA431796367779e3738250',
    encryptedBlob: '00ff11ee',
    blindIndices: ['deadbeef', 'cafef00d'],
    decayScore: 0.9,
    source: 'mcp_test',
    contentFp: 'abcd',
    agentId: 'test',
    ...overrides,
  };
}

describe('encodeFactProtobuf — outer wrapper version', () => {
  it('exposes PROTOBUF_VERSION_V4 === 4', () => {
    expect(PROTOBUF_VERSION_V4).toBe(4);
  });

  it('writes field 8 (version) as 4 for a standard fact payload', () => {
    const buf = encodeFactProtobuf(makeFact());
    const version = readVarintField(buf, 8);
    expect(version).toBe(4);
  });

  it('writes field 8 (version) as 4 for a tombstone payload', () => {
    const tombstone = makeFact({
      encryptedBlob: Buffer.from('tombstone').toString('hex'),
      blindIndices: [],
      decayScore: 0,
      source: 'mcp_dedup',
      contentFp: '',
    });
    const buf = encodeFactProtobuf(tombstone);
    const version = readVarintField(buf, 8);
    expect(version).toBe(4);
  });

  it('writes field 8 (version) as 4 when encrypted_embedding is present', () => {
    const buf = encodeFactProtobuf(
      makeFact({ encryptedEmbedding: 'aa'.repeat(16) }),
    );
    const version = readVarintField(buf, 8);
    expect(version).toBe(4);
  });

  it('also writes is_active (field 7) as 1', () => {
    const buf = encodeFactProtobuf(makeFact());
    const isActive = readVarintField(buf, 7);
    expect(isActive).toBe(1);
  });

  // Sanity check: our varint reader actually works.
  it('varint helper roundtrips', () => {
    const buf = Buffer.concat([encodeVarint(4)]);
    const [v, n] = readVarint(buf, 0);
    expect(v).toBe(4);
    expect(n).toBe(1);
  });
});
