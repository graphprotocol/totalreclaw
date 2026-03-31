//! Store pipeline -- encrypt, index, encode, submit.
//!
//! Orchestrates the full fact storage pipeline:
//! text -> embed -> encrypt -> blind indices + LSH -> protobuf -> relay submission.

use base64::Engine;

use crate::blind;
use crate::crypto;
use crate::embedding::EmbeddingProvider;
use crate::fingerprint;
use crate::lsh::LshHasher;
use crate::protobuf::{self, FactPayload};
use crate::relay::RelayClient;
use crate::search;
use crate::Result;

/// Store a fact on-chain via native UserOp submission.
///
/// Full pipeline:
/// 1. Check for near-duplicates (content fingerprint)
/// 2. Generate embedding
/// 3. Encrypt content (AES-256-GCM)
/// 4. Encrypt embedding
/// 5. Generate blind indices (word + stem hashes)
/// 6. Generate LSH bucket hashes from embedding
/// 7. Generate content fingerprint (dedup)
/// 8. Encode protobuf
/// 9. Submit via native UserOp (or legacy if no private key)
pub async fn store_fact(
    content: &str,
    source: &str,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<String> {
    // 1. Content fingerprint (used for exact dedup AND in the protobuf)
    let content_fp = fingerprint::generate_content_fingerprint(content, &keys.dedup_key);

    // 2. Check for near-duplicate: search by content fingerprint
    //    If an existing fact has the same fingerprint, tombstone it (supersede).
    if let Ok(existing) =
        search::search_by_fingerprint(relay, relay.wallet_address(), &content_fp).await
    {
        if let Some(dup) = existing {
            // Exact duplicate found -- supersede it with a tombstone
            let _ = store_tombstone(&dup.id, relay, private_key).await;
        }
    }

    // 3. Generate embedding
    let embedding = embedding_provider.embed(content).await?;

    // 2. Encrypt content
    let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
    // Convert base64 -> raw bytes -> hex for protobuf
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    // 3. Encrypt embedding (float32 -> LE bytes -> base64 -> AES-GCM)
    let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
    let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
    let encrypted_embedding = crypto::encrypt(&emb_b64, &keys.encryption_key)?;

    // 4. Generate blind indices
    let mut blind_indices = blind::generate_blind_indices(content);

    // 5. Generate LSH bucket hashes
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets.into_iter());

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

    if let Some(pk) = private_key {
        relay.submit_fact_native(&protobuf, pk).await?;
    } else {
        relay.submit_protobuf(&protobuf).await?;
    }

    Ok(fact_id)
}

/// Store multiple facts in a single on-chain transaction (batched UserOp).
///
/// Gas savings: ~64% vs individual submissions for batch of 5.
/// Max batch size: 15 (matches extraction cap).
pub async fn store_fact_batch(
    facts: &[(&str, &str)], // (content, source) pairs
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: &[u8; 32],
) -> Result<Vec<String>> {
    let mut protobuf_payloads = Vec::with_capacity(facts.len());
    let mut fact_ids = Vec::with_capacity(facts.len());

    for (content, source) in facts {
        // Full pipeline per fact: embed -> encrypt -> indices -> protobuf
        let embedding = embedding_provider.embed(content).await?;
        let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
        let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
            .decode(&encrypted_blob_b64)
            .map_err(|e| crate::Error::Crypto(e.to_string()))?;
        let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

        let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
        let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
        let encrypted_embedding = crypto::encrypt(&emb_b64, &keys.encryption_key)?;

        let mut blind_indices = blind::generate_blind_indices(content);
        let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
        let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
        blind_indices.extend(lsh_buckets.into_iter());

        let content_fp = fingerprint::generate_content_fingerprint(content, &keys.dedup_key);

        let fact_id = uuid::Uuid::now_v7().to_string();
        let timestamp =
            chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

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

        protobuf_payloads.push(protobuf::encode_fact_protobuf(&payload));
        fact_ids.push(fact_id);
    }

    // Submit all as one batched UserOp
    relay
        .submit_fact_batch_native(&protobuf_payloads, private_key)
        .await?;

    Ok(fact_ids)
}

/// Store a tombstone on-chain (soft-delete a fact).
pub async fn store_tombstone(
    fact_id: &str,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<()> {
    let protobuf = protobuf::encode_tombstone_protobuf(fact_id, relay.wallet_address());

    if let Some(pk) = private_key {
        relay.submit_fact_native(&protobuf, pk).await?;
    } else {
        relay.submit_protobuf(&protobuf).await?;
    }
    Ok(())
}
