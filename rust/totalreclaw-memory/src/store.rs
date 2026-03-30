//! Store pipeline — encrypt, index, encode, submit.
//!
//! Orchestrates the full fact storage pipeline:
//! text → embed → encrypt → blind indices + LSH → protobuf → relay submission.

use base64::Engine;

use crate::blind;
use crate::crypto;
use crate::embedding::EmbeddingProvider;
use crate::fingerprint;
use crate::lsh::LshHasher;
use crate::protobuf::{self, FactPayload};
use crate::relay::RelayClient;
use crate::Result;

/// Store a fact on-chain via the relay.
///
/// Full pipeline:
/// 1. Generate embedding
/// 2. Encrypt content (AES-256-GCM)
/// 3. Encrypt embedding
/// 4. Generate blind indices (word + stem hashes)
/// 5. Generate LSH bucket hashes from embedding
/// 6. Generate content fingerprint (dedup)
/// 7. Encode protobuf
/// 8. Submit via relay
pub async fn store_fact(
    content: &str,
    source: &str,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
) -> Result<String> {
    // 1. Generate embedding
    let embedding = embedding_provider.embed(content).await?;

    // 2. Encrypt content
    let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
    // Convert base64 → raw bytes → hex for protobuf
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    // 3. Encrypt embedding (float32 → LE bytes → base64 → AES-GCM)
    let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
    let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
    let encrypted_embedding = crypto::encrypt(&emb_b64, &keys.encryption_key)?;

    // 4. Generate blind indices
    let mut blind_indices = blind::generate_blind_indices(content);

    // 5. Generate LSH bucket hashes
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets.into_iter());

    // 6. Content fingerprint
    let content_fp = fingerprint::generate_content_fingerprint(content, &keys.dedup_key);

    // 7. Build fact payload
    let fact_id = uuid::Uuid::now_v7().to_string();
    let timestamp = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let payload = FactPayload {
        id: fact_id.clone(),
        timestamp,
        owner: relay.wallet_address().to_string(),
        encrypted_blob_hex,
        blind_indices,
        decay_score: 1.0,
        source: source.to_string(),
        content_fp,
        agent_id: "zeroclaw".to_string(),
        encrypted_embedding: Some(encrypted_embedding),
    };

    // 8. Encode and submit
    let protobuf = protobuf::encode_fact_protobuf(&payload);
    relay.submit_protobuf(&protobuf).await?;

    Ok(fact_id)
}

/// Store a tombstone on-chain (soft-delete a fact).
pub async fn store_tombstone(
    fact_id: &str,
    relay: &RelayClient,
) -> Result<()> {
    let protobuf = protobuf::encode_tombstone_protobuf(fact_id, relay.wallet_address());
    relay.submit_protobuf(&protobuf).await?;
    Ok(())
}
