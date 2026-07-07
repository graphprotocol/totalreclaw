//! `bind_store` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

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
#[pyo3(name = "prepare_fact")]
pub(crate) fn py_prepare_fact(
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
#[pyo3(name = "prepare_fact_with_decay_score")]
pub(crate) fn py_prepare_fact_with_decay_score(
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
#[pyo3(name = "build_single_calldata_from_prepared")]
pub(crate) fn py_build_single_calldata_from_prepared<'py>(
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
#[pyo3(name = "build_batch_calldata_from_prepared")]
pub(crate) fn py_build_batch_calldata_from_prepared<'py>(
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
#[pyo3(name = "prepare_tombstone")]
pub(crate) fn py_prepare_tombstone<'py>(py: Python<'py>, fact_id: &str, owner: &str) -> Bound<'py, PyBytes> {
    let bytes = store::prepare_tombstone(fact_id, owner);
    PyBytes::new(py, &bytes)
}

