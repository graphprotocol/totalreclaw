//! `bind_extraction` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Debrief
// ---------------------------------------------------------------------------

/// Parse a debrief LLM response into a list of validated items.
///
/// Returns a list of dicts with keys: text, type, importance.
#[pyfunction]
#[pyo3(name = "parse_debrief_response")]
pub(crate) fn py_parse_debrief_response(py: Python<'_>, response: &str) -> PyResult<PyObject> {
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
#[pyo3(name = "get_debrief_system_prompt")]
pub(crate) fn py_get_debrief_system_prompt() -> &'static str {
    debrief::DEBRIEF_SYSTEM_PROMPT
}

// ---------------------------------------------------------------------------
// Canonical extraction + compaction system prompts (core 2.2.0 hoist)
// ---------------------------------------------------------------------------

/// Get the canonical v1 merged-topic extraction system prompt.
///
/// Single source of truth across Python (via this binding), TypeScript
/// (via the WASM binding `getExtractionSystemPrompt`), and Rust callers.
/// The prompt includes the Rule 6 meta-request filter.
#[pyfunction]
#[pyo3(name = "get_extraction_system_prompt")]
pub(crate) fn py_get_extraction_system_prompt() -> &'static str {
    crate::prompts::get_extraction_system_prompt()
}

/// Get the canonical v1 compaction system prompt.
///
/// Used on the pre-compaction surface (importance floor 5, not 6).
#[pyfunction]
#[pyo3(name = "get_compaction_system_prompt")]
pub(crate) fn py_get_compaction_system_prompt() -> &'static str {
    crate::prompts::get_compaction_system_prompt()
}


// ---------------------------------------------------------------------------
// Forget guard (internal#605 / #640)
// ---------------------------------------------------------------------------

/// Does an extracted candidate restate a fact the user explicitly forgot?
///
/// ``embedding_json`` is a JSON array of numbers or ``"null"``.
/// ``entries_json`` is a JSON array of ``{"id", "text", "embedding"?}``
/// (the client's forgotten-facts ledger, decrypted in memory). Returns
/// ``"null"`` or ``{"id", "reason", "cosine"}`` as JSON, where reason is
/// ``exact`` / ``subset`` / ``superset`` / ``anchor``. Raises ``ValueError`` on bad JSON.
#[pyfunction]
#[pyo3(name = "match_forgotten")]
pub(crate) fn py_match_forgotten(
    text: &str,
    embedding_json: &str,
    entries_json: &str,
) -> PyResult<String> {
    crate::forget_guard::match_forgotten_json(text, embedding_json, entries_json)
        .map_err(PyValueError::new_err)
}

/// True when ``text`` is a note about deleting a memory ("User asked to
/// forget X") rather than a fact about the user.
#[pyfunction]
#[pyo3(name = "is_forget_request")]
pub(crate) fn py_is_forget_request(text: &str) -> bool {
    crate::forget_guard::is_forget_request(text)
}
