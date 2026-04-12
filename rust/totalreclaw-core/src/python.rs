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

use crate::{blind, claims, crypto, debrief, digest, fingerprint, lsh, protobuf, reranker, store};
#[cfg(feature = "managed")]
use crate::search;
#[cfg(feature = "managed")]
use crate::userop;

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
fn encode_batch_call<'py>(py: Python<'py>, payloads: Vec<Vec<u8>>) -> PyResult<Bound<'py, PyBytes>> {
    let encoded = userop::encode_batch_call(&payloads)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
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
    let sig = userop::sign_userop(&h, &pk)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
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
    let calldata = store::build_batch_calldata(&prepared)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
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
    search::generate_search_trapdoors(query, &query_embedding, &lsh_hasher.inner)
        .map_err(to_pyerr)
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

    // Reranker
    m.add_function(wrap_pyfunction!(py_rerank, m)?)?;
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

    // Consolidation / dedup
    crate::consolidation::register_python_functions(m)?;

    // Smart import profiling
    crate::smart_import::register_python_functions(m)?;

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
        assert_eq!(
            out,
            r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#
        );
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
}
