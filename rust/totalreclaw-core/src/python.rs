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

use crate::{blind, crypto, debrief, fingerprint, lsh, protobuf, reranker};

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
fn derive_lsh_seed<'py>(py: Python<'py>, mnemonic: &str, salt: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
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

/// Encrypt a UTF-8 plaintext string with AES-256-GCM.
///
/// Returns base64-encoded ciphertext (wire format: iv || tag || ciphertext).
#[pyfunction]
fn encrypt(plaintext: &str, encryption_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    crypto::encrypt(plaintext, &key).map_err(to_pyerr)
}

/// Decrypt a base64-encoded AES-256-GCM blob back to a UTF-8 string.
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
    ///     dims: Embedding dimensionality (e.g. 1024).
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
    };

    let encoded = protobuf::encode_fact_protobuf(&payload);
    Ok(PyBytes::new(py, &encoded))
}

/// Encode a tombstone protobuf for soft-deleting a fact.
///
/// Args:
///     fact_id: The fact ID to tombstone.
///     owner: The owner address.
///
/// Returns:
///     bytes (protobuf wire format).
#[pyfunction]
fn encode_tombstone_protobuf<'py>(py: Python<'py>, fact_id: &str, owner: &str) -> PyResult<Bound<'py, PyBytes>> {
    let encoded = protobuf::encode_tombstone_protobuf(fact_id, owner);
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
// Reranker
// ---------------------------------------------------------------------------

/// Rerank candidates using BM25 + Cosine + RRF fusion.
///
/// Args:
///     query: Search query text.
///     query_embedding: Query embedding vector (list of floats).
///     candidates_json: JSON array of ``{ id, text, embedding, timestamp }`` objects.
///     top_k: Number of top results to return.
///
/// Returns:
///     JSON string of ranked results.
#[pyfunction]
#[pyo3(name = "rerank")]
fn py_rerank(query: &str, query_embedding: Vec<f32>, candidates_json: &str, top_k: usize) -> PyResult<String> {
    let candidates: Vec<reranker::Candidate> = serde_json::from_str(candidates_json)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let results = reranker::rerank(query, &query_embedding, &candidates, top_k)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    serde_json::to_string(&results)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
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
    serde_json::to_string(&w)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Derive just the Ethereum EOA address from a BIP-39 mnemonic.
///
/// Returns: ``"0x..."`` (lowercase hex).
#[pyfunction]
fn derive_eoa_address(mnemonic: &str) -> PyResult<String> {
    crate::wallet::derive_eoa_address(mnemonic).map_err(to_pyerr)
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

/// TotalReclaw core crypto primitives (Rust implementation).
///
/// This module provides byte-for-byte compatible implementations of all
/// TotalReclaw cryptographic operations: key derivation, AES-256-GCM
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

    // Reranker
    m.add_function(wrap_pyfunction!(py_rerank, m)?)?;
    m.add_function(wrap_pyfunction!(py_cosine_similarity, m)?)?;

    // Wallet derivation
    m.add_function(wrap_pyfunction!(derive_eoa, m)?)?;
    m.add_function(wrap_pyfunction!(derive_eoa_address, m)?)?;

    Ok(())
}
