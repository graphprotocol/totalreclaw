//! WASM bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features wasm`. Built with `wasm-pack build --target nodejs`.
//!
//! All byte arrays cross the boundary as hex strings. Complex return types
//! (Vec<String>, structs) are serialized as JSON strings or JsValues.

use wasm_bindgen::prelude::*;

use crate::blind;
use crate::crypto;
use crate::debrief;
use crate::fingerprint;
use crate::lsh;
use crate::protobuf;
use crate::reranker;

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
    let salt_bytes = hex::decode(salt_hex).map_err(|e| JsError::new(&format!("invalid salt hex: {}", e)))?;
    if salt_bytes.len() != 32 {
        return Err(JsError::new(&format!("salt must be 32 bytes, got {}", salt_bytes.len())));
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
        return Err(JsError::new(&format!("auth_key must be 32 bytes, got {}", key_bytes.len())));
    }
    let mut key = [0u8; 32];
    key.copy_from_slice(&key_bytes);
    Ok(crypto::compute_auth_key_hash(&key))
}

// ---------------------------------------------------------------------------
// AES-256-GCM
// ---------------------------------------------------------------------------

/// Encrypt a UTF-8 plaintext with AES-256-GCM.
///
/// `encryption_key_hex`: 64-char hex string (32 bytes).
/// Returns base64-encoded ciphertext (wire format: iv || tag || ciphertext).
#[wasm_bindgen(js_name = "encrypt")]
pub fn wasm_encrypt(plaintext: &str, encryption_key_hex: &str) -> Result<String, JsError> {
    let key = parse_key_hex(encryption_key_hex, "encryption_key")?;
    crypto::encrypt(plaintext, &key).map_err(to_js_error)
}

/// Decrypt a base64-encoded AES-256-GCM blob.
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
// LSH Hasher
// ---------------------------------------------------------------------------

/// Random Hyperplane LSH hasher (WASM wrapper).
///
/// Construct with `new WasmLshHasher(seedHex, dims)`.
/// Call `hash(embeddingFloat64Array)` to get bucket IDs.
#[wasm_bindgen]
pub struct WasmLshHasher {
    inner: lsh::LshHasher,
}

#[wasm_bindgen]
impl WasmLshHasher {
    /// Create a new LSH hasher with default parameters (20 tables, 32 bits).
    ///
    /// `seed_hex`: hex-encoded seed (>= 32 chars = 16 bytes).
    /// `dims`: embedding dimensionality (e.g. 1024).
    #[wasm_bindgen(constructor)]
    pub fn new(seed_hex: &str, dims: usize) -> Result<WasmLshHasher, JsError> {
        let seed = hex::decode(seed_hex)
            .map_err(|e| JsError::new(&format!("invalid seed hex: {}", e)))?;
        let inner = lsh::LshHasher::new(&seed, dims).map_err(to_js_error)?;
        Ok(WasmLshHasher { inner })
    }

    /// Create a new LSH hasher with custom parameters.
    ///
    /// `seed_hex`: hex-encoded seed.
    /// `dims`: embedding dimensionality.
    /// `n_tables`: number of hash tables.
    /// `n_bits`: bits per table.
    #[wasm_bindgen(js_name = "withParams")]
    pub fn with_params(
        seed_hex: &str,
        dims: usize,
        n_tables: usize,
        n_bits: usize,
    ) -> Result<WasmLshHasher, JsError> {
        let seed = hex::decode(seed_hex)
            .map_err(|e| JsError::new(&format!("invalid seed hex: {}", e)))?;
        let inner = lsh::LshHasher::with_params(&seed, dims, n_tables, n_bits)
            .map_err(to_js_error)?;
        Ok(WasmLshHasher { inner })
    }

    /// Hash an embedding vector to blind-hashed bucket IDs.
    ///
    /// `embedding`: Float64Array of length `dims`.
    /// Returns a JSON array of hex strings (one per table).
    pub fn hash(&self, embedding: &[f64]) -> Result<JsValue, JsError> {
        let hashes = self.inner.hash(embedding).map_err(to_js_error)?;
        serde_wasm_bindgen::to_value(&hashes).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Number of hash tables.
    #[wasm_bindgen(getter)]
    pub fn tables(&self) -> usize {
        self.inner.tables()
    }

    /// Bits per table.
    #[wasm_bindgen(getter)]
    pub fn bits(&self) -> usize {
        self.inner.bits()
    }

    /// Embedding dimensionality.
    #[wasm_bindgen(getter)]
    pub fn dimensions(&self) -> usize {
        self.inner.dimensions()
    }
}

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
    let payload: FactPayloadJson = serde_json::from_str(json)
        .map_err(|e| JsError::new(&format!("invalid JSON: {}", e)))?;

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
    };

    Ok(protobuf::encode_fact_protobuf(&fact))
}

/// Encode a tombstone protobuf for soft-deleting a fact.
///
/// Returns the protobuf bytes as a Uint8Array.
#[wasm_bindgen(js_name = "encodeTombstoneProtobuf")]
pub fn wasm_encode_tombstone_protobuf(fact_id: &str, owner: &str) -> Vec<u8> {
    protobuf::encode_tombstone_protobuf(fact_id, owner)
}

// ---------------------------------------------------------------------------
// Debrief
// ---------------------------------------------------------------------------

/// Parse a debrief LLM response into validated items.
///
/// Returns a JSON array of `{ text, type, importance }` objects.
#[wasm_bindgen(js_name = "parseDebriefResponse")]
pub fn wasm_parse_debrief_response(response: &str) -> Result<JsValue, JsError> {
    let items = debrief::parse_debrief_response(response);
    serde_wasm_bindgen::to_value(&items).map_err(|e| JsError::new(&e.to_string()))
}

/// Get the canonical debrief system prompt template.
///
/// Contains `{already_stored_facts}` placeholder.
#[wasm_bindgen(js_name = "getDebriefSystemPrompt")]
pub fn wasm_get_debrief_system_prompt() -> String {
    debrief::DEBRIEF_SYSTEM_PROMPT.to_string()
}

/// Build the debrief prompt with already-stored facts filled in.
///
/// `stored_facts_json`: JSON array of strings (fact texts already stored).
#[wasm_bindgen(js_name = "buildDebriefPrompt")]
pub fn wasm_build_debrief_prompt(stored_facts_json: &str) -> Result<String, JsError> {
    let facts: Vec<String> = serde_json::from_str(stored_facts_json)
        .map_err(|e| JsError::new(&format!("invalid JSON array: {}", e)))?;
    let refs: Vec<&str> = facts.iter().map(|s| s.as_str()).collect();
    Ok(debrief::build_debrief_prompt(&refs))
}

// ---------------------------------------------------------------------------
// Constants (exposed as getter functions since wasm_bindgen doesn't support statics)
// ---------------------------------------------------------------------------

/// Minimum messages for debrief (8 = 4 turns).
#[wasm_bindgen(js_name = "getMinDebriefMessages")]
pub fn wasm_min_debrief_messages() -> usize {
    debrief::MIN_DEBRIEF_MESSAGES
}

/// Maximum debrief items (5).
#[wasm_bindgen(js_name = "getMaxDebriefItems")]
pub fn wasm_max_debrief_items() -> usize {
    debrief::MAX_DEBRIEF_ITEMS
}

/// Source tag for debrief items.
#[wasm_bindgen(js_name = "getDebriefSource")]
pub fn wasm_debrief_source() -> String {
    debrief::DEBRIEF_SOURCE.to_string()
}

// ---------------------------------------------------------------------------
// Reranker
// ---------------------------------------------------------------------------

/// Rerank candidates using BM25 + Cosine + RRF fusion.
///
/// `candidates_json`: JSON array of `{ id, text, embedding, timestamp }` objects.
/// Returns a JsValue (array of `RankedResult` objects).
#[wasm_bindgen(js_name = "rerank")]
pub fn wasm_rerank(query: &str, query_embedding: &[f32], candidates_json: &str, top_k: usize) -> Result<JsValue, JsError> {
    let candidates: Vec<reranker::Candidate> = serde_json::from_str(candidates_json)
        .map_err(|e| JsError::new(&format!("Invalid candidates JSON: {}", e)))?;
    let results = reranker::rerank(query, query_embedding, &candidates, top_k)
        .map_err(|e| JsError::new(&e.to_string()))?;
    serde_wasm_bindgen::to_value(&results)
        .map_err(|e| JsError::new(&e.to_string()))
}

/// Cosine similarity between two f32 vectors.
#[wasm_bindgen(js_name = "cosineSimilarity")]
pub fn wasm_cosine_similarity(a: &[f32], b: &[f32]) -> f64 {
    reranker::cosine_similarity_f32(a, b)
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Convert DerivedKeys to a JsValue (plain JS object with hex strings).
fn keys_to_js(keys: &crypto::DerivedKeys) -> Result<JsValue, JsError> {
    // Use js_sys to build a plain object (not a Map) for ergonomic JS access.
    let obj = js_sys::Object::new();
    js_sys::Reflect::set(&obj, &"auth_key".into(), &hex::encode(keys.auth_key).into())
        .map_err(|_| JsError::new("failed to set auth_key"))?;
    js_sys::Reflect::set(&obj, &"encryption_key".into(), &hex::encode(keys.encryption_key).into())
        .map_err(|_| JsError::new("failed to set encryption_key"))?;
    js_sys::Reflect::set(&obj, &"dedup_key".into(), &hex::encode(keys.dedup_key).into())
        .map_err(|_| JsError::new("failed to set dedup_key"))?;
    js_sys::Reflect::set(&obj, &"salt".into(), &hex::encode(keys.salt).into())
        .map_err(|_| JsError::new("failed to set salt"))?;
    Ok(obj.into())
}

/// Parse a 32-byte hex key, returning a friendly error on failure.
fn parse_key_hex(hex_str: &str, name: &str) -> Result<[u8; 32], JsError> {
    let bytes =
        hex::decode(hex_str).map_err(|e| JsError::new(&format!("invalid {} hex: {}", name, e)))?;
    if bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "{} must be 32 bytes, got {}",
            name,
            bytes.len()
        )));
    }
    let mut key = [0u8; 32];
    key.copy_from_slice(&bytes);
    Ok(key)
}

/// Convert a crate::Error to JsError.
fn to_js_error(e: crate::Error) -> JsError {
    JsError::new(&e.to_string())
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
}
