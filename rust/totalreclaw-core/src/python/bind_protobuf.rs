//! `bind_protobuf` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

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
#[pyo3(name = "encode_fact_protobuf")]
pub(crate) fn py_encode_fact_protobuf<'py>(py: Python<'py>, json_str: &str) -> PyResult<Bound<'py, PyBytes>> {
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
#[pyo3(name = "encode_tombstone_protobuf")]
pub(crate) fn py_encode_tombstone_protobuf<'py>(
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

