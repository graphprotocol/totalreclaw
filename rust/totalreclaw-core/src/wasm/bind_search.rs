//! `bind_search` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Search pipeline (feature-gated: managed)
// ---------------------------------------------------------------------------

/// Generate all search trapdoors for a query (word hashes + LSH bucket hashes).
///
/// `query`: The search query text.
/// `query_embedding`: Float32Array of the query embedding.
/// `lsh_hasher`: A `WasmLshHasher` instance.
///
/// Returns a JsValue (JSON array of hex-encoded trapdoor strings).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "generateSearchTrapdoors")]
pub fn wasm_generate_search_trapdoors(
    query: &str,
    query_embedding: &[f32],
    lsh_hasher: &WasmLshHasher,
) -> Result<JsValue, JsError> {
    let trapdoors = search::generate_search_trapdoors(query, query_embedding, &lsh_hasher.inner)
        .map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&trapdoors).map_err(|e| JsError::new(&e.to_string()))
}

/// Parse a blind index search GraphQL response into SubgraphFact list.
///
/// `response_json`: Raw JSON string from the GraphQL response.
/// Returns a JsValue (JSON array of SubgraphFact objects).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "parseSearchResponse")]
pub fn wasm_parse_search_response(response_json: &str) -> Result<JsValue, JsError> {
    let facts = search::parse_search_response(response_json).map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&facts).map_err(|e| JsError::new(&e.to_string()))
}

/// Parse a broadened search GraphQL response into SubgraphFact list.
///
/// `response_json`: Raw JSON string from the GraphQL response.
/// Returns a JsValue (JSON array of SubgraphFact objects).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "parseBroadenedResponse")]
pub fn wasm_parse_broadened_response(response_json: &str) -> Result<JsValue, JsError> {
    let facts = search::parse_broadened_response(response_json).map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&facts).map_err(|e| JsError::new(&e.to_string()))
}

/// Decrypt and rerank search candidates.
///
/// Takes raw SubgraphFacts (as JSON), decrypts their content + embeddings,
/// and returns top-K ranked results using BM25 + Cosine + RRF fusion.
///
/// `facts_json`: JSON array of SubgraphFact objects.
/// `query`: The search query text.
/// `query_embedding`: Float32Array of the query embedding.
/// `encryption_key_hex`: 64-char hex string (32 bytes).
/// `top_k`: Number of top results to return.
///
/// Returns a JsValue (JSON array of RankedResult objects).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "decryptAndRerank")]
pub fn wasm_decrypt_and_rerank(
    facts_json: &str,
    query: &str,
    query_embedding: &[f32],
    encryption_key_hex: &str,
    top_k: usize,
) -> Result<JsValue, JsError> {
    let facts: Vec<search::SubgraphFact> = serde_json::from_str(facts_json)
        .map_err(|e| JsError::new(&format!("Invalid SubgraphFact array JSON: {}", e)))?;
    let results =
        search::decrypt_and_rerank(&facts, query, query_embedding, encryption_key_hex, top_k)
            .map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&results).map_err(|e| JsError::new(&e.to_string()))
}

/// Get the GraphQL query string for blind index search.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getSearchQuery")]
pub fn wasm_get_search_query() -> String {
    search::search_query().to_string()
}

/// Get the GraphQL query string for broadened (fallback) search.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getBroadenedSearchQuery")]
pub fn wasm_get_broadened_search_query() -> String {
    search::broadened_search_query().to_string()
}

/// Get the GraphQL query string for paginated export.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getExportQuery")]
pub fn wasm_get_export_query() -> String {
    search::export_query().to_string()
}

/// Get the GraphQL query string for fact count.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getCountQuery")]
pub fn wasm_get_count_query() -> String {
    search::count_query().to_string()
}

/// Convert a subgraph hex blob to base64 for decryption.
///
/// `hex_blob`: Hex string (optionally `0x`-prefixed) from the subgraph.
/// Returns base64-encoded bytes, or null if the hex is invalid.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "hexBlobToBase64")]
pub fn wasm_hex_blob_to_base64(hex_blob: &str) -> Option<String> {
    search::hex_blob_to_base64(hex_blob)
}

/// Get the trapdoor batch size constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getTrapdoorBatchSize")]
pub fn wasm_trapdoor_batch_size() -> usize {
    search::TRAPDOOR_BATCH_SIZE
}

/// Get the page size constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getPageSize")]
pub fn wasm_page_size() -> usize {
    search::PAGE_SIZE
}

/// Generate trapdoors for multiple query reformulations (expansion pipeline).
///
/// `queries_json`: JSON array of query strings (original + reformulations).
/// `embeddings_json`: JSON array of Float32Array-compatible arrays (one per query).
/// `lsh_hasher`: A `WasmLshHasher` instance.
///
/// Returns a JsValue (JSON array of trapdoor-string arrays, one per query).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "generateExpansionTrapdoors")]
pub fn wasm_generate_expansion_trapdoors(
    queries_json: &str,
    embeddings_json: &str,
    lsh_hasher: &WasmLshHasher,
) -> Result<JsValue, JsError> {
    let queries: Vec<String> = serde_json::from_str(queries_json)
        .map_err(|e| JsError::new(&format!("Invalid queries JSON: {}", e)))?;
    let embeddings: Vec<Vec<f32>> = serde_json::from_str(embeddings_json)
        .map_err(|e| JsError::new(&format!("Invalid embeddings JSON: {}", e)))?;
    let query_refs: Vec<&str> = queries.iter().map(|s| s.as_str()).collect();
    let embedding_refs: Vec<&[f32]> = embeddings.iter().map(|v| v.as_slice()).collect();
    let result =
        search::generate_expansion_trapdoors(&query_refs, &embedding_refs, &lsh_hasher.inner)
            .map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&result).map_err(|e| JsError::new(&e.to_string()))
}

/// Merge multiple SubgraphFact sets from parallel query reformulations via RRF.
///
/// `fact_sets_json`: JSON array of SubgraphFact arrays (one array per reformulation).
/// `rrf_k`: RRF k-parameter (use 60.0 for default behaviour).
///
/// Returns a JsValue (merged, deduplicated SubgraphFact array sorted by RRF score).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "mergeExpansionResults")]
pub fn wasm_merge_expansion_results(fact_sets_json: &str, rrf_k: f64) -> Result<JsValue, JsError> {
    let fact_sets: Vec<Vec<search::SubgraphFact>> = serde_json::from_str(fact_sets_json)
        .map_err(|e| JsError::new(&format!("Invalid fact_sets JSON: {}", e)))?;
    let set_refs: Vec<&[search::SubgraphFact]> = fact_sets.iter().map(|v| v.as_slice()).collect();
    let config = search::ExpansionConfig { rrf_k };
    let merged = search::merge_expansion_results(&set_refs, &config);
    serde_wasm_bindgen::to_value(&merged).map_err(|e| JsError::new(&e.to_string()))
}

