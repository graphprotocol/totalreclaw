/**
 * TotalReclaw MCP — outer protobuf encoder for on-chain fact payloads.
 *
 * Dependency-free on purpose: the cross-language parity suite
 * (`tests/parity/outer-protobuf-parity.test.ts`) loads this file directly,
 * without the MCP runtime dependencies installed. `subgraph/store.ts`
 * re-exports everything here, so imports from `./subgraph/store.js` are
 * unchanged.
 *
 * Wire layout is byte-identical to the canonical encoders for the same input:
 *   - Rust:   rust/totalreclaw-core/src/protobuf.rs  encode_fact_protobuf
 *   - Python: python/src/totalreclaw/protobuf.py     encode_fact_protobuf
 *
 * Field numbers (server/proto/totalreclaw.proto):
 *   1 id, 2 timestamp, 3 owner, 4 encrypted_blob (bytes),
 *   5 blind_indices (repeated), 6 decay_score (double), 7 is_active,
 *   8 version, 9 source (NOT WRITTEN), 10 content_fp,
 *   11 agent_id (NOT WRITTEN), 12 sequence_id (assigned by the subgraph),
 *   13 encrypted_embedding.
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
  blindIndices: string[];  // SHA-256 hashes (word + LSH)
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

/**
 * Encode a fact payload as a minimal Protobuf wire format.
 *
 * Field 8 (`version`) is always `PROTOBUF_VERSION_V4` (4) — every MCP write
 * carries a v1 inner blob. Fields 9 (`source`) and 11 (`agent_id`) are never
 * written, whatever the caller puts in `fact.source` / `fact.agentId`.
 */
export function encodeFactProtobuf(fact: FactPayload): Buffer {
  const parts: Buffer[] = [];

  // Helper: encode a string field (skipped when empty, like core/Python)
  const writeString = (fieldNumber: number, value: string) => {
    if (!value) return;
    const data = Buffer.from(value, 'utf-8');
    const key = (fieldNumber << 3) | 2; // wire type 2 = length-delimited
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(data.length));
    parts.push(data);
  };

  // Helper: encode a bytes field (always written, even when empty)
  const writeBytes = (fieldNumber: number, value: Buffer) => {
    const key = (fieldNumber << 3) | 2;
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(value.length));
    parts.push(value);
  };

  // Helper: encode a double field (wire type 1 = 64-bit)
  const writeDouble = (fieldNumber: number, value: number) => {
    const key = (fieldNumber << 3) | 1;
    parts.push(encodeVarint(key));
    const buf = Buffer.alloc(8);
    buf.writeDoubleLE(value);
    parts.push(buf);
  };

  // Helper: encode a varint field (wire type 0)
  const writeVarintField = (fieldNumber: number, value: number) => {
    const key = (fieldNumber << 3) | 0;
    parts.push(encodeVarint(key));
    parts.push(encodeVarint(value));
  };

  writeString(1, fact.id);
  writeString(2, fact.timestamp);
  writeString(3, fact.owner);
  writeBytes(4, Buffer.from(fact.encryptedBlob, 'hex'));

  for (const index of fact.blindIndices) {
    writeString(5, index);
  }

  writeDouble(6, fact.decayScore);
  writeVarintField(7, 1); // is_active = true
  writeVarintField(8, PROTOBUF_VERSION_V4); // version = 4 (Memory Taxonomy v1)
  // Field 9 (source) is NOT written — removed from the wire in v3; the
  // provenance lives inside the encrypted blob (field 4). PRD-04 F8 / DEP-6.
  writeString(10, fact.contentFp);
  // Field 11 (agent_id) is NOT written — same reason as field 9.
  // Field 12 (sequence_id) is assigned by the subgraph mapping, not the client.
  if (fact.encryptedEmbedding) {
    writeString(13, fact.encryptedEmbedding);
  }

  return Buffer.concat(parts);
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
