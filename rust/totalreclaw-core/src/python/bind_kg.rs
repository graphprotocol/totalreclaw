//! `bind_kg` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 1
// ---------------------------------------------------------------------------

// The JSON-in/JSON-out inner implementations live in [`crate::kg_ffi`] (shared
// byte-for-byte with the wasm bindings). The pyfunction wrappers below just call
// those `kg_*_inner` helpers and map String errors to PyValueError.

/// Normalize an entity name (NFC, lowercase, trim, collapse whitespace).
#[pyfunction]
#[pyo3(name = "normalize_entity_name")]
pub(crate) fn py_normalize_entity_name(name: &str) -> String {
    claims::normalize_entity_name(name)
}

/// Deterministic entity ID from a name (first 8 bytes of SHA256 as hex).
#[pyfunction]
#[pyo3(name = "deterministic_entity_id")]
pub(crate) fn py_deterministic_entity_id(name: &str) -> String {
    claims::deterministic_entity_id(name)
}

/// Parse a decrypted blob as a Claim, falling back to legacy formats.
/// Returns JSON-serialized Claim string.
#[pyfunction]
#[pyo3(name = "parse_claim_or_legacy")]
pub(crate) fn py_parse_claim_or_legacy(decrypted: &str) -> PyResult<String> {
    kg_parse_claim_or_legacy_inner(decrypted).map_err(PyValueError::new_err)
}

/// Canonicalize a Claim JSON: strict-parse as Claim, re-serialize to canonical bytes.
/// Rejects legacy or malformed input. Use before encryption for byte-identical
/// blobs across TS/Python/Rust.
#[pyfunction]
#[pyo3(name = "canonicalize_claim")]
pub(crate) fn py_canonicalize_claim(claim_json: &str) -> PyResult<String> {
    kg_canonicalize_claim_inner(claim_json).map_err(PyValueError::new_err)
}

/// Build a template digest from a JSON array of Claim.
/// Returns JSON-serialized Digest.
#[pyfunction]
#[pyo3(name = "build_template_digest")]
pub(crate) fn py_build_template_digest(claims_json: &str, now_unix_seconds: i64) -> PyResult<String> {
    kg_build_template_digest_inner(claims_json, now_unix_seconds).map_err(PyValueError::new_err)
}

/// Build the LLM prompt for digest compilation.
/// Claims array must be non-empty; empty raises ValueError.
#[pyfunction]
#[pyo3(name = "build_digest_prompt")]
pub(crate) fn py_build_digest_prompt(claims_json: &str) -> PyResult<String> {
    kg_build_digest_prompt_inner(claims_json).map_err(PyValueError::new_err)
}

/// Parse an LLM digest response string.
/// Returns JSON-serialized ParsedDigestResponse.
#[pyfunction]
#[pyo3(name = "parse_digest_response")]
pub(crate) fn py_parse_digest_response(raw: &str) -> PyResult<String> {
    kg_parse_digest_response_inner(raw).map_err(PyValueError::new_err)
}

/// Assemble a full Digest from a parsed LLM response and source claims.
#[pyfunction]
#[pyo3(name = "assemble_digest_from_llm")]
pub(crate) fn py_assemble_digest_from_llm(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> PyResult<String> {
    kg_assemble_digest_from_llm_inner(parsed_json, claims_json, now_unix_seconds)
        .map_err(PyValueError::new_err)
}

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 2: contradiction detection + feedback log
// ---------------------------------------------------------------------------

/// Default P2-3 resolution weights as JSON.
#[pyfunction]
#[pyo3(name = "default_resolution_weights")]
pub(crate) fn py_default_resolution_weights() -> PyResult<String> {
    kg_default_resolution_weights_inner().map_err(PyValueError::new_err)
}

/// Compute a claim's score components for contradiction resolution.
#[pyfunction]
#[pyo3(name = "compute_score_components")]
pub(crate) fn py_compute_score_components(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> PyResult<String> {
    kg_compute_score_components_inner(claim_json, now_unix_seconds, weights_json)
        .map_err(PyValueError::new_err)
}

/// Run the resolution formula on two contradicting claims; returns ResolutionOutcome JSON.
#[pyfunction]
#[pyo3(name = "resolve_pair")]
pub(crate) fn py_resolve_pair(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> PyResult<String> {
    kg_resolve_pair_inner(
        claim_a_json,
        claim_a_id,
        claim_b_json,
        claim_b_id,
        now_unix_seconds,
        weights_json,
    )
    .map_err(PyValueError::new_err)
}

/// Detect contradictions between a new claim and existing claims (array of {claim, id, embedding}).
#[pyfunction]
#[pyo3(name = "detect_contradictions")]
pub(crate) fn py_detect_contradictions(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> PyResult<String> {
    kg_detect_contradictions_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        existing_json,
        lower_threshold,
        upper_threshold,
    )
    .map_err(PyValueError::new_err)
}

/// Apply a single counterexample to the weights; returns updated ResolutionWeights JSON.
#[pyfunction]
#[pyo3(name = "apply_feedback")]
pub(crate) fn py_apply_feedback(weights_json: &str, counterexample_json: &str) -> PyResult<String> {
    kg_apply_feedback_inner(weights_json, counterexample_json).map_err(PyValueError::new_err)
}

/// Build a fresh default WeightsFile JSON with the given timestamp.
#[pyfunction]
#[pyo3(name = "default_weights_file")]
pub(crate) fn py_default_weights_file(now_unix_seconds: i64) -> PyResult<String> {
    kg_default_weights_file_inner(now_unix_seconds).map_err(PyValueError::new_err)
}

/// Serialize a WeightsFile JSON to pretty-printed JSON (2-space indent).
#[pyfunction]
#[pyo3(name = "serialize_weights_file")]
pub(crate) fn py_serialize_weights_file(file_json: &str) -> PyResult<String> {
    kg_serialize_weights_file_inner(file_json).map_err(PyValueError::new_err)
}

/// Parse a WeightsFile from JSON; rejects unknown versions and malformed input.
#[pyfunction]
#[pyo3(name = "parse_weights_file")]
pub(crate) fn py_parse_weights_file(content: &str) -> PyResult<String> {
    kg_parse_weights_file_inner(content).map_err(PyValueError::new_err)
}

/// Append one feedback entry to existing JSONL content.
#[pyfunction]
#[pyo3(name = "append_feedback_to_jsonl")]
pub(crate) fn py_append_feedback_to_jsonl(existing: &str, entry_json: &str) -> PyResult<String> {
    kg_append_feedback_to_jsonl_inner(existing, entry_json).map_err(PyValueError::new_err)
}

/// Parse JSONL content. Returns JSON: `{"entries": [...], "warnings": [...]}`.
#[pyfunction]
#[pyo3(name = "read_feedback_jsonl")]
pub(crate) fn py_read_feedback_jsonl(content: &str) -> PyResult<String> {
    kg_read_feedback_jsonl_inner(content).map_err(PyValueError::new_err)
}

/// Keep only the most recent `max_lines` non-empty feedback log lines.
#[pyfunction]
#[pyo3(name = "rotate_feedback_log")]
pub(crate) fn py_rotate_feedback_log(content: &str, max_lines: i64) -> String {
    kg_rotate_feedback_log_inner(content, max_lines)
}

/// Convert a feedback entry into a counterexample; returns JSON or "null".
#[pyfunction]
#[pyo3(name = "feedback_to_counterexample")]
pub(crate) fn py_feedback_to_counterexample(entry_json: &str) -> PyResult<String> {
    kg_feedback_to_counterexample_inner(entry_json).map_err(PyValueError::new_err)
}

// ---------------------------------------------------------------------------
// Pin status + decision log (Steps B & C)
// ---------------------------------------------------------------------------

use crate::decision_log;

/// Check whether a JSON-serialized claim has pinned status.
#[pyfunction]
#[pyo3(name = "is_pinned_claim")]
pub(crate) fn py_is_pinned_claim(claim_json: &str) -> bool {
    claims::is_pinned_json(claim_json)
}

/// Apply pin-status and tie-zone checks to a resolution outcome.
/// Returns a JSON-serialized ResolutionAction.
#[pyfunction]
#[pyo3(name = "respect_pin_in_resolution")]
pub(crate) fn py_respect_pin_in_resolution(
    existing_claim_json: &str,
    new_claim_id: &str,
    existing_claim_id: &str,
    resolution_winner: &str,
    score_gap: f64,
    similarity: f64,
    tie_tolerance: f64,
) -> PyResult<String> {
    let action = claims::respect_pin_in_resolution(
        existing_claim_json,
        new_claim_id,
        existing_claim_id,
        resolution_winner,
        score_gap,
        similarity,
        tie_tolerance,
    );
    serde_json::to_string(&action).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Find the loser claim JSON from the decision log for a given fact ID.
/// Returns the loser_claim_json string, or None.
#[pyfunction]
#[pyo3(name = "find_loser_claim_in_decision_log")]
pub(crate) fn py_find_loser_claim_in_decision_log(fact_id: &str, log_content: &str) -> Option<String> {
    decision_log::find_loser_claim_in_decision_log(fact_id, log_content)
}

/// Find a decision-log entry matching a fact as winner or loser.
/// Returns the JSON-serialized DecisionLogEntry, or None.
#[pyfunction]
#[pyo3(name = "find_decision_for_pin")]
pub(crate) fn py_find_decision_for_pin(fact_id: &str, role: &str, log_content: &str) -> Option<String> {
    decision_log::find_decision_for_pin(fact_id, role, log_content)
}

/// Build a FeedbackEntry JSON from a decision-log entry JSON + pin action.
/// Returns the JSON string, or None on failure.
#[pyfunction]
#[pyo3(name = "build_feedback_from_decision")]
pub(crate) fn py_build_feedback_from_decision(
    decision_json: &str,
    action: &str,
    now_unix: i64,
) -> Option<String> {
    decision_log::build_feedback_from_decision(decision_json, action, now_unix)
}

/// Append one decision entry to existing JSONL content.
#[pyfunction]
#[pyo3(name = "append_decision_entry")]
pub(crate) fn py_append_decision_entry(existing_content: &str, entry_json: &str) -> String {
    decision_log::append_decision_entry(existing_content, entry_json)
}

/// Decision log max lines constant.
#[pyfunction]
#[pyo3(name = "decision_log_max_lines")]
pub(crate) fn py_decision_log_max_lines() -> usize {
    decision_log::DECISION_LOG_MAX_LINES
}

/// Contradiction candidate cap constant.
#[pyfunction]
#[pyo3(name = "contradiction_candidate_cap")]
pub(crate) fn py_contradiction_candidate_cap() -> usize {
    decision_log::CONTRADICTION_CANDIDATE_CAP
}

/// Tie-zone score tolerance constant.
#[pyfunction]
#[pyo3(name = "tie_zone_score_tolerance")]
pub(crate) fn py_tie_zone_score_tolerance() -> f64 {
    claims::TIE_ZONE_SCORE_TOLERANCE
}

// ---------------------------------------------------------------------------
// Step D: Contradiction orchestration bindings
// ---------------------------------------------------------------------------

/// Orchestrate contradiction detection + resolution for a new claim against candidates.
/// Returns a JSON array of ResolutionAction.
#[pyfunction]
#[pyo3(name = "resolve_with_candidates")]
pub(crate) fn py_resolve_with_candidates(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    candidates_json: &str,
    weights_json: &str,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix: i64,
    tie_tolerance: f64,
) -> PyResult<String> {
    kg_resolve_with_candidates_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        candidates_json,
        weights_json,
        threshold_lower,
        threshold_upper,
        now_unix,
        tie_tolerance,
    )
    .map_err(PyValueError::new_err)
}

/// Build decision log entries from resolution actions.
/// Returns a JSON array of DecisionLogEntry.
#[pyfunction]
#[pyo3(name = "build_decision_log_entries")]
pub(crate) fn py_build_decision_log_entries(
    actions_json: &str,
    new_claim_json: &str,
    existing_claims_json: &str,
    mode: &str,
    now_unix: i64,
) -> PyResult<String> {
    kg_build_decision_log_entries_inner(
        actions_json,
        new_claim_json,
        existing_claims_json,
        mode,
        now_unix,
    )
    .map_err(PyValueError::new_err)
}

/// Filter resolution actions by mode ("active" passes through, "shadow"/"off" returns empty).
/// Returns a JSON array of ResolutionAction.
#[pyfunction]
#[pyo3(name = "filter_shadow_mode")]
pub(crate) fn py_filter_shadow_mode(actions_json: &str, mode: &str) -> PyResult<String> {
    let actions: Vec<claims::ResolutionAction> = serde_json::from_str(actions_json)
        .map_err(|e| PyValueError::new_err(format!("invalid actions JSON: {}", e)))?;
    let filtered = contradiction::filter_shadow_mode(actions, mode);
    serde_json::to_string(&filtered).map_err(|e| PyValueError::new_err(e.to_string()))
}

