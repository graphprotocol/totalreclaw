//! `bind_search` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Search pipeline (feature-gated: managed)
// ---------------------------------------------------------------------------

/// Generate all search trapdoors for a query (word hashes + LSH bucket hashes).
///
/// Args:
///     query: The search query text.
///     query_embedding: List of floats (query embedding vector).
///     lsh_hasher: A LshHasher instance.
///
/// Returns:
///     List of hex-encoded trapdoor strings.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "generate_search_trapdoors")]
pub(crate) fn py_generate_search_trapdoors(
    query: &str,
    query_embedding: Vec<f32>,
    lsh_hasher: &PyLshHasher,
) -> PyResult<Vec<String>> {
    search::generate_search_trapdoors(query, &query_embedding, &lsh_hasher.inner).map_err(to_pyerr)
}

/// Parse a blind index search GraphQL response into SubgraphFact list.
///
/// Args:
///     response_json: Raw JSON string from the GraphQL response.
///
/// Returns:
///     JSON string of SubgraphFact array.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "parse_search_response")]
pub(crate) fn py_parse_search_response(response_json: &str) -> PyResult<String> {
    let facts = search::parse_search_response(response_json).map_err(to_pyerr)?;
    serde_json::to_string(&facts)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Parse a broadened search GraphQL response into SubgraphFact list.
///
/// Args:
///     response_json: Raw JSON string from the GraphQL response.
///
/// Returns:
///     JSON string of SubgraphFact array.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "parse_broadened_response")]
pub(crate) fn py_parse_broadened_response(response_json: &str) -> PyResult<String> {
    let facts = search::parse_broadened_response(response_json).map_err(to_pyerr)?;
    serde_json::to_string(&facts)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Decrypt and rerank search candidates.
///
/// Args:
///     facts_json: JSON array of SubgraphFact objects.
///     query: The search query text.
///     query_embedding: List of floats (query embedding vector).
///     encryption_key: 32-byte encryption key.
///     top_k: Number of top results to return.
///
/// Returns:
///     JSON string of ranked results.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "decrypt_and_rerank")]
pub(crate) fn py_decrypt_and_rerank(
    facts_json: &str,
    query: &str,
    query_embedding: Vec<f32>,
    encryption_key: &[u8],
    top_k: usize,
) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    let facts: Vec<search::SubgraphFact> = serde_json::from_str(facts_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid SubgraphFact array JSON: {}", e)))?;
    let results = search::decrypt_and_rerank_with_key(&facts, query, &query_embedding, &key, top_k)
        .map_err(to_pyerr)?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Get the GraphQL query string for blind index search.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "get_search_query")]
pub(crate) fn py_get_search_query() -> &'static str {
    search::search_query()
}

/// Get the GraphQL query string for broadened (fallback) search.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "get_broadened_search_query")]
pub(crate) fn py_get_broadened_search_query() -> &'static str {
    search::broadened_search_query()
}

/// Get the GraphQL query string for paginated export.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "get_export_query")]
pub(crate) fn py_get_export_query() -> &'static str {
    search::export_query()
}

/// Convert a subgraph hex blob to base64 for decryption.
///
/// Args:
///     hex_blob: Hex string (optionally ``0x``-prefixed) from the subgraph.
///
/// Returns:
///     Base64-encoded bytes, or None if the hex is invalid.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "hex_blob_to_base64")]
pub(crate) fn py_hex_blob_to_base64(hex_blob: &str) -> Option<String> {
    search::hex_blob_to_base64(hex_blob)
}

/// Generate trapdoors for multiple query reformulations (expansion pipeline).
///
/// Args:
///     queries: List of query strings (original query + reformulations).
///     embeddings: List of embedding vectors (one list-of-floats per query).
///     lsh_hasher: A LshHasher instance.
///
/// Returns:
///     List of trapdoor-string lists, one per query.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "generate_expansion_trapdoors")]
pub(crate) fn py_generate_expansion_trapdoors(
    queries: Vec<String>,
    embeddings: Vec<Vec<f32>>,
    lsh_hasher: &PyLshHasher,
) -> PyResult<Vec<Vec<String>>> {
    let query_refs: Vec<&str> = queries.iter().map(|s| s.as_str()).collect();
    let embedding_refs: Vec<&[f32]> = embeddings.iter().map(|v| v.as_slice()).collect();
    search::generate_expansion_trapdoors(&query_refs, &embedding_refs, &lsh_hasher.inner)
        .map_err(to_pyerr)
}

/// Merge multiple SubgraphFact sets from parallel query reformulations via RRF.
///
/// Args:
///     fact_sets_json: JSON string containing an array of SubgraphFact arrays
///         (one array per reformulation, ordered by relevance).
///     rrf_k: RRF k-parameter. Use 60.0 for default behaviour.
///
/// Returns:
///     JSON string of the merged, deduplicated SubgraphFact array sorted by
///     descending RRF score.
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(name = "merge_expansion_results")]
pub(crate) fn py_merge_expansion_results(fact_sets_json: &str, rrf_k: f64) -> PyResult<String> {
    let fact_sets: Vec<Vec<search::SubgraphFact>> = serde_json::from_str(fact_sets_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid fact_sets JSON: {}", e)))?;
    let set_refs: Vec<&[search::SubgraphFact]> = fact_sets.iter().map(|v| v.as_slice()).collect();
    let config = search::ExpansionConfig { rrf_k };
    let merged = search::merge_expansion_results(&set_refs, &config);
    serde_json::to_string(&merged)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

