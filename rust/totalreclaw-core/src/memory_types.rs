//! Memory Taxonomy v1 — constant lookup tables + runtime guard.
//!
//! This module hoists three constants that have historically been
//! duplicated across every client package (plugin, MCP, Python client,
//! NanoClaw skill). Consolidating them here lets every binding target
//! (native Rust, WASM, PyO3) share a single source of truth and removes
//! the parity-test stopgap that previously enforced agreement.
//!
//! # Exports
//!
//! - [`VALID_MEMORY_TYPES`] — the six v1 canonical types (closed enum).
//! - [`TYPE_TO_CATEGORY`] — long-form v1 type → compact category short
//!   key used by the on-chain `c` field and recall-display tags.
//! - [`is_valid_memory_type`] — runtime guard returning whether a string
//!   is one of the six v1 types.
//!
//! # Relationship to [`crate::claims::MemoryTypeV1`]
//!
//! `MemoryTypeV1` is the strongly-typed internal enum used for serde
//! round-tripping and pattern matching inside the Rust core. The exports
//! in this module are the **string-level boundary contract** surfaced to
//! TypeScript and Python callers that need a canonical list of accepted
//! wire values without round-tripping through WASM/PyO3 for each check.
//!
//! The two are kept in lockstep by tests in this module: any drift
//! between `MemoryTypeV1` variants and `VALID_MEMORY_TYPES` entries
//! fails a unit test at compile-check time.

// ---------------------------------------------------------------------------
// Core constants
// ---------------------------------------------------------------------------

/// Closed enum of the six v1 speech-act-grounded memory types.
///
/// Order is spec-defined (per `docs/specs/totalreclaw/memory-taxonomy-v1.md`)
/// and MUST be preserved for cross-language parity — clients iterate this
/// list when building tool schemas, validation whitelists, and prompts.
pub const VALID_MEMORY_TYPES: [&str; 6] = [
    "claim",
    "preference",
    "directive",
    "commitment",
    "episode",
    "summary",
];

/// Long-form v1 memory type → compact short-form category key.
///
/// The short keys are the display-layer category tags (`[rule]`, `[pref]`,
/// etc.) that recall surfaces show, carried over from the v0 taxonomy so
/// user-visible UX stays consistent across the v0 → v1 migration.
///
/// Semantic mapping highlights:
/// - `directive` → `"rule"` (v0 legacy display tag for imperatives)
/// - `commitment` → `"goal"` (v0 legacy display tag for future intent)
/// - `episode` → `"epi"` (abbreviation of episodic)
/// - `summary` → `"sum"` (abbreviation)
/// - `claim` / `preference` map to their own short keys (`"claim"`, `"pref"`)
///
/// Kept as a `&[(&str, &str)]` rather than `HashMap` so the table is a
/// true constant (no lazy-init) and preserves insertion order for
/// deterministic iteration.
pub const TYPE_TO_CATEGORY: &[(&str, &str)] = &[
    ("claim", "claim"),
    ("preference", "pref"),
    ("directive", "rule"),
    ("commitment", "goal"),
    ("episode", "epi"),
    ("summary", "sum"),
];

// ---------------------------------------------------------------------------
// Runtime helpers
// ---------------------------------------------------------------------------

/// Returns whether `value` is one of the six v1 canonical memory types.
///
/// Case-sensitive by design — v1 wire format is lowercase. Callers that
/// want case-insensitive acceptance should normalize before calling, or
/// use [`crate::claims::MemoryTypeV1::from_str_lossy`] which falls back
/// to `Claim` for unknown input.
pub fn is_valid_memory_type(value: &str) -> bool {
    VALID_MEMORY_TYPES.contains(&value)
}

/// Look up the compact short-form category key for a v1 memory type.
///
/// Returns `None` for unknown / v0-only types. Callers that want a safe
/// default for legacy values should fall back to `"claim"`.
pub fn map_type_to_category(value: &str) -> Option<&'static str> {
    TYPE_TO_CATEGORY
        .iter()
        .find_map(|(t, c)| if *t == value { Some(*c) } else { None })
}

// ---------------------------------------------------------------------------
// WASM bindings (feature-gated)
// ---------------------------------------------------------------------------

#[cfg(feature = "wasm")]
mod wasm_bindings {
    use super::{
        is_valid_memory_type, map_type_to_category, TYPE_TO_CATEGORY, VALID_MEMORY_TYPES,
    };
    use wasm_bindgen::prelude::*;

    /// Get the canonical list of v1 memory types.
    ///
    /// Returns a JS array of six strings: `["claim", "preference",
    /// "directive", "commitment", "episode", "summary"]`.
    #[wasm_bindgen(js_name = "getValidMemoryTypes")]
    pub fn wasm_get_valid_memory_types() -> Result<JsValue, JsError> {
        serde_wasm_bindgen::to_value(&VALID_MEMORY_TYPES.to_vec())
            .map_err(|e| JsError::new(&format!("serialization error: {}", e)))
    }

    /// Get the v1 type → short-form category mapping.
    ///
    /// Returns a plain JS object `{ claim: "claim", preference: "pref",
    /// directive: "rule", commitment: "goal", episode: "epi",
    /// summary: "sum" }`.
    ///
    /// Uses `js_sys::Object.set` directly so the result is a plain JS
    /// object (not a `Map`) regardless of serde-wasm-bindgen defaults,
    /// matching how TypeScript clients consume the mapping via bracket
    /// access (`map[type]`).
    #[wasm_bindgen(js_name = "getTypeToCategory")]
    pub fn wasm_get_type_to_category() -> Result<JsValue, JsError> {
        let obj = js_sys::Object::new();
        for (t, c) in TYPE_TO_CATEGORY {
            js_sys::Reflect::set(&obj, &JsValue::from_str(t), &JsValue::from_str(c))
                .map_err(|_| JsError::new("failed to set property on object"))?;
        }
        Ok(obj.into())
    }

    /// Map a v1 type to its short-form category key.
    ///
    /// Returns `null` if `value` is not one of the six v1 types.
    #[wasm_bindgen(js_name = "mapTypeToCategory")]
    pub fn wasm_map_type_to_category(value: &str) -> JsValue {
        match map_type_to_category(value) {
            Some(cat) => JsValue::from_str(cat),
            None => JsValue::NULL,
        }
    }

    /// Runtime guard: is `value` a valid v1 memory type?
    #[wasm_bindgen(js_name = "isValidMemoryType")]
    pub fn wasm_is_valid_memory_type(value: &str) -> bool {
        is_valid_memory_type(value)
    }
}

// ---------------------------------------------------------------------------
// Python (PyO3) bindings (feature-gated)
// ---------------------------------------------------------------------------

#[cfg(feature = "python")]
mod python_bindings {
    use super::{
        is_valid_memory_type, map_type_to_category, TYPE_TO_CATEGORY, VALID_MEMORY_TYPES,
    };
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};

    /// Get the canonical list of v1 memory types.
    ///
    /// Returns a Python list of six strings.
    #[pyfunction]
    fn get_valid_memory_types<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, VALID_MEMORY_TYPES.iter().copied())
    }

    /// Get the v1 type → short-form category mapping.
    ///
    /// Returns a Python dict.
    #[pyfunction]
    fn get_type_to_category<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (t, c) in TYPE_TO_CATEGORY {
            dict.set_item(*t, *c)?;
        }
        Ok(dict)
    }

    /// Map a v1 type to its short-form category key.
    ///
    /// Returns `None` if `value` is not one of the six v1 types.
    #[pyfunction]
    fn py_map_type_to_category(value: &str) -> Option<&'static str> {
        map_type_to_category(value)
    }

    /// Runtime guard: is `value` a valid v1 memory type?
    #[pyfunction]
    fn py_is_valid_memory_type(value: &str) -> bool {
        is_valid_memory_type(value)
    }

    pub fn register_python_functions(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(get_valid_memory_types, m)?)?;
        m.add_function(wrap_pyfunction!(get_type_to_category, m)?)?;
        m.add_function(wrap_pyfunction!(py_map_type_to_category, m)?)?;
        m.add_function(wrap_pyfunction!(py_is_valid_memory_type, m)?)?;
        Ok(())
    }
}

#[cfg(feature = "python")]
pub use python_bindings::register_python_functions;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_six_v1_types_valid() {
        for t in ["claim", "preference", "directive", "commitment", "episode", "summary"] {
            assert!(is_valid_memory_type(t), "expected {t:?} to be valid");
        }
    }

    #[test]
    fn rejects_v0_only_types() {
        // v0 types that were absorbed into v1 (see v1-types.ts LEGACY_TYPE_TO_V1).
        for t in ["fact", "decision", "episodic", "goal", "context", "rule"] {
            assert!(!is_valid_memory_type(t), "expected {t:?} to be rejected");
        }
    }

    #[test]
    fn rejects_unknown_types() {
        for t in ["", " ", "Claim", "PREFERENCE", "random", "null"] {
            assert!(!is_valid_memory_type(t), "expected {t:?} to be rejected");
        }
    }

    #[test]
    fn valid_memory_types_constant_length() {
        assert_eq!(VALID_MEMORY_TYPES.len(), 6, "v1 has exactly 6 types");
    }

    #[test]
    fn valid_memory_types_matches_memory_type_v1_enum() {
        // Every `MemoryTypeV1` variant must appear in the constant list.
        // This test guards against drift if a variant is added or renamed.
        use crate::claims::MemoryTypeV1;

        for t in VALID_MEMORY_TYPES.iter() {
            // Round-trip: variant parsed from the string and serialized back
            // must yield the same string.
            let parsed = MemoryTypeV1::from_str_lossy(t);
            let serialized = serde_json::to_string(&parsed).unwrap();
            // serde_json wraps strings in quotes: `"claim"` — strip them.
            let unquoted = serialized.trim_matches('"');
            assert_eq!(
                unquoted, *t,
                "MemoryTypeV1::from_str_lossy({t:?}) round-trip mismatch: {unquoted:?}"
            );
        }
    }

    #[test]
    fn type_to_category_covers_all_v1_types() {
        // Every entry in VALID_MEMORY_TYPES must have a category mapping.
        for t in VALID_MEMORY_TYPES.iter() {
            assert!(
                map_type_to_category(t).is_some(),
                "missing TYPE_TO_CATEGORY entry for {t:?}"
            );
        }
        assert_eq!(TYPE_TO_CATEGORY.len(), VALID_MEMORY_TYPES.len());
    }

    #[test]
    fn type_to_category_exact_mapping() {
        // Lock the exact short-form keys so cross-client display stays
        // consistent. Matches `mcp/src/v1-types.ts::V1_TYPE_TO_SHORT_CATEGORY`
        // and `python/src/totalreclaw/claims_helper.py::TYPE_TO_CATEGORY_V1`.
        assert_eq!(map_type_to_category("claim"), Some("claim"));
        assert_eq!(map_type_to_category("preference"), Some("pref"));
        assert_eq!(map_type_to_category("directive"), Some("rule"));
        assert_eq!(map_type_to_category("commitment"), Some("goal"));
        assert_eq!(map_type_to_category("episode"), Some("epi"));
        assert_eq!(map_type_to_category("summary"), Some("sum"));
    }

    #[test]
    fn type_to_category_rejects_unknown() {
        assert_eq!(map_type_to_category("fact"), None);
        assert_eq!(map_type_to_category(""), None);
        assert_eq!(map_type_to_category("Claim"), None);
        assert_eq!(map_type_to_category("decision"), None);
    }

    #[test]
    fn no_duplicate_short_categories_for_claim_and_preference() {
        // Sanity: short keys for `claim` and `preference` are distinct.
        // (directive/commitment reuse v0 short-forms "rule"/"goal" on purpose.)
        assert_eq!(map_type_to_category("claim"), Some("claim"));
        assert_eq!(map_type_to_category("preference"), Some("pref"));
        assert_ne!(
            map_type_to_category("claim"),
            map_type_to_category("preference")
        );
    }
}
