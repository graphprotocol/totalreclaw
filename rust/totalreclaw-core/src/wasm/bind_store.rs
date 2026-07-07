//! `bind_store` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Store pipeline (pure computation, no I/O)
// ---------------------------------------------------------------------------

/// Prepare a fact for on-chain storage.
///
/// Pure computation: encrypt, generate indices, encode protobuf.
/// Does NOT submit -- the host handles I/O.
///
/// `encryption_key_hex`: 64-char hex string (32 bytes).
/// `dedup_key_hex`: 64-char hex string (32 bytes).
/// `lsh_hasher`: A `WasmLshHasher` instance.
/// `embedding`: Float32Array of the pre-computed embedding vector.
/// `importance`: Importance score on 1-10 scale (normalized to 0.0-1.0).
///
/// Returns a JSON string with `PreparedFact` fields.
#[wasm_bindgen(js_name = "prepareFact")]
pub fn wasm_prepare_fact(
    text: &str,
    encryption_key_hex: &str,
    dedup_key_hex: &str,
    lsh_hasher: &WasmLshHasher,
    embedding: &[f32],
    importance: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> Result<JsValue, JsError> {
    let enc_key = parse_key_hex(encryption_key_hex, "encryption_key")?;
    let ded_key = parse_key_hex(dedup_key_hex, "dedup_key")?;

    let prepared = store::prepare_fact(
        text,
        &enc_key,
        &ded_key,
        &lsh_hasher.inner,
        embedding,
        importance,
        source,
        owner,
        agent_id,
    )
    .map_err(to_js_error)?;

    serde_wasm_bindgen::to_value(&prepared).map_err(|e| JsError::new(&e.to_string()))
}

/// Prepare a fact with a pre-normalized decay score (already 0.0-1.0).
///
/// Same as `prepareFact()` but takes a raw decay score.
#[wasm_bindgen(js_name = "prepareFactWithDecayScore")]
pub fn wasm_prepare_fact_with_decay_score(
    text: &str,
    encryption_key_hex: &str,
    dedup_key_hex: &str,
    lsh_hasher: &WasmLshHasher,
    embedding: &[f32],
    decay_score: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> Result<JsValue, JsError> {
    let enc_key = parse_key_hex(encryption_key_hex, "encryption_key")?;
    let ded_key = parse_key_hex(dedup_key_hex, "dedup_key")?;

    let prepared = store::prepare_fact_with_decay_score(
        text,
        &enc_key,
        &ded_key,
        &lsh_hasher.inner,
        embedding,
        decay_score,
        source,
        owner,
        agent_id,
    )
    .map_err(to_js_error)?;

    serde_wasm_bindgen::to_value(&prepared).map_err(|e| JsError::new(&e.to_string()))
}

/// Build ABI-encoded calldata for a single prepared fact.
///
/// `prepared_json`: JSON string of a `PreparedFact`.
/// Returns ABI-encoded calldata (Uint8Array).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "buildSingleCalldataFromPrepared")]
pub fn wasm_build_single_calldata_from_prepared(prepared_json: &str) -> Result<Vec<u8>, JsError> {
    let prepared: store::PreparedFact = serde_json::from_str(prepared_json)
        .map_err(|e| JsError::new(&format!("Invalid PreparedFact JSON: {}", e)))?;
    Ok(store::build_single_calldata(&prepared))
}

/// Build ABI-encoded calldata for a batch of prepared facts.
///
/// `prepared_array_json`: JSON array of `PreparedFact` objects.
/// Returns ABI-encoded calldata (Uint8Array).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "buildBatchCalldataFromPrepared")]
pub fn wasm_build_batch_calldata_from_prepared(
    prepared_array_json: &str,
) -> Result<Vec<u8>, JsError> {
    let prepared: Vec<store::PreparedFact> = serde_json::from_str(prepared_array_json)
        .map_err(|e| JsError::new(&format!("Invalid PreparedFact array JSON: {}", e)))?;
    store::build_batch_calldata(&prepared).map_err(|e| JsError::new(&e.to_string()))
}

/// Prepare a tombstone (soft-delete) protobuf.
///
/// Returns the protobuf bytes as a Uint8Array.
#[wasm_bindgen(js_name = "prepareTombstone")]
pub fn wasm_prepare_tombstone(fact_id: &str, owner: &str) -> Vec<u8> {
    store::prepare_tombstone(fact_id, owner)
}

/// Compute the content fingerprint for dedup checks.
///
/// `dedup_key_hex`: 64-char hex string (32 bytes).
/// Returns 64-char hex fingerprint.
#[wasm_bindgen(js_name = "computeContentFingerprint")]
pub fn wasm_compute_content_fingerprint(
    text: &str,
    dedup_key_hex: &str,
) -> Result<String, JsError> {
    let key = parse_key_hex(dedup_key_hex, "dedup_key")?;
    Ok(store::compute_content_fingerprint(text, &key))
}

