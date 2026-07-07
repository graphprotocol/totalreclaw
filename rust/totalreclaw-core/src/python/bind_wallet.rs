//! `bind_wallet` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Wallet derivation
// ---------------------------------------------------------------------------

/// Derive an Ethereum EOA wallet from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Returns a JSON string: ``{"private_key": "hex...", "address": "0x..."}``.
#[pyfunction]
#[pyo3(name = "derive_eoa")]
pub(crate) fn py_derive_eoa(mnemonic: &str) -> PyResult<String> {
    let w = crate::wallet::derive_eoa(mnemonic).map_err(to_pyerr)?;
    serde_json::to_string(&w).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// Derive just the Ethereum EOA address from a BIP-39 mnemonic.
///
/// Returns: ``"0x..."`` (lowercase hex).
#[pyfunction]
#[pyo3(name = "derive_eoa_address")]
pub(crate) fn py_derive_eoa_address(mnemonic: &str) -> PyResult<String> {
    crate::wallet::derive_eoa_address(mnemonic).map_err(to_pyerr)
}

