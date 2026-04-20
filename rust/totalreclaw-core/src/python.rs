//! Python (PyO3) bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features python`. Built with `maturin develop --features python`.
//!
//! Module name: `totalreclaw_core` (underscore, not hyphen).
//!
//! All byte arrays are returned as Python `bytes` objects.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

#[cfg(feature = "managed")]
use crate::search;
#[cfg(feature = "managed")]
use crate::userop;
use crate::{
    blind, claims, contradiction, crypto, debrief, digest, feedback_log, fingerprint, lsh,
    protobuf, reranker, store,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Convert our crate Error to a Python ValueError.
fn to_pyerr(e: crate::Error) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// Extract a `[u8; 32]` from a Python `bytes` object.
fn bytes_to_array32(b: &[u8]) -> PyResult<[u8; 32]> {
    b.try_into()
        .map_err(|_| PyValueError::new_err(format!("expected 32 bytes, got {}", b.len())))
}

// ---------------------------------------------------------------------------
// Key derivation
// ---------------------------------------------------------------------------

/// Derive all crypto keys from a BIP-39 mnemonic (strict checksum validation).
///
/// Returns a dict with keys: salt, auth_key, encryption_key, dedup_key (all bytes).
#[pyfunction]
fn derive_keys_from_mnemonic(py: Python<'_>, mnemonic: &str) -> PyResult<PyObject> {
    let keys = crypto::derive_keys_from_mnemonic(mnemonic).map_err(to_pyerr)?;
    keys_to_dict(py, &keys)
}

/// Derive all crypto keys from a BIP-39 mnemonic (lenient -- skips checksum).
///
/// Returns a dict with keys: salt, auth_key, encryption_key, dedup_key (all bytes).
#[pyfunction]
fn derive_keys_from_mnemonic_lenient(py: Python<'_>, mnemonic: &str) -> PyResult<PyObject> {
    let keys = crypto::derive_keys_from_mnemonic_lenient(mnemonic).map_err(to_pyerr)?;
    keys_to_dict(py, &keys)
}

/// Build a Python dict from DerivedKeys.
fn keys_to_dict(py: Python<'_>, keys: &crypto::DerivedKeys) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    dict.set_item("salt", PyBytes::new(py, &keys.salt))?;
    dict.set_item("auth_key", PyBytes::new(py, &keys.auth_key))?;
    dict.set_item("encryption_key", PyBytes::new(py, &keys.encryption_key))?;
    dict.set_item("dedup_key", PyBytes::new(py, &keys.dedup_key))?;
    Ok(dict.into())
}

/// Derive the 32-byte LSH seed from a BIP-39 mnemonic and salt.
///
/// Returns bytes (32 bytes).
#[pyfunction]
fn derive_lsh_seed<'py>(
    py: Python<'py>,
    mnemonic: &str,
    salt: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let salt_arr = bytes_to_array32(salt)?;
    let seed = crypto::derive_lsh_seed(mnemonic, &salt_arr).map_err(to_pyerr)?;
    Ok(PyBytes::new(py, &seed))
}

/// Compute SHA-256(authKey) as a hex string.
#[pyfunction]
fn compute_auth_key_hash(auth_key: &[u8]) -> PyResult<String> {
    let arr = bytes_to_array32(auth_key)?;
    Ok(crypto::compute_auth_key_hash(&arr))
}

// ---------------------------------------------------------------------------
// Encryption
// ---------------------------------------------------------------------------

/// Encrypt a UTF-8 plaintext string with XChaCha20-Poly1305.
///
/// Returns base64-encoded ciphertext (wire format: nonce || tag || ciphertext).
#[pyfunction]
fn encrypt(plaintext: &str, encryption_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    crypto::encrypt(plaintext, &key).map_err(to_pyerr)
}

/// Decrypt a base64-encoded XChaCha20-Poly1305 blob back to a UTF-8 string.
#[pyfunction]
fn decrypt(encrypted_base64: &str, encryption_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    crypto::decrypt(encrypted_base64, &key).map_err(to_pyerr)
}

// ---------------------------------------------------------------------------
// Blind indices
// ---------------------------------------------------------------------------

/// Generate blind indices (SHA-256 hashes of tokens + stems) for a text string.
///
/// Returns a list of hex strings.
#[pyfunction]
fn generate_blind_indices(text: &str) -> Vec<String> {
    blind::generate_blind_indices(text)
}

// ---------------------------------------------------------------------------
// Content fingerprint
// ---------------------------------------------------------------------------

/// Compute HMAC-SHA256 content fingerprint. Returns 64-char hex string.
#[pyfunction]
fn generate_content_fingerprint(plaintext: &str, dedup_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(dedup_key)?;
    Ok(fingerprint::generate_content_fingerprint(plaintext, &key))
}

/// Normalize text for deterministic fingerprinting (NFC, lowercase, collapse whitespace, trim).
#[pyfunction]
fn normalize_text(text: &str) -> String {
    fingerprint::normalize_text(text)
}

// ---------------------------------------------------------------------------
// LSH Hasher
// ---------------------------------------------------------------------------

/// Locality-Sensitive Hashing (Random Hyperplane LSH).
///
/// Construct with a seed (bytes) and embedding dimensionality.
/// Call ``hash()`` with an embedding vector to get blind-hashed bucket IDs.
#[pyclass(name = "LshHasher")]
struct PyLshHasher {
    inner: lsh::LshHasher,
}

#[pymethods]
impl PyLshHasher {
    /// Create a new LSH hasher.
    ///
    /// Args:
    ///     seed: 32-byte seed (from derive_lsh_seed).
    ///     dims: Embedding dimensionality (e.g. 640).
    ///     n_tables: Number of hash tables (default 20).
    ///     n_bits: Number of bits per table (default 32).
    #[new]
    #[pyo3(signature = (seed, dims, n_tables=20, n_bits=32))]
    fn new(seed: &[u8], dims: usize, n_tables: usize, n_bits: usize) -> PyResult<Self> {
        let inner = lsh::LshHasher::with_params(seed, dims, n_tables, n_bits).map_err(to_pyerr)?;
        Ok(Self { inner })
    }

    /// Hash an embedding vector to blind-hashed bucket IDs.
    ///
    /// Args:
    ///     embedding: List of floats (must have ``dims`` elements).
    ///
    /// Returns:
    ///     List of hex strings (one blind hash per table).
    fn hash(&self, embedding: Vec<f64>) -> PyResult<Vec<String>> {
        self.inner.hash(&embedding).map_err(to_pyerr)
    }

    /// Number of hash tables.
    #[getter]
    fn tables(&self) -> usize {
        self.inner.tables()
    }

    /// Number of bits per table.
    #[getter]
    fn bits(&self) -> usize {
        self.inner.bits()
    }

    /// Embedding dimensionality.
    #[getter]
    fn dimensions(&self) -> usize {
        self.inner.dimensions()
    }
}

// ---------------------------------------------------------------------------
// Protobuf encoding
// ---------------------------------------------------------------------------

/// Encode a fact payload as minimal protobuf wire format.
///
/// Args:
///     json_str: JSON string with keys: id, timestamp, owner, encrypted_blob_hex,
///               blind_indices, decay_score, source, content_fp, agent_id,
///               encrypted_embedding (optional).
///
/// Returns:
///     bytes (protobuf wire format).
#[pyfunction]
fn encode_fact_protobuf<'py>(py: Python<'py>, json_str: &str) -> PyResult<Bound<'py, PyBytes>> {
    let value: serde_json::Value =
        serde_json::from_str(json_str).map_err(|e| PyValueError::new_err(e.to_string()))?;

    let obj = value
        .as_object()
        .ok_or_else(|| PyValueError::new_err("expected JSON object"))?;

    let payload = protobuf::FactPayload {
        id: obj
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        timestamp: obj
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        owner: obj
            .get("owner")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        encrypted_blob_hex: obj
            .get("encrypted_blob_hex")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        blind_indices: obj
            .get("blind_indices")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default(),
        decay_score: obj
            .get("decay_score")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.8),
        source: obj
            .get("source")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        content_fp: obj
            .get("content_fp")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        agent_id: obj
            .get("agent_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        encrypted_embedding: obj
            .get("encrypted_embedding")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string()),
        version: obj
            .get("version")
            .and_then(|v| v.as_u64())
            .map(|v| v as u32)
            .unwrap_or(protobuf::DEFAULT_PROTOBUF_VERSION),
    };

    let encoded = protobuf::encode_fact_protobuf(&payload);
    Ok(PyBytes::new(py, &encoded))
}

/// Encode a tombstone protobuf for soft-deleting a fact.
///
/// Args:
///     fact_id: The fact ID to tombstone.
///     owner: The owner address.
///     version: Outer protobuf schema version (optional, default 3).
///         Pass 4 for Memory Taxonomy v1 tombstones.
///
/// Returns:
///     bytes (protobuf wire format).
#[pyfunction]
#[pyo3(signature = (fact_id, owner, version=None))]
fn encode_tombstone_protobuf<'py>(
    py: Python<'py>,
    fact_id: &str,
    owner: &str,
    version: Option<u32>,
) -> PyResult<Bound<'py, PyBytes>> {
    let encoded = protobuf::encode_tombstone_protobuf(
        fact_id,
        owner,
        version.unwrap_or(protobuf::DEFAULT_PROTOBUF_VERSION),
    );
    Ok(PyBytes::new(py, &encoded))
}

// ---------------------------------------------------------------------------
// Debrief
// ---------------------------------------------------------------------------

/// Parse a debrief LLM response into a list of validated items.
///
/// Returns a list of dicts with keys: text, type, importance.
#[pyfunction]
fn parse_debrief_response(py: Python<'_>, response: &str) -> PyResult<PyObject> {
    let items = debrief::parse_debrief_response(response);
    let list = PyList::empty(py);
    for item in &items {
        let dict = PyDict::new(py);
        dict.set_item("text", &item.text)?;
        dict.set_item("type", item.item_type.to_string())?;
        dict.set_item("importance", item.importance)?;
        list.append(dict)?;
    }
    Ok(list.into())
}

/// Get the canonical debrief system prompt template.
///
/// Contains ``{already_stored_facts}`` placeholder.
#[pyfunction]
fn get_debrief_system_prompt() -> &'static str {
    debrief::DEBRIEF_SYSTEM_PROMPT
}

// ---------------------------------------------------------------------------
// Canonical extraction + compaction system prompts (core 2.2.0 hoist)
// ---------------------------------------------------------------------------

/// Get the canonical v1 merged-topic extraction system prompt.
///
/// Single source of truth across Python (via this binding), TypeScript
/// (via the WASM binding `getExtractionSystemPrompt`), and Rust callers.
/// The prompt includes the Rule 6 meta-request filter.
#[pyfunction]
fn get_extraction_system_prompt() -> &'static str {
    crate::prompts::get_extraction_system_prompt()
}

/// Get the canonical v1 compaction system prompt.
///
/// Used on the pre-compaction surface (importance floor 5, not 6).
#[pyfunction]
fn get_compaction_system_prompt() -> &'static str {
    crate::prompts::get_compaction_system_prompt()
}

// ---------------------------------------------------------------------------
// Reranker
// ---------------------------------------------------------------------------

/// Rerank candidates using BM25 + Cosine + RRF fusion (v0-compatible).
///
/// Args:
///     query: Search query text.
///     query_embedding: Query embedding vector (list of floats).
///     candidates_json: JSON array of ``{ id, text, embedding, timestamp, source? }`` objects.
///     top_k: Number of top results to return.
///
/// Returns:
///     JSON string of ranked results.
#[pyfunction]
#[pyo3(name = "rerank")]
fn py_rerank(
    query: &str,
    query_embedding: Vec<f32>,
    candidates_json: &str,
    top_k: usize,
) -> PyResult<String> {
    let candidates: Vec<reranker::Candidate> =
        serde_json::from_str(candidates_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let results = reranker::rerank(query, &query_embedding, &candidates, top_k)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Rerank candidates with a config flag (Retrieval v2 Tier 1).
///
/// When ``apply_source_weights`` is True, each candidate's final fused score is
/// multiplied by the provenance weight derived from its ``source`` field.
/// Legacy candidates without ``source`` receive the v0 fallback weight.
///
/// Args:
///     query: Search query text.
///     query_embedding: Query embedding vector (list of floats).
///     candidates_json: JSON array of ``{ id, text, embedding, timestamp, source? }`` objects.
///     top_k: Number of top results to return.
///     apply_source_weights: If True, apply v1 source weighting.
///
/// Returns:
///     JSON string of ranked results including ``source_weight``.
#[pyfunction]
#[pyo3(name = "rerank_with_config")]
fn py_rerank_with_config(
    query: &str,
    query_embedding: Vec<f32>,
    candidates_json: &str,
    top_k: usize,
    apply_source_weights: bool,
) -> PyResult<String> {
    let candidates: Vec<reranker::Candidate> =
        serde_json::from_str(candidates_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let config = reranker::RerankerConfig {
        apply_source_weights,
    };
    let results = reranker::rerank_with_config(query, &query_embedding, &candidates, top_k, config)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Return the source weight multiplier for a given source string.
///
/// Accepted values: ``"user" | "user-inferred" | "assistant" | "external" | "derived"``.
///
/// Unknown input is routed through ``MemorySource::from_str_lossy`` which
/// falls back to ``user-inferred`` (weight 0.90). Callers who want the
/// "no source field at all" fallback (weight 0.85) should call
/// :func:`legacy_claim_fallback_weight` instead.
#[pyfunction]
#[pyo3(name = "source_weight")]
fn py_source_weight(source: &str) -> f64 {
    let src = crate::claims::MemorySource::from_str_lossy(source);
    reranker::source_weight(src)
}

/// Return the v1 legacy-claim fallback weight (applied to candidates that
/// have no ``source`` field).
#[pyfunction]
#[pyo3(name = "legacy_claim_fallback_weight")]
fn py_legacy_claim_fallback_weight() -> f64 {
    reranker::LEGACY_CLAIM_FALLBACK_WEIGHT
}

/// Validate a Memory Taxonomy v1 claim (JSON in, canonical JSON out).
///
/// Raises ``ValueError`` on any schema violation (wrong type token, missing
/// required field, unsupported schema_version).
#[pyfunction]
#[pyo3(name = "validate_memory_claim_v1")]
fn py_validate_memory_claim_v1(claim_json: &str) -> PyResult<String> {
    let claim: crate::claims::MemoryClaimV1 = serde_json::from_str(claim_json)
        .map_err(|e| PyValueError::new_err(format!("invalid v1 claim: {}", e)))?;
    if claim.schema_version != crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION {
        return Err(PyValueError::new_err(format!(
            "unsupported schema_version {}: only {} is supported",
            claim.schema_version,
            crate::claims::MEMORY_CLAIM_V1_SCHEMA_VERSION
        )));
    }
    serde_json::to_string(&claim)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Case-insensitive parse of a memory type string. Unknown input returns "claim".
#[pyfunction]
#[pyo3(name = "parse_memory_type_v1")]
fn py_parse_memory_type_v1(s: &str) -> String {
    let t = crate::claims::MemoryTypeV1::from_str_lossy(s);
    serde_json::to_string(&t)
        .unwrap_or_else(|_| "\"claim\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a memory source string. Unknown input returns "user-inferred".
#[pyfunction]
#[pyo3(name = "parse_memory_source")]
fn py_parse_memory_source(s: &str) -> String {
    let src = crate::claims::MemorySource::from_str_lossy(s);
    serde_json::to_string(&src)
        .unwrap_or_else(|_| "\"user-inferred\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Case-insensitive parse of a v1.1 pin_status string. Unknown input returns "unpinned".
#[pyfunction]
#[pyo3(name = "parse_pin_status")]
fn py_parse_pin_status(s: &str) -> String {
    let st = crate::claims::PinStatus::from_str_lossy(s);
    serde_json::to_string(&st)
        .unwrap_or_else(|_| "\"unpinned\"".to_string())
        .trim_matches('"')
        .to_string()
}

/// Check whether a JSON-encoded claim is pinned.
///
/// Recognises both the v0 short-key sentinel (``st == "p"``) and the v1.1
/// field (``pin_status == "pinned"``). Returns ``False`` on any parse failure.
#[pyfunction]
#[pyo3(name = "is_pinned_claim_json")]
fn py_is_pinned_claim_json(claim_json: &str) -> bool {
    crate::claims::is_pinned_json(claim_json)
}

/// Cosine similarity between two f32 vectors.
///
/// Args:
///     a: First vector (list of floats).
///     b: Second vector (list of floats).
///
/// Returns:
///     Cosine similarity as a float (0.0 to 1.0 for normalized vectors).
#[pyfunction]
#[pyo3(name = "cosine_similarity")]
fn py_cosine_similarity(a: Vec<f32>, b: Vec<f32>) -> f64 {
    reranker::cosine_similarity_f32(&a, &b)
}

// ---------------------------------------------------------------------------
// Wallet derivation
// ---------------------------------------------------------------------------

/// Derive an Ethereum EOA wallet from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Returns a JSON string: ``{"private_key": "hex...", "address": "0x..."}``.
#[pyfunction]
fn derive_eoa(mnemonic: &str) -> PyResult<String> {
    let w = crate::wallet::derive_eoa(mnemonic).map_err(to_pyerr)?;
    serde_json::to_string(&w).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Derive just the Ethereum EOA address from a BIP-39 mnemonic.
///
/// Returns: ``"0x..."`` (lowercase hex).
#[pyfunction]
fn derive_eoa_address(mnemonic: &str) -> PyResult<String> {
    crate::wallet::derive_eoa_address(mnemonic).map_err(to_pyerr)
}

// ---------------------------------------------------------------------------
// UserOp (ERC-4337) — feature-gated: managed
// ---------------------------------------------------------------------------

/// Encode a single fact submission as SimpleAccount.execute() calldata.
///
/// Args:
///     protobuf_payload: Raw protobuf bytes.
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
fn encode_single_call<'py>(py: Python<'py>, protobuf_payload: &[u8]) -> Bound<'py, PyBytes> {
    let encoded = userop::encode_single_call(protobuf_payload);
    PyBytes::new(py, &encoded)
}

/// Encode multiple fact submissions as SimpleAccount.executeBatch() calldata.
///
/// Args:
///     payloads: List of bytes (raw protobuf payloads).
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
fn encode_batch_call<'py>(
    py: Python<'py>,
    payloads: Vec<Vec<u8>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let encoded =
        userop::encode_batch_call(&payloads).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &encoded))
}

/// Compute the ERC-4337 v0.7 UserOp hash for signing.
///
/// Args:
///     userop_json: JSON string of a UserOperationV7 struct.
///     entrypoint: EntryPoint address (0x-prefixed).
///     chain_id: Chain ID (e.g. 84532).
///
/// Returns:
///     bytes (32-byte hash).
#[cfg(feature = "managed")]
#[pyfunction]
fn hash_userop<'py>(
    py: Python<'py>,
    userop_json: &str,
    entrypoint: &str,
    chain_id: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    let op: userop::UserOperationV7 = serde_json::from_str(userop_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid UserOp JSON: {}", e)))?;
    let hash = userop::hash_userop(&op, entrypoint, chain_id)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &hash))
}

/// Sign a UserOp hash with an ECDSA private key (EIP-191 prefixed).
///
/// Args:
///     hash: 32-byte UserOp hash.
///     private_key: 32-byte private key.
///
/// Returns:
///     bytes (65-byte signature: r + s + v).
#[cfg(feature = "managed")]
#[pyfunction]
fn sign_userop<'py>(
    py: Python<'py>,
    hash: &[u8],
    private_key: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let h = hash
        .try_into()
        .map_err(|_| PyValueError::new_err(format!("Hash must be 32 bytes, got {}", hash.len())))?;
    let pk = bytes_to_array32(private_key)?;
    let sig = userop::sign_userop(&h, &pk).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &sig))
}

// ---------------------------------------------------------------------------
// Store pipeline (pure computation, no I/O)
// ---------------------------------------------------------------------------

/// Prepare a fact for on-chain storage.
///
/// Pure computation: encrypt, generate indices, encode protobuf.
/// Does NOT submit -- the host handles I/O.
///
/// Args:
///     text: Plaintext fact content.
///     encryption_key: 32-byte encryption key.
///     dedup_key: 32-byte dedup key.
///     lsh_hasher: A LshHasher instance.
///     embedding: List of floats (pre-computed embedding vector).
///     importance: Importance score on 1-10 scale.
///     source: Source tag (e.g. "auto_extraction").
///     owner: Owner address (Smart Account address).
///     agent_id: Agent identifier.
///
/// Returns:
///     JSON string of PreparedFact.
#[pyfunction]
fn prepare_fact(
    text: &str,
    encryption_key: &[u8],
    dedup_key: &[u8],
    lsh_hasher: &PyLshHasher,
    embedding: Vec<f32>,
    importance: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> PyResult<String> {
    let enc_key = bytes_to_array32(encryption_key)?;
    let ded_key = bytes_to_array32(dedup_key)?;

    let prepared = store::prepare_fact(
        text,
        &enc_key,
        &ded_key,
        &lsh_hasher.inner,
        &embedding,
        importance,
        source,
        owner,
        agent_id,
    )
    .map_err(to_pyerr)?;

    serde_json::to_string(&prepared)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Prepare a fact with a pre-normalized decay score (already 0.0-1.0).
///
/// Same as `prepare_fact()` but takes a raw decay score.
#[pyfunction]
fn prepare_fact_with_decay_score(
    text: &str,
    encryption_key: &[u8],
    dedup_key: &[u8],
    lsh_hasher: &PyLshHasher,
    embedding: Vec<f32>,
    decay_score: f64,
    source: &str,
    owner: &str,
    agent_id: &str,
) -> PyResult<String> {
    let enc_key = bytes_to_array32(encryption_key)?;
    let ded_key = bytes_to_array32(dedup_key)?;

    let prepared = store::prepare_fact_with_decay_score(
        text,
        &enc_key,
        &ded_key,
        &lsh_hasher.inner,
        &embedding,
        decay_score,
        source,
        owner,
        agent_id,
    )
    .map_err(to_pyerr)?;

    serde_json::to_string(&prepared)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Build ABI-encoded calldata for a single prepared fact.
///
/// Args:
///     prepared_json: JSON string of a PreparedFact.
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
fn build_single_calldata_from_prepared<'py>(
    py: Python<'py>,
    prepared_json: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let prepared: store::PreparedFact = serde_json::from_str(prepared_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid PreparedFact JSON: {}", e)))?;
    let calldata = store::build_single_calldata(&prepared);
    Ok(PyBytes::new(py, &calldata))
}

/// Build ABI-encoded calldata for a batch of prepared facts.
///
/// Args:
///     prepared_array_json: JSON array of PreparedFact objects.
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
fn build_batch_calldata_from_prepared<'py>(
    py: Python<'py>,
    prepared_array_json: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let prepared: Vec<store::PreparedFact> = serde_json::from_str(prepared_array_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid PreparedFact array JSON: {}", e)))?;
    let calldata =
        store::build_batch_calldata(&prepared).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &calldata))
}

/// Prepare a tombstone (soft-delete) protobuf.
///
/// Args:
///     fact_id: The fact ID to tombstone.
///     owner: The owner address.
///
/// Returns:
///     bytes (protobuf wire format).
#[pyfunction]
fn prepare_tombstone<'py>(py: Python<'py>, fact_id: &str, owner: &str) -> Bound<'py, PyBytes> {
    let bytes = store::prepare_tombstone(fact_id, owner);
    PyBytes::new(py, &bytes)
}

// ---------------------------------------------------------------------------
// Search pipeline (feature-gated: managed)
// ---------------------------------------------------------------------------

/// Generate all search trapdoors for a query (word hashes + LSH bucket hashes).
///
/// Args:
///     query: The search query text.
///     query_embedding: List of floats (query embedding vector).
///     lsh_hasher: A LshHasher instance.
///
/// Returns:
///     List of hex-encoded trapdoor strings.
#[cfg(feature = "managed")]
#[pyfunction]
fn generate_search_trapdoors(
    query: &str,
    query_embedding: Vec<f32>,
    lsh_hasher: &PyLshHasher,
) -> PyResult<Vec<String>> {
    search::generate_search_trapdoors(query, &query_embedding, &lsh_hasher.inner).map_err(to_pyerr)
}

/// Parse a blind index search GraphQL response into SubgraphFact list.
///
/// Args:
///     response_json: Raw JSON string from the GraphQL response.
///
/// Returns:
///     JSON string of SubgraphFact array.
#[cfg(feature = "managed")]
#[pyfunction]
fn parse_search_response(response_json: &str) -> PyResult<String> {
    let facts = search::parse_search_response(response_json).map_err(to_pyerr)?;
    serde_json::to_string(&facts)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Parse a broadened search GraphQL response into SubgraphFact list.
///
/// Args:
///     response_json: Raw JSON string from the GraphQL response.
///
/// Returns:
///     JSON string of SubgraphFact array.
#[cfg(feature = "managed")]
#[pyfunction]
fn parse_broadened_response(response_json: &str) -> PyResult<String> {
    let facts = search::parse_broadened_response(response_json).map_err(to_pyerr)?;
    serde_json::to_string(&facts)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Decrypt and rerank search candidates.
///
/// Args:
///     facts_json: JSON array of SubgraphFact objects.
///     query: The search query text.
///     query_embedding: List of floats (query embedding vector).
///     encryption_key: 32-byte encryption key.
///     top_k: Number of top results to return.
///
/// Returns:
///     JSON string of ranked results.
#[cfg(feature = "managed")]
#[pyfunction]
fn decrypt_and_rerank(
    facts_json: &str,
    query: &str,
    query_embedding: Vec<f32>,
    encryption_key: &[u8],
    top_k: usize,
) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    let facts: Vec<search::SubgraphFact> = serde_json::from_str(facts_json)
        .map_err(|e| PyValueError::new_err(format!("Invalid SubgraphFact array JSON: {}", e)))?;
    let results = search::decrypt_and_rerank_with_key(&facts, query, &query_embedding, &key, top_k)
        .map_err(to_pyerr)?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Get the GraphQL query string for blind index search.
#[cfg(feature = "managed")]
#[pyfunction]
fn get_search_query() -> &'static str {
    search::search_query()
}

/// Get the GraphQL query string for broadened (fallback) search.
#[cfg(feature = "managed")]
#[pyfunction]
fn get_broadened_search_query() -> &'static str {
    search::broadened_search_query()
}

/// Get the GraphQL query string for paginated export.
#[cfg(feature = "managed")]
#[pyfunction]
fn get_export_query() -> &'static str {
    search::export_query()
}

/// Convert a subgraph hex blob to base64 for decryption.
///
/// Args:
///     hex_blob: Hex string (optionally ``0x``-prefixed) from the subgraph.
///
/// Returns:
///     Base64-encoded bytes, or None if the hex is invalid.
#[cfg(feature = "managed")]
#[pyfunction]
fn hex_blob_to_base64(hex_blob: &str) -> Option<String> {
    search::hex_blob_to_base64(hex_blob)
}

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 1
// ---------------------------------------------------------------------------

// Inner (non-PyO3) implementations so unit tests can exercise logic without
// linking against libpython. The pyfunction wrappers below just call these
// and map String errors to PyValueError.

fn kg_parse_claim_or_legacy_inner(decrypted: &str) -> Result<String, String> {
    let claim = claims::parse_claim_or_legacy(decrypted);
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

fn kg_canonicalize_claim_inner(claim_json: &str) -> Result<String, String> {
    let claim: claims::Claim =
        serde_json::from_str(claim_json).map_err(|e| format!("invalid claim JSON: {}", e))?;
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

fn kg_build_template_digest_inner(
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, String> {
    let parsed: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::build_template_digest(&parsed, now_unix_seconds);
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

fn kg_build_digest_prompt_inner(claims_json: &str) -> Result<String, String> {
    let parsed: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
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
    let source_claims: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::assemble_digest_from_llm(&parsed, &source_claims, now_unix_seconds)?;
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

/// Normalize an entity name (NFC, lowercase, trim, collapse whitespace).
#[pyfunction]
#[pyo3(name = "normalize_entity_name")]
fn py_normalize_entity_name(name: &str) -> String {
    claims::normalize_entity_name(name)
}

/// Deterministic entity ID from a name (first 8 bytes of SHA256 as hex).
#[pyfunction]
#[pyo3(name = "deterministic_entity_id")]
fn py_deterministic_entity_id(name: &str) -> String {
    claims::deterministic_entity_id(name)
}

/// Parse a decrypted blob as a Claim, falling back to legacy formats.
/// Returns JSON-serialized Claim string.
#[pyfunction]
#[pyo3(name = "parse_claim_or_legacy")]
fn py_parse_claim_or_legacy(decrypted: &str) -> PyResult<String> {
    kg_parse_claim_or_legacy_inner(decrypted).map_err(PyValueError::new_err)
}

/// Canonicalize a Claim JSON: strict-parse as Claim, re-serialize to canonical bytes.
/// Rejects legacy or malformed input. Use before encryption for byte-identical
/// blobs across TS/Python/Rust.
#[pyfunction]
#[pyo3(name = "canonicalize_claim")]
fn py_canonicalize_claim(claim_json: &str) -> PyResult<String> {
    kg_canonicalize_claim_inner(claim_json).map_err(PyValueError::new_err)
}

/// Build a template digest from a JSON array of Claim.
/// Returns JSON-serialized Digest.
#[pyfunction]
#[pyo3(name = "build_template_digest")]
fn py_build_template_digest(claims_json: &str, now_unix_seconds: i64) -> PyResult<String> {
    kg_build_template_digest_inner(claims_json, now_unix_seconds).map_err(PyValueError::new_err)
}

/// Build the LLM prompt for digest compilation.
/// Claims array must be non-empty; empty raises ValueError.
#[pyfunction]
#[pyo3(name = "build_digest_prompt")]
fn py_build_digest_prompt(claims_json: &str) -> PyResult<String> {
    kg_build_digest_prompt_inner(claims_json).map_err(PyValueError::new_err)
}

/// Parse an LLM digest response string.
/// Returns JSON-serialized ParsedDigestResponse.
#[pyfunction]
#[pyo3(name = "parse_digest_response")]
fn py_parse_digest_response(raw: &str) -> PyResult<String> {
    kg_parse_digest_response_inner(raw).map_err(PyValueError::new_err)
}

/// Assemble a full Digest from a parsed LLM response and source claims.
#[pyfunction]
#[pyo3(name = "assemble_digest_from_llm")]
fn py_assemble_digest_from_llm(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> PyResult<String> {
    kg_assemble_digest_from_llm_inner(parsed_json, claims_json, now_unix_seconds)
        .map_err(PyValueError::new_err)
}

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 2: contradiction detection + feedback log
// ---------------------------------------------------------------------------

/// Input shape for `detect_contradictions`: array of these as `existing_json`.
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
    let claim: claims::Claim =
        serde_json::from_str(claim_json).map_err(|e| format!("invalid claim JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
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
    let claim_a: claims::Claim =
        serde_json::from_str(claim_a_json).map_err(|e| format!("invalid claim_a JSON: {}", e))?;
    let claim_b: claims::Claim =
        serde_json::from_str(claim_b_json).map_err(|e| format!("invalid claim_b JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
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
    let items: Vec<DetectContradictionsItem> =
        serde_json::from_str(existing_json).map_err(|e| {
            format!(
                "invalid existing JSON (expected array of {{claim, id, embedding}}): {}",
                e
            )
        })?;
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
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
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
    let f: feedback_log::WeightsFile =
        serde_json::from_str(file_json).map_err(|e| format!("invalid weights file JSON: {}", e))?;
    Ok(feedback_log::serialize_weights_file(&f))
}

fn kg_parse_weights_file_inner(content: &str) -> Result<String, String> {
    let f = feedback_log::parse_weights_file(content)?;
    serde_json::to_string(&f).map_err(|e| e.to_string())
}

fn kg_append_feedback_to_jsonl_inner(existing: &str, entry_json: &str) -> Result<String, String> {
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
    let cap = if max_lines < 0 {
        0usize
    } else {
        max_lines as usize
    };
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
#[pyfunction]
#[pyo3(name = "default_resolution_weights")]
fn py_default_resolution_weights() -> PyResult<String> {
    kg_default_resolution_weights_inner().map_err(PyValueError::new_err)
}

/// Compute a claim's score components for contradiction resolution.
#[pyfunction]
#[pyo3(name = "compute_score_components")]
fn py_compute_score_components(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> PyResult<String> {
    kg_compute_score_components_inner(claim_json, now_unix_seconds, weights_json)
        .map_err(PyValueError::new_err)
}

/// Run the resolution formula on two contradicting claims; returns ResolutionOutcome JSON.
#[pyfunction]
#[pyo3(name = "resolve_pair")]
fn py_resolve_pair(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> PyResult<String> {
    kg_resolve_pair_inner(
        claim_a_json,
        claim_a_id,
        claim_b_json,
        claim_b_id,
        now_unix_seconds,
        weights_json,
    )
    .map_err(PyValueError::new_err)
}

/// Detect contradictions between a new claim and existing claims (array of {claim, id, embedding}).
#[pyfunction]
#[pyo3(name = "detect_contradictions")]
fn py_detect_contradictions(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> PyResult<String> {
    kg_detect_contradictions_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        existing_json,
        lower_threshold,
        upper_threshold,
    )
    .map_err(PyValueError::new_err)
}

/// Apply a single counterexample to the weights; returns updated ResolutionWeights JSON.
#[pyfunction]
#[pyo3(name = "apply_feedback")]
fn py_apply_feedback(weights_json: &str, counterexample_json: &str) -> PyResult<String> {
    kg_apply_feedback_inner(weights_json, counterexample_json).map_err(PyValueError::new_err)
}

/// Build a fresh default WeightsFile JSON with the given timestamp.
#[pyfunction]
#[pyo3(name = "default_weights_file")]
fn py_default_weights_file(now_unix_seconds: i64) -> PyResult<String> {
    kg_default_weights_file_inner(now_unix_seconds).map_err(PyValueError::new_err)
}

/// Serialize a WeightsFile JSON to pretty-printed JSON (2-space indent).
#[pyfunction]
#[pyo3(name = "serialize_weights_file")]
fn py_serialize_weights_file(file_json: &str) -> PyResult<String> {
    kg_serialize_weights_file_inner(file_json).map_err(PyValueError::new_err)
}

/// Parse a WeightsFile from JSON; rejects unknown versions and malformed input.
#[pyfunction]
#[pyo3(name = "parse_weights_file")]
fn py_parse_weights_file(content: &str) -> PyResult<String> {
    kg_parse_weights_file_inner(content).map_err(PyValueError::new_err)
}

/// Append one feedback entry to existing JSONL content.
#[pyfunction]
#[pyo3(name = "append_feedback_to_jsonl")]
fn py_append_feedback_to_jsonl(existing: &str, entry_json: &str) -> PyResult<String> {
    kg_append_feedback_to_jsonl_inner(existing, entry_json).map_err(PyValueError::new_err)
}

/// Parse JSONL content. Returns JSON: `{"entries": [...], "warnings": [...]}`.
#[pyfunction]
#[pyo3(name = "read_feedback_jsonl")]
fn py_read_feedback_jsonl(content: &str) -> PyResult<String> {
    kg_read_feedback_jsonl_inner(content).map_err(PyValueError::new_err)
}

/// Keep only the most recent `max_lines` non-empty feedback log lines.
#[pyfunction]
#[pyo3(name = "rotate_feedback_log")]
fn py_rotate_feedback_log(content: &str, max_lines: i64) -> String {
    kg_rotate_feedback_log_inner(content, max_lines)
}

/// Convert a feedback entry into a counterexample; returns JSON or "null".
#[pyfunction]
#[pyo3(name = "feedback_to_counterexample")]
fn py_feedback_to_counterexample(entry_json: &str) -> PyResult<String> {
    kg_feedback_to_counterexample_inner(entry_json).map_err(PyValueError::new_err)
}

// ---------------------------------------------------------------------------
// Pin status + decision log (Steps B & C)
// ---------------------------------------------------------------------------

use crate::decision_log;

/// Check whether a JSON-serialized claim has pinned status.
#[pyfunction]
#[pyo3(name = "is_pinned_claim")]
fn py_is_pinned_claim(claim_json: &str) -> bool {
    claims::is_pinned_json(claim_json)
}

/// Apply pin-status and tie-zone checks to a resolution outcome.
/// Returns a JSON-serialized ResolutionAction.
#[pyfunction]
#[pyo3(name = "respect_pin_in_resolution")]
fn py_respect_pin_in_resolution(
    existing_claim_json: &str,
    new_claim_id: &str,
    existing_claim_id: &str,
    resolution_winner: &str,
    score_gap: f64,
    similarity: f64,
    tie_tolerance: f64,
) -> PyResult<String> {
    let action = claims::respect_pin_in_resolution(
        existing_claim_json,
        new_claim_id,
        existing_claim_id,
        resolution_winner,
        score_gap,
        similarity,
        tie_tolerance,
    );
    serde_json::to_string(&action).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Find the loser claim JSON from the decision log for a given fact ID.
/// Returns the loser_claim_json string, or None.
#[pyfunction]
#[pyo3(name = "find_loser_claim_in_decision_log")]
fn py_find_loser_claim_in_decision_log(fact_id: &str, log_content: &str) -> Option<String> {
    decision_log::find_loser_claim_in_decision_log(fact_id, log_content)
}

/// Find a decision-log entry matching a fact as winner or loser.
/// Returns the JSON-serialized DecisionLogEntry, or None.
#[pyfunction]
#[pyo3(name = "find_decision_for_pin")]
fn py_find_decision_for_pin(fact_id: &str, role: &str, log_content: &str) -> Option<String> {
    decision_log::find_decision_for_pin(fact_id, role, log_content)
}

/// Build a FeedbackEntry JSON from a decision-log entry JSON + pin action.
/// Returns the JSON string, or None on failure.
#[pyfunction]
#[pyo3(name = "build_feedback_from_decision")]
fn py_build_feedback_from_decision(
    decision_json: &str,
    action: &str,
    now_unix: i64,
) -> Option<String> {
    decision_log::build_feedback_from_decision(decision_json, action, now_unix)
}

/// Append one decision entry to existing JSONL content.
#[pyfunction]
#[pyo3(name = "append_decision_entry")]
fn py_append_decision_entry(existing_content: &str, entry_json: &str) -> String {
    decision_log::append_decision_entry(existing_content, entry_json)
}

/// Decision log max lines constant.
#[pyfunction]
#[pyo3(name = "decision_log_max_lines")]
fn py_decision_log_max_lines() -> usize {
    decision_log::DECISION_LOG_MAX_LINES
}

/// Contradiction candidate cap constant.
#[pyfunction]
#[pyo3(name = "contradiction_candidate_cap")]
fn py_contradiction_candidate_cap() -> usize {
    decision_log::CONTRADICTION_CANDIDATE_CAP
}

/// Tie-zone score tolerance constant.
#[pyfunction]
#[pyo3(name = "tie_zone_score_tolerance")]
fn py_tie_zone_score_tolerance() -> f64 {
    claims::TIE_ZONE_SCORE_TOLERANCE
}

// ---------------------------------------------------------------------------
// Step D: Contradiction orchestration bindings
// ---------------------------------------------------------------------------

fn kg_resolve_with_candidates_inner(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    candidates_json: &str,
    weights_json: &str,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix: i64,
    tie_tolerance: f64,
) -> Result<String, String> {
    let new_claim: claims::Claim = serde_json::from_str(new_claim_json)
        .map_err(|e| format!("invalid new_claim JSON: {}", e))?;
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| format!("invalid new_embedding JSON: {}", e))?;
    let items: Vec<DetectContradictionsItem> = serde_json::from_str(candidates_json)
        .map_err(|e| format!("invalid candidates JSON: {}", e))?;
    let candidates: Vec<(claims::Claim, String, Vec<f32>)> = items
        .into_iter()
        .map(|it| (it.claim, it.id, it.embedding))
        .collect();
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
    let actions = contradiction::resolve_with_candidates(
        &new_claim,
        new_claim_id,
        &new_embedding,
        &candidates,
        &weights,
        threshold_lower,
        threshold_upper,
        now_unix,
        tie_tolerance,
    );
    serde_json::to_string(&actions).map_err(|e| e.to_string())
}

fn kg_build_decision_log_entries_inner(
    actions_json: &str,
    new_claim_json: &str,
    existing_claims_json: &str,
    mode: &str,
    now_unix: i64,
) -> Result<String, String> {
    let actions: Vec<claims::ResolutionAction> =
        serde_json::from_str(actions_json).map_err(|e| format!("invalid actions JSON: {}", e))?;
    let existing_map: std::collections::HashMap<String, String> =
        serde_json::from_str(existing_claims_json)
            .map_err(|e| format!("invalid existing_claims JSON: {}", e))?;
    let entries = contradiction::build_decision_log_entries(
        &actions,
        new_claim_json,
        &existing_map,
        mode,
        now_unix,
    );
    serde_json::to_string(&entries).map_err(|e| e.to_string())
}

/// Orchestrate contradiction detection + resolution for a new claim against candidates.
/// Returns a JSON array of ResolutionAction.
#[pyfunction]
#[pyo3(name = "resolve_with_candidates")]
fn py_resolve_with_candidates(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    candidates_json: &str,
    weights_json: &str,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix: i64,
    tie_tolerance: f64,
) -> PyResult<String> {
    kg_resolve_with_candidates_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        candidates_json,
        weights_json,
        threshold_lower,
        threshold_upper,
        now_unix,
        tie_tolerance,
    )
    .map_err(PyValueError::new_err)
}

/// Build decision log entries from resolution actions.
/// Returns a JSON array of DecisionLogEntry.
#[pyfunction]
#[pyo3(name = "build_decision_log_entries")]
fn py_build_decision_log_entries(
    actions_json: &str,
    new_claim_json: &str,
    existing_claims_json: &str,
    mode: &str,
    now_unix: i64,
) -> PyResult<String> {
    kg_build_decision_log_entries_inner(
        actions_json,
        new_claim_json,
        existing_claims_json,
        mode,
        now_unix,
    )
    .map_err(PyValueError::new_err)
}

/// Filter resolution actions by mode ("active" passes through, "shadow"/"off" returns empty).
/// Returns a JSON array of ResolutionAction.
#[pyfunction]
#[pyo3(name = "filter_shadow_mode")]
fn py_filter_shadow_mode(actions_json: &str, mode: &str) -> PyResult<String> {
    let actions: Vec<claims::ResolutionAction> = serde_json::from_str(actions_json)
        .map_err(|e| PyValueError::new_err(format!("invalid actions JSON: {}", e)))?;
    let filtered = contradiction::filter_shadow_mode(actions, mode);
    serde_json::to_string(&filtered).map_err(|e| PyValueError::new_err(e.to_string()))
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

/// TotalReclaw core crypto primitives (Rust implementation).
///
/// This module provides byte-for-byte compatible implementations of all
/// TotalReclaw cryptographic operations: key derivation, XChaCha20-Poly1305
/// encryption, blind indices, content fingerprinting, LSH hashing,
/// protobuf encoding, and debrief parsing.
#[pymodule]
fn totalreclaw_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Key derivation
    m.add_function(wrap_pyfunction!(derive_keys_from_mnemonic, m)?)?;
    m.add_function(wrap_pyfunction!(derive_keys_from_mnemonic_lenient, m)?)?;
    m.add_function(wrap_pyfunction!(derive_lsh_seed, m)?)?;
    m.add_function(wrap_pyfunction!(compute_auth_key_hash, m)?)?;

    // Encryption
    m.add_function(wrap_pyfunction!(encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(decrypt, m)?)?;

    // Search
    m.add_function(wrap_pyfunction!(generate_blind_indices, m)?)?;
    m.add_function(wrap_pyfunction!(generate_content_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(normalize_text, m)?)?;

    // LSH
    m.add_class::<PyLshHasher>()?;

    // Protobuf
    m.add_function(wrap_pyfunction!(encode_fact_protobuf, m)?)?;
    m.add_function(wrap_pyfunction!(encode_tombstone_protobuf, m)?)?;

    // Debrief
    m.add_function(wrap_pyfunction!(parse_debrief_response, m)?)?;
    m.add_function(wrap_pyfunction!(get_debrief_system_prompt, m)?)?;

    // Canonical extraction + compaction system prompts (core 2.2.0 hoist).
    m.add_function(wrap_pyfunction!(get_extraction_system_prompt, m)?)?;
    m.add_function(wrap_pyfunction!(get_compaction_system_prompt, m)?)?;

    // Reranker
    m.add_function(wrap_pyfunction!(py_rerank, m)?)?;
    m.add_function(wrap_pyfunction!(py_rerank_with_config, m)?)?;
    m.add_function(wrap_pyfunction!(py_source_weight, m)?)?;
    m.add_function(wrap_pyfunction!(py_legacy_claim_fallback_weight, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate_memory_claim_v1, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_memory_type_v1, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_memory_source, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_pin_status, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_pinned_claim_json, m)?)?;
    m.add_function(wrap_pyfunction!(py_cosine_similarity, m)?)?;

    // Wallet derivation
    m.add_function(wrap_pyfunction!(derive_eoa, m)?)?;
    m.add_function(wrap_pyfunction!(derive_eoa_address, m)?)?;

    // Store pipeline
    m.add_function(wrap_pyfunction!(prepare_fact, m)?)?;
    m.add_function(wrap_pyfunction!(prepare_fact_with_decay_score, m)?)?;
    m.add_function(wrap_pyfunction!(prepare_tombstone, m)?)?;

    // UserOp (ERC-4337) — feature-gated: managed
    #[cfg(feature = "managed")]
    {
        m.add_function(wrap_pyfunction!(encode_single_call, m)?)?;
        m.add_function(wrap_pyfunction!(encode_batch_call, m)?)?;
        m.add_function(wrap_pyfunction!(hash_userop, m)?)?;
        m.add_function(wrap_pyfunction!(sign_userop, m)?)?;
        m.add_function(wrap_pyfunction!(build_single_calldata_from_prepared, m)?)?;
        m.add_function(wrap_pyfunction!(build_batch_calldata_from_prepared, m)?)?;
    }

    // Search pipeline — feature-gated: managed
    #[cfg(feature = "managed")]
    {
        m.add_function(wrap_pyfunction!(generate_search_trapdoors, m)?)?;
        m.add_function(wrap_pyfunction!(parse_search_response, m)?)?;
        m.add_function(wrap_pyfunction!(parse_broadened_response, m)?)?;
        m.add_function(wrap_pyfunction!(decrypt_and_rerank, m)?)?;
        m.add_function(wrap_pyfunction!(get_search_query, m)?)?;
        m.add_function(wrap_pyfunction!(get_broadened_search_query, m)?)?;
        m.add_function(wrap_pyfunction!(get_export_query, m)?)?;
        m.add_function(wrap_pyfunction!(hex_blob_to_base64, m)?)?;
    }

    // Knowledge Graph Phase 1
    m.add_function(wrap_pyfunction!(py_normalize_entity_name, m)?)?;
    m.add_function(wrap_pyfunction!(py_deterministic_entity_id, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_claim_or_legacy, m)?)?;
    m.add_function(wrap_pyfunction!(py_canonicalize_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_template_digest, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_digest_prompt, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_digest_response, m)?)?;
    m.add_function(wrap_pyfunction!(py_assemble_digest_from_llm, m)?)?;

    // Knowledge Graph Phase 2: contradiction detection + feedback log
    m.add_function(wrap_pyfunction!(py_default_resolution_weights, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_score_components, m)?)?;
    m.add_function(wrap_pyfunction!(py_resolve_pair, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_contradictions, m)?)?;
    m.add_function(wrap_pyfunction!(py_apply_feedback, m)?)?;
    m.add_function(wrap_pyfunction!(py_default_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_serialize_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_append_feedback_to_jsonl, m)?)?;
    m.add_function(wrap_pyfunction!(py_read_feedback_jsonl, m)?)?;
    m.add_function(wrap_pyfunction!(py_rotate_feedback_log, m)?)?;
    m.add_function(wrap_pyfunction!(py_feedback_to_counterexample, m)?)?;

    // Pin status + decision log (Steps B & C)
    m.add_function(wrap_pyfunction!(py_is_pinned_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_respect_pin_in_resolution, m)?)?;
    m.add_function(wrap_pyfunction!(py_find_loser_claim_in_decision_log, m)?)?;
    m.add_function(wrap_pyfunction!(py_find_decision_for_pin, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_feedback_from_decision, m)?)?;
    m.add_function(wrap_pyfunction!(py_append_decision_entry, m)?)?;
    m.add_function(wrap_pyfunction!(py_decision_log_max_lines, m)?)?;
    m.add_function(wrap_pyfunction!(py_contradiction_candidate_cap, m)?)?;
    m.add_function(wrap_pyfunction!(py_tie_zone_score_tolerance, m)?)?;

    // Step D: Contradiction orchestration
    m.add_function(wrap_pyfunction!(py_resolve_with_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_decision_log_entries, m)?)?;
    m.add_function(wrap_pyfunction!(py_filter_shadow_mode, m)?)?;

    // Consolidation / dedup
    crate::consolidation::register_python_functions(m)?;

    // Smart import profiling
    crate::smart_import::register_python_functions(m)?;

    // Memory Taxonomy v1 constants + guard
    crate::memory_types::register_python_functions(m)?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Tests (direct Rust fn invocation, no Python interpreter needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_claim_json() -> &'static str {
        r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#
    }

    fn two_claims_json() -> &'static str {
        r#"[
            {"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"},
            {"t":"lives in Lisbon","c":"fact","cf":0.95,"i":9,"sa":"oc"}
        ]"#
    }

    #[test]
    fn py_normalize_entity_name_lowercases() {
        assert_eq!(py_normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn py_deterministic_entity_id_known_answer_pedro() {
        assert_eq!(py_deterministic_entity_id("pedro"), "ee5cd7d5d96c8874");
    }

    #[test]
    fn py_parse_claim_or_legacy_full_claim_roundtrips() {
        let out = py_parse_claim_or_legacy(sample_claim_json()).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "prefers PostgreSQL");
        assert_eq!(c.category, claims::ClaimCategory::Preference);
    }

    #[test]
    fn py_parse_claim_or_legacy_legacy_object() {
        let out = py_parse_claim_or_legacy(r#"{"t":"hello","a":"oc"}"#).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "hello");
        assert_eq!(c.source_agent, "oc");
        assert_eq!(c.category, claims::ClaimCategory::Fact);
    }

    #[test]
    fn py_build_template_digest_empty_vault() {
        let out = py_build_template_digest("[]", 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 0);
        assert!(!d.prompt_text.is_empty());
    }

    #[test]
    fn py_build_template_digest_two_claims() {
        let out = py_build_template_digest(two_claims_json(), 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 2);
    }

    #[test]
    fn py_build_digest_prompt_empty_is_error() {
        let result = py_build_digest_prompt("[]");
        assert!(result.is_err());
    }

    #[test]
    fn py_build_digest_prompt_one_claim_returns_prompt() {
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let prompt = py_build_digest_prompt(one).unwrap();
        assert!(!prompt.is_empty());
        assert!(prompt.contains("JSON"));
    }

    #[test]
    fn py_parse_digest_response_valid_fenced() {
        let raw = "```json\n{\"identity\":\"You are a developer.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[\"skynet\"]}\n```";
        let out = py_parse_digest_response(raw).unwrap();
        let p: digest::ParsedDigestResponse = serde_json::from_str(&out).unwrap();
        assert_eq!(p.identity, "You are a developer.");
        assert_eq!(p.top_claim_indices, vec![1]);
        assert_eq!(p.active_project_names, vec!["skynet".to_string()]);
    }

    #[test]
    fn py_parse_digest_response_invalid_is_error() {
        let result = py_parse_digest_response("not valid json");
        assert!(result.is_err());
    }

    #[test]
    fn py_assemble_digest_from_llm_builds_digest() {
        let parsed = r#"{"identity":"You are a developer.","top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":["skynet"]}"#;
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let out = py_assemble_digest_from_llm(parsed, one, 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 1);
        assert_eq!(d.identity, "You are a developer.");
    }

    #[test]
    fn py_canonicalize_claim_round_trips_canonical_input() {
        let input = sample_claim_json();
        let out = py_canonicalize_claim(input).unwrap();
        assert_eq!(out, input);
    }

    #[test]
    fn py_canonicalize_claim_omits_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"a"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(!out.contains("\"st\""));
    }

    #[test]
    fn py_canonicalize_claim_preserves_non_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"s"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(out.contains("\"st\":\"s\""));
    }

    #[test]
    fn py_canonicalize_claim_omits_default_corroboration() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","cc":1}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(!out.contains("\"cc\""));
    }

    #[test]
    fn py_canonicalize_claim_reorders_fields_to_struct_order() {
        let input = r#"{"sa":"oc","i":5,"cf":0.9,"c":"fact","t":"hi"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert_eq!(out, r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#);
    }

    #[test]
    fn py_canonicalize_claim_rejects_malformed_json() {
        let result = py_canonicalize_claim("{not valid");
        assert!(result.is_err());
    }

    #[test]
    fn py_canonicalize_claim_rejects_missing_required_field() {
        let result = py_canonicalize_claim(r#"{"t":"hi","cf":0.9,"i":5,"sa":"oc"}"#);
        assert!(result.is_err());
    }

    #[test]
    fn py_canonicalize_claim_rejects_legacy_format() {
        let result = py_canonicalize_claim(r#"{"t":"hi","a":"oc"}"#);
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
    fn py_default_resolution_weights_matches_p2_3_defaults() {
        let out = kg_default_resolution_weights_inner().unwrap();
        let w: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        assert_eq!(w.confidence, 0.25);
        assert_eq!(w.corroboration, 0.15);
        assert_eq!(w.recency, 0.40);
        assert_eq!(w.validation, 0.20);
    }

    #[test]
    fn py_compute_score_components_known_answer() {
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
    fn py_compute_score_components_rejects_malformed_claim() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let result = kg_compute_score_components_inner("{not json", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn py_compute_score_components_rejects_malformed_weights() {
        let claim = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_compute_score_components_inner(claim, PHASE2_NOW, "{not json");
        assert!(result.is_err());
    }

    #[test]
    fn py_resolve_pair_vim_vs_vscode_defaults_vscode_wins() {
        let vim = format!(
            r#"{{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","ea":"{}","cc":3,"e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(60)
        );
        let vscode = format!(
            r#"{{"t":"uses VS Code","c":"fact","cf":0.9,"i":5,"sa":"oc","ea":"{}","e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(7)
        );
        let weights = kg_default_resolution_weights_inner().unwrap();
        let out = kg_resolve_pair_inner(&vim, "vim_id", &vscode, "vscode_id", PHASE2_NOW, &weights)
            .unwrap();
        let outcome: contradiction::ResolutionOutcome = serde_json::from_str(&out).unwrap();
        assert_eq!(outcome.winner_id, "vscode_id");
        assert_eq!(outcome.loser_id, "vim_id");
        assert!(outcome.winner_score > outcome.loser_score);
        assert!(outcome.score_delta > 0.0);
    }

    #[test]
    fn py_resolve_pair_rejects_malformed_claim_a() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let good = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_resolve_pair_inner("{bad", "a", good, "b", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn py_detect_contradictions_empty_existing_returns_empty_array() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0, 0.0, 0.0]).unwrap();
        let out =
            kg_detect_contradictions_inner(new_claim, "new_id", &emb, "[]", 0.3, 0.85).unwrap();
        assert_eq!(out, "[]");
    }

    #[test]
    fn py_detect_contradictions_single_in_band_returns_one() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let existing_claim_obj = r#"{"t":"uses Emacs","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
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
        assert_eq!(
            parsed[0].entity_id,
            claims::deterministic_entity_id("editor")
        );
    }

    #[test]
    fn py_detect_contradictions_rejects_malformed_existing_shape() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0]).unwrap();
        let existing = r#"[{"claim":{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}}]"#;
        let result = kg_detect_contradictions_inner(new_claim, "new_id", &emb, existing, 0.3, 0.85);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("existing"), "err: {}", err);
    }

    #[test]
    fn py_apply_feedback_returns_clamped_weights() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let ce = r#"{
            "formula_winner":{"confidence":0.9,"corroboration":3.0,"recency":1.0,"validation":1.0,"weighted_total":0.975},
            "formula_loser":{"confidence":0.3,"corroboration":1.0,"recency":0.1,"validation":0.7,"weighted_total":0.24},
            "user_pinned":"loser"
        }"#;
        let out = kg_apply_feedback_inner(&weights, ce).unwrap();
        let new: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        for v in [
            new.confidence,
            new.corroboration,
            new.recency,
            new.validation,
        ] {
            assert!(v >= 0.05 - 1e-12, "weight below clamp: {}", v);
            assert!(v <= 0.60 + 1e-12, "weight above clamp: {}", v);
        }
        let sum = new.confidence + new.corroboration + new.recency + new.validation;
        assert!(
            sum >= 0.9 - 1e-9 && sum <= 1.1 + 1e-9,
            "weight sum out of range: {}",
            sum
        );
    }

    // --- Phase 2 Slice 2c: feedback_log bindings ------------------------

    fn phase2_sample_entry_json() -> String {
        r#"{"ts":1776384000,"claim_a_id":"0xaaa","claim_b_id":"0xbbb","formula_winner":"a","user_decision":"pin_b","winner_components":{"confidence":0.8,"corroboration":1.732,"recency":0.333,"validation":0.7,"weighted_total":0.7331},"loser_components":{"confidence":0.6,"corroboration":1.0,"recency":0.125,"validation":0.5,"weighted_total":0.4025}}"#.to_string()
    }

    #[test]
    fn py_default_weights_file_round_trips() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        let back = kg_parse_weights_file_inner(&pretty).unwrap();
        assert_eq!(back, out);
    }

    #[test]
    fn py_serialize_weights_file_is_pretty() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        assert!(pretty.contains('\n'), "pretty JSON must contain newlines");
        assert!(pretty.contains("  "), "pretty JSON must use 2-space indent");
    }

    #[test]
    fn py_parse_weights_file_rejects_malformed() {
        let result = kg_parse_weights_file_inner("not-json");
        assert!(result.is_err());
    }

    #[test]
    fn py_parse_weights_file_rejects_unknown_version() {
        let bad = r#"{"version":99,"updated_at":0,"weights":{"confidence":0.25,"corroboration":0.15,"recency":0.4,"validation":0.2},"threshold_lower":0.3,"threshold_upper":0.85,"feedback_count":0}"#;
        let result = kg_parse_weights_file_inner(bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("unsupported"));
    }

    #[test]
    fn py_append_feedback_to_jsonl_empty_produces_one_line() {
        let out = kg_append_feedback_to_jsonl_inner("", &phase2_sample_entry_json()).unwrap();
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn py_append_feedback_to_jsonl_existing_produces_two_lines() {
        let entry = phase2_sample_entry_json();
        let first = kg_append_feedback_to_jsonl_inner("", &entry).unwrap();
        let second = kg_append_feedback_to_jsonl_inner(&first, &entry).unwrap();
        assert_eq!(second.matches('\n').count(), 2);
    }

    #[test]
    fn py_read_feedback_jsonl_round_trip_many_entries() {
        let mut content = String::new();
        for _ in 0..3 {
            content =
                kg_append_feedback_to_jsonl_inner(&content, &phase2_sample_entry_json()).unwrap();
        }
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 3);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn py_read_feedback_jsonl_surfaces_warnings_for_bad_lines() {
        let content = "not-json\n".to_string() + &phase2_sample_entry_json() + "\n";
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 1);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn py_rotate_feedback_log_drops_oldest_when_over_cap() {
        let mut content = String::new();
        for i in 0..5 {
            let entry =
                phase2_sample_entry_json().replace("1776384000", &format!("177638400{}", i));
            content = kg_append_feedback_to_jsonl_inner(&content, &entry).unwrap();
        }
        let rotated = kg_rotate_feedback_log_inner(&content, 3);
        let out = kg_read_feedback_jsonl_inner(&rotated).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        let entries = parsed["entries"].as_array().unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0]["ts"].as_i64().unwrap(), 1_776_384_002);
        assert_eq!(entries[2]["ts"].as_i64().unwrap(), 1_776_384_004);
    }

    #[test]
    fn py_rotate_feedback_log_preserves_content_below_cap() {
        let content = kg_append_feedback_to_jsonl_inner("", &phase2_sample_entry_json()).unwrap();
        let rotated = kg_rotate_feedback_log_inner(&content, 10);
        assert_eq!(rotated, content);
    }

    #[test]
    fn py_feedback_to_counterexample_pin_b_when_formula_winner_a_returns_ce() {
        let out = kg_feedback_to_counterexample_inner(&phase2_sample_entry_json()).unwrap();
        assert_ne!(out, "null");
        let ce: contradiction::Counterexample = serde_json::from_str(&out).unwrap();
        assert_eq!(ce.user_pinned, contradiction::UserPinned::Loser);
    }

    #[test]
    fn py_feedback_to_counterexample_pin_a_when_formula_winner_a_returns_null() {
        let entry = phase2_sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"pin_a\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn py_feedback_to_counterexample_unpin_returns_null() {
        let entry = phase2_sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"unpin\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn py_feedback_to_counterexample_rejects_malformed_entry() {
        let result = kg_feedback_to_counterexample_inner("{not-an-entry");
        assert!(result.is_err());
    }

    // === Retrieval v2 Tier 1 bindings ===

    #[test]
    fn py_source_weight_known_values() {
        // Direct call — no GIL needed because py_source_weight returns f64.
        assert_eq!(py_source_weight("user"), 1.00);
        assert_eq!(py_source_weight("user-inferred"), 0.90);
        assert_eq!(py_source_weight("derived"), 0.70);
        assert_eq!(py_source_weight("external"), 0.70);
        assert_eq!(py_source_weight("assistant"), 0.55);
    }

    #[test]
    fn py_source_weight_unknown_returns_fallback() {
        // Policy: unknown source string -> user-inferred (0.90), not 0.85.
        // 0.85 is ONLY the fallback for missing-source candidates in the
        // reranker. The binding here maps string -> MemorySource -> weight,
        // and from_str_lossy routes unknowns to UserInferred.
        assert_eq!(py_source_weight("bot"), 0.90);
        assert_eq!(py_source_weight(""), 0.90);
    }

    #[test]
    fn py_legacy_claim_fallback_weight_value() {
        assert_eq!(py_legacy_claim_fallback_weight(), 0.85);
    }

    #[test]
    fn py_parse_memory_type_v1_returns_string_values() {
        assert_eq!(py_parse_memory_type_v1("CLAIM"), "claim");
        assert_eq!(py_parse_memory_type_v1("directive"), "directive");
        // unknown -> claim
        assert_eq!(py_parse_memory_type_v1("fact"), "claim");
    }

    #[test]
    fn py_parse_memory_source_returns_string_values() {
        assert_eq!(py_parse_memory_source("user"), "user");
        assert_eq!(py_parse_memory_source("user-inferred"), "user-inferred");
        assert_eq!(py_parse_memory_source("USER_INFERRED"), "user-inferred");
        // unknown -> user-inferred
        assert_eq!(py_parse_memory_source("bot"), "user-inferred");
    }

    // Validate + rerank_with_config return PyResult<String>; constructing PyErr
    // requires the GIL so we stick to is_err() / is_ok() checks in unit tests.
    // Full integration tests live in tests/python_parity_test.py.

    #[test]
    fn py_validate_memory_claim_v1_accepts_valid_claim() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"prefers PostgreSQL","type":"preference","source":"user","created_at":"2026-04-17T10:00:00Z"}"#;
        let out = py_validate_memory_claim_v1(json).unwrap();
        assert!(out.contains("\"text\":\"prefers PostgreSQL\""));
        assert!(out.contains("\"type\":\"preference\""));
    }

    #[test]
    fn py_validate_memory_claim_v1_rejects_unknown_schema_version() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"hi","type":"claim","source":"user","created_at":"2026-04-17T10:00:00Z","schema_version":"2.0"}"#;
        let result = py_validate_memory_claim_v1(json);
        assert!(result.is_err(), "unknown schema_version must be rejected");
    }

    #[test]
    fn py_validate_memory_claim_v1_rejects_legacy_type_token() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"hi","type":"fact","source":"user","created_at":"2026-04-17T10:00:00Z"}"#;
        let result = py_validate_memory_claim_v1(json);
        assert!(result.is_err(), "legacy token 'fact' must be rejected");
    }

    #[test]
    fn py_rerank_with_config_flag_on_prefers_user() {
        let candidates = r#"[
            {"id":"a","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"assistant"},
            {"id":"u","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"user"}
        ]"#;
        let out = py_rerank_with_config(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates,
            10,
            true,
        )
        .unwrap();
        // User must come first — the JSON array is ordered by score desc.
        let first_u = out.find("\"id\":\"u\"").unwrap_or(usize::MAX);
        let first_a = out.find("\"id\":\"a\"").unwrap_or(usize::MAX);
        assert!(
            first_u < first_a,
            "user must rank before assistant: {}",
            out
        );
    }

    #[test]
    fn py_rerank_with_config_flag_off_ignores_source() {
        // With flag OFF the same input must behave like the v0 rerank.
        let candidates_json = r#"[
            {"id":"a","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"assistant"},
            {"id":"u","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"user"}
        ]"#;
        let off = py_rerank_with_config(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates_json,
            10,
            false,
        )
        .unwrap();
        let v0 = py_rerank(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates_json,
            10,
        )
        .unwrap();
        assert_eq!(off, v0, "flag OFF must equal v0 rerank output");
    }
}
