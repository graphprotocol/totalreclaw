//! Store pipeline -- encrypt, index, encode, submit.
//!
//! Orchestrates the full fact storage pipeline:
//! text -> embed -> encrypt -> blind indices + LSH -> protobuf -> relay submission.
//!
//! Uses `totalreclaw-core::store::prepare_fact()` for the pure computation phase
//! (encrypt, index, encode) and handles the I/O phase (dedup checks, relay
//! submission) in this crate.
//!
//! Includes store-time near-duplicate detection via
//! `totalreclaw_core::consolidation::find_best_near_duplicate` (cosine >= 0.85).
//!
//! Phase 2 KG support: `store_claim_with_contradiction_check` runs the full
//! `totalreclaw_core::contradiction::resolve_with_candidates` pipeline against
//! pre-fetched candidates before storing a canonical `Claim`.

use base64::Engine;

use totalreclaw_core::blind;
use totalreclaw_core::claims::{
    Claim, MemoryClaimV1, MemoryScope, MemorySource, MemoryTypeV1, MemoryVolatility,
    ResolutionAction, MEMORY_CLAIM_V1_SCHEMA_VERSION,
};
use totalreclaw_core::consolidation;
use totalreclaw_core::contradiction;
use totalreclaw_core::crypto;
use totalreclaw_core::decision_log;
use totalreclaw_core::fingerprint;
use totalreclaw_core::lsh::LshHasher;
use totalreclaw_core::store as core_store;

use crate::embedding::EmbeddingProvider;
use crate::relay::RelayClient;
use crate::search;
use crate::Result;

/// Store a fact on-chain via native UserOp submission.
///
/// Full pipeline:
/// 1. Content fingerprint (exact dedup)
/// 2. Check fingerprint against existing (supersede if exact match)
/// 3. Generate embedding
/// 4. Store-time best-match dedup (supersede if cosine >= 0.85)
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

    // 4. Store-time best-match dedup: find highest-similarity near-duplicate
    if let Some(dup) = find_best_duplicate(content, &embedding, keys, relay).await {
        // Near-duplicate found -- tombstone and supersede (same behavior as TS consolidation.ts)
        let _ = store_tombstone(&dup.fact_id, relay, private_key).await;
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

    // Best-match near-duplicate check: supersede if found
    if let Some(dup) = find_best_duplicate(content, &embedding, keys, relay).await {
        let _ = store_tombstone(&dup.fact_id, relay, private_key).await;
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
///
/// Legacy (v3 outer protobuf). For Memory Taxonomy v1 vaults, prefer
/// `store_tombstone_v1()` so the tombstone protobuf carries `version = 4`.
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

/// Store a Memory Taxonomy v1 tombstone on-chain.
///
/// Emits `version = 4` on the outer protobuf so the subgraph indexes this
/// tombstone under the v1 taxonomy. Semantically identical to
/// `store_tombstone()`.
pub async fn store_tombstone_v1(
    fact_id: &str,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<()> {
    let protobuf = core_store::prepare_tombstone_v1(fact_id, relay.wallet_address());

    if let Some(pk) = private_key {
        relay.submit_fact_native(&protobuf, pk).await?;
    } else {
        relay.submit_protobuf(&protobuf).await?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Memory Taxonomy v1 store path
// ---------------------------------------------------------------------------

/// Input for building a v1 memory claim from ZeroClaw's high-level API.
///
/// The ZeroClaw Memory trait deals in `(key, content, category, session_id)`.
/// `V1StoreInput` is the adapter shape that maps those plus explicit v1
/// provenance (`source`, `scope`, `volatility`) onto the canonical
/// `MemoryClaimV1` the core write path expects.
#[derive(Debug, Clone)]
pub struct V1StoreInput {
    /// Plaintext body of the claim (5-512 UTF-8 chars).
    pub text: String,
    /// v1 memory type (claim | preference | directive | commitment |
    /// episode | summary).
    pub memory_type: MemoryTypeV1,
    /// v1 provenance source.
    pub source: MemorySource,
    /// Importance on the 1-10 scale. Normalized to 0.0-1.0 on-chain.
    pub importance: u8,
    /// Life-domain scope. Defaults to `Unspecified`.
    pub scope: MemoryScope,
    /// Stability signal. Defaults to `Updatable`.
    pub volatility: MemoryVolatility,
    /// Decision-with-reasoning clause (only meaningful for `type: claim`).
    pub reasoning: Option<String>,
}

impl V1StoreInput {
    /// Convenience constructor for a plain claim with default scope + volatility.
    pub fn new_claim(text: impl Into<String>, importance: u8) -> Self {
        Self {
            text: text.into(),
            memory_type: MemoryTypeV1::Claim,
            source: MemorySource::UserInferred,
            importance,
            scope: MemoryScope::Unspecified,
            volatility: MemoryVolatility::Updatable,
            reasoning: None,
        }
    }
}

/// Build a canonical `MemoryClaimV1` from a `V1StoreInput`.
///
/// Populates `id` (UUIDv7) and `created_at` (RFC 3339 UTC) and threads the
/// rest through verbatim. The resulting claim is the JSON envelope that
/// `prepare_fact_v1()` encrypts into the outer v4 protobuf.
pub fn build_memory_claim_v1(input: &V1StoreInput) -> MemoryClaimV1 {
    MemoryClaimV1 {
        id: uuid::Uuid::now_v7().to_string(),
        text: input.text.clone(),
        memory_type: input.memory_type,
        source: input.source,
        created_at: chrono::Utc::now()
            .to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        schema_version: MEMORY_CLAIM_V1_SCHEMA_VERSION.to_string(),
        scope: input.scope,
        volatility: input.volatility,
        entities: Vec::new(),
        reasoning: input.reasoning.clone(),
        expires_at: None,
        importance: Some(input.importance),
        confidence: None,
        superseded_by: None,
        // v1.1 additive field: pin_status is user-controlled via totalreclaw_pin.
        // Extraction-path claims start unpinned (field absent).
        pin_status: None,
    }
}

/// Store a Memory Taxonomy v1 claim on-chain.
///
/// Full v1 pipeline:
///  1. Build canonical `MemoryClaimV1`
///  2. Serialize to JSON envelope (the inner blob)
///  3. Content fingerprint exact-dedup check (tombstone v4 if match)
///  4. Generate embedding
///  5. Best-match near-duplicate dedup (tombstone v4 if match, cosine ≥ 0.85)
///  6. `core::prepare_fact_v1()` — encrypt envelope, build blind indices,
///     encrypt embedding, emit v4 protobuf
///  7. Submit via native UserOp (or legacy if no private key)
///
/// Returns the fact_id of the stored claim.
pub async fn store_fact_v1(
    input: &V1StoreInput,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
) -> Result<String> {
    // 1. Build canonical v1 claim
    let claim = build_memory_claim_v1(input);

    // 2. Serialize envelope
    let envelope_json = serde_json::to_string(&claim)
        .map_err(|e| crate::Error::Crypto(format!("v1 envelope serialize: {e}")))?;

    // 3. Exact-dedup via content fingerprint
    let content_fp = fingerprint::generate_content_fingerprint(&claim.text, &keys.dedup_key);
    if let Ok(Some(dup)) =
        search::search_by_fingerprint(relay, relay.wallet_address(), &content_fp).await
    {
        // v1 vaults emit v4 tombstones
        let _ = store_tombstone_v1(&dup.id, relay, private_key).await;
    }

    // 4. Generate embedding
    let embedding = embedding_provider.embed(&claim.text).await?;

    // 5. Best-match near-duplicate supersede
    if let Some(dup) = find_best_duplicate(&claim.text, &embedding, keys, relay).await {
        let _ = store_tombstone_v1(&dup.fact_id, relay, private_key).await;
    }

    // 6. Prepare v1 fact (encrypt envelope + v4 protobuf)
    //    Source tag for on-chain analytics uses the v1 provenance token
    //    (e.g. `zeroclaw_v1_user-inferred`).
    let source_tag = format!("zeroclaw_v1_{}", v1_source_to_str(input.source));
    let prepared = core_store::prepare_fact_v1(
        &envelope_json,
        &claim.text,
        &keys.encryption_key,
        &keys.dedup_key,
        lsh_hasher,
        &embedding,
        input.importance as f64,
        &source_tag,
        relay.wallet_address(),
        "zeroclaw",
    )
    .map_err(|e| crate::Error::Crypto(e.to_string()))?;

    // 7. Submit
    if let Some(pk) = private_key {
        relay.submit_fact_native(&prepared.protobuf_bytes, pk).await?;
    } else {
        relay.submit_protobuf(&prepared.protobuf_bytes).await?;
    }

    Ok(prepared.fact_id)
}

/// Render a `MemorySource` enum value to its kebab-case wire token.
fn v1_source_to_str(src: MemorySource) -> &'static str {
    match src {
        MemorySource::User => "user",
        MemorySource::UserInferred => "user-inferred",
        MemorySource::Assistant => "assistant",
        MemorySource::External => "external",
        MemorySource::Derived => "derived",
    }
}

// ---------------------------------------------------------------------------
// Store-time near-duplicate detection
// ---------------------------------------------------------------------------

/// Find the best near-duplicate among existing facts using core's
/// `find_best_near_duplicate` (returns highest-similarity match, not first).
///
/// Fetches up to `STORE_DEDUP_MAX_CANDIDATES` existing facts via blind index
/// search, decrypts their embeddings, and delegates to the core consolidation
/// module.
///
/// Returns `Some(DupMatch)` if a match above `STORE_DEDUP_COSINE_THRESHOLD`
/// is found, `None` otherwise.
async fn find_best_duplicate(
    content: &str,
    new_embedding: &[f32],
    keys: &crypto::DerivedKeys,
    relay: &RelayClient,
) -> Option<consolidation::DupMatch> {
    // Generate word trapdoors from the content being stored
    let trapdoors = blind::generate_blind_indices(content);
    if trapdoors.is_empty() {
        return None;
    }

    // Fetch existing candidates (up to core's STORE_DEDUP_MAX_CANDIDATES)
    let candidates = search::search_candidates(
        relay,
        relay.wallet_address(),
        &trapdoors,
        consolidation::STORE_DEDUP_MAX_CANDIDATES,
    )
    .await
    .ok()?;

    // Decrypt embeddings into (id, embedding) pairs for the core function
    let mut existing: Vec<(String, Vec<f32>)> = Vec::with_capacity(candidates.len());
    for fact in &candidates {
        let enc_emb = match &fact.encrypted_embedding {
            Some(e) => e,
            None => continue,
        };
        let b64 = match crypto::decrypt(enc_emb, &keys.encryption_key) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let bytes = match base64::engine::general_purpose::STANDARD.decode(&b64) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let emb: Vec<f32> = bytes
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect();
        existing.push((fact.id.clone(), emb));
    }

    consolidation::find_best_near_duplicate(
        new_embedding,
        &existing,
        consolidation::STORE_DEDUP_COSINE_THRESHOLD,
    )
}

// ---------------------------------------------------------------------------
// Phase 2 KG: Contradiction-aware store
// ---------------------------------------------------------------------------

/// Result of a contradiction-checked store operation.
#[derive(Debug)]
pub struct ContradictionStoreResult {
    /// The fact ID that was stored (or would be stored).
    pub fact_id: String,
    /// Resolution actions taken (supersede, skip, tie). Empty if no contradictions.
    pub actions: Vec<ResolutionAction>,
    /// Decision log entries generated for audit trail. Empty if no contradictions.
    pub decision_log_entries: Vec<decision_log::DecisionLogEntry>,
}

/// Store a claim with full Phase 2 contradiction detection.
///
/// This is the KG-aware store path. It:
/// 1. Runs content fingerprint exact-dedup (same as basic store)
/// 2. Generates embedding
/// 3. Runs `resolve_with_candidates` from core against pre-fetched candidates
/// 4. For `SupersedeExisting` actions: tombstones the existing claim
/// 5. For `SkipNew` actions: skips storing entirely
/// 6. For `TieLeaveBoth` or no contradictions: stores normally
/// 7. Returns decision log entries for the caller to persist
///
/// All I/O (candidate fetching, decryption) is done here in the adapter layer.
/// Pure contradiction logic is delegated to `totalreclaw_core::contradiction`.
pub async fn store_claim_with_contradiction_check(
    claim: &Claim,
    claim_id: &str,
    source: &str,
    importance: f64,
    keys: &crypto::DerivedKeys,
    lsh_hasher: &LshHasher,
    embedding_provider: &dyn EmbeddingProvider,
    relay: &RelayClient,
    private_key: Option<&[u8; 32]>,
    weights: &contradiction::ResolutionWeights,
    now_unix_seconds: i64,
) -> Result<ContradictionStoreResult> {
    let content = &claim.text;

    // 1. Content fingerprint exact-dedup
    let content_fp = fingerprint::generate_content_fingerprint(content, &keys.dedup_key);
    if let Ok(Some(dup)) =
        search::search_by_fingerprint(relay, relay.wallet_address(), &content_fp).await
    {
        let _ = store_tombstone(&dup.id, relay, private_key).await;
    }

    // 2. Generate embedding
    let embedding = embedding_provider.embed(content).await?;

    // 3. Fetch candidates for contradiction detection (by entity blind indices)
    let candidates = fetch_contradiction_candidates(
        claim,
        &embedding,
        keys,
        relay,
    )
    .await;

    // 4. Run core contradiction resolution
    let actions = contradiction::resolve_with_candidates(
        claim,
        claim_id,
        &embedding,
        &candidates,
        weights,
        contradiction::DEFAULT_LOWER_THRESHOLD,
        contradiction::DEFAULT_UPPER_THRESHOLD,
        now_unix_seconds,
        totalreclaw_core::claims::TIE_ZONE_SCORE_TOLERANCE,
    );

    // 5. Build decision log entries
    let existing_claims_json: std::collections::HashMap<String, String> = candidates
        .iter()
        .filter_map(|(c, id, _)| {
            serde_json::to_string(c).ok().map(|json| (id.clone(), json))
        })
        .collect();
    let new_claim_json = serde_json::to_string(claim).unwrap_or_default();
    let decision_log_entries = contradiction::build_decision_log_entries(
        &actions,
        &new_claim_json,
        &existing_claims_json,
        "active",
        now_unix_seconds,
    );

    // 6. Process actions
    let mut should_store = true;
    for action in &actions {
        match action {
            ResolutionAction::SupersedeExisting { existing_id, .. } => {
                // Tombstone the losing existing claim
                let _ = store_tombstone(existing_id, relay, private_key).await;
            }
            ResolutionAction::SkipNew { .. } => {
                // Existing claim wins or is pinned — do not store the new claim
                should_store = false;
                break;
            }
            ResolutionAction::TieLeaveBoth { .. } | ResolutionAction::NoContradiction => {
                // Keep both — store normally
            }
        }
    }

    if !should_store {
        return Ok(ContradictionStoreResult {
            fact_id: claim_id.to_string(),
            actions,
            decision_log_entries,
        });
    }

    // 7. Store the new claim (standard pipeline)
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

    if let Some(pk) = private_key {
        relay
            .submit_fact_native(&prepared.protobuf_bytes, pk)
            .await?;
    } else {
        relay.submit_protobuf(&prepared.protobuf_bytes).await?;
    }

    Ok(ContradictionStoreResult {
        fact_id: prepared.fact_id,
        actions,
        decision_log_entries,
    })
}

/// Fetch and decrypt candidate claims for contradiction detection.
///
/// Uses entity names from the new claim to generate blind index trapdoors,
/// then decrypts the returned facts into `(Claim, id, embedding)` tuples
/// that `resolve_with_candidates` expects.
async fn fetch_contradiction_candidates(
    new_claim: &Claim,
    _new_embedding: &[f32],
    keys: &crypto::DerivedKeys,
    relay: &RelayClient,
) -> Vec<(Claim, String, Vec<f32>)> {
    if new_claim.entities.is_empty() {
        return Vec::new();
    }

    // Generate trapdoors from entity names
    let mut trapdoors = Vec::new();
    for entity in &new_claim.entities {
        trapdoors.extend(blind::generate_blind_indices(&entity.name));
    }
    if trapdoors.is_empty() {
        return Vec::new();
    }

    // Fetch candidates from subgraph
    let facts = match search::search_candidates(
        relay,
        relay.wallet_address(),
        &trapdoors,
        decision_log::CONTRADICTION_CANDIDATE_CAP,
    )
    .await
    {
        Ok(f) => f,
        Err(_) => return Vec::new(),
    };

    // Decrypt and parse each candidate into (Claim, id, embedding)
    let mut candidates = Vec::new();
    for fact in &facts {
        // Decrypt content blob
        let blob_b64 = match search::hex_blob_to_base64(&fact.encrypted_blob) {
            Some(b) => b,
            None => continue,
        };
        let decrypted = match crypto::decrypt(&blob_b64, &keys.encryption_key) {
            Ok(t) => t,
            Err(_) => continue,
        };

        // Try to parse as a canonical Claim (KG facts store claims as the envelope)
        // Fall back: try parsing the "t" field from the standard envelope as a Claim
        let claim: Claim = if let Ok(c) = serde_json::from_str(&decrypted) {
            c
        } else if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&decrypted) {
            let text = obj.get("t").and_then(|v| v.as_str()).unwrap_or(&decrypted);
            match serde_json::from_str(text) {
                Ok(c) => c,
                Err(_) => continue, // Not a Claim — skip for contradiction detection
            }
        } else {
            continue;
        };

        // Decrypt embedding
        let emb = fact
            .encrypted_embedding
            .as_deref()
            .and_then(|e| crypto::decrypt(e, &keys.encryption_key).ok())
            .and_then(|b64| {
                base64::engine::general_purpose::STANDARD
                    .decode(&b64)
                    .ok()
            })
            .map(|bytes| {
                bytes
                    .chunks_exact(4)
                    .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                    .collect::<Vec<f32>>()
            })
            .unwrap_or_default();

        candidates.push((claim, fact.id.clone(), emb));
    }

    candidates
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_store_dedup_threshold_matches_core() {
        // Verify the core constant matches spec
        assert!(
            (consolidation::STORE_DEDUP_COSINE_THRESHOLD - 0.85).abs() < 1e-10
        );
    }

    #[test]
    fn test_store_dedup_fetch_limit_matches_core() {
        // Verify the core constant matches spec
        assert_eq!(consolidation::STORE_DEDUP_MAX_CANDIDATES, 50);
    }

    #[test]
    fn test_find_best_near_duplicate_selects_highest() {
        // Verify best-match behaviour: given two candidates above threshold,
        // the one with higher similarity wins.
        let new_emb: Vec<f32> = vec![1.0, 0.0, 0.0];
        let existing = vec![
            ("id_a".to_string(), vec![0.9, 0.1, 0.0]),  // lower similarity
            ("id_b".to_string(), vec![0.99, 0.01, 0.0]), // higher similarity
        ];

        let result =
            consolidation::find_best_near_duplicate(&new_emb, &existing, 0.5);
        assert!(result.is_some());
        let dup = result.unwrap();
        assert_eq!(dup.fact_id, "id_b");
        assert!(dup.similarity > 0.99);
    }

    #[test]
    fn test_find_best_near_duplicate_none_below_threshold() {
        let new_emb: Vec<f32> = vec![1.0, 0.0, 0.0];
        let existing = vec![
            ("id_a".to_string(), vec![0.0, 1.0, 0.0]), // orthogonal
        ];

        let result = consolidation::find_best_near_duplicate(
            &new_emb,
            &existing,
            consolidation::STORE_DEDUP_COSINE_THRESHOLD,
        );
        assert!(result.is_none());
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

    // -----------------------------------------------------------------------
    // Phase 2 KG: Core types accessible via this crate
    // -----------------------------------------------------------------------

    #[test]
    fn test_core_claim_types_accessible() {
        use totalreclaw_core::claims::{
            Claim, ClaimCategory, ClaimStatus, EntityRef, EntityType,
        };

        let claim = Claim {
            text: "Pedro uses ZeroClaw".to_string(),
            category: ClaimCategory::Fact,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "zeroclaw".to_string(),
            source_conversation: None,
            extracted_at: Some("2026-04-16T12:00:00Z".to_string()),
            entities: vec![EntityRef {
                name: "Pedro".to_string(),
                entity_type: EntityType::Person,
                role: Some("user".to_string()),
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        assert_eq!(claim.category, ClaimCategory::Fact);
        assert!(!totalreclaw_core::claims::is_pinned_claim(&claim));
    }

    #[test]
    fn test_pinned_claim_detection() {
        use totalreclaw_core::claims::{Claim, ClaimCategory, ClaimStatus};

        let mut claim = Claim {
            text: "pinned fact".to_string(),
            category: ClaimCategory::Fact,
            confidence: 1.0,
            importance: 10,
            corroboration_count: 1,
            source_agent: "totalreclaw_remember".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        assert!(!totalreclaw_core::claims::is_pinned_claim(&claim));

        claim.status = ClaimStatus::Pinned;
        assert!(totalreclaw_core::claims::is_pinned_claim(&claim));
    }

    #[test]
    fn test_resolve_with_candidates_no_entities() {
        use totalreclaw_core::claims::{Claim, ClaimCategory, ClaimStatus};

        let claim = Claim {
            text: "no entities here".to_string(),
            category: ClaimCategory::Fact,
            confidence: 0.9,
            importance: 7,
            corroboration_count: 1,
            source_agent: "zeroclaw".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![], // no entities => no contradictions possible
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };

        let emb = vec![1.0_f32; 3];
        let weights = contradiction::default_weights();
        let actions = contradiction::resolve_with_candidates(
            &claim,
            "new_id",
            &emb,
            &[], // no candidates
            &weights,
            contradiction::DEFAULT_LOWER_THRESHOLD,
            contradiction::DEFAULT_UPPER_THRESHOLD,
            1_776_384_000,
            totalreclaw_core::claims::TIE_ZONE_SCORE_TOLERANCE,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_decision_log_entry_round_trip() {
        let entry = decision_log::DecisionLogEntry {
            ts: 1_776_384_000,
            entity_id: "ent123".to_string(),
            new_claim_id: "0xnew".to_string(),
            existing_claim_id: "0xold".to_string(),
            similarity: 0.72,
            action: "supersede_existing".to_string(),
            reason: Some("new_wins".to_string()),
            winner_score: Some(0.73),
            loser_score: Some(0.40),
            winner_components: None,
            loser_components: None,
            loser_claim_json: None,
            mode: "active".to_string(),
        };
        let json = serde_json::to_string(&entry).unwrap();
        let back: decision_log::DecisionLogEntry = serde_json::from_str(&json).unwrap();
        assert_eq!(entry, back);
    }

    #[test]
    fn test_contradiction_candidate_cap() {
        assert_eq!(decision_log::CONTRADICTION_CANDIDATE_CAP, 20);
    }

    #[test]
    fn test_default_weights() {
        let w = contradiction::default_weights();
        let sum = w.confidence + w.corroboration + w.recency + w.validation;
        assert!((sum - 1.0).abs() < 1e-10, "weights should sum to 1.0");
    }

    #[test]
    fn test_tie_zone_tolerance() {
        assert!(
            (totalreclaw_core::claims::TIE_ZONE_SCORE_TOLERANCE - 0.01).abs() < 1e-10,
            "tie zone tolerance should be 0.01"
        );
    }
}
