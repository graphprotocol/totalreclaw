//! `bind_recall` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Recall context formatter
// ---------------------------------------------------------------------------

/// Unix seconds → ``"YYYY-MM-DD"`` (UTC).
///
/// Returns an empty string for ``0``, negative values, or any value that
/// cannot be represented as a valid UTC date (overflow).
#[pyfunction]
#[pyo3(name = "format_memory_date")]
pub(crate) fn py_format_memory_date(created_at_unix: i64) -> String {
    recall_context::format_memory_date(created_at_unix)
}

/// Build the recall-context header string (current-date + temporal-reasoning nudge).
///
/// ``now_unix``: current time as Unix seconds.
/// Returns the header with a trailing newline, e.g.:
/// ``"## Relevant memories from TotalReclaw\\nThe current date is 2024-01-15. ..."``
#[pyfunction]
#[pyo3(name = "recall_context_header")]
pub(crate) fn py_recall_context_header(now_unix: i64) -> String {
    recall_context::recall_context_header(now_unix)
}

/// Build the full recall-context block: header + one line per memory item.
///
/// ``items_json``: JSON array of ``{ category, text, created_at }``. Any field
/// may be absent (defaults to empty string / 0). Bad or empty JSON → header
/// only (no exception raised).
///
/// Output line format:
///
/// - With date:    ``"- [category] (YYYY-MM-DD) text"``
/// - Without date: ``"- [category] text"``
///
/// ``now_unix``: current time as Unix seconds (used in the header date).
#[pyfunction]
#[pyo3(name = "format_recall_context")]
pub(crate) fn py_format_recall_context(items_json: &str, now_unix: i64) -> String {
    recall_context::format_recall_context(items_json, now_unix)
}

// ---------------------------------------------------------------------------
// Session segmentation (import Crystal grouping) — #368 core hoist
// ---------------------------------------------------------------------------

/// Centroid-walk session segmentation over time-ordered turns.
///
/// Mirrors `session_segmentation.py:segment_sessions` byte-for-byte. Embedding
/// stays client-side; this is the pure segmentation math only.
///
/// # Parameters
/// - `timestamps`: list of Unix seconds (`float`) or `None`, chronological.
///   `None` entries are treated as a 0-gap to the previous turn.
/// - `embeddings`: list of L2-normalised embedding vectors (`list[list[float]]`).
/// - `gap_seconds`: minimum time gap (strict `>`) that forces a new session
///   (default 1800 = 30 min).
/// - `sim_threshold`: cosine threshold (strict `<` splits; default 0.55).
///
/// # Returns
/// `list[list[int]]` — ordered sessions, each a list of turn indices
/// (contiguous, ascending). Empty input → `[]`.
#[pyfunction]
#[pyo3(name = "segment_sessions")]
#[pyo3(signature = (timestamps, embeddings, gap_seconds=1800.0, sim_threshold=0.55))]
pub(crate) fn py_segment_sessions(
    timestamps: Vec<Option<f64>>,
    embeddings: Vec<Vec<f64>>,
    gap_seconds: f64,
    sim_threshold: f64,
) -> Vec<Vec<usize>> {
    session_segmentation::segment_sessions(&timestamps, &embeddings, gap_seconds, sim_threshold)
}

// ---------------------------------------------------------------------------
// Read-after-write (confirm_indexed)
// ---------------------------------------------------------------------------

/// GraphQL query string for confirm_indexed (`fact(id: $id) { id, isActive,
/// blockNumber }`). Pair with `confirm_indexed_parse` in a host-side polling
/// loop.
#[pyfunction]
#[pyo3(name = "confirm_indexed_query")]
pub(crate) fn py_confirm_indexed_query() -> &'static str {
    confirm::confirm_indexed_query()
}

/// Parse a subgraph response JSON for confirm_indexed and return whether the
/// fact is indexed AND active.
#[pyfunction]
#[pyo3(name = "confirm_indexed_parse")]
pub(crate) fn py_confirm_indexed_parse(response_json: &str) -> PyResult<bool> {
    confirm::parse_indexed_response(response_json).map_err(to_pyerr)
}

/// Default polling interval (ms) — exposed so Python adapters share the
/// same default without re-declaring the constant.
#[pyfunction]
#[pyo3(name = "confirm_indexed_default_poll_ms")]
pub(crate) fn py_confirm_indexed_default_poll_ms() -> u64 {
    confirm::DEFAULT_POLL_INTERVAL_MS
}

/// Default total timeout (ms) — exposed so Python adapters share the same default.
#[pyfunction]
#[pyo3(name = "confirm_indexed_default_timeout_ms")]
pub(crate) fn py_confirm_indexed_default_timeout_ms() -> u64 {
    confirm::DEFAULT_TIMEOUT_MS
}

