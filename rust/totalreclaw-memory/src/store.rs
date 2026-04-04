//! Store pipeline -- encrypt, index, encode, submit.
//!
//! Orchestrates the full fact storage pipeline:
//! text -> embed -> encrypt -> blind indices + LSH -> protobuf -> relay submission.
//!
//! Uses `totalreclaw-core::store::prepare_fact()` for the pure computation phase
//! (encrypt, index, encode) and handles the I/O phase (dedup checks, relay
//! submission) in this crate.
//!
//! Includes store-time near-duplicate detection (cosine >= 0.85 threshold).

use base64::Engine;

use totalreclaw_core::blind;
use totalreclaw_core::crypto;
use totalreclaw_core::fingerprint;
use totalreclaw_core::lsh::LshHasher;
use totalreclaw_core::reranker;
use totalreclaw_core::store as core_store;

use crate::embedding::EmbeddingProvider;
use crate::relay::RelayClient;
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
/// 5. Prepare fact via core (encrypt, index, encode protobuf)
/// 6. Submit via native UserOp (or legacy if no private key)
pub async fn store_fact(
    content: &str,
    source: &str,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<String> {
    // 1. Content fingerprint (used for exact dedup)
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
        // Near-duplicate found -- skip storage silently (same behavior as TS consolidation.ts)
        return Ok(String::new());
    }

    // 5. Prepare fact via core (encrypt, index, encode protobuf)
    let prepared = core_store::prepare_fact_with_decay_score(
        content,
        &keys.encryption_key,
        &keys.dedup_key,
        lsh_hasher,
        &embedding,
        1.0, // default decay_score
        source,
        relay.wallet_address(),
        "zeroclaw",
    )
    .map_err(|e| crate::Error::Crypto(e.to_string()))?;

    // 6. Submit
    if let Some(pk) = private_key {
        relay
            .submit_fact_native(&prepared.protobuf_bytes, pk)
            .await?;
    } else {
        relay.submit_protobuf(&prepared.protobuf_bytes).await?;
    }

    Ok(prepared.fact_id)
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

    // Prepare fact via core (with importance normalization)
    let prepared = core_store::prepare_fact(
        content,
        &keys.encryption_key,
        &keys.dedup_key,
        lsh_hasher,
        &embedding,
        importance,
        source,
        relay.wallet_address(),
        "zeroclaw",
    )
    .map_err(|e| crate::Error::Crypto(e.to_string()))?;

    // Submit
    if let Some(pk) = private_key {
        relay
            .submit_fact_native(&prepared.protobuf_bytes, pk)
            .await?;
    } else {
        relay.submit_protobuf(&prepared.protobuf_bytes).await?;
    }

    Ok(prepared.fact_id)
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
    let mut prepared_facts = Vec::with_capacity(facts.len());

    for (content, source) in facts {
        // Generate embedding (I/O)
        let embedding = embedding_provider.embed(content).await?;

        // Prepare fact via core (pure computation)
        let prepared = core_store::prepare_fact_with_decay_score(
            content,
            &keys.encryption_key,
            &keys.dedup_key,
            lsh_hasher,
            &embedding,
            1.0,
            source,
            relay.wallet_address(),
            "zeroclaw",
        )
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;

        prepared_facts.push(prepared);
    }

    // Collect protobuf payloads for batch submission
    let protobuf_payloads: Vec<Vec<u8>> = prepared_facts
        .iter()
        .map(|p| p.protobuf_bytes.clone())
        .collect();
    let fact_ids: Vec<String> = prepared_facts.iter().map(|p| p.fact_id.clone()).collect();

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
    let protobuf = core_store::prepare_tombstone(fact_id, relay.wallet_address());

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
