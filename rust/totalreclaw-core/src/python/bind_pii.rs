//! `bind_pii` PyO3 bindings — PII / credential filter (PRD-04 DEP-4).
//!
//! Shared imports come from the parent module via `use super::*;`.
//! Registered in `super`'s `#[pymodule]`.

use super::*;

/// Classify a candidate memory text before it is stored.
///
/// Returns the canonical decision JSON (``str``, identical bytes to the WASM
/// ``piiFilter``): ``{"decision":"allow"}`` or
/// ``{"decision":"block"|"flag","category":"<tag>","message":"<user-facing text>"}``.
#[pyfunction]
#[pyo3(name = "pii_filter")]
pub(crate) fn py_pii_filter(text: &str) -> String {
    crate::pii::pii_filter(text).to_json()
}
