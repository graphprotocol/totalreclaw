//! `bind_userop` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// UserOp (ERC-4337) — feature-gated: managed
// ---------------------------------------------------------------------------

/// Encode a single fact submission as SimpleAccount.execute() calldata.
///
/// `protobuf_payload`: raw protobuf bytes (Uint8Array).
/// Returns ABI-encoded calldata (Uint8Array).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "encodeSingleCall")]
pub fn wasm_encode_single_call(protobuf_payload: &[u8]) -> Vec<u8> {
    userop::encode_single_call(protobuf_payload)
}

/// Like `encodeSingleCall` but targets an explicit DataEdge address.
///
/// Chain/environment-aware clients pass the authoritative address from the
/// relay's `/v1/billing/status` `data_edge_address` (#366) — the isolated
/// staging Gnosis DataEdge differs from prod's. Throws on a bad address.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "encodeSingleCallTo")]
pub fn wasm_encode_single_call_to(
    protobuf_payload: &[u8],
    data_edge_address: &str,
) -> Result<Vec<u8>, JsError> {
    userop::encode_single_call_to(protobuf_payload, data_edge_address)
        .map_err(|e| JsError::new(&e.to_string()))
}

/// Encode multiple fact submissions as SimpleAccount.executeBatch() calldata.
///
/// `payloads_json`: JSON array of hex-encoded payload strings (e.g. `["deadbeef", "cafebabe"]`).
/// Returns ABI-encoded calldata (Uint8Array).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "encodeBatchCall")]
pub fn wasm_encode_batch_call(payloads_json: &str) -> Result<Vec<u8>, JsError> {
    let hex_strings: Vec<String> = serde_json::from_str(payloads_json)
        .map_err(|e| JsError::new(&format!("Invalid payloads JSON: {}", e)))?;
    let payloads: Vec<Vec<u8>> = hex_strings
        .iter()
        .map(|h| hex::decode(h.trim_start_matches("0x")).unwrap_or_default())
        .collect();
    userop::encode_batch_call(&payloads).map_err(|e| JsError::new(&e.to_string()))
}

/// Like `encodeBatchCall` but targets an explicit DataEdge address (#366).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "encodeBatchCallTo")]
pub fn wasm_encode_batch_call_to(
    payloads_json: &str,
    data_edge_address: &str,
) -> Result<Vec<u8>, JsError> {
    let hex_strings: Vec<String> = serde_json::from_str(payloads_json)
        .map_err(|e| JsError::new(&format!("Invalid payloads JSON: {}", e)))?;
    let payloads: Vec<Vec<u8>> = hex_strings
        .iter()
        .map(|h| hex::decode(h.trim_start_matches("0x")).unwrap_or_default())
        .collect();
    userop::encode_batch_call_to(&payloads, data_edge_address)
        .map_err(|e| JsError::new(&e.to_string()))
}

/// Compute the ERC-4337 v0.7 UserOp hash for signing.
///
/// `userop_json`: JSON string of a UserOperationV7 struct.
/// `entrypoint`: EntryPoint address (0x-prefixed).
/// `chain_id`: Chain ID (e.g. 84532 for Base Sepolia).
/// Returns 32-byte hash as hex string.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "hashUserOp")]
pub fn wasm_hash_userop(
    userop_json: &str,
    entrypoint: &str,
    chain_id: u64,
) -> Result<String, JsError> {
    let op: userop::UserOperationV7 = serde_json::from_str(userop_json)
        .map_err(|e| JsError::new(&format!("Invalid UserOp JSON: {}", e)))?;
    let hash =
        userop::hash_userop(&op, entrypoint, chain_id).map_err(|e| JsError::new(&e.to_string()))?;
    Ok(hex::encode(hash))
}

/// Sign a UserOp hash with an ECDSA private key (EIP-191 prefixed).
///
/// `hash_hex`: 64-char hex string (32-byte UserOp hash).
/// `private_key_hex`: 64-char hex string (32-byte private key).
/// Returns 65-byte signature as hex string (r + s + v).
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "signUserOp")]
pub fn wasm_sign_userop(hash_hex: &str, private_key_hex: &str) -> Result<String, JsError> {
    let hash_bytes = hex::decode(hash_hex.trim_start_matches("0x"))
        .map_err(|e| JsError::new(&format!("Invalid hash hex: {}", e)))?;
    let mut hash = [0u8; 32];
    if hash_bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "Hash must be 32 bytes, got {}",
            hash_bytes.len()
        )));
    }
    hash.copy_from_slice(&hash_bytes);

    let pk_bytes = hex::decode(private_key_hex.trim_start_matches("0x"))
        .map_err(|e| JsError::new(&format!("Invalid private key hex: {}", e)))?;
    let mut pk = [0u8; 32];
    if pk_bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "Private key must be 32 bytes, got {}",
            pk_bytes.len()
        )));
    }
    pk.copy_from_slice(&pk_bytes);

    let sig = userop::sign_userop(&hash, &pk).map_err(|e| JsError::new(&e.to_string()))?;
    Ok(hex::encode(sig))
}

/// Get the DataEdge contract address constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getDataEdgeAddress")]
pub fn wasm_data_edge_address() -> String {
    userop::DATA_EDGE_ADDRESS.to_string()
}

/// Get the EntryPoint v0.7 address constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getEntryPointAddress")]
pub fn wasm_entrypoint_address() -> String {
    userop::ENTRYPOINT_ADDRESS.to_string()
}

/// Get the SimpleAccountFactory address constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getSimpleAccountFactory")]
pub fn wasm_simple_account_factory() -> String {
    userop::SIMPLE_ACCOUNT_FACTORY.to_string()
}

/// Get the maximum batch size constant.
#[cfg(feature = "managed")]
#[wasm_bindgen(js_name = "getMaxBatchSize")]
pub fn wasm_max_batch_size() -> usize {
    userop::MAX_BATCH_SIZE
}

