//! `bind_pii` wasm-bindgen bindings — PII / credential filter (PRD-04 DEP-4).
//!
//! Shared imports come from the parent module via `use super::*;`.

use super::*;

/// Classify a candidate memory text before it is stored.
///
/// Returns the canonical decision JSON (identical bytes to the PyO3
/// `pii_filter`): `{"decision":"allow"}` or
/// `{"decision":"block"|"flag","category":"<tag>","message":"<user-facing text>"}`.
#[wasm_bindgen(js_name = "piiFilter")]
pub fn wasm_pii_filter(text: &str) -> String {
    crate::pii::pii_filter(text).to_json()
}
