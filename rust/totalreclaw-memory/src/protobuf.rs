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
        };
        let encoded = encode_fact_protobuf(&payload);
        assert!(!encoded.is_empty());
        assert!(encoded.windows(7).any(|w| w == b"test-id"));
    }
}
