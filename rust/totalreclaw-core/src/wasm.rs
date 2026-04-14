//! WASM bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features wasm`. Built with `wasm-pack build --target nodejs`.
//!
//! All byte arrays cross the boundary as hex strings. Complex return types
//! (Vec<String>, structs) are serialized as JSON strings or JsValues.

use wasm_bindgen::prelude::*;

use crate::blind;
use crate::claims;
use crate::contradiction;
use crate::crypto;
use crate::debrief;
use crate::digest;
use crate::feedback_log;
use crate::fingerprint;
use crate::lsh;
use crate::protobuf;
use crate::reranker;
use crate::store;
#[cfg(feature = "managed")]
use crate::search;
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
    /// `dims`: embedding dimensionality (e.g. 640).
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
pub fn wasm_build_single_calldata_from_prepared(
    prepared_json: &str,
) -> Result<Vec<u8>, JsError> {
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
    store::build_batch_calldata(&prepared)
        .map_err(|e| JsError::new(&e.to_string()))
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
    let results = search::decrypt_and_rerank(&facts, query, query_embedding, encryption_key_hex, top_k)
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

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 1
// ---------------------------------------------------------------------------

/// Normalize an entity name (NFC, lowercase, trim, collapse whitespace).
#[wasm_bindgen(js_name = "normalizeEntityName")]
pub fn wasm_normalize_entity_name(name: &str) -> String {
    claims::normalize_entity_name(name)
}

/// Deterministic entity ID from a name (first 8 bytes of SHA256 as hex).
#[wasm_bindgen(js_name = "deterministicEntityId")]
pub fn wasm_deterministic_entity_id(name: &str) -> String {
    claims::deterministic_entity_id(name)
}

// Inner (non-wasm_bindgen) implementations so unit tests can exercise them
// without constructing JsError, which is only valid on wasm targets.

fn kg_parse_claim_or_legacy_inner(decrypted: &str) -> Result<String, String> {
    let claim = claims::parse_claim_or_legacy(decrypted);
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

fn kg_canonicalize_claim_inner(claim_json: &str) -> Result<String, String> {
    let claim: claims::Claim = serde_json::from_str(claim_json)
        .map_err(|e| format!("invalid claim JSON: {}", e))?;
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

fn kg_build_template_digest_inner(
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, String> {
    let parsed: Vec<claims::Claim> = serde_json::from_str(claims_json)
        .map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::build_template_digest(&parsed, now_unix_seconds);
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

fn kg_build_digest_prompt_inner(claims_json: &str) -> Result<String, String> {
    let parsed: Vec<claims::Claim> = serde_json::from_str(claims_json)
        .map_err(|e| format!("invalid claims JSON: {}", e))?;
    if parsed.is_empty() {
        return Err("build_digest_prompt requires at least one claim".to_string());
    }
    Ok(digest::build_digest_prompt(&parsed))
}

fn kg_parse_digest_response_inner(raw: &str) -> Result<String, String> {
    let parsed = digest::parse_digest_response(raw)?;
    serde_json::to_string(&parsed).map_err(|e| e.to_string())
}

fn kg_assemble_digest_from_llm_inner(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, String> {
    let parsed: digest::ParsedDigestResponse = serde_json::from_str(parsed_json)
        .map_err(|e| format!("invalid ParsedDigestResponse JSON: {}", e))?;
    let source_claims: Vec<claims::Claim> = serde_json::from_str(claims_json)
        .map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::assemble_digest_from_llm(&parsed, &source_claims, now_unix_seconds)?;
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

/// Parse a decrypted blob as a Claim, falling back to legacy formats.
/// Returns JSON-serialized Claim.
#[wasm_bindgen(js_name = "parseClaimOrLegacy")]
pub fn wasm_parse_claim_or_legacy(decrypted: &str) -> Result<String, JsError> {
    kg_parse_claim_or_legacy_inner(decrypted).map_err(|e| JsError::new(&e))
}

/// Canonicalize a Claim JSON: strict-parse as Claim, re-serialize to canonical bytes.
/// Rejects legacy or malformed input. Use before encryption so TS/Python/Rust all
/// produce byte-identical blobs for the same logical claim.
#[wasm_bindgen(js_name = "canonicalizeClaim")]
pub fn wasm_canonicalize_claim(claim_json: &str) -> Result<String, JsError> {
    kg_canonicalize_claim_inner(claim_json).map_err(|e| JsError::new(&e))
}

/// Build a template digest from an array of active claims.
/// `claims_json`: JSON array of Claim. Returns JSON-serialized Digest.
#[wasm_bindgen(js_name = "buildTemplateDigest")]
pub fn wasm_build_template_digest(
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, JsError> {
    kg_build_template_digest_inner(claims_json, now_unix_seconds).map_err(|e| JsError::new(&e))
}

/// Build the LLM prompt for digest compilation.
/// `claims_json`: JSON array of Claim (must be non-empty).
#[wasm_bindgen(js_name = "buildDigestPrompt")]
pub fn wasm_build_digest_prompt(claims_json: &str) -> Result<String, JsError> {
    kg_build_digest_prompt_inner(claims_json).map_err(|e| JsError::new(&e))
}

/// Parse an LLM digest response.
/// Returns JSON-serialized ParsedDigestResponse.
#[wasm_bindgen(js_name = "parseDigestResponse")]
pub fn wasm_parse_digest_response(raw: &str) -> Result<String, JsError> {
    kg_parse_digest_response_inner(raw).map_err(|e| JsError::new(&e))
}

/// Assemble a full Digest from a parsed LLM response and source claims.
#[wasm_bindgen(js_name = "assembleDigestFromLlm")]
pub fn wasm_assemble_digest_from_llm(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, JsError> {
    kg_assemble_digest_from_llm_inner(parsed_json, claims_json, now_unix_seconds)
        .map_err(|e| JsError::new(&e))
}

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 2: contradiction detection + feedback log
// ---------------------------------------------------------------------------

/// Input shape for `detectContradictions`: an array of these as `existing_json`.
#[derive(serde::Deserialize)]
struct DetectContradictionsItem {
    claim: claims::Claim,
    id: String,
    embedding: Vec<f32>,
}

fn kg_default_resolution_weights_inner() -> Result<String, String> {
    let w = contradiction::default_weights();
    serde_json::to_string(&w).map_err(|e| e.to_string())
}

fn kg_compute_score_components_inner(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, String> {
    let claim: claims::Claim = serde_json::from_str(claim_json)
        .map_err(|e| format!("invalid claim JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights = serde_json::from_str(weights_json)
        .map_err(|e| format!("invalid weights JSON: {}", e))?;
    let sc = contradiction::compute_score_components(&claim, now_unix_seconds, &weights);
    serde_json::to_string(&sc).map_err(|e| e.to_string())
}

fn kg_resolve_pair_inner(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, String> {
    let claim_a: claims::Claim = serde_json::from_str(claim_a_json)
        .map_err(|e| format!("invalid claim_a JSON: {}", e))?;
    let claim_b: claims::Claim = serde_json::from_str(claim_b_json)
        .map_err(|e| format!("invalid claim_b JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights = serde_json::from_str(weights_json)
        .map_err(|e| format!("invalid weights JSON: {}", e))?;
    let outcome = contradiction::resolve_pair(
        &claim_a,
        claim_a_id,
        &claim_b,
        claim_b_id,
        now_unix_seconds,
        &weights,
    );
    serde_json::to_string(&outcome).map_err(|e| e.to_string())
}

fn kg_detect_contradictions_inner(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> Result<String, String> {
    let new_claim: claims::Claim = serde_json::from_str(new_claim_json)
        .map_err(|e| format!("invalid new_claim JSON: {}", e))?;
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| format!("invalid new_embedding JSON: {}", e))?;
    let items: Vec<DetectContradictionsItem> = serde_json::from_str(existing_json)
        .map_err(|e| format!("invalid existing JSON (expected array of {{claim, id, embedding}}): {}", e))?;
    let existing: Vec<(claims::Claim, String, Vec<f32>)> = items
        .into_iter()
        .map(|it| (it.claim, it.id, it.embedding))
        .collect();
    let out = contradiction::detect_contradictions(
        &new_claim,
        new_claim_id,
        &new_embedding,
        &existing,
        lower_threshold,
        upper_threshold,
    );
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

fn kg_apply_feedback_inner(
    weights_json: &str,
    counterexample_json: &str,
) -> Result<String, String> {
    let weights: contradiction::ResolutionWeights = serde_json::from_str(weights_json)
        .map_err(|e| format!("invalid weights JSON: {}", e))?;
    let ce: contradiction::Counterexample = serde_json::from_str(counterexample_json)
        .map_err(|e| format!("invalid counterexample JSON: {}", e))?;
    let new_weights = contradiction::apply_feedback(&weights, &ce);
    serde_json::to_string(&new_weights).map_err(|e| e.to_string())
}

fn kg_default_weights_file_inner(now_unix_seconds: i64) -> Result<String, String> {
    let f = feedback_log::default_weights_file(now_unix_seconds);
    serde_json::to_string(&f).map_err(|e| e.to_string())
}

fn kg_serialize_weights_file_inner(file_json: &str) -> Result<String, String> {
    let f: feedback_log::WeightsFile = serde_json::from_str(file_json)
        .map_err(|e| format!("invalid weights file JSON: {}", e))?;
    Ok(feedback_log::serialize_weights_file(&f))
}

fn kg_parse_weights_file_inner(content: &str) -> Result<String, String> {
    let f = feedback_log::parse_weights_file(content)?;
    serde_json::to_string(&f).map_err(|e| e.to_string())
}

fn kg_append_feedback_to_jsonl_inner(
    existing: &str,
    entry_json: &str,
) -> Result<String, String> {
    let entry: feedback_log::FeedbackEntry = serde_json::from_str(entry_json)
        .map_err(|e| format!("invalid feedback entry JSON: {}", e))?;
    Ok(feedback_log::append_to_jsonl(existing, &entry))
}

#[derive(serde::Serialize)]
struct ReadFeedbackJsonlResult {
    entries: Vec<feedback_log::FeedbackEntry>,
    warnings: Vec<String>,
}

fn kg_read_feedback_jsonl_inner(content: &str) -> Result<String, String> {
    let (entries, warnings) = feedback_log::read_jsonl(content);
    let result = ReadFeedbackJsonlResult { entries, warnings };
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

fn kg_rotate_feedback_log_inner(content: &str, max_lines: i64) -> String {
    let cap = if max_lines < 0 { 0usize } else { max_lines as usize };
    feedback_log::rotate_if_needed(content, cap)
}

fn kg_feedback_to_counterexample_inner(entry_json: &str) -> Result<String, String> {
    let entry: feedback_log::FeedbackEntry = serde_json::from_str(entry_json)
        .map_err(|e| format!("invalid feedback entry JSON: {}", e))?;
    match feedback_log::feedback_to_counterexample(&entry) {
        Some(ce) => serde_json::to_string(&ce).map_err(|e| e.to_string()),
        None => Ok("null".to_string()),
    }
}

/// Default P2-3 resolution weights as JSON.
#[wasm_bindgen(js_name = "defaultResolutionWeights")]
pub fn wasm_default_resolution_weights() -> Result<String, JsError> {
    kg_default_resolution_weights_inner().map_err(|e| JsError::new(&e))
}

/// Compute a claim's score components for contradiction resolution.
#[wasm_bindgen(js_name = "computeScoreComponents")]
pub fn wasm_compute_score_components(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, JsError> {
    kg_compute_score_components_inner(claim_json, now_unix_seconds, weights_json)
        .map_err(|e| JsError::new(&e))
}

/// Run the resolution formula on two contradicting claims; returns ResolutionOutcome JSON.
#[wasm_bindgen(js_name = "resolvePair")]
pub fn wasm_resolve_pair(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, JsError> {
    kg_resolve_pair_inner(
        claim_a_json,
        claim_a_id,
        claim_b_json,
        claim_b_id,
        now_unix_seconds,
        weights_json,
    )
    .map_err(|e| JsError::new(&e))
}

/// Detect contradictions between a new claim and existing claims (JSON array of {claim, id, embedding}).
#[wasm_bindgen(js_name = "detectContradictions")]
pub fn wasm_detect_contradictions(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> Result<String, JsError> {
    kg_detect_contradictions_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        existing_json,
        lower_threshold,
        upper_threshold,
    )
    .map_err(|e| JsError::new(&e))
}

/// Apply a single counterexample to the weights; returns updated ResolutionWeights JSON.
#[wasm_bindgen(js_name = "applyFeedback")]
pub fn wasm_apply_feedback(
    weights_json: &str,
    counterexample_json: &str,
) -> Result<String, JsError> {
    kg_apply_feedback_inner(weights_json, counterexample_json).map_err(|e| JsError::new(&e))
}

/// Build a fresh default WeightsFile JSON with the given timestamp.
#[wasm_bindgen(js_name = "defaultWeightsFile")]
pub fn wasm_default_weights_file(now_unix_seconds: i64) -> Result<String, JsError> {
    kg_default_weights_file_inner(now_unix_seconds).map_err(|e| JsError::new(&e))
}

/// Serialize a WeightsFile JSON to pretty-printed JSON (2-space indent).
#[wasm_bindgen(js_name = "serializeWeightsFile")]
pub fn wasm_serialize_weights_file(file_json: &str) -> Result<String, JsError> {
    kg_serialize_weights_file_inner(file_json).map_err(|e| JsError::new(&e))
}

/// Parse a WeightsFile from JSON; rejects unknown versions and malformed input.
#[wasm_bindgen(js_name = "parseWeightsFile")]
pub fn wasm_parse_weights_file(content: &str) -> Result<String, JsError> {
    kg_parse_weights_file_inner(content).map_err(|e| JsError::new(&e))
}

/// Append one feedback entry to existing JSONL content.
#[wasm_bindgen(js_name = "appendFeedbackToJsonl")]
pub fn wasm_append_feedback_to_jsonl(
    existing: &str,
    entry_json: &str,
) -> Result<String, JsError> {
    kg_append_feedback_to_jsonl_inner(existing, entry_json).map_err(|e| JsError::new(&e))
}

/// Parse JSONL content. Returns JSON: `{"entries": [...], "warnings": [...]}`.
#[wasm_bindgen(js_name = "readFeedbackJsonl")]
pub fn wasm_read_feedback_jsonl(content: &str) -> Result<String, JsError> {
    kg_read_feedback_jsonl_inner(content).map_err(|e| JsError::new(&e))
}

/// Keep only the most recent `max_lines` non-empty feedback log lines. Non-falliable.
#[wasm_bindgen(js_name = "rotateFeedbackLog")]
pub fn wasm_rotate_feedback_log(content: &str, max_lines: i64) -> String {
    kg_rotate_feedback_log_inner(content, max_lines)
}

/// Convert a feedback entry into a counterexample for weight tuning. Returns
/// JSON Counterexample or the literal string "null" if the entry has no signal.
#[wasm_bindgen(js_name = "feedbackToCounterexample")]
pub fn wasm_feedback_to_counterexample(entry_json: &str) -> Result<String, JsError> {
    kg_feedback_to_counterexample_inner(entry_json).map_err(|e| JsError::new(&e))
}

// ---------------------------------------------------------------------------
// Tests (non-wasm runtime — direct Rust fn invocation)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_claim_json() -> String {
        r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#.to_string()
    }

    fn two_claims_json() -> String {
        r#"[
            {"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"},
            {"t":"lives in Lisbon","c":"fact","cf":0.95,"i":9,"sa":"oc"}
        ]"#
        .to_string()
    }

    #[test]
    fn wasm_normalize_entity_name_lowercases() {
        assert_eq!(wasm_normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn wasm_deterministic_entity_id_known_answer_pedro() {
        assert_eq!(
            wasm_deterministic_entity_id("pedro"),
            "ee5cd7d5d96c8874"
        );
    }

    #[test]
    fn wasm_parse_claim_or_legacy_full_claim_roundtrips() {
        let input = sample_claim_json();
        let out = kg_parse_claim_or_legacy_inner(&input).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "prefers PostgreSQL");
        assert_eq!(c.category, claims::ClaimCategory::Preference);
    }

    #[test]
    fn wasm_parse_claim_or_legacy_legacy_object() {
        let out = kg_parse_claim_or_legacy_inner(r#"{"t":"hello","a":"oc"}"#).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "hello");
        assert_eq!(c.source_agent, "oc");
        assert_eq!(c.category, claims::ClaimCategory::Fact);
    }

    #[test]
    fn wasm_build_template_digest_empty_vault() {
        let out = kg_build_template_digest_inner("[]", 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 0);
        assert!(!d.prompt_text.is_empty());
    }

    #[test]
    fn wasm_build_template_digest_two_claims() {
        let out = kg_build_template_digest_inner(&two_claims_json(), 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 2);
    }

    #[test]
    fn wasm_build_digest_prompt_empty_is_error() {
        let result = kg_build_digest_prompt_inner("[]");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_build_digest_prompt_one_claim_returns_prompt() {
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let prompt = kg_build_digest_prompt_inner(one).unwrap();
        assert!(!prompt.is_empty());
        assert!(prompt.contains("JSON"));
    }

    #[test]
    fn wasm_parse_digest_response_valid_fenced() {
        let raw = "```json\n{\"identity\":\"You are a developer.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[\"skynet\"]}\n```";
        let out = kg_parse_digest_response_inner(raw).unwrap();
        let p: digest::ParsedDigestResponse = serde_json::from_str(&out).unwrap();
        assert_eq!(p.identity, "You are a developer.");
        assert_eq!(p.top_claim_indices, vec![1]);
        assert_eq!(p.active_project_names, vec!["skynet".to_string()]);
    }

    #[test]
    fn wasm_parse_digest_response_invalid_is_error() {
        let result = kg_parse_digest_response_inner("not valid json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_assemble_digest_from_llm_builds_digest() {
        let parsed = r#"{"identity":"You are a developer.","top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":["skynet"]}"#;
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let out = kg_assemble_digest_from_llm_inner(parsed, one, 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 1);
        assert_eq!(d.identity, "You are a developer.");
    }

    #[test]
    fn wasm_canonicalize_claim_round_trips_canonical_input() {
        let input = sample_claim_json();
        let out = kg_canonicalize_claim_inner(&input).unwrap();
        assert_eq!(out, input);
    }

    #[test]
    fn wasm_canonicalize_claim_omits_default_status() {
        // Client sends verbose input with status="a" (Active, the default).
        // Canonical output omits the field.
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"a"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(!out.contains("\"st\""));
        assert!(out.contains("\"t\":\"hi\""));
    }

    #[test]
    fn wasm_canonicalize_claim_preserves_non_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"s"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(out.contains("\"st\":\"s\""));
    }

    #[test]
    fn wasm_canonicalize_claim_omits_default_corroboration() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","cc":1}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(!out.contains("\"cc\""));
    }

    #[test]
    fn wasm_canonicalize_claim_reorders_fields_to_struct_order() {
        // Input fields in random order; canonical output must follow Claim struct field order.
        let input = r#"{"sa":"oc","i":5,"cf":0.9,"c":"fact","t":"hi"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert_eq!(
            out,
            r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#
        );
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_malformed_json() {
        let result = kg_canonicalize_claim_inner("{not valid");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_missing_required_field() {
        // Missing `c` (category)
        let result = kg_canonicalize_claim_inner(r#"{"t":"hi","cf":0.9,"i":5,"sa":"oc"}"#);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_legacy_format() {
        // Legacy {"t":"...","a":"..."} has no `c` field so strict parse fails.
        let result = kg_canonicalize_claim_inner(r#"{"t":"hi","a":"oc"}"#);
        assert!(result.is_err());
    }

    // --- Phase 2 Slice 2c: contradiction bindings -----------------------

    /// 2026-04-12T00:00:00Z — matches the contradiction module's test NOW.
    const PHASE2_NOW: i64 = 1776211200;

    fn iso_days_ago_str(days: i64) -> String {
        let ts = PHASE2_NOW - days * 86400;
        chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0)
            .unwrap()
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    }

    #[test]
    fn wasm_default_resolution_weights_matches_p2_3_defaults() {
        let out = kg_default_resolution_weights_inner().unwrap();
        let w: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        assert_eq!(w.confidence, 0.25);
        assert_eq!(w.corroboration, 0.15);
        assert_eq!(w.recency, 0.40);
        assert_eq!(w.validation, 0.20);
    }

    #[test]
    fn wasm_compute_score_components_known_answer() {
        // 0.9*0.25 + 1*0.15 + 1*0.40 + 1*0.20 = 0.975 (explicit remember, today).
        let claim = format!(
            r#"{{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"totalreclaw_remember","ea":"{}"}}"#,
            iso_days_ago_str(0)
        );
        let weights = kg_default_resolution_weights_inner().unwrap();
        let out = kg_compute_score_components_inner(&claim, PHASE2_NOW, &weights).unwrap();
        let sc: contradiction::ScoreComponents = serde_json::from_str(&out).unwrap();
        assert_eq!(sc.confidence, 0.9);
        assert!((sc.corroboration - 1.0).abs() < 1e-12);
        assert!((sc.recency - 1.0).abs() < 1e-12);
        assert_eq!(sc.validation, 1.0);
        assert!((sc.weighted_total - 0.975).abs() < 1e-12);
    }

    #[test]
    fn wasm_compute_score_components_rejects_malformed_claim() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let result = kg_compute_score_components_inner("{not json", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_compute_score_components_rejects_malformed_weights() {
        let claim = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_compute_score_components_inner(claim, PHASE2_NOW, "{not json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_resolve_pair_vim_vs_vscode_defaults_vscode_wins() {
        let vim = format!(
            r#"{{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","ea":"{}","cc":3,"e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(60)
        );
        let vscode = format!(
            r#"{{"t":"uses VS Code","c":"fact","cf":0.9,"i":5,"sa":"oc","ea":"{}","e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(7)
        );
        let weights = kg_default_resolution_weights_inner().unwrap();
        let out =
            kg_resolve_pair_inner(&vim, "vim_id", &vscode, "vscode_id", PHASE2_NOW, &weights)
                .unwrap();
        let outcome: contradiction::ResolutionOutcome = serde_json::from_str(&out).unwrap();
        assert_eq!(outcome.winner_id, "vscode_id");
        assert_eq!(outcome.loser_id, "vim_id");
        assert!(outcome.winner_score > outcome.loser_score);
        assert!(outcome.score_delta > 0.0);
    }

    #[test]
    fn wasm_resolve_pair_rejects_malformed_claim_a() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let good = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_resolve_pair_inner("{bad", "a", good, "b", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_detect_contradictions_empty_existing_returns_empty_array() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0, 0.0, 0.0]).unwrap();
        let out =
            kg_detect_contradictions_inner(new_claim, "new_id", &emb, "[]", 0.3, 0.85).unwrap();
        assert_eq!(out, "[]");
    }

    #[test]
    fn wasm_detect_contradictions_single_in_band_returns_one() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let existing_claim_obj = r#"{"t":"uses Emacs","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        // Build an 8-d vector pair with cosine ~0.5 (in-band).
        let new_emb: Vec<f32> = vec![1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        let mut ex_emb = vec![0.0f32; 8];
        let cos = 0.5_f64;
        let sin = (1.0 - cos * cos).sqrt();
        ex_emb[0] = cos as f32;
        ex_emb[1] = sin as f32;
        let new_emb_json = serde_json::to_string(&new_emb).unwrap();
        let existing_json = format!(
            r#"[{{"claim":{},"id":"exist","embedding":{}}}]"#,
            existing_claim_obj,
            serde_json::to_string(&ex_emb).unwrap()
        );
        let out = kg_detect_contradictions_inner(
            new_claim,
            "new_id",
            &new_emb_json,
            &existing_json,
            0.3,
            0.85,
        )
        .unwrap();
        let parsed: Vec<contradiction::Contradiction> = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].claim_a_id, "new_id");
        assert_eq!(parsed[0].claim_b_id, "exist");
        assert!((parsed[0].similarity - 0.5).abs() < 1e-6);
        // Sanity: the returned entity_id is the deterministic id of "editor".
        assert_eq!(parsed[0].entity_id, claims::deterministic_entity_id("editor"));
    }

    #[test]
    fn wasm_detect_contradictions_rejects_malformed_existing_shape() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0]).unwrap();
        // Wrong shape: missing "id" and "embedding" keys.
        let existing = r#"[{"claim":{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}}]"#;
        let result =
            kg_detect_contradictions_inner(new_claim, "new_id", &emb, existing, 0.3, 0.85);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("existing"), "err: {}", err);
    }

    #[test]
    fn wasm_apply_feedback_returns_clamped_weights() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        // Counterexample with asymmetric deltas — the gradient step stays within [0.05, 0.60].
        let ce = r#"{
            "formula_winner":{"confidence":0.9,"corroboration":3.0,"recency":1.0,"validation":1.0,"weighted_total":0.975},
            "formula_loser":{"confidence":0.3,"corroboration":1.0,"recency":0.1,"validation":0.7,"weighted_total":0.24},
            "user_pinned":"loser"
        }"#;
        let out = kg_apply_feedback_inner(&weights, ce).unwrap();
        let new: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        for v in [new.confidence, new.corroboration, new.recency, new.validation] {
            assert!(v >= 0.05 - 1e-12, "weight below clamp: {}", v);
            assert!(v <= 0.60 + 1e-12, "weight above clamp: {}", v);
        }
        let sum = new.confidence + new.corroboration + new.recency + new.validation;
        assert!(sum >= 0.9 - 1e-9 && sum <= 1.1 + 1e-9, "weight sum out of range: {}", sum);
    }

    // --- Phase 2 Slice 2c: feedback_log bindings ------------------------

    fn sample_entry_json() -> String {
        r#"{"ts":1776384000,"claim_a_id":"0xaaa","claim_b_id":"0xbbb","formula_winner":"a","user_decision":"pin_b","winner_components":{"confidence":0.8,"corroboration":1.732,"recency":0.333,"validation":0.7,"weighted_total":0.7331},"loser_components":{"confidence":0.6,"corroboration":1.0,"recency":0.125,"validation":0.5,"weighted_total":0.4025}}"#.to_string()
    }

    #[test]
    fn wasm_default_weights_file_round_trips() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        let back = kg_parse_weights_file_inner(&pretty).unwrap();
        // Canonical serialisation of the parsed file must match the original.
        assert_eq!(back, out);
    }

    #[test]
    fn wasm_serialize_weights_file_is_pretty() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        assert!(pretty.contains('\n'), "pretty JSON must contain newlines");
        assert!(pretty.contains("  "), "pretty JSON must use 2-space indent");
    }

    #[test]
    fn wasm_parse_weights_file_rejects_malformed() {
        let result = kg_parse_weights_file_inner("not-json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_parse_weights_file_rejects_unknown_version() {
        let bad = r#"{"version":99,"updated_at":0,"weights":{"confidence":0.25,"corroboration":0.15,"recency":0.4,"validation":0.2},"threshold_lower":0.3,"threshold_upper":0.85,"feedback_count":0}"#;
        let result = kg_parse_weights_file_inner(bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("unsupported"));
    }

    #[test]
    fn wasm_append_feedback_to_jsonl_empty_produces_one_line() {
        let out = kg_append_feedback_to_jsonl_inner("", &sample_entry_json()).unwrap();
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn wasm_append_feedback_to_jsonl_existing_produces_two_lines() {
        let entry = sample_entry_json();
        let first = kg_append_feedback_to_jsonl_inner("", &entry).unwrap();
        let second = kg_append_feedback_to_jsonl_inner(&first, &entry).unwrap();
        assert_eq!(second.matches('\n').count(), 2);
    }

    #[test]
    fn wasm_read_feedback_jsonl_round_trip_many_entries() {
        let mut content = String::new();
        for _ in 0..3 {
            content = kg_append_feedback_to_jsonl_inner(&content, &sample_entry_json()).unwrap();
        }
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 3);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn wasm_read_feedback_jsonl_surfaces_warnings_for_bad_lines() {
        let content = "not-json\n".to_string() + &sample_entry_json() + "\n";
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 1);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn wasm_rotate_feedback_log_drops_oldest_when_over_cap() {
        let mut content = String::new();
        for i in 0..5 {
            let entry = sample_entry_json().replace("1776384000", &format!("177638400{}", i));
            content = kg_append_feedback_to_jsonl_inner(&content, &entry).unwrap();
        }
        let rotated = kg_rotate_feedback_log_inner(&content, 3);
        let out = kg_read_feedback_jsonl_inner(&rotated).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        let entries = parsed["entries"].as_array().unwrap();
        assert_eq!(entries.len(), 3);
        // Most recent three kept: ts 1776384002, 1776384003, 1776384004.
        assert_eq!(entries[0]["ts"].as_i64().unwrap(), 1_776_384_002);
        assert_eq!(entries[2]["ts"].as_i64().unwrap(), 1_776_384_004);
    }

    #[test]
    fn wasm_rotate_feedback_log_preserves_content_below_cap() {
        let content = kg_append_feedback_to_jsonl_inner("", &sample_entry_json()).unwrap();
        let rotated = kg_rotate_feedback_log_inner(&content, 10);
        assert_eq!(rotated, content);
    }

    #[test]
    fn wasm_feedback_to_counterexample_pin_b_when_formula_winner_a_returns_ce() {
        // Sample entry already has formula_winner=a, user_decision=pin_b.
        let out = kg_feedback_to_counterexample_inner(&sample_entry_json()).unwrap();
        assert_ne!(out, "null");
        let ce: contradiction::Counterexample = serde_json::from_str(&out).unwrap();
        assert_eq!(ce.user_pinned, contradiction::UserPinned::Loser);
    }

    #[test]
    fn wasm_feedback_to_counterexample_pin_a_when_formula_winner_a_returns_null() {
        let entry = sample_entry_json().replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"pin_a\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn wasm_feedback_to_counterexample_unpin_returns_null() {
        let entry = sample_entry_json().replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"unpin\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn wasm_feedback_to_counterexample_rejects_malformed_entry() {
        let result = kg_feedback_to_counterexample_inner("{not-an-entry");
        assert!(result.is_err());
    }
}
