//! Store pipeline — pure computation phase (encrypt, index, encode).
//!
//! This module provides the I/O-free "prepare" phase of fact storage.
//! The host language handles all network I/O (relay submission, dedup checks).
//!
//! Pipeline:
//!   text + embedding -> encrypt -> blind indices + LSH -> protobuf -> PreparedFact
//!
//! The host then takes `PreparedFact.protobuf_bytes` and submits via relay.
//! For UserOp construction, the host uses `build_single_calldata()` or
//! `build_batch_calldata()` to wrap the protobuf in ABI-encoded calldata.

use base64::Engine;

use crate::blind;
use crate::crypto;
use crate::fingerprint;
use crate::lsh::LshHasher;
use crate::protobuf::{self, FactPayload};
#[cfg(feature = "managed")]
use crate::userop;
use crate::Result;

// ---------------------------------------------------------------------------
// PreparedFact
// ---------------------------------------------------------------------------

/// A fact prepared for on-chain storage (all pure computation done).
///
/// The host takes `protobuf_bytes` and submits via relay (legacy) or
/// wraps in a UserOp using `build_single_calldata()`.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PreparedFact {
    /// UUID v7 fact identifier.
    pub fact_id: String,
    /// RFC 3339 timestamp (millisecond precision).
    pub timestamp: String,
    /// Owner address (Smart Account or wallet).
    pub owner: String,
    /// Hex-encoded AES-256-GCM encrypted content.
    pub encrypted_blob_hex: String,
    /// Blind indices: SHA-256 word hashes + stem hashes + LSH bucket hashes.
    pub blind_indices: Vec<String>,
    /// Normalized importance score (0.0-1.0).
    pub decay_score: f64,
    /// Source tag (e.g. "zeroclaw_fact", "auto_extraction").
    pub source: String,
    /// HMAC-SHA256 content fingerprint for exact dedup.
    pub content_fp: String,
    /// Agent identifier.
    pub agent_id: String,
    /// Base64-encoded AES-256-GCM encrypted embedding (optional).
    pub encrypted_embedding: Option<String>,
    /// Raw protobuf bytes ready for relay submission or UserOp wrapping.
    pub protobuf_bytes: Vec<u8>,
}

// ---------------------------------------------------------------------------
// Public API — pure computation, no I/O
// ---------------------------------------------------------------------------

/// Prepare a fact for on-chain storage.
///
/// Pure computation: encrypt text, generate blind indices + LSH bucket hashes,
/// compute content fingerprint, encrypt embedding, encode protobuf.
///
/// Does NOT perform I/O — no relay calls, no dedup checks. The host handles
/// duplicate detection (fingerprint check, cosine dedup) and submission.
///
/// # Arguments
///
/// * `text` - Plaintext fact content.
/// * `encryption_key` - 32-byte AES-256-GCM encryption key.
/// * `dedup_key` - 32-byte HMAC-SHA256 dedup key.
/// * `lsh_hasher` - Pre-initialized LSH hasher (seeded from mnemonic).
/// * `embedding` - Pre-computed embedding vector (f32, e.g. 640d).
/// * `importance` - Importance score on 1-10 scale. Normalized to 0.0-1.0.
/// * `source` - Source tag (e.g. "auto_extraction", "explicit_remember").
/// * `owner` - Owner address (Smart Account address for managed service).
/// * `agent_id` - Agent identifier (e.g. "zeroclaw", "openclaw").
pub fn prepare_fact(
    text: &str,
    encryption_key: &[u8; 32],
    dedup_key: &[u8; 32],
    lsh_hasher: &LshHasher,
    embedding: &[f32],
    importance: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> Result<PreparedFact> {
    // 1. Generate fact_id (UUID v7 — time-sortable)
    let fact_id = uuid::Uuid::now_v7().to_string();

    // 2. Generate timestamp (RFC 3339, millisecond precision, UTC)
    let timestamp = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    // 3. Encrypt text -> base64 -> decode -> hex
    let encrypted_blob_b64 = crypto::encrypt(text, encryption_key)?;
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    // 4. Generate blind indices (word + stem hashes)
    let mut blind_indices = blind::generate_blind_indices(text);

    // 5. Generate LSH bucket hashes from embedding
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets);

    // 6. Generate content fingerprint
    let content_fp = fingerprint::generate_content_fingerprint(text, dedup_key);

    // 7. Encrypt embedding (float32 -> LE bytes -> base64 -> AES-GCM)
    let encrypted_embedding = encrypt_embedding(embedding, encryption_key)?;

    // 8. Normalize importance: input 1-10 -> stored 0.0-1.0
    let decay_score = (importance / 10.0).clamp(0.0, 1.0);

    // 9. Build FactPayload and encode protobuf
    let payload = FactPayload {
        id: fact_id.clone(),
        timestamp: timestamp.clone(),
        owner: owner.to_string(),
        encrypted_blob_hex: encrypted_blob_hex.clone(),
        blind_indices: blind_indices.clone(),
        decay_score,
        source: source.to_string(),
        content_fp: content_fp.clone(),
        agent_id: agent_id.to_string(),
        encrypted_embedding: Some(encrypted_embedding.clone()),
    };

    let protobuf_bytes = protobuf::encode_fact_protobuf(&payload);

    Ok(PreparedFact {
        fact_id,
        timestamp,
        owner: owner.to_string(),
        encrypted_blob_hex,
        blind_indices,
        decay_score,
        source: source.to_string(),
        content_fp,
        agent_id: agent_id.to_string(),
        encrypted_embedding: Some(encrypted_embedding),
        protobuf_bytes,
    })
}

/// Prepare a fact with a pre-normalized decay score (already 0.0-1.0).
///
/// Same as `prepare_fact()` but takes a raw decay_score instead of an
/// importance value on the 1-10 scale. Useful when the caller has already
/// normalized the importance (e.g. the memory crate's `store_fact()` which
/// defaults to decay_score=1.0).
pub fn prepare_fact_with_decay_score(
    text: &str,
    encryption_key: &[u8; 32],
    dedup_key: &[u8; 32],
    lsh_hasher: &LshHasher,
    embedding: &[f32],
    decay_score: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> Result<PreparedFact> {
    let fact_id = uuid::Uuid::now_v7().to_string();
    let timestamp = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let encrypted_blob_b64 = crypto::encrypt(text, encryption_key)?;
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    let mut blind_indices = blind::generate_blind_indices(text);
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets);

    let content_fp = fingerprint::generate_content_fingerprint(text, dedup_key);
    let encrypted_embedding = encrypt_embedding(embedding, encryption_key)?;

    let clamped_decay = decay_score.clamp(0.0, 1.0);

    let payload = FactPayload {
        id: fact_id.clone(),
        timestamp: timestamp.clone(),
        owner: owner.to_string(),
        encrypted_blob_hex: encrypted_blob_hex.clone(),
        blind_indices: blind_indices.clone(),
        decay_score: clamped_decay,
        source: source.to_string(),
        content_fp: content_fp.clone(),
        agent_id: agent_id.to_string(),
        encrypted_embedding: Some(encrypted_embedding.clone()),
    };

    let protobuf_bytes = protobuf::encode_fact_protobuf(&payload);

    Ok(PreparedFact {
        fact_id,
        timestamp,
        owner: owner.to_string(),
        encrypted_blob_hex,
        blind_indices,
        decay_score: clamped_decay,
        source: source.to_string(),
        content_fp,
        agent_id: agent_id.to_string(),
        encrypted_embedding: Some(encrypted_embedding),
        protobuf_bytes,
    })
}

/// Build ABI-encoded calldata for a single fact submission.
///
/// Wraps the protobuf bytes as `SimpleAccount.execute(DataEdge, 0, protobuf)`.
/// Returns the ABI-encoded calldata ready for a UserOp's `callData` field.
#[cfg(feature = "managed")]
pub fn build_single_calldata(prepared: &PreparedFact) -> Vec<u8> {
    userop::encode_single_call(&prepared.protobuf_bytes)
}

/// Build ABI-encoded calldata for a batch fact submission.
///
/// Wraps multiple protobuf payloads as `SimpleAccount.executeBatch(...)`.
/// Uses `execute()` for single-payload batches (no overhead).
/// Max batch size: 15 (matches extraction cap).
#[cfg(feature = "managed")]
pub fn build_batch_calldata(prepared: &[PreparedFact]) -> Result<Vec<u8>> {
    let payloads: Vec<Vec<u8>> = prepared.iter().map(|p| p.protobuf_bytes.clone()).collect();
    userop::encode_batch_call(&payloads)
}

/// Prepare a tombstone (soft-delete) protobuf for on-chain submission.
///
/// Returns raw protobuf bytes. The host wraps in a UserOp or submits directly.
pub fn prepare_tombstone(fact_id: &str, owner: &str) -> Vec<u8> {
    protobuf::encode_tombstone_protobuf(fact_id, owner)
}

/// Compute the content fingerprint for a text (for dedup checks).
///
/// Convenience wrapper so hosts don't need to import `fingerprint` separately.
pub fn compute_content_fingerprint(text: &str, dedup_key: &[u8; 32]) -> String {
    fingerprint::generate_content_fingerprint(text, dedup_key)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Encrypt an embedding vector with AES-256-GCM.
///
/// Pipeline: float32 -> LE bytes -> base64 -> AES-GCM encrypt -> base64.
/// Matches the TypeScript and memory crate implementations.
fn encrypt_embedding(embedding: &[f32], encryption_key: &[u8; 32]) -> Result<String> {
    let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
    let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
    crypto::encrypt(&emb_b64, encryption_key)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lsh::LshHasher;

    /// Test mnemonic (BIP-39 "abandon" mnemonic).
    const TEST_MNEMONIC: &str = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";

    fn test_keys() -> (crypto::DerivedKeys, LshHasher) {
        let keys = crypto::derive_keys_from_mnemonic(TEST_MNEMONIC).unwrap();
        let lsh_seed = crypto::derive_lsh_seed(TEST_MNEMONIC, &keys.salt).unwrap();
        let lsh_hasher = LshHasher::new(&lsh_seed, 640).unwrap();
        (keys, lsh_hasher)
    }

    fn dummy_embedding() -> Vec<f32> {
        // 640-dim unit vector (first component = 1.0, rest = 0.0)
        let mut emb = vec![0.0f32; 640];
        emb[0] = 1.0;
        emb
    }

    #[test]
    fn test_prepare_fact_returns_valid_struct() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        let prepared = prepare_fact(
            "User prefers dark mode",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            8.0,
            "auto_extraction",
            "0xABCD1234",
            "test_agent",
        )
        .unwrap();

        // Fact ID should be a valid UUID
        assert!(uuid::Uuid::parse_str(&prepared.fact_id).is_ok());

        // Timestamp should be valid RFC 3339
        assert!(prepared.timestamp.contains('T'));
        assert!(prepared.timestamp.ends_with('Z'));

        // Owner, source, agent_id preserved
        assert_eq!(prepared.owner, "0xABCD1234");
        assert_eq!(prepared.source, "auto_extraction");
        assert_eq!(prepared.agent_id, "test_agent");

        // Importance normalized: 8.0 / 10.0 = 0.8
        assert!((prepared.decay_score - 0.8).abs() < 1e-10);

        // Encrypted blob should be non-empty hex
        assert!(!prepared.encrypted_blob_hex.is_empty());
        assert!(hex::decode(&prepared.encrypted_blob_hex).is_ok());

        // Blind indices should include both word hashes and LSH bucket hashes
        // "user" (2 chars), "prefers" (7 chars), "dark" (4 chars), "mode" (4 chars) + stems + 20 LSH buckets
        assert!(prepared.blind_indices.len() > 20, "Should have word hashes + 20 LSH buckets");

        // Content fingerprint should be 64-char hex
        assert_eq!(prepared.content_fp.len(), 64);
        assert!(hex::decode(&prepared.content_fp).is_ok());

        // Encrypted embedding should be present
        assert!(prepared.encrypted_embedding.is_some());

        // Protobuf bytes should be non-empty
        assert!(!prepared.protobuf_bytes.is_empty());

        // Protobuf should contain the fact_id
        assert!(prepared.protobuf_bytes.windows(prepared.fact_id.len())
            .any(|w| w == prepared.fact_id.as_bytes()));
    }

    #[test]
    fn test_prepare_fact_importance_normalization() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        // Test various importance values
        let cases = vec![
            (0.0, 0.0),
            (1.0, 0.1),
            (5.0, 0.5),
            (8.0, 0.8),
            (10.0, 1.0),
            (15.0, 1.0),  // Clamped to 1.0
            (-5.0, 0.0),  // Clamped to 0.0
        ];

        for (importance, expected_decay) in cases {
            let prepared = prepare_fact(
                "test",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                importance,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            assert!(
                (prepared.decay_score - expected_decay).abs() < 1e-10,
                "importance {} should produce decay_score {}, got {}",
                importance,
                expected_decay,
                prepared.decay_score,
            );
        }
    }

    #[test]
    fn test_prepare_fact_with_decay_score() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        let prepared = prepare_fact_with_decay_score(
            "User prefers dark mode",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            1.0,
            "zeroclaw_fact",
            "0xABCD",
            "zeroclaw",
        )
        .unwrap();

        // decay_score should be passed through directly (not normalized)
        assert!((prepared.decay_score - 1.0).abs() < 1e-10);
    }

    #[test]
    fn test_prepare_fact_content_fingerprint_deterministic() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        let p1 = prepare_fact(
            "User prefers dark mode",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            8.0,
            "test",
            "0xABCD",
            "test",
        )
        .unwrap();

        let p2 = prepare_fact(
            "User prefers dark mode",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            8.0,
            "test",
            "0xABCD",
            "test",
        )
        .unwrap();

        // Content fingerprints must be identical for same text
        assert_eq!(p1.content_fp, p2.content_fp);

        // But fact IDs and timestamps will differ (time-based UUID v7)
        assert_ne!(p1.fact_id, p2.fact_id);
    }

    #[test]
    fn test_prepare_fact_encrypted_content_decryptable() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        let prepared = prepare_fact(
            "Secret fact content",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            8.0,
            "test",
            "0xABCD",
            "test",
        )
        .unwrap();

        // Decrypt the encrypted blob back
        let encrypted_bytes = hex::decode(&prepared.encrypted_blob_hex).unwrap();
        let encrypted_b64 = base64::engine::general_purpose::STANDARD.encode(&encrypted_bytes);
        let decrypted = crypto::decrypt(&encrypted_b64, &keys.encryption_key).unwrap();
        assert_eq!(decrypted, "Secret fact content");
    }

    #[test]
    fn test_prepare_fact_encrypted_embedding_decryptable() {
        let (keys, lsh_hasher) = test_keys();
        let emb = dummy_embedding();

        let prepared = prepare_fact(
            "test",
            &keys.encryption_key,
            &keys.dedup_key,
            &lsh_hasher,
            &emb,
            8.0,
            "test",
            "0xABCD",
            "test",
        )
        .unwrap();

        // Decrypt the encrypted embedding
        let enc_emb = prepared.encrypted_embedding.as_ref().unwrap();
        let decrypted_b64 = crypto::decrypt(enc_emb, &keys.encryption_key).unwrap();
        let emb_bytes = base64::engine::general_purpose::STANDARD
            .decode(&decrypted_b64)
            .unwrap();
        let recovered: Vec<f32> = emb_bytes
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect();

        assert_eq!(recovered.len(), 640);
        assert!((recovered[0] - 1.0).abs() < 1e-6);
        assert!((recovered[1] - 0.0).abs() < 1e-6);
    }

    #[test]
    fn test_prepare_tombstone() {
        let tombstone_bytes = prepare_tombstone("test-fact-id", "0xABCD");

        // Should be non-empty protobuf
        assert!(!tombstone_bytes.is_empty());

        // Should contain the fact_id
        assert!(tombstone_bytes
            .windows("test-fact-id".len())
            .any(|w| w == b"test-fact-id"));
    }

    #[test]
    fn test_compute_content_fingerprint() {
        let key = [0u8; 32];
        let fp = compute_content_fingerprint("hello world", &key);
        assert_eq!(fp.len(), 64);

        // Same text -> same fingerprint
        let fp2 = compute_content_fingerprint("hello world", &key);
        assert_eq!(fp, fp2);

        // Different text -> different fingerprint
        let fp3 = compute_content_fingerprint("hello mars", &key);
        assert_ne!(fp, fp3);
    }

    #[cfg(feature = "managed")]
    mod managed_tests {
        use super::*;

        #[test]
        fn test_build_single_calldata() {
            let (keys, lsh_hasher) = test_keys();
            let emb = dummy_embedding();

            let prepared = prepare_fact(
                "test fact",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                8.0,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            let calldata = build_single_calldata(&prepared);

            // Should start with execute() selector 0xb61d27f6
            assert_eq!(&calldata[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
            assert!(calldata.len() > 100);
        }

        #[test]
        fn test_build_batch_calldata() {
            let (keys, lsh_hasher) = test_keys();
            let emb = dummy_embedding();

            let p1 = prepare_fact(
                "fact one",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                8.0,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            let p2 = prepare_fact(
                "fact two",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                6.0,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            let calldata = build_batch_calldata(&[p1, p2]).unwrap();

            // Two payloads -> executeBatch() selector 0x47e1da2a
            assert_eq!(&calldata[..4], &[0x47, 0xe1, 0xda, 0x2a]);
        }

        #[test]
        fn test_build_batch_single_uses_execute() {
            let (keys, lsh_hasher) = test_keys();
            let emb = dummy_embedding();

            let p1 = prepare_fact(
                "single fact",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                8.0,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            let calldata = build_batch_calldata(&[p1]).unwrap();

            // Single payload should use execute(), not executeBatch()
            assert_eq!(&calldata[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
        }

        #[test]
        fn test_build_single_matches_direct_userop() {
            let (keys, lsh_hasher) = test_keys();
            let emb = dummy_embedding();

            let prepared = prepare_fact(
                "parity test",
                &keys.encryption_key,
                &keys.dedup_key,
                &lsh_hasher,
                &emb,
                8.0,
                "test",
                "0xABCD",
                "test",
            )
            .unwrap();

            // build_single_calldata should produce same result as userop::encode_single_call
            let via_store = build_single_calldata(&prepared);
            let via_userop = userop::encode_single_call(&prepared.protobuf_bytes);
            assert_eq!(via_store, via_userop);
        }
    }
}
