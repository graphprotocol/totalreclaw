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
// Topical dedup context (PRD-04 DEP-3)
// ---------------------------------------------------------------------------

/// Render the "Existing memories" block appended to the extraction user prompt.
///
/// Each argument is a JSON array of ``{"id": str, "text": str}`` objects
/// (``"null"`` = empty section; extra keys such as ``embedding`` are ignored).
/// Pinned first, then topical, then recent; dedupe by fact id; at most
/// ``cap`` lines; returns ``""`` when no line survives.
///
/// Raises ``ValueError`` on malformed JSON.
#[pyfunction]
#[pyo3(name = "build_dedup_context")]
pub(crate) fn py_build_dedup_context(
    topical_json: &str,
    pinned_json: &str,
    recent_json: &str,
    cap: usize,
) -> PyResult<String> {
    crate::dedup_context::build_dedup_context_json(topical_json, pinned_json, recent_json, cap)
        .map_err(to_pyerr)
}

