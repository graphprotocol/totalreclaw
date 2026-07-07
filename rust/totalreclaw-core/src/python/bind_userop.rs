//! `bind_userop` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// UserOp (ERC-4337) — feature-gated: managed
// ---------------------------------------------------------------------------

/// Encode a single fact submission as SimpleAccount.execute() calldata.
///
/// Args:
///     protobuf_payload: Raw protobuf bytes.
///     data_edge_address: Optional 0x-prefixed DataEdge address. When omitted
///         (or None), uses the default DataEdge (prod). Chain/environment-aware
///         clients pass the authoritative address from the relay's
///         /v1/billing/status `data_edge_address` field (#366). Raises
///         ValueError on an unparseable address.
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(signature = (protobuf_payload, data_edge_address=None))]
#[pyo3(name = "encode_single_call")]
pub(crate) fn py_encode_single_call<'py>(
    py: Python<'py>,
    protobuf_payload: &[u8],
    data_edge_address: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let encoded = match data_edge_address {
        Some(addr) => userop::encode_single_call_to(protobuf_payload, addr)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
        None => userop::encode_single_call(protobuf_payload),
    };
    Ok(PyBytes::new(py, &encoded))
}

/// Encode multiple fact submissions as SimpleAccount.executeBatch() calldata.
///
/// Args:
///     payloads: List of bytes (raw protobuf payloads).
///     data_edge_address: Optional 0x-prefixed DataEdge address (see
///         encode_single_call). Omit / None for the default DataEdge.
///
/// Returns:
///     bytes (ABI-encoded calldata).
#[cfg(feature = "managed")]
#[pyfunction]
#[pyo3(signature = (payloads, data_edge_address=None))]
#[pyo3(name = "encode_batch_call")]
pub(crate) fn py_encode_batch_call<'py>(
    py: Python<'py>,
    payloads: Vec<Vec<u8>>,
    data_edge_address: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let encoded = match data_edge_address {
        Some(addr) => userop::encode_batch_call_to(&payloads, addr),
        None => userop::encode_batch_call(&payloads),
    }
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
#[pyo3(name = "hash_userop")]
pub(crate) fn py_hash_userop<'py>(
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
#[pyo3(name = "sign_userop")]
pub(crate) fn py_sign_userop<'py>(
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

