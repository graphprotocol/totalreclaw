//! Minimal protobuf encoder for TotalReclaw fact payloads.
//!
//! Delegates to `totalreclaw_core::protobuf` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::protobuf::*;

#[cfg(test)]
mod tests {
    use super::*;

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
            version: DEFAULT_PROTOBUF_VERSION,
        };
        let encoded = encode_fact_protobuf(&payload);
        assert!(!encoded.is_empty());
        assert!(encoded.windows(7).any(|w| w == b"test-id"));
    }

    #[test]
    fn test_encode_fact_protobuf_v4() {
        // v1 blobs use version = 4
        let payload = FactPayload {
            id: "v1-test".into(),
            timestamp: "2026-04-18T12:00:00Z".into(),
            owner: "0xABCD".into(),
            encrypted_blob_hex: "cafe".into(),
            blind_indices: vec![],
            decay_score: 0.9,
            source: "zeroclaw_v1_user".into(),
            content_fp: "fp_v1".into(),
            agent_id: "zeroclaw".into(),
            encrypted_embedding: None,
            version: PROTOBUF_VERSION_V4,
        };
        let encoded = encode_fact_protobuf(&payload);
        // Field 8 (version) tag byte = (8<<3)|0 = 0x40, value = 4
        assert!(encoded.windows(2).any(|w| w == [0x40, 4]));
    }

    #[test]
    fn test_encode_tombstone_v4() {
        let v3 = encode_tombstone_protobuf("t", "0xABCD", DEFAULT_PROTOBUF_VERSION);
        let v4 = encode_tombstone_protobuf("t", "0xABCD", PROTOBUF_VERSION_V4);
        assert!(v3.windows(2).any(|w| w == [0x40, 3]));
        assert!(v4.windows(2).any(|w| w == [0x40, 4]));
    }
}
