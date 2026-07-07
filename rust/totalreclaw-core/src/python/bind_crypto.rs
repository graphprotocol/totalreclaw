//! `bind_crypto` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Key derivation
// ---------------------------------------------------------------------------

/// Derive all crypto keys from a BIP-39 mnemonic (strict checksum validation).
///
/// Returns a dict with keys: salt, auth_key, encryption_key, dedup_key (all bytes).
#[pyfunction]
#[pyo3(name = "derive_keys_from_mnemonic")]
pub(crate) fn py_derive_keys_from_mnemonic(py: Python<'_>, mnemonic: &str) -> PyResult<PyObject> {
    let keys = crypto::derive_keys_from_mnemonic(mnemonic).map_err(to_pyerr)?;
    keys_to_dict(py, &keys)
}

/// Derive all crypto keys from a BIP-39 mnemonic (lenient -- skips checksum).
///
/// Returns a dict with keys: salt, auth_key, encryption_key, dedup_key (all bytes).
#[pyfunction]
#[pyo3(name = "derive_keys_from_mnemonic_lenient")]
pub(crate) fn py_derive_keys_from_mnemonic_lenient(py: Python<'_>, mnemonic: &str) -> PyResult<PyObject> {
    let keys = crypto::derive_keys_from_mnemonic_lenient(mnemonic).map_err(to_pyerr)?;
    keys_to_dict(py, &keys)
}

/// Build a Python dict from DerivedKeys.
pub(crate) fn keys_to_dict(py: Python<'_>, keys: &crypto::DerivedKeys) -> PyResult<PyObject> {
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
#[pyo3(name = "derive_lsh_seed")]
pub(crate) fn py_derive_lsh_seed<'py>(
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
#[pyo3(name = "compute_auth_key_hash")]
pub(crate) fn py_compute_auth_key_hash(auth_key: &[u8]) -> PyResult<String> {
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
#[pyo3(name = "encrypt")]
pub(crate) fn py_encrypt(plaintext: &str, encryption_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(encryption_key)?;
    crypto::encrypt(plaintext, &key).map_err(to_pyerr)
}

/// Decrypt a base64-encoded XChaCha20-Poly1305 blob back to a UTF-8 string.
#[pyfunction]
#[pyo3(name = "decrypt")]
pub(crate) fn py_decrypt(encrypted_base64: &str, encryption_key: &[u8]) -> PyResult<String> {
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
#[pyo3(name = "generate_blind_indices")]
pub(crate) fn py_generate_blind_indices(text: &str) -> Vec<String> {
    blind::generate_blind_indices(text)
}

// ---------------------------------------------------------------------------
// Content fingerprint
// ---------------------------------------------------------------------------

/// Compute HMAC-SHA256 content fingerprint. Returns 64-char hex string.
#[pyfunction]
#[pyo3(name = "generate_content_fingerprint")]
pub(crate) fn py_generate_content_fingerprint(plaintext: &str, dedup_key: &[u8]) -> PyResult<String> {
    let key = bytes_to_array32(dedup_key)?;
    Ok(fingerprint::generate_content_fingerprint(plaintext, &key))
}

/// Normalize text for deterministic fingerprinting (NFC, lowercase, collapse whitespace, trim).
#[pyfunction]
#[pyo3(name = "normalize_text")]
pub(crate) fn py_normalize_text(text: &str) -> String {
    fingerprint::normalize_text(text)
}

