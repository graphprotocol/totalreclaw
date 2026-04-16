//! Consolidation and dedup logic for store-time near-duplicate detection.
//!
//! This module provides the core math functions used by clients to detect
//! near-duplicate facts before storing them. The orchestration (fetching
//! existing facts, comparing, submitting tombstones) stays in the client layer.
//!
//! # Constants
//!
//! - `STORE_DEDUP_COSINE_THRESHOLD` (0.85): Cosine similarity threshold for
//!   store-time near-duplicate detection. If a new fact's embedding is >= 0.85
//!   similar to an existing fact, it is considered a duplicate.
//! - `STORE_DEDUP_MAX_CANDIDATES` (50): Maximum number of existing facts to
//!   compare against during store-time dedup.
//! - `CONSOLIDATION_COSINE_THRESHOLD` (0.88): Cosine similarity threshold for
//!   bulk consolidation clustering.

use serde::{Deserialize, Serialize};

use crate::reranker;

/// Cosine similarity threshold for store-time near-duplicate detection.
pub const STORE_DEDUP_COSINE_THRESHOLD: f64 = 0.85;

/// Maximum number of existing facts to compare against during store-time dedup.
pub const STORE_DEDUP_MAX_CANDIDATES: usize = 50;

/// Cosine similarity threshold for bulk consolidation clustering.
pub const CONSOLIDATION_COSINE_THRESHOLD: f64 = 0.88;

/// Result of a best-match near-duplicate search.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DupMatch {
    /// ID of the matching existing fact.
    pub fact_id: String,
    /// Cosine similarity between the new and existing fact (0.0–1.0).
    pub similarity: f64,
}

/// A candidate fact for bulk consolidation clustering.
#[derive(Debug, Clone, Deserialize)]
pub struct ConsolidationCandidate {
    /// Unique fact identifier.
    pub id: String,
    /// Human-readable text of the fact.
    pub text: String,
    /// Embedding vector.
    pub embedding: Vec<f32>,
    /// Importance score (0.0–1.0).
    pub importance: f64,
    /// Decay score (0.0–1.0). Higher means more alive.
    pub decay_score: f64,
    /// Unix timestamp (seconds) when the fact was created.
    pub created_at: i64,
    /// Optional version counter (higher = more edits).
    pub version: Option<u32>,
}

/// A cluster produced by `cluster_facts`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ConsolidationCluster {
    /// ID of the representative fact (the one to keep).
    pub representative: String,
    /// IDs of duplicate facts in this cluster (excluding the representative).
    pub duplicates: Vec<String>,
}

/// Find the best (highest-similarity) near-duplicate among existing facts.
///
/// Unlike [`find_near_duplicate`] which returns the first match, this function
/// iterates ALL candidates and returns the one with the highest cosine
/// similarity above `threshold`.
///
/// # Arguments
/// - `new_embedding` — Embedding vector of the new fact.
/// - `existing` — Slice of (fact_id, embedding) pairs for existing facts.
/// - `threshold` — Cosine similarity threshold (default: 0.85).
///
/// # Returns
/// `Some(DupMatch)` with the best match, or `None` if nothing exceeds the threshold.
pub fn find_best_near_duplicate(
    new_embedding: &[f32],
    existing: &[(String, Vec<f32>)],
    threshold: f64,
) -> Option<DupMatch> {
    let mut best: Option<DupMatch> = None;

    for (fact_id, embedding) in existing {
        let similarity = reranker::cosine_similarity_f32(new_embedding, embedding);
        if similarity >= threshold {
            if best.as_ref().map_or(true, |b| similarity > b.similarity) {
                best = Some(DupMatch {
                    fact_id: fact_id.clone(),
                    similarity,
                });
            }
        }
    }

    best
}

/// Cluster a set of facts into groups of near-duplicates.
///
/// Uses greedy single-pass clustering: for each candidate, find the first
/// existing cluster whose representative has cosine similarity >= `threshold`.
/// If found, add to that cluster. Otherwise, start a new cluster.
///
/// # Arguments
/// - `candidates` — Slice of consolidation candidates.
/// - `threshold` — Cosine similarity threshold for grouping.
///
/// # Returns
/// A list of clusters. Each cluster has a representative (chosen by
/// [`pick_representative`]) and a list of duplicate IDs.
pub fn cluster_facts(
    candidates: &[ConsolidationCandidate],
    threshold: f64,
) -> Vec<ConsolidationCluster> {
    if candidates.is_empty() {
        return vec![];
    }

    // Each cluster is a Vec of indices into `candidates`.
    let mut clusters: Vec<Vec<usize>> = Vec::new();
    // Cache representative embeddings for fast lookup.
    let mut rep_embeddings: Vec<usize> = Vec::new(); // index into candidates for each cluster's first member

    for (i, candidate) in candidates.iter().enumerate() {
        let mut found_cluster = None;
        for (ci, &rep_idx) in rep_embeddings.iter().enumerate() {
            let sim = reranker::cosine_similarity_f32(
                &candidate.embedding,
                &candidates[rep_idx].embedding,
            );
            if sim >= threshold {
                found_cluster = Some(ci);
                break;
            }
        }

        match found_cluster {
            Some(ci) => clusters[ci].push(i),
            None => {
                clusters.push(vec![i]);
                rep_embeddings.push(i);
            }
        }
    }

    // Now pick representative for each cluster and build output.
    clusters
        .into_iter()
        .filter(|c| !c.is_empty())
        .map(|member_indices| {
            let cluster_candidates: Vec<&ConsolidationCandidate> =
                member_indices.iter().map(|&i| &candidates[i]).collect();
            let rep_id = pick_representative_from_refs(&cluster_candidates)
                .unwrap_or_else(|| cluster_candidates[0].id.clone());
            let duplicates: Vec<String> = cluster_candidates
                .iter()
                .filter(|c| c.id != rep_id)
                .map(|c| c.id.clone())
                .collect();
            ConsolidationCluster {
                representative: rep_id,
                duplicates,
            }
        })
        .collect()
}

/// Pick the best representative from a set of candidates.
///
/// Tiebreak order:
/// 1. Highest `decay_score` (most alive)
/// 2. Most recent `created_at` (newest)
/// 3. Longest `text` (most detailed)
///
/// # Returns
/// `Some(id)` of the best candidate, or `None` if the slice is empty.
pub fn pick_representative(candidates: &[ConsolidationCandidate]) -> Option<String> {
    let refs: Vec<&ConsolidationCandidate> = candidates.iter().collect();
    pick_representative_from_refs(&refs)
}

/// Internal helper that works on references.
fn pick_representative_from_refs(candidates: &[&ConsolidationCandidate]) -> Option<String> {
    if candidates.is_empty() {
        return None;
    }

    let mut best = candidates[0];
    for &c in &candidates[1..] {
        if c.decay_score > best.decay_score
            || (c.decay_score == best.decay_score && c.created_at > best.created_at)
            || (c.decay_score == best.decay_score
                && c.created_at == best.created_at
                && c.text.len() > best.text.len())
        {
            best = c;
        }
    }

    Some(best.id.clone())
}

/// Check if a new fact is a near-duplicate of any existing fact.
///
/// Compares the new fact's embedding against each existing fact's embedding
/// using cosine similarity. Returns the ID of the first match above `threshold`.
///
/// # Deprecation
///
/// Use [`find_best_near_duplicate`] instead, which returns the highest-similarity
/// match rather than the first match found.
///
/// # Arguments
/// - `new_embedding` — Embedding vector of the new fact.
/// - `existing` — Slice of (fact_id, embedding) pairs for existing facts.
/// - `threshold` — Cosine similarity threshold (default: 0.85).
///
/// # Returns
/// `Some(fact_id)` if a near-duplicate is found, `None` otherwise.
#[deprecated(since = "1.5.0", note = "Use find_best_near_duplicate instead, which returns the highest-similarity match")]
pub fn find_near_duplicate(
    new_embedding: &[f32],
    existing: &[(String, Vec<f32>)],
    threshold: f64,
) -> Option<String> {
    for (fact_id, embedding) in existing {
        let similarity = reranker::cosine_similarity_f32(new_embedding, embedding);
        if similarity >= threshold {
            return Some(fact_id.clone());
        }
    }
    None
}

/// Determine if a new fact should supersede an existing one.
///
/// A new fact supersedes an existing one when its importance is equal to or
/// greater than the existing fact's importance. This ensures that more recent
/// information at the same or higher importance level takes precedence.
///
/// # Arguments
/// - `new_importance` — Importance score of the new fact (0.0-1.0).
/// - `existing_importance` — Importance score of the existing fact (0.0-1.0).
///
/// # Returns
/// `true` if the new fact should replace the existing one.
pub fn should_supersede(new_importance: f64, existing_importance: f64) -> bool {
    new_importance >= existing_importance
}

// ---------------------------------------------------------------------------
// WASM bindings
// ---------------------------------------------------------------------------

/// Input format for existing facts in the WASM/PyO3 bindings.
#[derive(Deserialize)]
#[allow(dead_code)] // fields read via serde deserialization (used by wasm/python feature gates)
struct ExistingFactEntry {
    id: String,
    embedding: Vec<f32>,
}

/// WASM binding for `find_near_duplicate` (deprecated — use `findBestNearDuplicate`).
///
/// `new_embedding`: Float32Array of the new fact's embedding.
/// `existing_json`: JSON array of `{ id: string, embedding: number[] }` objects.
/// `threshold`: Cosine similarity threshold.
///
/// Returns `null` if no duplicate found, or a string (the duplicate fact ID).
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "findNearDuplicate")]
#[allow(deprecated)]
pub fn wasm_find_near_duplicate(
    new_embedding: &[f32],
    existing_json: &str,
    threshold: f64,
) -> Result<wasm_bindgen::JsValue, wasm_bindgen::JsError> {
    let entries: Vec<ExistingFactEntry> = serde_json::from_str(existing_json)
        .map_err(|e| wasm_bindgen::JsError::new(&format!("Invalid existing facts JSON: {}", e)))?;

    let existing: Vec<(String, Vec<f32>)> = entries
        .into_iter()
        .map(|e| (e.id, e.embedding))
        .collect();

    match find_near_duplicate(new_embedding, &existing, threshold) {
        Some(id) => Ok(wasm_bindgen::JsValue::from_str(&id)),
        None => Ok(wasm_bindgen::JsValue::NULL),
    }
}

/// WASM binding for `find_best_near_duplicate`.
///
/// `new_embedding_json`: JSON array of floats (embedding vector).
/// `existing_json`: JSON array of `{ id: string, embedding: number[] }` objects.
/// `threshold`: Cosine similarity threshold.
///
/// Returns JSON `{ fact_id: string, similarity: number }` or null.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "findBestNearDuplicate")]
pub fn wasm_find_best_near_duplicate(
    new_embedding_json: &str,
    existing_json: &str,
    threshold: f64,
) -> Result<wasm_bindgen::JsValue, wasm_bindgen::JsError> {
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| wasm_bindgen::JsError::new(&format!("Invalid new_embedding JSON: {}", e)))?;

    let entries: Vec<ExistingFactEntry> = serde_json::from_str(existing_json)
        .map_err(|e| wasm_bindgen::JsError::new(&format!("Invalid existing facts JSON: {}", e)))?;

    let existing: Vec<(String, Vec<f32>)> = entries
        .into_iter()
        .map(|e| (e.id, e.embedding))
        .collect();

    match find_best_near_duplicate(&new_embedding, &existing, threshold) {
        Some(dup) => {
            let json = serde_json::to_string(&dup)
                .map_err(|e| wasm_bindgen::JsError::new(&format!("Serialization error: {}", e)))?;
            Ok(wasm_bindgen::JsValue::from_str(&json))
        }
        None => Ok(wasm_bindgen::JsValue::NULL),
    }
}

/// WASM binding for `cluster_facts`.
///
/// `candidates_json`: JSON array of `ConsolidationCandidate` objects.
/// `threshold`: Cosine similarity threshold for clustering.
///
/// Returns JSON array of `{ representative: string, duplicates: string[] }`.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "clusterFacts")]
pub fn wasm_cluster_facts(
    candidates_json: &str,
    threshold: f64,
) -> Result<wasm_bindgen::JsValue, wasm_bindgen::JsError> {
    let candidates: Vec<ConsolidationCandidate> = serde_json::from_str(candidates_json)
        .map_err(|e| wasm_bindgen::JsError::new(&format!("Invalid candidates JSON: {}", e)))?;

    let clusters = cluster_facts(&candidates, threshold);
    let json = serde_json::to_string(&clusters)
        .map_err(|e| wasm_bindgen::JsError::new(&format!("Serialization error: {}", e)))?;
    Ok(wasm_bindgen::JsValue::from_str(&json))
}

/// WASM binding: get the store-time dedup cosine threshold constant.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "getStoreDedupCosineThreshold")]
pub fn wasm_store_dedup_cosine_threshold() -> f64 {
    STORE_DEDUP_COSINE_THRESHOLD
}

/// WASM binding: get the store-time dedup max candidates constant.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "getStoreDedupMaxCandidates")]
pub fn wasm_store_dedup_max_candidates() -> usize {
    STORE_DEDUP_MAX_CANDIDATES
}

/// WASM binding: get the consolidation cosine threshold constant.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "getConsolidationCosineThreshold")]
pub fn wasm_consolidation_cosine_threshold() -> f64 {
    CONSOLIDATION_COSINE_THRESHOLD
}

/// WASM binding: determine if a new fact should supersede an existing one.
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "shouldSupersede")]
pub fn wasm_should_supersede(new_importance: f64, existing_importance: f64) -> bool {
    should_supersede(new_importance, existing_importance)
}

// ---------------------------------------------------------------------------
// PyO3 bindings
// ---------------------------------------------------------------------------

/// PyO3 binding for `find_near_duplicate` (deprecated — use `find_best_near_duplicate`).
///
/// Args:
///     new_embedding: List of floats (embedding vector of the new fact).
///     existing_json: JSON array of `{"id": "...", "embedding": [...]}` objects.
///     threshold: Cosine similarity threshold.
///
/// Returns:
///     The duplicate fact ID (str), or None if no duplicate found.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "find_near_duplicate")]
#[allow(deprecated)]
fn py_find_near_duplicate(
    new_embedding: Vec<f32>,
    existing_json: &str,
    threshold: f64,
) -> pyo3::PyResult<Option<String>> {
    let entries: Vec<ExistingFactEntry> = serde_json::from_str(existing_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid JSON: {}", e)))?;

    let existing: Vec<(String, Vec<f32>)> = entries
        .into_iter()
        .map(|e| (e.id, e.embedding))
        .collect();

    Ok(find_near_duplicate(&new_embedding, &existing, threshold))
}

/// PyO3 binding for `find_best_near_duplicate`.
///
/// Args:
///     new_embedding_json: JSON array of floats (embedding vector).
///     existing_json: JSON array of `{"id": "...", "embedding": [...]}` objects.
///     threshold: Cosine similarity threshold.
///
/// Returns:
///     JSON string `{"fact_id": "...", "similarity": ...}` or None.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "find_best_near_duplicate")]
fn py_find_best_near_duplicate(
    new_embedding_json: &str,
    existing_json: &str,
    threshold: f64,
) -> pyo3::PyResult<Option<String>> {
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid new_embedding JSON: {}", e)))?;

    let entries: Vec<ExistingFactEntry> = serde_json::from_str(existing_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid existing JSON: {}", e)))?;

    let existing: Vec<(String, Vec<f32>)> = entries
        .into_iter()
        .map(|e| (e.id, e.embedding))
        .collect();

    match find_best_near_duplicate(&new_embedding, &existing, threshold) {
        Some(dup) => {
            let json = serde_json::to_string(&dup)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Serialization error: {}", e)))?;
            Ok(Some(json))
        }
        None => Ok(None),
    }
}

/// PyO3 binding for `cluster_facts`.
///
/// Args:
///     candidates_json: JSON array of `ConsolidationCandidate` objects.
///     threshold: Cosine similarity threshold for clustering.
///
/// Returns:
///     JSON string — array of `{"representative": "...", "duplicates": [...]}`.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "cluster_facts")]
fn py_cluster_facts(
    candidates_json: &str,
    threshold: f64,
) -> pyo3::PyResult<String> {
    let candidates: Vec<ConsolidationCandidate> = serde_json::from_str(candidates_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid candidates JSON: {}", e)))?;

    let clusters = cluster_facts(&candidates, threshold);
    serde_json::to_string(&clusters)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Serialization error: {}", e)))
}

/// PyO3 binding for `should_supersede`.
///
/// Args:
///     new_importance: Importance score of the new fact (0.0-1.0).
///     existing_importance: Importance score of the existing fact (0.0-1.0).
///
/// Returns:
///     True if the new fact should replace the existing one.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "should_supersede")]
fn py_should_supersede(new_importance: f64, existing_importance: f64) -> bool {
    should_supersede(new_importance, existing_importance)
}

/// Register consolidation functions on the PyO3 module.
///
/// Called from the main `python.rs` module registration.
#[cfg(feature = "python")]
pub fn register_python_functions(m: &pyo3::prelude::Bound<'_, pyo3::prelude::PyModule>) -> pyo3::PyResult<()> {
    use pyo3::prelude::*;
    m.add_function(pyo3::wrap_pyfunction!(py_find_near_duplicate, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_find_best_near_duplicate, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_cluster_facts, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_should_supersede, m)?)?;
    Ok(())
}

#[cfg(test)]
#[allow(deprecated)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------------
    // Legacy find_near_duplicate tests (kept for backward compat)
    // -----------------------------------------------------------------------

    #[test]
    fn test_find_near_duplicate_match() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.0f32, 1.0, 0.0, 0.0]), // orthogonal
            ("fact-2".to_string(), vec![0.99f32, 0.1, 0.0, 0.0]), // very similar
        ];

        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(result, Some("fact-2".to_string()));
    }

    #[test]
    fn test_find_near_duplicate_no_match() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.0f32, 1.0, 0.0, 0.0]), // orthogonal
            ("fact-2".to_string(), vec![0.0f32, 0.0, 1.0, 0.0]), // orthogonal
        ];

        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_none());
    }

    #[test]
    fn test_find_near_duplicate_empty() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing: Vec<(String, Vec<f32>)> = vec![];

        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_none());
    }

    #[test]
    fn test_find_near_duplicate_exact_match() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![1.0f32, 0.0, 0.0, 0.0]), // exact same
        ];

        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(result, Some("fact-1".to_string()));
    }

    #[test]
    fn test_find_near_duplicate_returns_first_match() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.98f32, 0.1, 0.0, 0.0]), // similar
            ("fact-2".to_string(), vec![0.99f32, 0.05, 0.0, 0.0]), // also similar
        ];

        // Should return first match, not the best match
        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(result, Some("fact-1".to_string()));
    }

    #[test]
    fn test_find_near_duplicate_custom_threshold() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.9f32, 0.44, 0.0, 0.0]), // cosine ~0.898
        ];

        // At 0.85 threshold, this should match
        assert!(find_near_duplicate(&new_emb, &existing, 0.85).is_some());

        // At 0.95 threshold, this should not match
        assert!(find_near_duplicate(&new_emb, &existing, 0.95).is_none());
    }

    #[test]
    fn test_find_near_duplicate_json_roundtrip() {
        // Test the JSON deserialization path used by WASM/PyO3 bindings
        let json = r#"[
            {"id": "fact-1", "embedding": [0.0, 1.0, 0.0, 0.0]},
            {"id": "fact-2", "embedding": [0.99, 0.1, 0.0, 0.0]}
        ]"#;

        let entries: Vec<ExistingFactEntry> = serde_json::from_str(json).unwrap();
        let existing: Vec<(String, Vec<f32>)> = entries
            .into_iter()
            .map(|e| (e.id, e.embedding))
            .collect();

        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(result, Some("fact-2".to_string()));
    }

    // -----------------------------------------------------------------------
    // find_best_near_duplicate tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_best_near_duplicate_returns_highest_similarity() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        // fact-1 is similar but fact-2 is MORE similar
        let existing = vec![
            ("fact-1".to_string(), vec![0.90f32, 0.44, 0.0, 0.0]),  // cosine ~0.898
            ("fact-2".to_string(), vec![0.99f32, 0.05, 0.0, 0.0]),  // cosine ~0.999
            ("fact-3".to_string(), vec![0.95f32, 0.31, 0.0, 0.0]),  // cosine ~0.951
        ];

        let result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_some());
        let dup = result.unwrap();
        assert_eq!(dup.fact_id, "fact-2");
        // fact-2 should have the highest similarity
        assert!(dup.similarity > 0.99);
    }

    #[test]
    fn test_best_near_duplicate_returns_none_below_threshold() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.0f32, 1.0, 0.0, 0.0]), // orthogonal
            ("fact-2".to_string(), vec![0.0f32, 0.0, 1.0, 0.0]), // orthogonal
        ];

        let result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_none());
    }

    #[test]
    fn test_best_near_duplicate_empty_existing() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing: Vec<(String, Vec<f32>)> = vec![];

        let result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_none());
    }

    #[test]
    fn test_best_near_duplicate_single_match() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.99f32, 0.1, 0.0, 0.0]),
        ];

        let result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert!(result.is_some());
        assert_eq!(result.unwrap().fact_id, "fact-1");
    }

    #[test]
    fn test_best_near_duplicate_differs_from_first_match() {
        // This is the key test: best != first
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let existing = vec![
            ("fact-1".to_string(), vec![0.90f32, 0.44, 0.0, 0.0]),  // first above threshold
            ("fact-2".to_string(), vec![0.99f32, 0.05, 0.0, 0.0]),  // better match
        ];

        // Old function returns first match
        let old_result = find_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(old_result, Some("fact-1".to_string()));

        // New function returns best match
        let new_result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(new_result.unwrap().fact_id, "fact-2");
    }

    // -----------------------------------------------------------------------
    // cluster_facts tests
    // -----------------------------------------------------------------------

    fn make_candidate(id: &str, embedding: Vec<f32>, decay_score: f64, created_at: i64, text: &str) -> ConsolidationCandidate {
        ConsolidationCandidate {
            id: id.to_string(),
            text: text.to_string(),
            embedding,
            importance: 0.5,
            decay_score,
            created_at,
            version: None,
        }
    }

    #[test]
    fn test_cluster_facts_groups_similar() {
        let candidates = vec![
            make_candidate("a", vec![1.0, 0.0, 0.0, 0.0], 1.0, 100, "fact a"),
            make_candidate("b", vec![0.99, 0.1, 0.0, 0.0], 0.9, 90, "fact b"),   // similar to a
            make_candidate("c", vec![0.0, 1.0, 0.0, 0.0], 1.0, 100, "fact c"),   // different
        ];

        let clusters = cluster_facts(&candidates, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(clusters.len(), 2); // {a, b} and {c}

        // Find the cluster containing "a"
        let cluster_ab = clusters.iter().find(|c| c.representative == "a" || c.duplicates.contains(&"a".to_string()));
        assert!(cluster_ab.is_some());
        let cluster_ab = cluster_ab.unwrap();
        // a has higher decay_score, so it's the representative
        assert_eq!(cluster_ab.representative, "a");
        assert_eq!(cluster_ab.duplicates, vec!["b".to_string()]);

        // c is its own cluster
        let cluster_c = clusters.iter().find(|c| c.representative == "c");
        assert!(cluster_c.is_some());
        assert!(cluster_c.unwrap().duplicates.is_empty());
    }

    #[test]
    fn test_cluster_facts_empty() {
        let clusters = cluster_facts(&[], STORE_DEDUP_COSINE_THRESHOLD);
        assert!(clusters.is_empty());
    }

    #[test]
    fn test_cluster_facts_all_unique() {
        let candidates = vec![
            make_candidate("a", vec![1.0, 0.0, 0.0, 0.0], 1.0, 100, "fact a"),
            make_candidate("b", vec![0.0, 1.0, 0.0, 0.0], 1.0, 100, "fact b"),
            make_candidate("c", vec![0.0, 0.0, 1.0, 0.0], 1.0, 100, "fact c"),
        ];

        let clusters = cluster_facts(&candidates, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(clusters.len(), 3);
        for cluster in &clusters {
            assert!(cluster.duplicates.is_empty());
        }
    }

    #[test]
    fn test_cluster_facts_all_duplicates() {
        let candidates = vec![
            make_candidate("a", vec![1.0, 0.0, 0.0, 0.0], 0.5, 100, "fact a"),
            make_candidate("b", vec![1.0, 0.0, 0.0, 0.0], 0.9, 200, "fact b"),
            make_candidate("c", vec![1.0, 0.0, 0.0, 0.0], 0.9, 100, "fact c"),
        ];

        let clusters = cluster_facts(&candidates, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(clusters.len(), 1);
        let cluster = &clusters[0];
        // b has highest decay_score (0.9) AND most recent created_at (200)
        assert_eq!(cluster.representative, "b");
        assert_eq!(cluster.duplicates.len(), 2);
    }

    // -----------------------------------------------------------------------
    // pick_representative tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_pick_representative_highest_decay() {
        let candidates = vec![
            make_candidate("a", vec![], 0.5, 100, "short"),
            make_candidate("b", vec![], 0.9, 50, "short"),
            make_candidate("c", vec![], 0.7, 200, "short"),
        ];

        assert_eq!(pick_representative(&candidates), Some("b".to_string()));
    }

    #[test]
    fn test_pick_representative_tiebreak_created_at() {
        let candidates = vec![
            make_candidate("a", vec![], 0.9, 100, "short"),
            make_candidate("b", vec![], 0.9, 200, "short"),
            make_candidate("c", vec![], 0.9, 50, "short"),
        ];

        assert_eq!(pick_representative(&candidates), Some("b".to_string()));
    }

    #[test]
    fn test_pick_representative_tiebreak_text_length() {
        let candidates = vec![
            make_candidate("a", vec![], 0.9, 100, "short"),
            make_candidate("b", vec![], 0.9, 100, "a much longer text for this fact"),
            make_candidate("c", vec![], 0.9, 100, "medium text"),
        ];

        assert_eq!(pick_representative(&candidates), Some("b".to_string()));
    }

    #[test]
    fn test_pick_representative_empty() {
        assert_eq!(pick_representative(&[]), None);
    }

    // -----------------------------------------------------------------------
    // Existing tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_should_supersede_higher() {
        assert!(should_supersede(0.9, 0.5));
    }

    #[test]
    fn test_should_supersede_equal() {
        assert!(should_supersede(0.8, 0.8));
    }

    #[test]
    fn test_should_supersede_lower() {
        assert!(!should_supersede(0.3, 0.7));
    }

    #[test]
    fn test_constants() {
        assert!((STORE_DEDUP_COSINE_THRESHOLD - 0.85).abs() < 1e-10);
        assert_eq!(STORE_DEDUP_MAX_CANDIDATES, 50);
        assert!((CONSOLIDATION_COSINE_THRESHOLD - 0.88).abs() < 1e-10);
    }

    #[test]
    fn test_consolidation_threshold_higher_than_dedup() {
        // Consolidation threshold should be stricter than store-time dedup
        assert!(CONSOLIDATION_COSINE_THRESHOLD > STORE_DEDUP_COSINE_THRESHOLD);
    }

    // -----------------------------------------------------------------------
    // JSON roundtrip tests for new bindings
    // -----------------------------------------------------------------------

    #[test]
    fn test_best_near_duplicate_json_roundtrip() {
        let new_emb = vec![1.0f32, 0.0, 0.0, 0.0];
        let json = r#"[
            {"id": "fact-1", "embedding": [0.0, 1.0, 0.0, 0.0]},
            {"id": "fact-2", "embedding": [0.99, 0.1, 0.0, 0.0]}
        ]"#;

        let entries: Vec<ExistingFactEntry> = serde_json::from_str(json).unwrap();
        let existing: Vec<(String, Vec<f32>)> = entries
            .into_iter()
            .map(|e| (e.id, e.embedding))
            .collect();

        let result = find_best_near_duplicate(&new_emb, &existing, STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(result.unwrap().fact_id, "fact-2");
    }

    #[test]
    fn test_consolidation_candidate_json_roundtrip() {
        let json = r#"[
            {"id": "a", "text": "fact a", "embedding": [1.0, 0.0], "importance": 0.5, "decay_score": 1.0, "created_at": 100, "version": null},
            {"id": "b", "text": "fact b", "embedding": [0.0, 1.0], "importance": 0.5, "decay_score": 0.9, "created_at": 90, "version": 2}
        ]"#;

        let candidates: Vec<ConsolidationCandidate> = serde_json::from_str(json).unwrap();
        assert_eq!(candidates.len(), 2);
        assert_eq!(candidates[0].id, "a");
        assert_eq!(candidates[1].version, Some(2));
    }

    #[test]
    fn test_dup_match_serialization() {
        let dup = DupMatch {
            fact_id: "fact-1".to_string(),
            similarity: 0.95,
        };
        let json = serde_json::to_string(&dup).unwrap();
        assert!(json.contains("fact-1"));
        assert!(json.contains("0.95"));

        let parsed: DupMatch = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, dup);
    }

    #[test]
    fn test_consolidation_cluster_serialization() {
        let cluster = ConsolidationCluster {
            representative: "a".to_string(),
            duplicates: vec!["b".to_string(), "c".to_string()],
        };
        let json = serde_json::to_string(&cluster).unwrap();
        let parsed: ConsolidationCluster = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed, cluster);
    }
}
