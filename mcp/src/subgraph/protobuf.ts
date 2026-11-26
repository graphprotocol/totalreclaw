/**
 * TotalReclaw MCP — outer protobuf encoder for on-chain fact payloads.
 *
 * Dependency-free on purpose: the cross-language parity suite
 * (`tests/parity/outer-protobuf-parity.test.ts`) loads this file directly,
 * without the MCP runtime dependencies installed. `subgraph/store.ts`
 * re-exports everything here, so imports from `./subgraph/store.js` are
 * unchanged.
 *
 * Wire layout is byte-identical to the canonical keyed encoders for the same
 * well-formed input (valid, even-length hex blob). On a blob that is not
 * valid hex, core skips field 4; this encoder throws instead, so a payload
 * with no blob never reaches the chain (the message never echoes the value).
 *   - Rust:   rust/totalreclaw-core/src/protobuf.rs
 *             encode_fact_protobuf_keyed / encode_tombstone_protobuf_keyed
 *   - Python: python/src/totalreclaw/protobuf.py (same names)
 *
 * Field numbers (server/proto/totalreclaw.proto):
 *   1 id, 2 timestamp, 3 owner, 4 encrypted_blob (bytes),
 *   5 blind_indices (repeated), 6 decay_score (double), 7 is_active,
 *   8 version, 9 source (NOT WRITTEN), 10 content_fp,
 *   11 agent_id (NOT WRITTEN), 12 sequence_id (assigned by the subgraph),
 *   13 encrypted_embedding, 14 index_scheme (always 1, written last).
 *
 * Fields 9 and 11 left the wire in v3 (provenance lives inside the encrypted
 * blob, field 4). MCP kept writing them as plaintext calldata until PRD-04
 * F8 / DEP-6. Pinned by tests/parity/fixtures/outer-protobuf-v1.json.
 */

/**
 * Memory Taxonomy v1 outer protobuf wrapper version.
 *
 * The v1 contract (shipped 2026-04-18) mandates that all clients write
 * `version = 4` on the outer protobuf so the subgraph + cross-client
 * readers can recognize the inner blob as a v1 JSON `MemoryClaim`.
 *
 * Canonical source: `rust/totalreclaw-core/src/protobuf.rs`
 * (`PROTOBUF_VERSION_V4 = 4`). The WASM/PyO3 bindings do not re-export
 * this constant to TS; switch to an import from `@totalreclaw/core` if
 * they ever do.
 */
export const PROTOBUF_VERSION_V4 = 4;

export interface FactPayload {
  id: string;
  timestamp: string;
  owner: string;           // Smart Account address (hex)
  encryptedBlob: string;   // Hex-encoded XChaCha20-Poly1305 ciphertext
  blindIndices: string[];  // keyed tags: 64 lowercase hex each (sorted + deduped on encode)
  decayScore: number;
  /**
   * Write-path label (e.g. `mcp_remember`). NOT encoded on-chain: outer
   * field 9 left the wire in v3 (PRD-04 F8 / DEP-6). Kept on the type so
   * call sites compile unchanged — the Rust `FactPayload.source` and Python
   * `FactPayload.source` fields are likewise accepted and ignored.
   */
  source: string;
  contentFp: string;
  /**
   * Writer id (e.g. `mcp-server`). NOT encoded on-chain: outer field 11
   * left the wire in v3 (PRD-04 F8 / DEP-6). Kept for call-site
   * compatibility, mirroring Rust/Python `agent_id`.
   */
  agentId: string;
  encryptedEmbedding?: string;
}

/** Outer field 14 value: keyed blind-index scheme 1. */
export const INDEX_SCHEME_KEYED_V1 = 1;

const TAG_RE = /^[0-9a-f]{64}$/;
const BLOB_HEX_RE = /^(?:[0-9a-fA-F]{2})*$/;

/** Shared low-level writers. */
function writer() {
  const parts: Buffer[] = [];
  return {
    parts,
    // string field (skipped when empty, like core)
    string(fieldNumber: number, value: string) {
      if (!value) return;
      const data = Buffer.from(value, 'utf-8');
      parts.push(encodeVarint((fieldNumber << 3) | 2), encodeVarint(data.length), data);
    },
    // bytes field (always written, even when empty)
    bytes(fieldNumber: number, value: Buffer) {
      parts.push(encodeVarint((fieldNumber << 3) | 2), encodeVarint(value.length), value);
    },
    double(fieldNumber: number, value: number) {
      parts.push(encodeVarint((fieldNumber << 3) | 1));
      const buf = Buffer.alloc(8);
      buf.writeDoubleLE(value);
      parts.push(buf);
    },
    varint(fieldNumber: number, value: number) {
      parts.push(encodeVarint(fieldNumber << 3), encodeVarint(value));
    },
  };
}

/**
 * Encode a fact payload as a keyed-scheme-1 outer protobuf.
 *
 * Field 5 is `sorted(set(tags))`; every tag must be 64 lowercase hex chars
 * (throws otherwise, without echoing the value). Field 8 (`version`) is always
 * 4. Fields 9 and 11 are never written. Field 14 = 1 is written last.
 * Throws on a blob that is not valid even-length hex.
 */
export function encodeFactProtobufKeyed(fact: FactPayload): Buffer {
  if (!BLOB_HEX_RE.test(fact.encryptedBlob)) {
    throw new Error('encryptedBlob is not valid hex');
  }
  for (const tag of fact.blindIndices) {
    if (!TAG_RE.test(tag)) throw new Error('blind index is not 64 lowercase hex characters');
  }
  const w = writer();
  w.string(1, fact.id);
  w.string(2, fact.timestamp);
  w.string(3, fact.owner);
  w.bytes(4, Buffer.from(fact.encryptedBlob, 'hex'));
  for (const tag of [...new Set(fact.blindIndices)].sort()) {
    w.string(5, tag);
  }
  w.double(6, fact.decayScore);
  w.varint(7, 1); // is_active = true
  w.varint(8, PROTOBUF_VERSION_V4); // version = 4 (Memory Taxonomy v1)
  // Field 9 (source) is NOT written: provenance lives inside the encrypted blob.
  w.string(10, fact.contentFp);
  // Field 11 (agent_id) is NOT written. Field 12 (sequence_id) is assigned by the subgraph.
  if (fact.encryptedEmbedding) {
    w.string(13, fact.encryptedEmbedding);
  }
  w.varint(14, INDEX_SCHEME_KEYED_V1);
  return Buffer.concat(w.parts);
}

/**
 * Encode a keyed-scheme-1 tombstone: empty blob, `decay_score` 0,
 * `is_active` 0, version 4, field 14 = 1. No fields 5, 10 or 13.
 *
 * `timestamp` defaults to now (`toISOString()`); tests pass a fixed one.
 */
export function encodeTombstoneProtobufKeyed(
  factId: string,
  owner: string,
  timestamp?: string,
): Buffer {
  const w = writer();
  w.string(1, factId);
  w.string(2, timestamp ?? new Date().toISOString());
  w.string(3, owner);
  w.bytes(4, Buffer.alloc(0));
  w.double(6, 0);
  w.varint(7, 0); // is_active = false
  w.varint(8, PROTOBUF_VERSION_V4);
  w.varint(14, INDEX_SCHEME_KEYED_V1);
  return Buffer.concat(w.parts);
}

/** Encode an integer as a Protobuf varint */
export function encodeVarint(value: number): Buffer {
  const bytes: number[] = [];
  let v = value >>> 0; // unsigned
  while (v > 0x7f) {
    bytes.push((v & 0x7f) | 0x80);
    v >>>= 7;
  }
  bytes.push(v & 0x7f);
  return Buffer.from(bytes);
}
