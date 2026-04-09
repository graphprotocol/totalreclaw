//! Minimal protobuf encoder for TotalReclaw fact payloads.
//!
//! Hand-rolled wire format matching `mcp/src/subgraph/store.ts:encodeFactProtobuf()`.
//!
//! Field numbers match server/proto/totalreclaw.proto:
//!   1: id (string), 2: timestamp (string), 3: owner (string),
//!   4: encrypted_blob (bytes), 5: blind_indices (repeated string),
//!   6: decay_score (double), 7: is_active (bool), 8: version (int32),
//!   9: (removed in v3 — now encrypted inside field 4),
//!   10: content_fp (string),
//!   11: (removed in v3 — now encrypted inside field 4),
//!   12: sequence_id (int64, server-assigned), 13: encrypted_embedding (string)

/// A fact payload ready for protobuf encoding and on-chain submission.
#[derive(Debug, Clone)]
pub struct FactPayload {
    pub id: String,
    pub timestamp: String,
    pub owner: String,
    pub encrypted_blob_hex: String,
    pub blind_indices: Vec<String>,
    pub decay_score: f64,
    pub source: String,
    pub content_fp: String,
    pub agent_id: String,
    pub encrypted_embedding: Option<String>,
}

/// Encode a fact payload as minimal protobuf wire format.
pub fn encode_fact_protobuf(fact: &FactPayload) -> Vec<u8> {
    let mut buf = Vec::with_capacity(512);

    // Field 1: id (string)
    write_string(&mut buf, 1, &fact.id);
    // Field 2: timestamp (string)
    write_string(&mut buf, 2, &fact.timestamp);
    // Field 3: owner (string)
    write_string(&mut buf, 3, &fact.owner);
    // Field 4: encrypted_blob (bytes) — stored as hex, decode to raw bytes
    if let Ok(blob_bytes) = hex::decode(&fact.encrypted_blob_hex) {
        write_bytes(&mut buf, 4, &blob_bytes);
    }
    // Field 5: blind_indices (repeated string)
    for index in &fact.blind_indices {
        write_string(&mut buf, 5, index);
    }
    // Field 6: decay_score (double)
    write_double(&mut buf, 6, fact.decay_score);
    // Field 7: is_active (bool = varint 1)
    write_varint_field(&mut buf, 7, 1);
    // Field 8: version (int32 = varint 3)
    write_varint_field(&mut buf, 8, 3);
    // Fields 9 (source) and 11 (agent_id) removed in v3 — now encrypted inside field 4
    // Field 10: content_fp (string)
    write_string(&mut buf, 10, &fact.content_fp);
    // Field 12: sequence_id — assigned by subgraph, not set client-side
    // Field 13: encrypted_embedding (string)
    if let Some(ref emb) = fact.encrypted_embedding {
        write_string(&mut buf, 13, emb);
    }

    buf
}

/// Encode a tombstone protobuf for soft-deleting a fact.
pub fn encode_tombstone_protobuf(fact_id: &str, owner: &str) -> Vec<u8> {
    let mut buf = Vec::with_capacity(128);

    write_string(&mut buf, 1, fact_id);
    write_string(&mut buf, 2, &chrono::Utc::now().to_rfc3339());
    write_string(&mut buf, 3, owner);
    // Empty encrypted blob
    write_bytes(&mut buf, 4, &[]);
    // decay_score = 0 (tombstone signal)
    write_double(&mut buf, 6, 0.0);
    // is_active = false
    write_varint_field(&mut buf, 7, 0);
    write_varint_field(&mut buf, 8, 3);
    // Fields 9 (source) and 11 (agent_id) removed in v3

    buf
}

// ---------------------------------------------------------------------------
// Wire-format helpers
// ---------------------------------------------------------------------------

fn write_string(buf: &mut Vec<u8>, field: u32, value: &str) {
    if value.is_empty() {
        return;
    }
    let data = value.as_bytes();
    let key = (field << 3) | 2; // wire type 2 = length-delimited
    encode_varint(buf, key);
    encode_varint(buf, data.len() as u32);
    buf.extend_from_slice(data);
}

fn write_bytes(buf: &mut Vec<u8>, field: u32, value: &[u8]) {
    let key = (field << 3) | 2;
    encode_varint(buf, key);
    encode_varint(buf, value.len() as u32);
    buf.extend_from_slice(value);
}

fn write_double(buf: &mut Vec<u8>, field: u32, value: f64) {
    let key = (field << 3) | 1; // wire type 1 = 64-bit
    encode_varint(buf, key);
    buf.extend_from_slice(&value.to_le_bytes());
}

fn write_varint_field(buf: &mut Vec<u8>, field: u32, value: u32) {
    let key = (field << 3) | 0; // wire type 0 = varint
    encode_varint(buf, key);
    encode_varint(buf, value);
}

fn encode_varint(buf: &mut Vec<u8>, mut value: u32) {
    loop {
        if value <= 0x7f {
            buf.push(value as u8);
            break;
        }
        buf.push(((value & 0x7f) | 0x80) as u8);
        value >>= 7;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_varint_encoding() {
        let mut buf = Vec::new();
        encode_varint(&mut buf, 1);
        assert_eq!(buf, vec![1]);

        buf.clear();
        encode_varint(&mut buf, 300);
        assert_eq!(buf, vec![0xAC, 0x02]);
    }

    #[test]
    fn test_encode_fact_protobuf() {
        let payload = FactPayload {
            id: "test-id".into(),
            timestamp: "2026-01-01T00:00:00Z".into(),
            owner: "0xABCD".into(),
            encrypted_blob_hex: "deadbeef".into(),
            blind_indices: vec!["hash1".into(), "hash2".into()],
            decay_score: 0.8,
            source: "zeroclaw_fact".into(),
            content_fp: "fp123".into(),
            agent_id: "zeroclaw".into(),
            encrypted_embedding: None,
        };
        let encoded = encode_fact_protobuf(&payload);
        assert!(!encoded.is_empty());
        // Should contain the string "test-id" somewhere
        assert!(encoded.windows(7).any(|w| w == b"test-id"));
    }
}
