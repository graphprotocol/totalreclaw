//! `bind_crypto` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Key derivation
// ---------------------------------------------------------------------------

/// Derive encryption keys from a BIP-39 mnemonic (strict checksum validation).
///
/// Returns a JSON object with hex-encoded keys:
/// `{ auth_key, encryption_key, dedup_key, salt }`
#[wasm_bindgen(js_name = "deriveKeysFromMnemonic")]
pub fn wasm_derive_keys_from_mnemonic(mnemonic: &str) -> Result<JsValue, JsError> {
    let keys = crypto::derive_keys_from_mnemonic(mnemonic).map_err(to_js_error)?;
    keys_to_js(&keys)
}

/// Derive encryption keys from a BIP-39 mnemonic (lenient -- skips checksum).
///
/// Same return format as `deriveKeysFromMnemonic`.
#[wasm_bindgen(js_name = "deriveKeysFromMnemonicLenient")]
pub fn wasm_derive_keys_from_mnemonic_lenient(mnemonic: &str) -> Result<JsValue, JsError> {
    let keys = crypto::derive_keys_from_mnemonic_lenient(mnemonic).map_err(to_js_error)?;
    keys_to_js(&keys)
}

/// Derive the 32-byte LSH seed from a BIP-39 mnemonic and salt.
///
/// `salt_hex`: 64-char hex string (32 bytes).
/// Returns hex-encoded 32-byte seed.
#[wasm_bindgen(js_name = "deriveLshSeed")]
pub fn wasm_derive_lsh_seed(mnemonic: &str, salt_hex: &str) -> Result<String, JsError> {
    let salt_bytes =
        hex::decode(salt_hex).map_err(|e| JsError::new(&format!("invalid salt hex: {}", e)))?;
    if salt_bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "salt must be 32 bytes, got {}",
            salt_bytes.len()
        )));
    }
    let mut salt = [0u8; 32];
    salt.copy_from_slice(&salt_bytes);

    let seed = crypto::derive_lsh_seed(mnemonic, &salt).map_err(to_js_error)?;
    Ok(hex::encode(seed))
}

/// Compute SHA-256(authKey) as a hex string.
///
/// `auth_key_hex`: 64-char hex string (32 bytes).
#[wasm_bindgen(js_name = "computeAuthKeyHash")]
pub fn wasm_compute_auth_key_hash(auth_key_hex: &str) -> Result<String, JsError> {
    let key_bytes = hex::decode(auth_key_hex)
        .map_err(|e| JsError::new(&format!("invalid auth_key hex: {}", e)))?;
    if key_bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "auth_key must be 32 bytes, got {}",
            key_bytes.len()
        )));
    }
    let mut key = [0u8; 32];
    key.copy_from_slice(&key_bytes);
    Ok(crypto::compute_auth_key_hash(&key))
}

// ---------------------------------------------------------------------------
// XChaCha20-Poly1305
// ---------------------------------------------------------------------------

/// Encrypt a UTF-8 plaintext with XChaCha20-Poly1305.
///
/// `encryption_key_hex`: 64-char hex string (32 bytes).
/// Returns base64-encoded ciphertext (wire format: nonce || tag || ciphertext).
#[wasm_bindgen(js_name = "encrypt")]
pub fn wasm_encrypt(plaintext: &str, encryption_key_hex: &str) -> Result<String, JsError> {
    let key = parse_key_hex(encryption_key_hex, "encryption_key")?;
    crypto::encrypt(plaintext, &key).map_err(to_js_error)
}

/// Decrypt a base64-encoded XChaCha20-Poly1305 blob.
///
/// `encryption_key_hex`: 64-char hex string (32 bytes).
/// Returns the plaintext UTF-8 string.
#[wasm_bindgen(js_name = "decrypt")]
pub fn wasm_decrypt(encrypted_base64: &str, encryption_key_hex: &str) -> Result<String, JsError> {
    let key = parse_key_hex(encryption_key_hex, "encryption_key")?;
    crypto::decrypt(encrypted_base64, &key).map_err(to_js_error)
}

// ---------------------------------------------------------------------------
// Blind indices
// ---------------------------------------------------------------------------

/// Generate blind indices (SHA-256 token hashes) for a text string.
///
/// Returns a JSON array of hex strings.
#[wasm_bindgen(js_name = "generateBlindIndices")]
pub fn wasm_generate_blind_indices(text: &str) -> Result<JsValue, JsError> {
    let indices = blind::generate_blind_indices(text);
    serde_wasm_bindgen::to_value(&indices).map_err(|e| JsError::new(&e.to_string()))
}

// ---------------------------------------------------------------------------
// Content fingerprint
// ---------------------------------------------------------------------------

/// Compute HMAC-SHA256 content fingerprint.
///
/// `dedup_key_hex`: 64-char hex string (32 bytes).
/// Returns 64-char hex fingerprint.
#[wasm_bindgen(js_name = "generateContentFingerprint")]
pub fn wasm_generate_content_fingerprint(
    plaintext: &str,
    dedup_key_hex: &str,
) -> Result<String, JsError> {
    let key = parse_key_hex(dedup_key_hex, "dedup_key")?;
    Ok(fingerprint::generate_content_fingerprint(plaintext, &key))
}

/// Normalize text (NFC, lowercase, collapse whitespace, trim).
#[wasm_bindgen(js_name = "normalizeText")]
pub fn wasm_normalize_text(text: &str) -> String {
    fingerprint::normalize_text(text)
}

// ---------------------------------------------------------------------------
// Embedding codec (canonical f16 + universal decoder) — internal#479 Part A
// ---------------------------------------------------------------------------

/// Pack an embedding into the canonical pre-encryption payload string.
///
/// `embedding`: the f32 vector (production = 640-d Harrier output). A 640-d
/// vector is packed little-endian f16; any other length is packed f32 (the
/// 640 guard — see `embedding_codec`). Returns the base64 ASCII string that
/// the client encrypts before storage.
#[wasm_bindgen(js_name = "encodeEmbeddingCanonical")]
pub fn wasm_encode_embedding_canonical(embedding: &[f32]) -> Result<String, JsError> {
    // Fallible (#479 review): rejects NaN/±inf and finite f16 overflow —
    // fail-closed at the API boundary, never a silently poisoned payload.
    // Precondition: inputs should be f32-precision-exact (JS numbers are f64;
    // the &[f32] boundary rounds f64→f32 first — see the core doc comment).
    crate::embedding_codec::encode_embedding_canonical(embedding)
        .map_err(|e| JsError::new(&e.to_string()))
}

/// Decode any embedding payload into a Float32Array (the forever-reader).
///
/// Dispatch mirrors `decode_embedding_universal`: a payload beginning with `[`
/// is parsed as a JSON float array (legacy TS plugin); otherwise base64-decode
/// and infer width from the byte count (`640*2` → f16 upcast to f32, else a
/// multiple of 4 → f32). Bad input → `JsError`, never a wrong-dim vector.
#[wasm_bindgen(js_name = "decodeEmbeddingUniversal")]
pub fn wasm_decode_embedding_universal(payload: &str) -> Result<Vec<f32>, JsError> {
    crate::embedding_codec::decode_embedding_universal(payload).map_err(to_js_error)
}

