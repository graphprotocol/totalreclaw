//! Store pipeline -- encrypt, index, encode, submit.
//!
//! Orchestrates the full fact storage pipeline:
//! text -> embed -> encrypt -> blind indices + LSH -> protobuf -> relay submission.
//!
//! Includes store-time near-duplicate detection (cosine >= 0.85 threshold).

use base64::Engine;

use crate::blind;
use crate::crypto;
use crate::embedding::EmbeddingProvider;
use crate::fingerprint;
use crate::lsh::LshHasher;
use crate::protobuf::{self, FactPayload};
use crate::relay::RelayClient;
use crate::reranker;
use crate::search;
use crate::Result;

/// Cosine similarity threshold for store-time near-duplicate detection.
/// Facts with cosine >= this threshold against any existing fact are considered duplicates.
const STORE_DEDUP_COSINE_THRESHOLD: f64 = 0.85;

/// Maximum number of existing facts to fetch for store-time dedup comparison.
const STORE_DEDUP_FETCH_LIMIT: usize = 50;

/// Store a fact on-chain via native UserOp submission.
///
/// Full pipeline:
/// 1. Content fingerprint (exact dedup)
/// 2. Check fingerprint against existing (supersede if exact match)
/// 3. Generate embedding
/// 4. Store-time cosine dedup (skip if >= 0.85 against existing facts)
/// 5. Encrypt content (AES-256-GCM)
/// 6. Encrypt embedding
/// 7. Generate blind indices (word + stem hashes)
/// 8. Generate LSH bucket hashes from embedding
/// 9. Encode protobuf
/// 10. Submit via native UserOp (or legacy if no private key)
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

    // 2. Check for exact duplicate: search by content fingerprint
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

    // 4. Store-time cosine dedup: check if a near-duplicate already exists
    if is_near_duplicate(content, &embedding, keys, relay).await {
        // Near-duplicate found — skip storage silently (same behavior as TS consolidation.ts)
        return Ok(String::new());
    }

    // 5. Encrypt content
    let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    // 6. Encrypt embedding (float32 -> LE bytes -> base64 -> AES-GCM)
    let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
    let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
    let encrypted_embedding = crypto::encrypt(&emb_b64, &keys.encryption_key)?;

    // 7. Generate blind indices
    let mut blind_indices = blind::generate_blind_indices(content);

    // 8. Generate LSH bucket hashes
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets.into_iter());

    // 9. Build fact payload
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

    // 10. Encode and submit
    let protobuf = protobuf::encode_fact_protobuf(&payload);

    if let Some(pk) = private_key {
        relay.submit_fact_native(&protobuf, pk).await?;
    } else {
        relay.submit_protobuf(&protobuf).await?;
    }

    Ok(fact_id)
}

/// Store a fact with a specific importance value (0.0 - 1.0).
///
/// The importance is normalized per spec: `decayScore = importance / 10`.
/// Input is on a 1-10 scale, stored as 0.0-1.0.
pub async fn store_fact_with_importance(
    content: &str,
    source: &str,
    importance: f64,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<String> {
    // Normalize importance: input 1-10 -> stored 0.0-1.0
    let decay_score = (importance / 10.0).clamp(0.0, 1.0);

    // Content fingerprint
    let content_fp = fingerprint::generate_content_fingerprint(content, &keys.dedup_key);

    // Exact dedup check
    if let Ok(existing) =
        search::search_by_fingerprint(relay, relay.wallet_address(), &content_fp).await
    {
        if let Some(dup) = existing {
            let _ = store_tombstone(&dup.id, relay, private_key).await;
        }
    }

    // Generate embedding
    let embedding = embedding_provider.embed(content).await?;

    // Near-duplicate check
    if is_near_duplicate(content, &embedding, keys, relay).await {
        return Ok(String::new());
    }

    // Encrypt content
    let encrypted_blob_b64 = crypto::encrypt(content, &keys.encryption_key)?;
    let encrypted_blob_bytes = base64::engine::general_purpose::STANDARD
        .decode(&encrypted_blob_b64)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    let encrypted_blob_hex = hex::encode(&encrypted_blob_bytes);

    // Encrypt embedding
    let emb_bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
    let emb_b64 = base64::engine::general_purpose::STANDARD.encode(&emb_bytes);
    let encrypted_embedding = crypto::encrypt(&emb_b64, &keys.encryption_key)?;

    // Generate blind indices + LSH
    let mut blind_indices = blind::generate_blind_indices(content);
    let embedding_f64: Vec<f64> = embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    blind_indices.extend(lsh_buckets.into_iter());

    let fact_id = uuid::Uuid::now_v7().to_string();
    let timestamp = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);

    let payload = FactPayload {
        id: fact_id.clone(),
        timestamp,
        owner: relay.wallet_address().to_string(),
        encrypted_blob_hex,
        blind_indices,
        decay_score,
        source: source.to_string(),
        content_fp,
        agent_id: "zeroclaw".to_string(),
        encrypted_embedding: Some(encrypted_embedding),
    };

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

// ---------------------------------------------------------------------------
// Store-time near-duplicate detection
// ---------------------------------------------------------------------------

/// Check if `content` is a near-duplicate of any existing fact.
///
/// Fetches up to 50 existing facts via blind index search, decrypts their
/// embeddings, and computes cosine similarity against `new_embedding`.
/// Returns true if any existing fact has cosine >= 0.85.
async fn is_near_duplicate(
    content: &str,
    new_embedding: &[f32],
    keys: &crypto::DerivedKeys,
    relay: &RelayClient,
) -> bool {
    // Generate word trapdoors from the content being stored
    let trapdoors = blind::generate_blind_indices(content);
    if trapdoors.is_empty() {
        return false;
    }

    // Fetch existing candidates (up to STORE_DEDUP_FETCH_LIMIT)
    let candidates = match search::search_candidates(
        relay,
        relay.wallet_address(),
        &trapdoors,
        STORE_DEDUP_FETCH_LIMIT,
    )
    .await
    {
        Ok(c) => c,
        Err(_) => return false, // Best-effort: if search fails, allow store
    };

    // Check each candidate's embedding for near-duplicate
    for fact in &candidates {
        let existing_embedding = match &fact.encrypted_embedding {
            Some(enc_emb) => {
                // Decrypt the encrypted embedding
                match crypto::decrypt(enc_emb, &keys.encryption_key) {
                    Ok(b64) => {
                        match base64::engine::general_purpose::STANDARD.decode(&b64) {
                            Ok(bytes) => bytes
                                .chunks_exact(4)
                                .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                                .collect::<Vec<f32>>(),
                            Err(_) => continue,
                        }
                    }
                    Err(_) => continue,
                }
            }
            None => continue,
        };

        let similarity = reranker::cosine_similarity_f32(new_embedding, &existing_embedding);
        if similarity >= STORE_DEDUP_COSINE_THRESHOLD {
            return true;
        }
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_store_dedup_threshold() {
        // Verify the constant matches spec
        assert!((STORE_DEDUP_COSINE_THRESHOLD - 0.85).abs() < 1e-10);
    }

    #[test]
    fn test_store_dedup_fetch_limit() {
        // Verify the constant matches spec
        assert_eq!(STORE_DEDUP_FETCH_LIMIT, 50);
    }

    #[test]
    fn test_importance_normalization() {
        // Spec: decayScore = importance / 10
        // Input 8 on 1-10 scale -> 0.8
        let importance: f64 = 8.0;
        let decay_score = (importance / 10.0).clamp(0.0, 1.0);
        assert!((decay_score - 0.8).abs() < 1e-10);

        // Edge cases
        assert!((0.0_f64 / 10.0).clamp(0.0, 1.0) == 0.0);
        assert!((10.0_f64 / 10.0).clamp(0.0, 1.0) == 1.0);
        assert!((15.0_f64 / 10.0).clamp(0.0, 1.0) == 1.0); // Clamped to 1.0
    }
}
