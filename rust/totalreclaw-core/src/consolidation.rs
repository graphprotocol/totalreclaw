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

use serde::Deserialize;

use crate::reranker;

/// Cosine similarity threshold for store-time near-duplicate detection.
pub const STORE_DEDUP_COSINE_THRESHOLD: f64 = 0.85;

/// Maximum number of existing facts to compare against during store-time dedup.
pub const STORE_DEDUP_MAX_CANDIDATES: usize = 50;

/// Cosine similarity threshold for bulk consolidation clustering.
pub const CONSOLIDATION_COSINE_THRESHOLD: f64 = 0.88;

/// Check if a new fact is a near-duplicate of any existing fact.
///
/// Compares the new fact's embedding against each existing fact's embedding
/// using cosine similarity. Returns the ID of the first match above `threshold`.
///
/// # Arguments
/// - `new_embedding` — Embedding vector of the new fact.
/// - `existing` — Slice of (fact_id, embedding) pairs for existing facts.
/// - `threshold` — Cosine similarity threshold (default: 0.85).
///
/// # Returns
/// `Some(fact_id)` if a near-duplicate is found, `None` otherwise.
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

/// WASM binding for `find_near_duplicate`.
///
/// `new_embedding`: Float32Array of the new fact's embedding.
/// `existing_json`: JSON array of `{ id: string, embedding: number[] }` objects.
/// `threshold`: Cosine similarity threshold.
///
/// Returns `null` if no duplicate found, or a string (the duplicate fact ID).
#[cfg(feature = "wasm")]
#[wasm_bindgen::prelude::wasm_bindgen(js_name = "findNearDuplicate")]
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

/// PyO3 binding for `find_near_duplicate`.
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
    m.add_function(pyo3::wrap_pyfunction!(py_should_supersede, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn test_consolidation_threshold_higher_than_dedup() {
        // Consolidation threshold should be stricter than store-time dedup
        assert!(CONSOLIDATION_COSINE_THRESHOLD > STORE_DEDUP_COSINE_THRESHOLD);
    }
}
