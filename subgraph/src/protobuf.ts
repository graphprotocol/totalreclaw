/**
 * Minimal Protobuf decoder for TotalReclawFact messages in AssemblyScript.
 *
 * The subgraph runs AssemblyScript (NOT TypeScript). We cannot use protobufjs.
 * Instead, we implement a minimal wire-format decoder for the fields we need.
 *
 * Protobuf wire format reference:
 *   - Field key = (field_number << 3) | wire_type
 *   - Wire type 0 = varint (int32, bool, enum)
 *   - Wire type 1 = 64-bit (double)
 *   - Wire type 2 = length-delimited (string, bytes, embedded messages, repeated)
 *
 * TotalReclawFact fields (from totalreclaw.proto):
 *   1:  string id                    (wire type 2)
 *   2:  string timestamp             (wire type 2)
 *   3:  string owner                 (wire type 2)
 *   4:  bytes encrypted_blob         (wire type 2) — XChaCha20-Poly1305 encrypted
 *   5:  repeated string blind_indices (wire type 2)
 *   6:  double decay_score           (wire type 1)
 *   7:  bool is_active               (wire type 0)
 *   8:  int32 version                (wire type 0)
 *   9:  (removed — encrypted in blob v3)
 *  10:  string content_fp            (wire type 2) — HMAC-SHA256 content fingerprint
 *  11:  (removed — encrypted in blob v3)
 *  12:  int64 sequence_id            (wire type 0) — monotonic per-user (varint)
 *  13:  string encrypted_embedding   (wire type 2) — XChaCha20-Poly1305 encrypted embedding (hex)
 *  14:  uint32 index_scheme          (wire type 0) — 1 = keyed blind indices (HMAC v1);
 *                                    absent = legacy. Written by the core encoder on facts
 *                                    AND tombstones. A field 14 with any other wire type is
 *                                    ignored (indexScheme stays 0).
 *
 * Shared test vectors: tests/parity/fixtures/protobuf-index-scheme-vectors.json
 * (generated from the core encoder; the relay decoder loads the same file).
 */

import { Bytes, BigDecimal, BigInt } from "@graphprotocol/graph-ts";

export class DecodedFact {
  id: string;
  timestamp: string;
  owner: string;
  encryptedBlob: Bytes;
  blindIndices: string[];
  decayScore: BigDecimal;
  isActive: boolean;
  version: i32;
  contentFp: string;
  sequenceId: i64;
  encryptedEmbedding: string;
  /** Decoded varint of field 14. 0 when absent, wrong wire type, or truncated. */
  indexScheme: i64;

  constructor() {
    this.id = "";
    this.timestamp = "";
    this.owner = "";
    this.encryptedBlob = Bytes.empty();
    this.blindIndices = [];
    this.decayScore = BigDecimal.zero();
    this.isActive = true;
    this.version = 0;
    this.contentFp = "";
    this.sequenceId = 0;
    this.encryptedEmbedding = "";
    this.indexScheme = 0;
  }
}

/** Only index_scheme == 1 counts. The mapping rule is total (design v2.1, section 6). */
export const INDEX_SCHEME_KEYED_V1: i64 = 1;

/** True when the payload carries field 14 = 1. */
export function isKeyedScheme(decoded: DecodedFact): boolean {
  return decoded.indexScheme == INDEX_SCHEME_KEYED_V1;
}

/**
 * A tombstone is what the core encoder writes for a forget: empty encrypted
 * blob and decay_score == 0.
 */
export function isTombstone(decoded: DecodedFact): boolean {
  return decoded.encryptedBlob.length == 0 && decoded.decayScore.equals(BigDecimal.zero());
}

/**
 * Decode a varint from the byte array starting at offset.
 * Returns [value, newOffset]. newOffset is -1 when the varint is truncated
 * (continuation bit set on the last available byte) or longer than 10 bytes;
 * callers must stop decoding. A truncated varint must never yield a value
 * (0x81 cut short would otherwise read as 1).
 */
function decodeVarint(data: Bytes, offset: i32): i64[] {
  let result: i64 = 0;
  let shift: i32 = 0;
  let pos = offset;

  while (pos < data.length) {
    if (shift >= 64) return [0, -1];
    let byte = data[pos] as i64;
    result |= (byte & 0x7f) << shift;
    pos++;
    if ((byte & 0x80) == 0) return [result, pos as i64];
    shift += 7;
  }

  return [0, -1];
}

/**
 * Decode an TotalReclawFact from raw Protobuf bytes.
 *
 * This is a best-effort decoder. If the payload is malformed or contains
 * fields we do not recognize, we skip them gracefully.
 */
export function decodeFact(data: Bytes): DecodedFact {
  let fact = new DecodedFact();

  let offset: i32 = 0;

  while (offset < data.length) {
    // Read field key
    let keyResult = decodeVarint(data, offset);
    if (keyResult[1] < 0) break;
    let key = keyResult[0];
    offset = keyResult[1] as i32;

    // i64 end to end: narrowing the key to i32 would let an oversized key
    // alias a real field number (e.g. 2^32 + 0x70 reading as field 14).
    let fieldNumber: i64 = key >> 3;
    let wireType = (key & 0x07) as i32;

    if (wireType == 0) {
      // Varint
      let valResult = decodeVarint(data, offset);
      if (valResult[1] < 0) break;
      let value = valResult[0];
      offset = valResult[1] as i32;

      if (fieldNumber == 7) {
        fact.isActive = value != 0;
      } else if (fieldNumber == 8) {
        fact.version = value as i32;
      } else if (fieldNumber == 12) {
        // sequence_id: decoded for completeness, but the mapping assigns
        // its own monotonic sequence IDs from GlobalState
        fact.sequenceId = value;
      } else if (fieldNumber == 14) {
        fact.indexScheme = value;
      }
    } else if (wireType == 1) {
      // 64-bit (double)
      if (offset + 8 > data.length) break;

      if (fieldNumber == 6) {
        // decay_score (#374 fix): protobuf encodes this f64 as 8 little-endian
        // bytes. Reassemble them into a u64 and reinterpret the bits as an
        // IEEE-754 double. Previously this was stubbed to a constant 1.0, so a
        // tombstone's decay_score=0 never reached the mapping and `isActive`
        // (computed as decayScore >= INACTIVE_THRESHOLD) never flipped to
        // false on a forget — the relay hot view dropped the fact but the
        // on-chain ledger kept it active.
        let bits: u64 = 0;
        for (let b = 0; b < 8; b++) {
          bits = bits | (u64(data[offset + b]) << u64(8 * b));
        }
        let dval = reinterpret<f64>(bits);
        fact.decayScore = BigDecimal.fromString(dval.toString());
      }
      offset += 8;
    } else if (wireType == 2) {
      // Length-delimited
      let lenResult = decodeVarint(data, offset);
      if (lenResult[1] < 0) break;
      let len64 = lenResult[0];
      offset = lenResult[1] as i32;

      // Compare in i64 so a huge or negative length can never pass.
      if (len64 < 0 || len64 > (data.length - offset) as i64) break;
      let len = len64 as i32;

      let slice = data.subarray(offset, offset + len);
      // Convert Uint8Array to Bytes so toString() does UTF-8 decoding
      // (Uint8Array.toString() returns comma-separated numbers in AssemblyScript)
      let sliceBytes = Bytes.fromUint8Array(slice);

      if (fieldNumber == 1) {
        fact.id = sliceBytes.toString();
      } else if (fieldNumber == 2) {
        fact.timestamp = sliceBytes.toString();
      } else if (fieldNumber == 3) {
        fact.owner = sliceBytes.toString();
      } else if (fieldNumber == 4) {
        fact.encryptedBlob = sliceBytes;
      } else if (fieldNumber == 5) {
        let indices = fact.blindIndices;
        indices.push(sliceBytes.toString());
        fact.blindIndices = indices;
      } else if (fieldNumber == 10) {
        fact.contentFp = sliceBytes.toString();
      } else if (fieldNumber == 13) {
        fact.encryptedEmbedding = sliceBytes.toString();
      }

      offset += len;
    } else if (wireType == 5) {
      // 32-bit fixed
      offset += 4;
    } else {
      // Unknown wire type — cannot skip safely, bail out
      break;
    }
  }

  return fact;
}
