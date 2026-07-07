//! `bind_recall` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Read-after-write (confirm_indexed)
// ---------------------------------------------------------------------------

/// GraphQL query string used to confirm a fact id has been indexed.
/// Pair with `wasmConfirmIndexedParse` in a host-side polling loop.
#[wasm_bindgen(js_name = wasmConfirmIndexedQuery)]
pub fn wasm_confirm_indexed_query() -> String {
    confirm::confirm_indexed_query().to_string()
}

/// Parse a subgraph response JSON and return whether the fact is indexed +
/// active. Returns `true` when the read-after-write loop can stop.
#[wasm_bindgen(js_name = wasmConfirmIndexedParse)]
pub fn wasm_confirm_indexed_parse(response_json: &str) -> Result<bool, JsError> {
    confirm::parse_indexed_response(response_json).map_err(|e| JsError::new(&e.to_string()))
}

/// Default polling interval (ms) — exposed so host adapters share the same
/// default without re-declaring the constant.
#[wasm_bindgen(js_name = wasmConfirmIndexedDefaultPollMs)]
pub fn wasm_confirm_indexed_default_poll_ms() -> u32 {
    confirm::DEFAULT_POLL_INTERVAL_MS as u32
}

/// Default total timeout (ms) — exposed so host adapters share the same default.
#[wasm_bindgen(js_name = wasmConfirmIndexedDefaultTimeoutMs)]
pub fn wasm_confirm_indexed_default_timeout_ms() -> u32 {
    confirm::DEFAULT_TIMEOUT_MS as u32
}

// ---------------------------------------------------------------------------
// Recall context formatter
// ---------------------------------------------------------------------------

/// Unix seconds → `"YYYY-MM-DD"` (UTC). Returns `""` for `0` or negative.
///
/// Maps directly to [`crate::recall_context::format_memory_date`].
#[wasm_bindgen(js_name = "formatMemoryDate")]
pub fn wasm_format_memory_date(created_at_unix: i64) -> String {
    crate::recall_context::format_memory_date(created_at_unix)
}

/// Build the recall-context header string (current-date + temporal-reasoning nudge).
///
/// `now_unix`: current time as Unix seconds.
/// Returns the header with a trailing newline, e.g.:
/// `"## Relevant memories from TotalReclaw\nThe current date is 2024-01-15. ..."`
#[wasm_bindgen(js_name = "recallContextHeader")]
pub fn wasm_recall_context_header(now_unix: i64) -> String {
    crate::recall_context::recall_context_header(now_unix)
}

/// Build the full recall-context block: header + one line per memory item.
///
/// `items_json`: JSON array of `{ category, text, created_at }`. Any field
/// may be absent (defaults to empty string / 0). Bad or empty JSON → header
/// only (no panic).
///
/// Output line format:
/// - With date:    `"- [category] (YYYY-MM-DD) text"`
/// - Without date: `"- [category] text"`
///
/// `now_unix`: current time as Unix seconds (used in the header date).
#[wasm_bindgen(js_name = "formatRecallContext")]
pub fn wasm_format_recall_context(items_json: &str, now_unix: i64) -> String {
    crate::recall_context::format_recall_context(items_json, now_unix)
}

// ---------------------------------------------------------------------------
// Session segmentation (import Crystal grouping) — #368 core hoist
// ---------------------------------------------------------------------------

/// Centroid-walk session segmentation over time-ordered turns.
///
/// Mirrors `session_segmentation.py:segment_sessions` byte-for-byte.
///
/// # Inputs (JSON strings, per this module's convention)
/// - `timestamps_json`: JSON array of Unix seconds or `null`, e.g.
///   `"[0.0, null, 5000.0]"`. `null` = 0-gap to the previous turn.
/// - `embeddings_json`: JSON array of L2-normalised vectors, e.g.
///   `"[[1.0,0.0],[0.9,0.1]]"`.
/// - `gap_seconds`: min time gap (strict `>`) forcing a new session (e.g. 1800).
/// - `sim_threshold`: cosine threshold (strict `<` splits; e.g. 0.55).
///
/// # Returns
/// A `JsValue` — array of sessions, each an array of turn indices
/// (`number[][]`), contiguous and ascending. Bad JSON → `JsError`.
#[wasm_bindgen(js_name = "segmentSessions")]
pub fn wasm_segment_sessions(
    timestamps_json: &str,
    embeddings_json: &str,
    gap_seconds: f64,
    sim_threshold: f64,
) -> Result<JsValue, JsError> {
    let timestamps: Vec<Option<f64>> =
        serde_json::from_str(timestamps_json).map_err(|e| JsError::new(&e.to_string()))?;
    let embeddings: Vec<Vec<f64>> =
        serde_json::from_str(embeddings_json).map_err(|e| JsError::new(&e.to_string()))?;
    let sessions = crate::session_segmentation::segment_sessions(
        &timestamps,
        &embeddings,
        gap_seconds,
        sim_threshold,
    );
    serde_wasm_bindgen::to_value(&sessions).map_err(|e| JsError::new(&e.to_string()))
}

