//! WASM bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features wasm`. Built with `wasm-pack build --target nodejs`.
//!
//! All byte arrays cross the boundary as hex strings. Complex return types
//! (Vec<String>, structs) are serialized as JSON strings or JsValues.

use wasm_bindgen::prelude::*;

use crate::blind;
use crate::claims;
use crate::crypto;
use crate::debrief;
use crate::digest;
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
}
