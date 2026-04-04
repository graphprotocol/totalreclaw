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
#[cfg(feature = "managed")]
use crate::userop;
use crate::wallet;

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
// Wallet derivation
// ---------------------------------------------------------------------------

/// Derive an Ethereum EOA wallet from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Returns a JS object: `{ private_key: "hex...", address: "0x..." }`.
#[wasm_bindgen(js_name = "deriveEoa")]
pub fn wasm_derive_eoa(mnemonic: &str) -> Result<JsValue, JsError> {
    let w = wallet::derive_eoa(mnemonic).map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&w).map_err(|e| JsError::new(&e.to_string()))
}

/// Derive just the Ethereum EOA address from a BIP-39 mnemonic.
///
/// Returns: `"0x..."` (lowercase hex).
#[wasm_bindgen(js_name = "deriveEoaAddress")]
pub fn wasm_derive_eoa_address(mnemonic: &str) -> Result<String, JsError> {
    wallet::derive_eoa_address(mnemonic).map_err(to_js_error)
}

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
