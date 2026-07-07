//! `bind_protobuf` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Protobuf encoding
// ---------------------------------------------------------------------------

/// Encode a fact payload as minimal protobuf wire format.
///
/// `json`: JSON string with shape:
/// ```json
/// {
///   "id": "...", "timestamp": "...", "owner": "...",
///   "encrypted_blob_hex": "...", "blind_indices": ["..."],
///   "decay_score": 0.8, "source": "...", "content_fp": "...",
///   "agent_id": "...", "encrypted_embedding": "..." (optional)
/// }
/// ```
///
/// Returns the protobuf bytes as a Uint8Array.
#[wasm_bindgen(js_name = "encodeFactProtobuf")]
pub fn wasm_encode_fact_protobuf(json: &str) -> Result<Vec<u8>, JsError> {
    let payload: FactPayloadJson =
        serde_json::from_str(json).map_err(|e| JsError::new(&format!("invalid JSON: {}", e)))?;

    let fact = protobuf::FactPayload {
        id: payload.id,
        timestamp: payload.timestamp,
        owner: payload.owner,
        encrypted_blob_hex: payload.encrypted_blob_hex,
        blind_indices: payload.blind_indices,
        decay_score: payload.decay_score,
        source: payload.source,
        content_fp: payload.content_fp,
        agent_id: payload.agent_id,
        encrypted_embedding: payload.encrypted_embedding,
        version: payload
            .version
            .unwrap_or(protobuf::DEFAULT_PROTOBUF_VERSION),
    };

    Ok(protobuf::encode_fact_protobuf(&fact))
}

/// Encode a tombstone protobuf for soft-deleting a fact.
///
/// `version` is optional; missing/0 defaults to `DEFAULT_PROTOBUF_VERSION` (3).
/// Pass `4` to emit a v1-taxonomy tombstone (outer protobuf version = 4).
///
/// Returns the protobuf bytes as a Uint8Array.
#[wasm_bindgen(js_name = "encodeTombstoneProtobuf")]
pub fn wasm_encode_tombstone_protobuf(fact_id: &str, owner: &str, version: Option<u32>) -> Vec<u8> {
    protobuf::encode_tombstone_protobuf(
        fact_id,
        owner,
        version.unwrap_or(protobuf::DEFAULT_PROTOBUF_VERSION),
    )
}

/// Serde-friendly FactPayload for JSON deserialization.
#[derive(serde::Deserialize)]
struct FactPayloadJson {
    id: String,
    timestamp: String,
    owner: String,
    encrypted_blob_hex: String,
    blind_indices: Vec<String>,
    decay_score: f64,
    source: String,
    content_fp: String,
    agent_id: String,
    encrypted_embedding: Option<String>,
    /// Optional outer protobuf version (3 legacy, 4 for v1 taxonomy).
    /// Missing/0 defaults to legacy v3 for back-compat.
    #[serde(default)]
    version: Option<u32>,
}

