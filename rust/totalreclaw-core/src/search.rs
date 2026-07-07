//! Search pipeline — pure computation phase (trapdoors, parse, decrypt, rerank).
//!
//! This module provides the I/O-free search pipeline. The host language handles
//! GraphQL I/O (relay queries), and this module handles:
//!
//!   1. Trapdoor generation (word blind indices + LSH bucket hashes)
//!   2. GraphQL response parsing (blindIndexes and facts array formats)
//!   3. Decryption of candidates (content + embeddings)
//!   4. Reranking via BM25 + Cosine + RRF fusion
//!
//! Feature-gated under `managed` since subgraph search is managed-service only.

use std::collections::HashMap;

use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::blind;
use crate::crypto;
use crate::lsh::LshHasher;
use crate::reranker::{self, Candidate, RankedResult};
use crate::Result;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Default number of trapdoors per GraphQL query batch.
pub const TRAPDOOR_BATCH_SIZE: usize = 5;

/// Default page size for GraphQL queries (Graph Studio limit = 1000).
pub const PAGE_SIZE: usize = 1000;

// ---------------------------------------------------------------------------
// GraphQL query strings
// ---------------------------------------------------------------------------

/// GraphQL query for blind index lookup.
const SEARCH_QUERY: &str = r#"
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: desc
    ) {
      id
      fact {
        id
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        createdAt
        isActive
        contentFp
      }
    }
  }
"#;

/// Broadened search query: fetch recent active facts by owner without trapdoor filtering.
/// Used as fallback when trapdoor search returns 0 candidates (e.g., vague queries).
const BROADENED_SEARCH_QUERY: &str = r#"
  query BroadenedSearch($owner: Bytes!, $first: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      isActive
      contentFp
    }
  }
"#;

/// Export all facts query.
const EXPORT_QUERY: &str = r#"
  query ExportFacts($owner: Bytes!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      skip: $skip
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      isActive
    }
  }
"#;

/// Fact count query.
const COUNT_QUERY: &str = r#"
  query FactCount($owner: Bytes!) {
    facts(where: { owner: $owner, isActive: true }, first: 1000) {
      id
    }
  }
"#;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// A raw fact from the subgraph GraphQL response.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubgraphFact {
    pub id: String,
    pub encrypted_blob: String,
    pub encrypted_embedding: Option<String>,
    pub decay_score: Option<String>,
    pub timestamp: Option<String>,
    pub created_at: Option<String>,
    pub is_active: Option<bool>,
    pub content_fp: Option<String>,
}

/// Internal: GraphQL response types for blind index search.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct BlindIndexEntry {
    #[allow(dead_code)]
    id: String,
    fact: Option<SubgraphFact>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct SearchData {
    blind_indexes: Option<Vec<BlindIndexEntry>>,
}

/// Internal: GraphQL response types for broadened search / export.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExportData {
    facts: Option<Vec<SubgraphFact>>,
}

// ---------------------------------------------------------------------------
// Query string accessors
// ---------------------------------------------------------------------------

/// Get the GraphQL query string for blind index search.
pub fn search_query() -> &'static str {
    SEARCH_QUERY
}

/// Get the GraphQL query string for broadened (fallback) search.
pub fn broadened_search_query() -> &'static str {
    BROADENED_SEARCH_QUERY
}

/// Get the GraphQL query string for paginated export.
pub fn export_query() -> &'static str {
    EXPORT_QUERY
}

/// Get the GraphQL query string for fact count.
pub fn count_query() -> &'static str {
    COUNT_QUERY
}

// ---------------------------------------------------------------------------
// Expansion pipeline config
// ---------------------------------------------------------------------------

/// Configuration for the query-expansion search pipeline.
///
/// The expansion pipeline lets the host generate 2-3 LLM reformulations of the
/// original query, run parallel trapdoor searches per reformulation, then merge
/// all result sets into one ranked list before decryption + reranking.
///
/// `rrf_k` controls the RRF merge step. The default (60.0) matches the value
/// used in the single-query reranker and agentmemory's reference implementation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExpansionConfig {
    /// RRF k-parameter for multi-query merge. Higher values dampen the
    /// influence of top-ranked documents. Default: 60.0.
    #[serde(default = "default_rrf_k")]
    pub rrf_k: f64,
}

fn default_rrf_k() -> f64 {
    60.0
}

impl Default for ExpansionConfig {
    fn default() -> Self {
        ExpansionConfig { rrf_k: 60.0 }
    }
}

// ---------------------------------------------------------------------------
// Trapdoor generation
// ---------------------------------------------------------------------------

/// Generate all search trapdoors for a query (word hashes + LSH bucket hashes).
///
/// Combines:
/// 1. Word blind indices (SHA-256 hashes of tokens + stems) from query text
/// 2. LSH bucket hashes from query embedding
///
/// The host batches these trapdoors (in groups of `TRAPDOOR_BATCH_SIZE`) and
/// sends each batch as a GraphQL query variable.
pub fn generate_search_trapdoors(
    query: &str,
    query_embedding: &[f32],
    lsh_hasher: &LshHasher,
) -> Result<Vec<String>> {
    // 1. Word blind indices
    let mut trapdoors = blind::generate_blind_indices(query);

    // 2. LSH bucket hashes
    let embedding_f64: Vec<f64> = query_embedding.iter().map(|&f| f as f64).collect();
    let lsh_buckets = lsh_hasher.hash(&embedding_f64)?;
    trapdoors.extend(lsh_buckets);

    Ok(trapdoors)
}

/// Generate trapdoors for multiple query reformulations in one call.
///
/// Convenience batch wrapper around `generate_search_trapdoors` for the
/// expansion pipeline. Returns one `Vec<String>` of trapdoors per input query.
/// `queries` and `query_embeddings` must have the same length.
pub fn generate_expansion_trapdoors(
    queries: &[&str],
    query_embeddings: &[&[f32]],
    lsh_hasher: &LshHasher,
) -> Result<Vec<Vec<String>>> {
    if queries.len() != query_embeddings.len() {
        return Err(crate::Error::InvalidInput(format!(
            "queries.len() ({}) != query_embeddings.len() ({})",
            queries.len(),
            query_embeddings.len()
        )));
    }
    queries
        .iter()
        .zip(query_embeddings.iter())
        .map(|(q, e)| generate_search_trapdoors(q, e, lsh_hasher))
        .collect()
}

/// Merge multiple ordered `SubgraphFact` lists from parallel query reformulations.
///
/// Each input set is an already-deduplicated, ordered slice of facts returned
/// from one reformulation query (position 0 = best match in that query).
/// Facts that appear across multiple sets accumulate RRF score contributions
/// and therefore surface higher in the merged output — this is the mechanism
/// that yields 2-3x richer recall vs. a single query.
///
/// The returned list is deduplicated (by fact id), sorted by descending RRF
/// score with a deterministic tie-break on id, and ready for `decrypt_and_rerank`.
///
/// Use `ExpansionConfig::default()` for standard k=60 RRF semantics.
pub fn merge_expansion_results(
    fact_sets: &[&[SubgraphFact]],
    config: &ExpansionConfig,
) -> Vec<SubgraphFact> {
    if fact_sets.is_empty() {
        return Vec::new();
    }
    if fact_sets.len() == 1 {
        return fact_sets[0].to_vec();
    }

    // Accumulate per-fact RRF scores and keep the first-seen copy of each fact.
    let mut scores: HashMap<String, f64> = HashMap::new();
    let mut facts_by_id: HashMap<String, SubgraphFact> = HashMap::new();

    for set in fact_sets {
        for (rank, fact) in set.iter().enumerate() {
            // 1-based rank: rank 0 in the slice → position 1 in the RRF formula.
            let rrf = 1.0 / (config.rrf_k + (rank + 1) as f64);
            *scores.entry(fact.id.clone()).or_insert(0.0) += rrf;
            facts_by_id.entry(fact.id.clone()).or_insert_with(|| fact.clone());
        }
    }

    // Sort by descending RRF score; tiebreak ascending by id for determinism.
    let mut scored: Vec<(String, f64)> = scores.into_iter().collect();
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });

    scored
        .into_iter()
        .filter_map(|(id, _)| facts_by_id.remove(&id))
        .collect()
}

// ---------------------------------------------------------------------------
// Response parsing
// ---------------------------------------------------------------------------

/// Parse a blind index search GraphQL response into a deduplicated SubgraphFact list.
///
/// Handles the `blindIndexes { id, fact { ... } }` response structure.
/// Deduplicates by fact ID and filters out inactive facts.
pub fn parse_search_response(response_json: &str) -> Result<Vec<SubgraphFact>> {
    let data: SearchData = serde_json::from_str(response_json)
        .map_err(|e| crate::Error::Parse(format!("failed to parse search response: {}", e)))?;

    let mut seen: HashMap<String, ()> = HashMap::new();
    let mut results = Vec::new();

    if let Some(entries) = data.blind_indexes {
        for entry in entries {
            if let Some(fact) = entry.fact {
                if fact.is_active != Some(false) && !seen.contains_key(&fact.id) {
                    seen.insert(fact.id.clone(), ());
                    results.push(fact);
                }
            }
        }
    }

    Ok(results)
}

/// Parse a broadened search or export GraphQL response into SubgraphFact list.
///
/// Handles the `facts [{ ... }]` response structure.
/// Filters out inactive facts.
pub fn parse_broadened_response(response_json: &str) -> Result<Vec<SubgraphFact>> {
    let data: ExportData = serde_json::from_str(response_json)
        .map_err(|e| crate::Error::Parse(format!("failed to parse broadened response: {}", e)))?;

    Ok(data
        .facts
        .unwrap_or_default()
        .into_iter()
        .filter(|f| f.is_active != Some(false))
        .collect())
}

// ---------------------------------------------------------------------------
// Hex <-> Base64 conversion
// ---------------------------------------------------------------------------

/// Convert a subgraph hex blob to base64 for decryption.
///
/// Subgraph returns `0x`-prefixed hex. Strip prefix, decode hex to bytes, base64-encode.
pub fn hex_blob_to_base64(hex_blob: &str) -> Option<String> {
    let hex_str = hex_blob.strip_prefix("0x").unwrap_or(hex_blob);
    let bytes = hex::decode(hex_str).ok()?;
    Some(base64::engine::general_purpose::STANDARD.encode(&bytes))
}

// ---------------------------------------------------------------------------
// Decrypt + Rerank pipeline
// ---------------------------------------------------------------------------

/// Extract text from a decrypted blob (handles JSON envelope or raw text).
///
/// JSON envelope format: `{"t": "text", "a": "agentId", "s": "source"}`
/// Falls back to treating the whole string as text for edge cases.
fn extract_text_from_blob(decrypted: &str) -> String {
    if let Ok(envelope) = serde_json::from_str::<serde_json::Value>(decrypted) {
        if let Some(text) = envelope.get("t").and_then(|v| v.as_str()) {
            return text.to_string();
        }
    }
    decrypted.to_string()
}

/// Decrypt and rerank search candidates.
///
/// This is the key pipeline function: takes raw `SubgraphFact` entries from
/// GraphQL, decrypts their content and embeddings, then reranks using
/// BM25 + Cosine + RRF fusion.
///
/// # Arguments
///
/// * `facts` - Raw facts from the subgraph (blind index search + broadened)
/// * `query` - The original search query text
/// * `query_embedding` - The query's embedding vector
/// * `encryption_key` - 32-byte XChaCha20-Poly1305 encryption key (hex-encoded)
/// * `top_k` - Number of top results to return
///
/// # Returns
///
/// Top-K results sorted by descending fused score.
pub fn decrypt_and_rerank(
    facts: &[SubgraphFact],
    query: &str,
    query_embedding: &[f32],
    encryption_key_hex: &str,
    top_k: usize,
) -> Result<Vec<RankedResult>> {
    if facts.is_empty() {
        return Ok(Vec::new());
    }

    // Parse encryption key
    let key_bytes = hex::decode(encryption_key_hex)
        .map_err(|e| crate::Error::InvalidInput(format!("invalid encryption key hex: {}", e)))?;
    if key_bytes.len() != 32 {
        return Err(crate::Error::InvalidInput(format!(
            "encryption key must be 32 bytes, got {}",
            key_bytes.len()
        )));
    }
    let mut encryption_key = [0u8; 32];
    encryption_key.copy_from_slice(&key_bytes);

    // Decrypt each candidate and build reranker input
    let mut candidates = Vec::new();
    for fact in facts {
        // Decrypt content: hex blob -> base64 -> XChaCha20-Poly1305 decrypt -> extract text from JSON envelope
        let blob_b64 = match hex_blob_to_base64(&fact.encrypted_blob) {
            Some(b) => b,
            None => continue, // skip unparseable blobs
        };
        let raw = match crypto::decrypt(&blob_b64, &encryption_key) {
            Ok(t) => t,
            Err(_) => continue, // skip undecryptable facts
        };
        let text = extract_text_from_blob(&raw);

        // Decrypt embedding (if available)
        let emb = decrypt_embedding(fact.encrypted_embedding.as_deref(), &encryption_key);

        candidates.push(Candidate {
            id: fact.id.clone(),
            text,
            embedding: emb,
            timestamp: fact.timestamp.clone().unwrap_or_default(),
            source: None,
        });
    }

    // Rerank all decrypted candidates
    reranker::rerank(query, query_embedding, &candidates, top_k)
}

/// Decrypt and rerank with a raw `[u8; 32]` key (for Rust callers).
///
/// Same as `decrypt_and_rerank` but accepts the key as bytes instead of hex.
pub fn decrypt_and_rerank_with_key(
    facts: &[SubgraphFact],
    query: &str,
    query_embedding: &[f32],
    encryption_key: &[u8; 32],
    top_k: usize,
) -> Result<Vec<RankedResult>> {
    if facts.is_empty() {
        return Ok(Vec::new());
    }

    let mut candidates = Vec::new();
    for fact in facts {
        let blob_b64 = match hex_blob_to_base64(&fact.encrypted_blob) {
            Some(b) => b,
            None => continue,
        };
        let raw = match crypto::decrypt(&blob_b64, encryption_key) {
            Ok(t) => t,
            Err(_) => continue,
        };
        let text = extract_text_from_blob(&raw);

        let emb = decrypt_embedding(fact.encrypted_embedding.as_deref(), encryption_key);

        candidates.push(Candidate {
            id: fact.id.clone(),
            text,
            embedding: emb,
            timestamp: fact.timestamp.clone().unwrap_or_default(),
            source: None,
        });
    }

    reranker::rerank(query, query_embedding, &candidates, top_k)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Decrypt an encrypted embedding.
///
/// Pipeline: XChaCha20-Poly1305 decrypt -> base64 decode -> chunks of 4 -> f32 LE.
fn decrypt_embedding(encrypted: Option<&str>, encryption_key: &[u8; 32]) -> Vec<f32> {
    encrypted
        .and_then(|e| crypto::decrypt(e, encryption_key).ok())
        .and_then(|b64| base64::engine::general_purpose::STANDARD.decode(&b64).ok())
        .map(|bytes| {
            bytes
                .chunks_exact(4)
                .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                .collect::<Vec<f32>>()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hex_blob_to_base64_with_prefix() {
        let hex_str = "0x48656c6c6f"; // "Hello" in hex
        let b64 = hex_blob_to_base64(hex_str).unwrap();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(&b64)
            .unwrap();
        assert_eq!(decoded, b"Hello");
    }

    #[test]
    fn test_hex_blob_to_base64_without_prefix() {
        let hex_str = "48656c6c6f";
        let b64 = hex_blob_to_base64(hex_str).unwrap();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(&b64)
            .unwrap();
        assert_eq!(decoded, b"Hello");
    }

    #[test]
    fn test_hex_blob_to_base64_invalid() {
        assert!(hex_blob_to_base64("0xZZZZ").is_none());
    }

    #[test]
    fn test_generate_search_trapdoors() {
        let keys = crate::crypto::derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();
        let lsh_seed = crate::crypto::derive_lsh_seed(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
            &keys.salt,
        )
        .unwrap();
        let lsh_hasher = LshHasher::new(&lsh_seed, 640).unwrap();

        let embedding = vec![0.5f32; 640];
        let trapdoors =
            generate_search_trapdoors("dark mode preference", &embedding, &lsh_hasher).unwrap();

        // Should have word hashes + stem hashes + 20 LSH bucket hashes
        assert!(
            trapdoors.len() > 20,
            "Should have word + stem + LSH trapdoors, got {}",
            trapdoors.len()
        );

        // All should be hex strings
        for t in &trapdoors {
            assert!(
                hex::decode(t).is_ok(),
                "Trapdoor should be valid hex: {}",
                t
            );
        }
    }

    #[test]
    fn test_parse_search_response() {
        let json = r#"{
            "blindIndexes": [
                {
                    "id": "idx1",
                    "fact": {
                        "id": "fact1",
                        "encryptedBlob": "0xdeadbeef",
                        "encryptedEmbedding": null,
                        "decayScore": "0.8",
                        "timestamp": "2026-01-01T00:00:00.000Z",
                        "isActive": true,
                        "contentFp": "abc123"
                    }
                },
                {
                    "id": "idx2",
                    "fact": {
                        "id": "fact1",
                        "encryptedBlob": "0xdeadbeef",
                        "encryptedEmbedding": null,
                        "decayScore": "0.8",
                        "timestamp": "2026-01-01T00:00:00.000Z",
                        "isActive": true,
                        "contentFp": "abc123"
                    }
                },
                {
                    "id": "idx3",
                    "fact": {
                        "id": "fact2",
                        "encryptedBlob": "0xcafebabe",
                        "encryptedEmbedding": null,
                        "decayScore": "0.5",
                        "timestamp": "2026-01-02T00:00:00.000Z",
                        "isActive": true,
                        "contentFp": "def456"
                    }
                }
            ]
        }"#;

        let facts = parse_search_response(json).unwrap();
        // fact1 appears twice but should be deduplicated
        assert_eq!(facts.len(), 2);
        assert_eq!(facts[0].id, "fact1");
        assert_eq!(facts[1].id, "fact2");
    }

    #[test]
    fn test_parse_search_response_filters_inactive() {
        let json = r#"{
            "blindIndexes": [
                {
                    "id": "idx1",
                    "fact": {
                        "id": "fact1",
                        "encryptedBlob": "0xdeadbeef",
                        "isActive": false,
                        "contentFp": null,
                        "decayScore": null,
                        "timestamp": null,
                        "encryptedEmbedding": null
                    }
                },
                {
                    "id": "idx2",
                    "fact": {
                        "id": "fact2",
                        "encryptedBlob": "0xcafebabe",
                        "isActive": true,
                        "contentFp": null,
                        "decayScore": null,
                        "timestamp": null,
                        "encryptedEmbedding": null
                    }
                }
            ]
        }"#;

        let facts = parse_search_response(json).unwrap();
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].id, "fact2");
    }

    #[test]
    fn test_parse_broadened_response() {
        let json = r#"{
            "facts": [
                {
                    "id": "fact1",
                    "encryptedBlob": "0xdeadbeef",
                    "encryptedEmbedding": null,
                    "decayScore": "0.8",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "isActive": true,
                    "contentFp": "abc123"
                },
                {
                    "id": "fact2",
                    "encryptedBlob": "0xcafebabe",
                    "encryptedEmbedding": null,
                    "decayScore": "0.5",
                    "timestamp": "2026-01-02T00:00:00.000Z",
                    "isActive": true,
                    "contentFp": "def456"
                }
            ]
        }"#;

        let facts = parse_broadened_response(json).unwrap();
        assert_eq!(facts.len(), 2);
    }

    #[test]
    fn test_parse_broadened_response_empty() {
        let json = r#"{ "facts": null }"#;
        let facts = parse_broadened_response(json).unwrap();
        assert!(facts.is_empty());
    }

    #[test]
    fn test_decrypt_and_rerank_empty() {
        let results =
            decrypt_and_rerank(&[], "query", &[0.5f32; 4], &hex::encode([0u8; 32]), 3).unwrap();
        assert!(results.is_empty());
    }

    #[test]
    fn test_decrypt_and_rerank_with_real_encryption() {
        use crate::crypto;
        use base64::Engine;

        let keys = crypto::derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();

        let encryption_key_hex = hex::encode(keys.encryption_key);

        // Create encrypted facts (JSON envelope format)
        let text1 = "User prefers dark mode in all applications";
        let text2 = "The weather is sunny today";

        let envelope1 = serde_json::json!({"t": text1, "a": "test", "s": "test"});
        let enc1 = crypto::encrypt(&envelope1.to_string(), &keys.encryption_key).unwrap();
        let enc1_bytes = base64::engine::general_purpose::STANDARD
            .decode(&enc1)
            .unwrap();
        let enc1_hex = format!("0x{}", hex::encode(&enc1_bytes));

        let envelope2 = serde_json::json!({"t": text2, "a": "test", "s": "test"});
        let enc2 = crypto::encrypt(&envelope2.to_string(), &keys.encryption_key).unwrap();
        let enc2_bytes = base64::engine::general_purpose::STANDARD
            .decode(&enc2)
            .unwrap();
        let enc2_hex = format!("0x{}", hex::encode(&enc2_bytes));

        let facts = vec![
            SubgraphFact {
                id: "fact1".to_string(),
                encrypted_blob: enc1_hex,
                encrypted_embedding: None,
                decay_score: Some("0.8".to_string()),
                timestamp: Some("2026-01-01T00:00:00.000Z".to_string()),
                created_at: None,
                is_active: Some(true),
                content_fp: None,
            },
            SubgraphFact {
                id: "fact2".to_string(),
                encrypted_blob: enc2_hex,
                encrypted_embedding: None,
                decay_score: Some("0.5".to_string()),
                timestamp: Some("2026-01-02T00:00:00.000Z".to_string()),
                created_at: None,
                is_active: Some(true),
                content_fp: None,
            },
        ];

        let query_embedding = vec![0.5f32; 4]; // Dummy embedding
        let results = decrypt_and_rerank(
            &facts,
            "dark mode",
            &query_embedding,
            &encryption_key_hex,
            2,
        )
        .unwrap();

        // Both facts should be decrypted and ranked
        assert_eq!(results.len(), 2);

        // The dark mode fact should rank higher for "dark mode" query (BM25 match)
        assert_eq!(results[0].text, text1);
        assert!(results[0].score >= results[1].score);
    }

    #[test]
    fn test_decrypt_and_rerank_skips_undecryptable() {
        let encryption_key_hex = hex::encode([0u8; 32]);

        let facts = vec![SubgraphFact {
            id: "bad_fact".to_string(),
            encrypted_blob: "0xdeadbeef".to_string(), // Not valid AES-GCM
            encrypted_embedding: None,
            decay_score: None,
            timestamp: None,
            created_at: None,
            is_active: Some(true),
            content_fp: None,
        }];

        let results =
            decrypt_and_rerank(&facts, "query", &[0.5f32; 4], &encryption_key_hex, 3).unwrap();

        // Undecryptable fact should be silently skipped
        assert!(results.is_empty());
    }

    #[test]
    fn test_query_strings_not_empty() {
        assert!(!search_query().is_empty());
        assert!(search_query().contains("blindIndexes"));
        assert!(!broadened_search_query().is_empty());
        assert!(broadened_search_query().contains("facts"));
        assert!(!export_query().is_empty());
        assert!(export_query().contains("skip"));
        assert!(!count_query().is_empty());
    }

    #[test]
    fn test_decrypt_and_rerank_with_key() {
        use crate::crypto;
        use base64::Engine;

        let keys = crypto::derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();

        let text = "User prefers dark mode";
        let envelope = serde_json::json!({"t": text, "a": "test", "s": "test"});
        let enc = crypto::encrypt(&envelope.to_string(), &keys.encryption_key).unwrap();
        let enc_bytes = base64::engine::general_purpose::STANDARD
            .decode(&enc)
            .unwrap();
        let enc_hex = format!("0x{}", hex::encode(&enc_bytes));

        let facts = vec![SubgraphFact {
            id: "fact1".to_string(),
            encrypted_blob: enc_hex,
            encrypted_embedding: None,
            decay_score: None,
            timestamp: Some("2026-01-01T00:00:00.000Z".to_string()),
            created_at: None,
            is_active: Some(true),
            content_fp: None,
        }];

        let results =
            decrypt_and_rerank_with_key(&facts, "dark mode", &[0.5f32; 4], &keys.encryption_key, 1)
                .unwrap();

        assert_eq!(results.len(), 1);
        assert_eq!(results[0].text, text);
    }

    #[test]
    fn test_extract_text_from_blob() {
        // JSON envelope
        let json = r#"{"t":"hello world","a":"agent","s":"source"}"#;
        assert_eq!(extract_text_from_blob(json), "hello world");

        // Raw string fallback
        assert_eq!(extract_text_from_blob("raw text"), "raw text");

        // JSON without "t" key falls back
        assert_eq!(extract_text_from_blob(r#"{"x":"y"}"#), r#"{"x":"y"}"#);
    }

    fn make_fact(id: &str) -> SubgraphFact {
        SubgraphFact {
            id: id.to_string(),
            encrypted_blob: "0xdeadbeef".to_string(),
            encrypted_embedding: None,
            decay_score: None,
            timestamp: None,
            created_at: None,
            is_active: Some(true),
            content_fp: None,
        }
    }

    #[test]
    fn test_expansion_config_default() {
        let cfg = ExpansionConfig::default();
        assert!((cfg.rrf_k - 60.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_merge_expansion_results_empty() {
        let merged = merge_expansion_results(&[], &ExpansionConfig::default());
        assert!(merged.is_empty());
    }

    #[test]
    fn test_merge_expansion_results_single_set() {
        let facts = vec![make_fact("a"), make_fact("b")];
        let sets: Vec<&[SubgraphFact]> = vec![&facts];
        let merged = merge_expansion_results(&sets, &ExpansionConfig::default());
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].id, "a");
        assert_eq!(merged[1].id, "b");
    }

    #[test]
    fn test_merge_expansion_results_deduplicates_across_sets() {
        // "shared" appears in both sets; "only_a" and "only_b" are unique.
        let set_a = vec![make_fact("shared"), make_fact("only_a")];
        let set_b = vec![make_fact("shared"), make_fact("only_b")];
        let sets: Vec<&[SubgraphFact]> = vec![&set_a, &set_b];
        let merged = merge_expansion_results(&sets, &ExpansionConfig::default());
        assert_eq!(merged.len(), 3, "should contain exactly 3 unique facts");
        let ids: Vec<&str> = merged.iter().map(|f| f.id.as_str()).collect();
        assert!(ids.contains(&"shared"), "shared fact must appear exactly once");
        assert!(ids.contains(&"only_a"));
        assert!(ids.contains(&"only_b"));
    }

    #[test]
    fn test_merge_expansion_results_shared_fact_ranks_first() {
        // A fact that appears first in both sets should score higher than one
        // that appears only once, even at rank 0 in its set.
        let set_a = vec![make_fact("shared"), make_fact("unique_a")];
        let set_b = vec![make_fact("shared"), make_fact("unique_b")];
        let sets: Vec<&[SubgraphFact]> = vec![&set_a, &set_b];
        let merged = merge_expansion_results(&sets, &ExpansionConfig::default());
        assert_eq!(merged[0].id, "shared");
    }

    #[test]
    fn test_merge_expansion_results_deterministic_tiebreak() {
        // Two facts each appear in only one set at rank 0 → equal RRF score.
        // Tiebreak must be ascending by id.
        let set_a = vec![make_fact("zzz")];
        let set_b = vec![make_fact("aaa")];
        let sets: Vec<&[SubgraphFact]> = vec![&set_a, &set_b];
        let merged = merge_expansion_results(&sets, &ExpansionConfig::default());
        assert_eq!(merged[0].id, "aaa");
        assert_eq!(merged[1].id, "zzz");
    }

    #[test]
    fn test_merge_expansion_results_rrf_k_parameter() {
        // Higher k dampens top-rank scores; with k=1 vs k=1000 the relative
        // ordering of a fact present in two sets vs one should be preserved.
        let set_a = vec![make_fact("both"), make_fact("one_only")];
        let set_b = vec![make_fact("both")];
        let sets: Vec<&[SubgraphFact]> = vec![&set_a, &set_b];
        for rrf_k in [1.0, 60.0, 1000.0] {
            let merged = merge_expansion_results(&sets, &ExpansionConfig { rrf_k });
            assert_eq!(merged[0].id, "both", "rrf_k={rrf_k}: cross-set fact must still rank first");
        }
    }

    #[test]
    fn test_generate_expansion_trapdoors_length_mismatch_errors() {
        let keys = crate::crypto::derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();
        let lsh_seed = crate::crypto::derive_lsh_seed(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
            &keys.salt,
        )
        .unwrap();
        let lsh_hasher = LshHasher::new(&lsh_seed, 640).unwrap();

        let queries = ["q1", "q2"];
        let embeddings: Vec<&[f32]> = vec![&[0.5f32; 640]]; // length mismatch
        assert!(
            generate_expansion_trapdoors(&queries, &embeddings, &lsh_hasher).is_err(),
            "mismatched lengths should return an error"
        );
    }

    #[test]
    fn test_generate_expansion_trapdoors_returns_one_vec_per_query() {
        let keys = crate::crypto::derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();
        let lsh_seed = crate::crypto::derive_lsh_seed(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
            &keys.salt,
        )
        .unwrap();
        let lsh_hasher = LshHasher::new(&lsh_seed, 640).unwrap();

        let emb = vec![0.5f32; 640];
        let queries = ["dark mode", "theme settings", "UI color scheme"];
        let embeddings: Vec<&[f32]> = vec![&emb, &emb, &emb];
        let result = generate_expansion_trapdoors(&queries, &embeddings, &lsh_hasher).unwrap();

        assert_eq!(result.len(), 3, "one trapdoor vec per query");
        for vec in &result {
            assert!(!vec.is_empty(), "each query should produce trapdoors");
        }
    }
}
