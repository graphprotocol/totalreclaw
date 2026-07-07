//! `bind_reranker` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Reranker
// ---------------------------------------------------------------------------

/// Rerank candidates using BM25 + Cosine + RRF fusion.
///
/// `candidates_json`: JSON array of `{ id, text, embedding, timestamp, source? }` objects.
/// Returns a JsValue (array of `RankedResult` objects).
#[wasm_bindgen(js_name = "rerank")]
pub fn wasm_rerank(
    query: &str,
    query_embedding: &[f32],
    candidates_json: &str,
    top_k: usize,
) -> Result<JsValue, JsError> {
    let candidates: Vec<reranker::Candidate> = serde_json::from_str(candidates_json)
        .map_err(|e| JsError::new(&format!("Invalid candidates JSON: {}", e)))?;
    let results = reranker::rerank(query, query_embedding, &candidates, top_k)
        .map_err(|e| JsError::new(&e.to_string()))?;
    serde_wasm_bindgen::to_value(&results).map_err(|e| JsError::new(&e.to_string()))
}

/// Rerank candidates with a config flag (Retrieval v2 Tier 1).
///
/// When `apply_source_weights` is `true`, each candidate's final score is
/// multiplied by the provenance weight from its `source` field (legacy
/// candidates without `source` use the v0 fallback weight).
///
/// `candidates_json`: JSON array of `{ id, text, embedding, timestamp, source? }` objects.
/// Returns a JsValue (array of `RankedResult` objects including `source_weight`).
#[wasm_bindgen(js_name = "rerankWithConfig")]
pub fn wasm_rerank_with_config(
    query: &str,
    query_embedding: &[f32],
    candidates_json: &str,
    top_k: usize,
    apply_source_weights: bool,
) -> Result<JsValue, JsError> {
    let candidates: Vec<reranker::Candidate> = serde_json::from_str(candidates_json)
        .map_err(|e| JsError::new(&format!("Invalid candidates JSON: {}", e)))?;
    let config = reranker::RerankerConfig {
        apply_source_weights,
        bm25_weight_override: None,
        vector_weight_override: None,
    };
    let results = reranker::rerank_with_config(query, query_embedding, &candidates, top_k, config)
        .map_err(|e| JsError::new(&e.to_string()))?;
    serde_wasm_bindgen::to_value(&results).map_err(|e| JsError::new(&e.to_string()))
}

/// Return the source weight multiplier for a given source string.
///
/// Accepted values: "user" | "user-inferred" | "assistant" | "external" | "derived".
///
/// Unknown input is routed through `MemorySource::from_str_lossy` which
/// falls back to `user-inferred` (v2-lenient weight 0.95). Callers who need
/// the "no source field at all" fallback (weight 0.85) should call
/// `legacyClaimFallbackWeight()` instead.
#[wasm_bindgen(js_name = "sourceWeight")]
pub fn wasm_source_weight(source: &str) -> f64 {
    let src = crate::claims::MemorySource::from_str_lossy(source);
    reranker::source_weight(src)
}

/// Return the v1 legacy-claim fallback weight (applied to candidates that
/// have no `source` field).
#[wasm_bindgen(js_name = "legacyClaimFallbackWeight")]
pub fn wasm_legacy_claim_fallback_weight() -> f64 {
    reranker::LEGACY_CLAIM_FALLBACK_WEIGHT
}

/// Validate a Memory Taxonomy v1 claim (JSON in, JSON out — canonicalised).
///
/// Returns the canonical JSON encoding on success. Throws on any schema
/// violation (wrong type token, missing required field, wrong schema_version).
///
/// See `docs/specs/totalreclaw/memory-taxonomy-v1.md`.
#[wasm_bindgen(js_name = "validateMemoryClaimV1")]
pub fn wasm_validate_memory_claim_v1(claim_json: &str) -> Result<String, JsError> {
    let claim: crate::claims::MemoryClaimV1 = serde_json::from_str(claim_json)
        .map_err(|e| JsError::new(&format!("invalid v1 claim: {}", e)))?;
    if claim.schema_version != crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION {
        return Err(JsError::new(&format!(
            "unsupported schema_version {}: only {} is supported",
            claim.schema_version,
            crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION
        )));
    }
    serde_json::to_string(&claim).map_err(|e| JsError::new(&e.to_string()))
}

/// Case-insensitive parse of a memory type string. Unknown input returns "claim".
#[wasm_bindgen(js_name = "parseMemoryTypeV1")]
pub fn wasm_parse_memory_type_v1(s: &str) -> String {
    let t = crate::claims::MemoryTypeV1::from_str_lossy(s);
    serde_json::to_string(&t)
        .unwrap_or_else(|_| "\"claim\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a memory source string. Unknown input returns "user-inferred".
#[wasm_bindgen(js_name = "parseMemorySource")]
pub fn wasm_parse_memory_source(s: &str) -> String {
    let src = crate::claims::MemorySource::from_str_lossy(s);
    serde_json::to_string(&src)
        .unwrap_or_else(|_| "\"user-inferred\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a v1.1 pin_status string. Unknown input returns "unpinned".
#[wasm_bindgen(js_name = "parsePinStatus")]
pub fn wasm_parse_pin_status(s: &str) -> String {
    let st = crate::claims::PinStatus::from_str_lossy(s);
    serde_json::to_string(&st)
        .unwrap_or_else(|_| "\"unpinned\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Check whether a JSON-encoded claim is pinned, recognizing both the v0
/// short-key sentinel (`st == "p"`) and the v1.1 field (`pin_status ==
/// "pinned"`). Returns `false` on any parse failure.
///
/// Wrapper around [`crate::claims::is_pinned_json`] for TS clients.
#[wasm_bindgen(js_name = "isPinnedClaimJson")]
pub fn wasm_is_pinned_claim_json(claim_json: &str) -> bool {
    crate::claims::is_pinned_json(claim_json)
}

/// Cosine similarity between two f32 vectors.
#[wasm_bindgen(js_name = "cosineSimilarity")]
pub fn wasm_cosine_similarity(a: &[f32], b: &[f32]) -> f64 {
    reranker::cosine_similarity_f32(a, b)
}

// ---------------------------------------------------------------------------
// Pin tier / boost / intent (kg-2 / F1 Pin UX 2.2.8)
// ---------------------------------------------------------------------------

/// Compute the [`PinTier`]'s multiplicative boost at a given timestamp.
///
/// `tier_json`: internally-tagged JSON, e.g. `{"tier":"soft","pinned_at":1716000000}`,
/// `{"tier":"hard"}`, `{"tier":"none"}`.
/// `now_unix`: seconds since epoch.
/// `config_json`: JSON of [`PinConfig`], e.g. `{"soft_half_life_days":90,"soft_max_boost":1.5,"hard_boost":1.5}`.
///
/// Returns the multiplicative boost factor (1.0 for `none`).
#[wasm_bindgen(js_name = "pinBoost")]
pub fn wasm_pin_boost(tier_json: &str, now_unix: i64, config_json: &str) -> Result<f64, JsError> {
    let tier: claims::PinTier = serde_json::from_str(tier_json)
        .map_err(|e| JsError::new(&format!("invalid PinTier json: {}", e)))?;
    let config: claims::PinConfig = serde_json::from_str(config_json)
        .map_err(|e| JsError::new(&format!("invalid PinConfig json: {}", e)))?;
    Ok(claims::pin_boost(tier, now_unix, &config))
}

/// Return the locked-default [`PinConfig`] as JSON. Clients that don't want
/// to retune can pass this verbatim to [`wasm_pin_boost`].
#[wasm_bindgen(js_name = "defaultPinConfig")]
pub fn wasm_default_pin_config() -> Result<String, JsError> {
    serde_json::to_string(&claims::PinConfig::default()).map_err(|e| JsError::new(&e.to_string()))
}

/// Classify natural-language pin/unpin intent from a user utterance.
///
/// Returns JSON of [`PinIntent`] when a trigger phrase matches, or `null` when
/// the utterance contains no recognised pin gesture. Lowercase normalization
/// is applied internally — callers pass the raw user text.
#[wasm_bindgen(js_name = "classifyPinIntent")]
pub fn wasm_classify_pin_intent(text: &str) -> Result<String, JsError> {
    match pin_intent::classify_pin_intent(text) {
        Some(intent) => serde_json::to_string(&intent).map_err(|e| JsError::new(&e.to_string())),
        None => Ok("null".to_string()),
    }
}

