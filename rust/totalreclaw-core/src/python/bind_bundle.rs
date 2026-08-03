//! `bind_bundle` PyO3 bindings for the `derived-bundle-v1` credential bundle
//! (Option E Phase 2). Shared imports + helpers (`to_pyerr`, `bytes_to_array32`)
//! come from the parent module via `use super::*;`. Registered in `super`'s
//! `#[pymodule]`.
//!
//! Deviation from the design doc's sketched signature: `derive_bundle_from_mnemonic`
//! takes `smart_account` as a **required** (not optional) parameter. Core has
//! no CREATE2 helper — the Smart Account address is derived today via an
//! `eth_call` RPC round-trip (network I/O, out of scope for this crate) — so
//! per the design doc's own explicit fallback, the caller must supply it.
//! See `bundle.rs`'s module doc for the full rationale.

use super::*;
use crate::bundle;

// ---------------------------------------------------------------------------
// Derivation
// ---------------------------------------------------------------------------

/// Derive a `derived-bundle-v1` bundle from a BIP-39 mnemonic.
///
/// Returns the canonical JSON string (keeping the FFI surface a string avoids
/// marshalling a nested struct through PyO3 and keeps one serialisation
/// implementation — `bundle::bundle_to_json`).
///
/// `smart_account` is the CREATE2 Smart Account address (`0x`-prefixed hex,
/// any casing — normalised to EIP-55 checksum casing in the output).
#[pyfunction]
#[pyo3(name = "derive_bundle_from_mnemonic")]
pub(crate) fn py_derive_bundle_from_mnemonic(
    mnemonic: &str,
    chain_id: u64,
    provisioned_by: &str,
    smart_account: &str,
) -> PyResult<String> {
    let derived =
        bundle::derive_bundle_from_mnemonic(mnemonic, chain_id, provisioned_by, smart_account)
            .map_err(to_pyerr)?;
    bundle::bundle_to_json(&derived).map_err(to_pyerr)
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/// Parse and fully validate a `derived-bundle-v1` JSON string.
///
/// Returns a nested dict matching the bundle shape, with `bytes` for every
/// 32-byte key field (matching how `derive_keys_from_mnemonic` returns
/// `PyBytes`):
///
/// ```text
/// {
///   "vault": {"encryption_key": bytes, "dedup_key": bytes, "auth_key": bytes, "lsh_seed": bytes},
///   "signing": {"kind": str, "private_key": bytes, "address": str, "grant": str | None},
///   "account": {"smart_account": str, "chain_id": int},
///   "provisioned_at": str,
///   "provisioned_by": str,
/// }
/// ```
///
/// `signing.grant` (only present for `session-key`, not shipped in Phase 2)
/// is returned as its raw JSON string rather than a deep-converted dict —
/// no client consumes it yet, and this crate has no generic
/// `serde_json::Value -> PyObject` converter today. Revisit if/when a Phase 3
/// client needs structured access.
///
/// Raises `ValueError` on any violation of derived-bundle-v1.md §4.7 —
/// unknown `version`/`schema`, malformed hex, unknown `signing.kind`, a
/// `grant`/no-`grant` mismatch with `kind`, or an address/private-key
/// mismatch.
#[pyfunction]
#[pyo3(name = "parse_bundle_v1")]
pub(crate) fn py_parse_bundle_v1(py: Python<'_>, json: &str) -> PyResult<PyObject> {
    let parsed = bundle::parse_bundle_v1(json).map_err(to_pyerr)?;

    let vault = PyDict::new(py);
    vault.set_item("encryption_key", PyBytes::new(py, &parsed.vault.encryption_key))?;
    vault.set_item("dedup_key", PyBytes::new(py, &parsed.vault.dedup_key))?;
    vault.set_item("auth_key", PyBytes::new(py, &parsed.vault.auth_key))?;
    vault.set_item("lsh_seed", PyBytes::new(py, &parsed.vault.lsh_seed))?;

    let signing = PyDict::new(py);
    signing.set_item("kind", parsed.signing.kind())?;
    signing.set_item("private_key", PyBytes::new(py, parsed.signing.private_key()))?;
    signing.set_item("address", parsed.signing.address())?;
    match &parsed.signing {
        bundle::SigningMaterial::SessionKey { grant, .. } => {
            signing.set_item("grant", grant.to_string())?;
        }
        bundle::SigningMaterial::OwnerEoa { .. } => {
            signing.set_item("grant", py.None())?;
        }
    }

    let account = PyDict::new(py);
    account.set_item("smart_account", &parsed.account.smart_account)?;
    account.set_item("chain_id", parsed.account.chain_id)?;

    let out = PyDict::new(py);
    out.set_item("vault", vault)?;
    out.set_item("signing", signing)?;
    out.set_item("account", account)?;
    out.set_item("provisioned_at", &parsed.provisioned_at)?;
    out.set_item("provisioned_by", &parsed.provisioned_by)?;

    Ok(out.into())
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/// Validate a `derived-bundle-v1` JSON string against every invariant in
/// derived-bundle-v1.md §4.7. Raises `ValueError` on any violation; returns
/// `None` on success.
///
/// `bundle::parse_bundle_v1` already runs `validate_bundle_v1` internally as
/// its last step, so this is a thin `PyResult<()>`-shaped wrapper over the
/// same parse+validate pipeline `parse_bundle_v1` above exposes as a dict.
#[pyfunction]
#[pyo3(name = "validate_bundle_v1")]
pub(crate) fn py_validate_bundle_v1(json: &str) -> PyResult<()> {
    bundle::parse_bundle_v1(json).map_err(to_pyerr)?;
    Ok(())
}
