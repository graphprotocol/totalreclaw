//! `bind_reranker` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Reranker
// ---------------------------------------------------------------------------

/// Rerank candidates using BM25 + Cosine + RRF fusion (v0-compatible).
///
/// Args:
///     query: Search query text.
///     query_embedding: Query embedding vector (list of floats).
///     candidates_json: JSON array of ``{ id, text, embedding, timestamp, source? }`` objects.
///     top_k: Number of top results to return.
///
/// Returns:
///     JSON string of ranked results.
#[pyfunction]
#[pyo3(name = "rerank")]
pub(crate) fn py_rerank(
    query: &str,
    query_embedding: Vec<f32>,
    candidates_json: &str,
    top_k: usize,
) -> PyResult<String> {
    let candidates: Vec<reranker::Candidate> =
        serde_json::from_str(candidates_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let results = reranker::rerank(query, &query_embedding, &candidates, top_k)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Rerank candidates with a config flag (Retrieval v2 Tier 1).
///
/// When ``apply_source_weights`` is True, each candidate's final fused score is
/// multiplied by the provenance weight derived from its ``source`` field.
/// Legacy candidates without ``source`` receive the v0 fallback weight.
///
/// Args:
///     query: Search query text.
///     query_embedding: Query embedding vector (list of floats).
///     candidates_json: JSON array of ``{ id, text, embedding, timestamp, source? }`` objects.
///     top_k: Number of top results to return.
///     apply_source_weights: If True, apply v1 source weighting.
///
/// Returns:
///     JSON string of ranked results including ``source_weight``.
#[pyfunction]
#[pyo3(name = "rerank_with_config")]
pub(crate) fn py_rerank_with_config(
    query: &str,
    query_embedding: Vec<f32>,
    candidates_json: &str,
    top_k: usize,
    apply_source_weights: bool,
) -> PyResult<String> {
    let candidates: Vec<reranker::Candidate> =
        serde_json::from_str(candidates_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let config = reranker::RerankerConfig {
        apply_source_weights,
        bm25_weight_override: None,
        vector_weight_override: None,
    };
    let results = reranker::rerank_with_config(query, &query_embedding, &candidates, top_k, config)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Return the source weight multiplier for a given source string.
///
/// Accepted values: ``"user" | "user-inferred" | "assistant" | "external" | "derived"``.
///
/// Unknown input is routed through ``MemorySource::from_str_lossy`` which
/// falls back to ``user-inferred`` (weight 0.90). Callers who want the
/// "no source field at all" fallback (weight 0.85) should call
/// :func:`legacy_claim_fallback_weight` instead.
#[pyfunction]
#[pyo3(name = "source_weight")]
pub(crate) fn py_source_weight(source: &str) -> f64 {
    let src = crate::claims::MemorySource::from_str_lossy(source);
    reranker::source_weight(src)
}

/// Return the v1 legacy-claim fallback weight (applied to candidates that
/// have no ``source`` field).
#[pyfunction]
#[pyo3(name = "legacy_claim_fallback_weight")]
pub(crate) fn py_legacy_claim_fallback_weight() -> f64 {
    reranker::LEGACY_CLAIM_FALLBACK_WEIGHT
}

/// Validate a Memory Taxonomy v1 claim (JSON in, canonical JSON out).
///
/// Raises ``ValueError`` on any schema violation (wrong type token, missing
/// required field, unsupported schema_version).
#[pyfunction]
#[pyo3(name = "validate_memory_claim_v1")]
pub(crate) fn py_validate_memory_claim_v1(claim_json: &str) -> PyResult<String> {
    let claim: crate::claims::MemoryClaimV1 = serde_json::from_str(claim_json)
        .map_err(|e| PyValueError::new_err(format!("invalid v1 claim: {}", e)))?;
    if claim.schema_version != crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported schema_version {}: only {} is supported",
            claim.schema_version,
            crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION
        )));
    }
    serde_json::to_string(&claim)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Case-insensitive parse of a memory type string. Unknown input returns "claim".
#[pyfunction]
#[pyo3(name = "parse_memory_type_v1")]
pub(crate) fn py_parse_memory_type_v1(s: &str) -> String {
    let t = crate::claims::MemoryTypeV1::from_str_lossy(s);
    serde_json::to_string(&t)
        .unwrap_or_else(|_| "\"claim\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a memory source string. Unknown input returns "user-inferred".
#[pyfunction]
#[pyo3(name = "parse_memory_source")]
pub(crate) fn py_parse_memory_source(s: &str) -> String {
    let src = crate::claims::MemorySource::from_str_lossy(s);
    serde_json::to_string(&src)
        .unwrap_or_else(|_| "\"user-inferred\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a v1.1 pin_status string. Unknown input returns "unpinned".
#[pyfunction]
#[pyo3(name = "parse_pin_status")]
pub(crate) fn py_parse_pin_status(s: &str) -> String {
    let st = crate::claims::PinStatus::from_str_lossy(s);
    serde_json::to_string(&st)
        .unwrap_or_else(|_| "\"unpinned\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Check whether a JSON-encoded claim is pinned.
///
/// Recognises both the v0 short-key sentinel (``st == "p"``) and the v1.1
/// field (``pin_status == "pinned"``). Returns ``False`` on any parse failure.
#[pyfunction]
#[pyo3(name = "is_pinned_claim_json")]
pub(crate) fn py_is_pinned_claim_json(claim_json: &str) -> bool {
    crate::claims::is_pinned_json(claim_json)
}

/// Cosine similarity between two f32 vectors.
///
/// Args:
///     a: First vector (list of floats).
///     b: Second vector (list of floats).
///
/// Returns:
///     Cosine similarity as a float (0.0 to 1.0 for normalized vectors).
#[pyfunction]
#[pyo3(name = "cosine_similarity")]
pub(crate) fn py_cosine_similarity(a: Vec<f32>, b: Vec<f32>) -> f64 {
    reranker::cosine_similarity_f32(&a, &b)
}

// ---------------------------------------------------------------------------
// Pin tier / boost / intent (kg-2 / F1 Pin UX 2.2.8)
// ---------------------------------------------------------------------------

/// Compute the pin tier's multiplicative boost at a given timestamp.
///
/// Args:
///     tier_json: Internally-tagged JSON, e.g. ``{"tier":"soft","pinned_at":1716000000}``,
///         ``{"tier":"hard"}``, or ``{"tier":"none"}``.
///     now_unix: Seconds since epoch.
///     config_json: JSON of ``PinConfig``,
///         e.g. ``{"soft_half_life_days":90,"soft_max_boost":1.5,"hard_boost":1.5}``.
///
/// Returns:
///     Multiplicative boost factor (1.0 for ``none``).
#[pyfunction]
#[pyo3(name = "pin_boost")]
pub(crate) fn py_pin_boost(tier_json: &str, now_unix: i64, config_json: &str) -> PyResult<f64> {
    let tier: claims::PinTier = serde_json::from_str(tier_json)
        .map_err(|e| PyValueError::new_err(format!("invalid PinTier json: {}", e)))?;
    let config: claims::PinConfig = serde_json::from_str(config_json)
        .map_err(|e| PyValueError::new_err(format!("invalid PinConfig json: {}", e)))?;
    Ok(claims::pin_boost(tier, now_unix, &config))
}

/// Return the locked-default ``PinConfig`` as JSON.
#[pyfunction]
#[pyo3(name = "default_pin_config")]
pub(crate) fn py_default_pin_config() -> PyResult<String> {
    serde_json::to_string(&claims::PinConfig::default())
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Classify natural-language pin/unpin intent from a user utterance.
///
/// Returns:
///     JSON of ``PinIntent`` when a trigger phrase matches, or ``None`` when
///     no recognised pin gesture is present.
#[pyfunction]
#[pyo3(name = "classify_pin_intent")]
pub(crate) fn py_classify_pin_intent(text: &str) -> PyResult<Option<String>> {
    match pin_intent::classify_pin_intent(text) {
        Some(intent) => Ok(Some(
            serde_json::to_string(&intent).map_err(|e| PyValueError::new_err(e.to_string()))?,
        )),
        None => Ok(None),
    }
}

