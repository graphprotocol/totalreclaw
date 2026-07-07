//! `bind_kg` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 1
// ---------------------------------------------------------------------------

/// Normalize an entity name (NFC, lowercase, trim, collapse whitespace).
#[wasm_bindgen(js_name = "normalizeEntityName")]
pub fn wasm_normalize_entity_name(name: &str) -> String {
    claims::normalize_entity_name(name)
}

/// Deterministic entity ID from a name (first 8 bytes of SHA256 as hex).
#[wasm_bindgen(js_name = "deterministicEntityId")]
pub fn wasm_deterministic_entity_id(name: &str) -> String {
    claims::deterministic_entity_id(name)
}

// The JSON-in/JSON-out inner implementations live in [`crate::kg_ffi`] (shared
// byte-for-byte with the python bindings). The `#[wasm_bindgen]` wrappers below
// just call those `kg_*_inner` helpers and map String errors to JsError.

/// Parse a decrypted blob as a Claim, falling back to legacy formats.
/// Returns JSON-serialized Claim.
#[wasm_bindgen(js_name = "parseClaimOrLegacy")]
pub fn wasm_parse_claim_or_legacy(decrypted: &str) -> Result<String, JsError> {
    kg_parse_claim_or_legacy_inner(decrypted).map_err(|e| JsError::new(&e))
}

/// Canonicalize a Claim JSON: strict-parse as Claim, re-serialize to canonical bytes.
/// Rejects legacy or malformed input. Use before encryption so TS/Python/Rust all
/// produce byte-identical blobs for the same logical claim.
#[wasm_bindgen(js_name = "canonicalizeClaim")]
pub fn wasm_canonicalize_claim(claim_json: &str) -> Result<String, JsError> {
    kg_canonicalize_claim_inner(claim_json).map_err(|e| JsError::new(&e))
}

/// Build a template digest from an array of active claims.
/// `claims_json`: JSON array of Claim. Returns JSON-serialized Digest.
#[wasm_bindgen(js_name = "buildTemplateDigest")]
pub fn wasm_build_template_digest(
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, JsError> {
    kg_build_template_digest_inner(claims_json, now_unix_seconds).map_err(|e| JsError::new(&e))
}

/// Build the LLM prompt for digest compilation.
/// `claims_json`: JSON array of Claim (must be non-empty).
#[wasm_bindgen(js_name = "buildDigestPrompt")]
pub fn wasm_build_digest_prompt(claims_json: &str) -> Result<String, JsError> {
    kg_build_digest_prompt_inner(claims_json).map_err(|e| JsError::new(&e))
}

/// Parse an LLM digest response.
/// Returns JSON-serialized ParsedDigestResponse.
#[wasm_bindgen(js_name = "parseDigestResponse")]
pub fn wasm_parse_digest_response(raw: &str) -> Result<String, JsError> {
    kg_parse_digest_response_inner(raw).map_err(|e| JsError::new(&e))
}

/// Assemble a full Digest from a parsed LLM response and source claims.
#[wasm_bindgen(js_name = "assembleDigestFromLlm")]
pub fn wasm_assemble_digest_from_llm(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, JsError> {
    kg_assemble_digest_from_llm_inner(parsed_json, claims_json, now_unix_seconds)
        .map_err(|e| JsError::new(&e))
}

// ---------------------------------------------------------------------------
// Knowledge Graph Phase 2: contradiction detection + feedback log
// ---------------------------------------------------------------------------

/// Default P2-3 resolution weights as JSON.
#[wasm_bindgen(js_name = "defaultResolutionWeights")]
pub fn wasm_default_resolution_weights() -> Result<String, JsError> {
    kg_default_resolution_weights_inner().map_err(|e| JsError::new(&e))
}

/// Compute a claim's score components for contradiction resolution.
#[wasm_bindgen(js_name = "computeScoreComponents")]
pub fn wasm_compute_score_components(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, JsError> {
    kg_compute_score_components_inner(claim_json, now_unix_seconds, weights_json)
        .map_err(|e| JsError::new(&e))
}

/// Run the resolution formula on two contradicting claims; returns ResolutionOutcome JSON.
#[wasm_bindgen(js_name = "resolvePair")]
pub fn wasm_resolve_pair(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, JsError> {
    kg_resolve_pair_inner(
        claim_a_json,
        claim_a_id,
        claim_b_json,
        claim_b_id,
        now_unix_seconds,
        weights_json,
    )
    .map_err(|e| JsError::new(&e))
}

/// Detect contradictions between a new claim and existing claims (JSON array of {claim, id, embedding}).
#[wasm_bindgen(js_name = "detectContradictions")]
pub fn wasm_detect_contradictions(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> Result<String, JsError> {
    kg_detect_contradictions_inner(
        new_claim_json,
        new_claim_id,
        new_embedding_json,
        existing_json,
        lower_threshold,
        upper_threshold,
    )
    .map_err(|e| JsError::new(&e))
}

/// Apply a single counterexample to the weights; returns updated ResolutionWeights JSON.
#[wasm_bindgen(js_name = "applyFeedback")]
pub fn wasm_apply_feedback(
    weights_json: &str,
    counterexample_json: &str,
) -> Result<String, JsError> {
    kg_apply_feedback_inner(weights_json, counterexample_json).map_err(|e| JsError::new(&e))
}

/// Build a fresh default WeightsFile JSON with the given timestamp.
#[wasm_bindgen(js_name = "defaultWeightsFile")]
pub fn wasm_default_weights_file(now_unix_seconds: i64) -> Result<String, JsError> {
    kg_default_weights_file_inner(now_unix_seconds).map_err(|e| JsError::new(&e))
}

/// Serialize a WeightsFile JSON to pretty-printed JSON (2-space indent).
#[wasm_bindgen(js_name = "serializeWeightsFile")]
pub fn wasm_serialize_weights_file(file_json: &str) -> Result<String, JsError> {
    kg_serialize_weights_file_inner(file_json).map_err(|e| JsError::new(&e))
}

/// Parse a WeightsFile from JSON; rejects unknown versions and malformed input.
#[wasm_bindgen(js_name = "parseWeightsFile")]
pub fn wasm_parse_weights_file(content: &str) -> Result<String, JsError> {
    kg_parse_weights_file_inner(content).map_err(|e| JsError::new(&e))
}

/// Append one feedback entry to existing JSONL content.
#[wasm_bindgen(js_name = "appendFeedbackToJsonl")]
pub fn wasm_append_feedback_to_jsonl(existing: &str, entry_json: &str) -> Result<String, JsError> {
    kg_append_feedback_to_jsonl_inner(existing, entry_json).map_err(|e| JsError::new(&e))
}

/// Parse JSONL content. Returns JSON: `{"entries": [...], "warnings": [...]}`.
#[wasm_bindgen(js_name = "readFeedbackJsonl")]
pub fn wasm_read_feedback_jsonl(content: &str) -> Result<String, JsError> {
    kg_read_feedback_jsonl_inner(content).map_err(|e| JsError::new(&e))
}

/// Keep only the most recent `max_lines` non-empty feedback log lines. Non-falliable.
#[wasm_bindgen(js_name = "rotateFeedbackLog")]
pub fn wasm_rotate_feedback_log(content: &str, max_lines: i64) -> String {
    kg_rotate_feedback_log_inner(content, max_lines)
}

/// Convert a feedback entry into a counterexample for weight tuning. Returns
/// JSON Counterexample or the literal string "null" if the entry has no signal.
#[wasm_bindgen(js_name = "feedbackToCounterexample")]
pub fn wasm_feedback_to_counterexample(entry_json: &str) -> Result<String, JsError> {
    kg_feedback_to_counterexample_inner(entry_json).map_err(|e| JsError::new(&e))
}

// ---------------------------------------------------------------------------
// Pin status + decision log (Steps B & C)
// ---------------------------------------------------------------------------

use crate::decision_log;

/// Check whether a JSON-serialized claim has pinned status.
#[wasm_bindgen(js_name = "isPinnedClaim")]
pub fn wasm_is_pinned_claim(claim_json: &str) -> bool {
    claims::is_pinned_json(claim_json)
}

/// Apply pin-status and tie-zone checks to a resolution outcome.
/// Returns a JSON-serialized `ResolutionAction`.
#[wasm_bindgen(js_name = "respectPinInResolution")]
pub fn wasm_respect_pin_in_resolution(
    existing_claim_json: &str,
    new_claim_id: &str,
    existing_claim_id: &str,
    resolution_winner: &str,
    score_gap: f64,
    similarity: f64,
    tie_tolerance: f64,
) -> Result<String, JsError> {
    let action = claims::respect_pin_in_resolution(
        existing_claim_json,
        new_claim_id,
        existing_claim_id,
        resolution_winner,
        score_gap,
        similarity,
        tie_tolerance,
    );
    serde_json::to_string(&action).map_err(|e| JsError::new(&e.to_string()))
}

/// Find the loser claim JSON from the decision log for a given fact ID.
/// Returns the loser_claim_json string, or the literal string "null" if not found.
#[wasm_bindgen(js_name = "findLoserClaimInDecisionLog")]
pub fn wasm_find_loser_claim_in_decision_log(fact_id: &str, log_content: &str) -> String {
    match decision_log::find_loser_claim_in_decision_log(fact_id, log_content) {
        Some(json) => json,
        None => "null".to_string(),
    }
}

/// Find a decision-log entry matching a fact as winner or loser.
/// Returns the JSON-serialized DecisionLogEntry, or the literal string "null".
#[wasm_bindgen(js_name = "findDecisionForPin")]
pub fn wasm_find_decision_for_pin(fact_id: &str, role: &str, log_content: &str) -> String {
    match decision_log::find_decision_for_pin(fact_id, role, log_content) {
        Some(json) => json,
        None => "null".to_string(),
    }
}

/// Build a FeedbackEntry JSON from a decision-log entry JSON + pin action.
/// Returns the JSON string, or the literal string "null" on failure.
#[wasm_bindgen(js_name = "buildFeedbackFromDecision")]
pub fn wasm_build_feedback_from_decision(
    decision_json: &str,
    action: &str,
    now_unix: i64,
) -> String {
    match decision_log::build_feedback_from_decision(decision_json, action, now_unix) {
        Some(json) => json,
        None => "null".to_string(),
    }
}

/// Append one decision entry to existing JSONL content. Non-fallible.
#[wasm_bindgen(js_name = "appendDecisionEntry")]
pub fn wasm_append_decision_entry(existing_content: &str, entry_json: &str) -> String {
    decision_log::append_decision_entry(existing_content, entry_json)
}

/// Decision log max lines constant.
#[wasm_bindgen(js_name = "DECISION_LOG_MAX_LINES")]
pub fn wasm_decision_log_max_lines() -> usize {
    decision_log::DECISION_LOG_MAX_LINES
}

/// Contradiction candidate cap constant.
#[wasm_bindgen(js_name = "CONTRADICTION_CANDIDATE_CAP")]
pub fn wasm_contradiction_candidate_cap() -> usize {
    decision_log::CONTRADICTION_CANDIDATE_CAP
}

/// Tie-zone score tolerance constant.
#[wasm_bindgen(js_name = "TIE_ZONE_SCORE_TOLERANCE")]
pub fn wasm_tie_zone_score_tolerance() -> f64 {
    claims::TIE_ZONE_SCORE_TOLERANCE
}

// ---------------------------------------------------------------------------
// Step D: Contradiction orchestration bindings
// ---------------------------------------------------------------------------

/// Orchestrate contradiction detection + resolution for a new claim against candidates.
///
/// Returns a JSON array of `ResolutionAction`.
#[wasm_bindgen(js_name = "resolveWithCandidates")]
pub fn wasm_resolve_with_candidates(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    candidates_json: &str,
    weights_json: &str,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix: i64,
    tie_tolerance: f64,
) -> Result<String, JsError> {
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
    .map_err(|e| JsError::new(&e))
}

/// Build decision log entries from resolution actions.
///
/// Returns a JSON array of `DecisionLogEntry`.
#[wasm_bindgen(js_name = "buildDecisionLogEntries")]
pub fn wasm_build_decision_log_entries(
    actions_json: &str,
    new_claim_json: &str,
    existing_claims_json: &str,
    mode: &str,
    now_unix: i64,
) -> Result<String, JsError> {
    kg_build_decision_log_entries_inner(
        actions_json,
        new_claim_json,
        existing_claims_json,
        mode,
        now_unix,
    )
    .map_err(|e| JsError::new(&e))
}

/// Filter resolution actions by mode ("active" passes through, "shadow"/"off" returns empty).
///
/// Returns a JSON array of `ResolutionAction`.
#[wasm_bindgen(js_name = "filterShadowMode")]
pub fn wasm_filter_shadow_mode(actions_json: &str, mode: &str) -> Result<String, JsError> {
    let actions: Vec<claims::ResolutionAction> = serde_json::from_str(actions_json)
        .map_err(|e| JsError::new(&format!("invalid actions JSON: {}", e)))?;
    let filtered = contradiction::filter_shadow_mode(actions, mode);
    serde_json::to_string(&filtered).map_err(|e| JsError::new(&e.to_string()))
}

