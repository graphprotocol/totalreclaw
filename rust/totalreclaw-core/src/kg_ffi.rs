//! Shared knowledge-graph FFI marshalling helpers.
//!
//! These are the pure-Rust, JSON-in/JSON-out implementations behind the
//! knowledge-graph binding surface. They contain **no** `pyo3` or
//! `wasm-bindgen` types — every argument and return value is a plain
//! `&str` / `String` / primitive — so the exact same logic is shared by both
//! the PyO3 wrappers in [`crate::python`] and the wasm-bindgen wrappers in
//! [`crate::wasm`]. Previously each binding file carried a byte-identical copy
//! of all twenty of these functions; this module is the single source of truth.
//!
//! The binding layers keep only the thin attribute-decorated wrappers that map
//! the `Result<String, String>` error channel onto their framework's exception
//! type (`PyValueError` / `JsValue`).

use crate::claims;
use crate::contradiction;
use crate::digest;
use crate::feedback_log;

/// Input shape for `detect_contradictions` / `resolve_with_candidates`:
/// an array of these is passed as the `existing_json` / `candidates_json` arg.
#[derive(serde::Deserialize)]
pub struct DetectContradictionsItem {
    pub claim: claims::Claim,
    pub id: String,
    pub embedding: Vec<f32>,
}

/// Result shape for `read_feedback_jsonl`: parsed entries plus any per-line
/// parse warnings.
#[derive(serde::Serialize)]
pub struct ReadFeedbackJsonlResult {
    pub entries: Vec<feedback_log::FeedbackEntry>,
    pub warnings: Vec<String>,
}

pub fn kg_parse_claim_or_legacy_inner(decrypted: &str) -> Result<String, String> {
    let claim = claims::parse_claim_or_legacy(decrypted);
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

pub fn kg_canonicalize_claim_inner(claim_json: &str) -> Result<String, String> {
    let claim: claims::Claim =
        serde_json::from_str(claim_json).map_err(|e| format!("invalid claim JSON: {}", e))?;
    serde_json::to_string(&claim).map_err(|e| e.to_string())
}

pub fn kg_build_template_digest_inner(
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, String> {
    let parsed: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::build_template_digest(&parsed, now_unix_seconds);
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

pub fn kg_build_digest_prompt_inner(claims_json: &str) -> Result<String, String> {
    let parsed: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
    if parsed.is_empty() {
        return Err("build_digest_prompt requires at least one claim".to_string());
    }
    Ok(digest::build_digest_prompt(&parsed))
}

pub fn kg_parse_digest_response_inner(raw: &str) -> Result<String, String> {
    let parsed = digest::parse_digest_response(raw).map_err(|e| e.to_string())?;
    serde_json::to_string(&parsed).map_err(|e| e.to_string())
}

pub fn kg_assemble_digest_from_llm_inner(
    parsed_json: &str,
    claims_json: &str,
    now_unix_seconds: i64,
) -> Result<String, String> {
    let parsed: digest::ParsedDigestResponse = serde_json::from_str(parsed_json)
        .map_err(|e| format!("invalid ParsedDigestResponse JSON: {}", e))?;
    let source_claims: Vec<claims::Claim> =
        serde_json::from_str(claims_json).map_err(|e| format!("invalid claims JSON: {}", e))?;
    let d = digest::assemble_digest_from_llm(&parsed, &source_claims, now_unix_seconds)
        .map_err(|e| e.to_string())?;
    serde_json::to_string(&d).map_err(|e| e.to_string())
}

pub fn kg_default_resolution_weights_inner() -> Result<String, String> {
    let w = contradiction::default_weights();
    serde_json::to_string(&w).map_err(|e| e.to_string())
}

pub fn kg_compute_score_components_inner(
    claim_json: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, String> {
    let claim: claims::Claim =
        serde_json::from_str(claim_json).map_err(|e| format!("invalid claim JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
    let sc = contradiction::compute_score_components(&claim, now_unix_seconds, &weights);
    serde_json::to_string(&sc).map_err(|e| e.to_string())
}

pub fn kg_resolve_pair_inner(
    claim_a_json: &str,
    claim_a_id: &str,
    claim_b_json: &str,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights_json: &str,
) -> Result<String, String> {
    let claim_a: claims::Claim =
        serde_json::from_str(claim_a_json).map_err(|e| format!("invalid claim_a JSON: {}", e))?;
    let claim_b: claims::Claim =
        serde_json::from_str(claim_b_json).map_err(|e| format!("invalid claim_b JSON: {}", e))?;
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
    let outcome = contradiction::resolve_pair(
        &claim_a,
        claim_a_id,
        &claim_b,
        claim_b_id,
        now_unix_seconds,
        &weights,
    );
    serde_json::to_string(&outcome).map_err(|e| e.to_string())
}

pub fn kg_detect_contradictions_inner(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    existing_json: &str,
    lower_threshold: f64,
    upper_threshold: f64,
) -> Result<String, String> {
    let new_claim: claims::Claim = serde_json::from_str(new_claim_json)
        .map_err(|e| format!("invalid new_claim JSON: {}", e))?;
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| format!("invalid new_embedding JSON: {}", e))?;
    let items: Vec<DetectContradictionsItem> =
        serde_json::from_str(existing_json).map_err(|e| {
            format!(
                "invalid existing JSON (expected array of {{claim, id, embedding}}): {}",
                e
            )
        })?;
    let existing: Vec<(claims::Claim, String, Vec<f32>)> = items
        .into_iter()
        .map(|it| (it.claim, it.id, it.embedding))
        .collect();
    let out = contradiction::detect_contradictions(
        &new_claim,
        new_claim_id,
        &new_embedding,
        &existing,
        lower_threshold,
        upper_threshold,
    );
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

pub fn kg_apply_feedback_inner(
    weights_json: &str,
    counterexample_json: &str,
) -> Result<String, String> {
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
    let ce: contradiction::Counterexample = serde_json::from_str(counterexample_json)
        .map_err(|e| format!("invalid counterexample JSON: {}", e))?;
    let new_weights = contradiction::apply_feedback(&weights, &ce);
    serde_json::to_string(&new_weights).map_err(|e| e.to_string())
}

pub fn kg_default_weights_file_inner(now_unix_seconds: i64) -> Result<String, String> {
    let f = feedback_log::default_weights_file(now_unix_seconds);
    serde_json::to_string(&f).map_err(|e| e.to_string())
}

pub fn kg_serialize_weights_file_inner(file_json: &str) -> Result<String, String> {
    let f: feedback_log::WeightsFile =
        serde_json::from_str(file_json).map_err(|e| format!("invalid weights file JSON: {}", e))?;
    Ok(feedback_log::serialize_weights_file(&f))
}

pub fn kg_parse_weights_file_inner(content: &str) -> Result<String, String> {
    let f = feedback_log::parse_weights_file(content).map_err(|e| e.to_string())?;
    serde_json::to_string(&f).map_err(|e| e.to_string())
}

pub fn kg_append_feedback_to_jsonl_inner(
    existing: &str,
    entry_json: &str,
) -> Result<String, String> {
    let entry: feedback_log::FeedbackEntry = serde_json::from_str(entry_json)
        .map_err(|e| format!("invalid feedback entry JSON: {}", e))?;
    Ok(feedback_log::append_to_jsonl(existing, &entry))
}

pub fn kg_read_feedback_jsonl_inner(content: &str) -> Result<String, String> {
    let (entries, warnings) = feedback_log::read_jsonl(content);
    let result = ReadFeedbackJsonlResult { entries, warnings };
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

pub fn kg_rotate_feedback_log_inner(content: &str, max_lines: i64) -> String {
    let cap = if max_lines < 0 {
        0usize
    } else {
        max_lines as usize
    };
    feedback_log::rotate_if_needed(content, cap)
}

pub fn kg_feedback_to_counterexample_inner(entry_json: &str) -> Result<String, String> {
    let entry: feedback_log::FeedbackEntry = serde_json::from_str(entry_json)
        .map_err(|e| format!("invalid feedback entry JSON: {}", e))?;
    match feedback_log::feedback_to_counterexample(&entry) {
        Some(ce) => serde_json::to_string(&ce).map_err(|e| e.to_string()),
        None => Ok("null".to_string()),
    }
}

pub fn kg_resolve_with_candidates_inner(
    new_claim_json: &str,
    new_claim_id: &str,
    new_embedding_json: &str,
    candidates_json: &str,
    weights_json: &str,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix: i64,
    tie_tolerance: f64,
) -> Result<String, String> {
    let new_claim: claims::Claim = serde_json::from_str(new_claim_json)
        .map_err(|e| format!("invalid new_claim JSON: {}", e))?;
    let new_embedding: Vec<f32> = serde_json::from_str(new_embedding_json)
        .map_err(|e| format!("invalid new_embedding JSON: {}", e))?;
    let items: Vec<DetectContradictionsItem> = serde_json::from_str(candidates_json)
        .map_err(|e| format!("invalid candidates JSON: {}", e))?;
    let candidates: Vec<(claims::Claim, String, Vec<f32>)> = items
        .into_iter()
        .map(|it| (it.claim, it.id, it.embedding))
        .collect();
    let weights: contradiction::ResolutionWeights =
        serde_json::from_str(weights_json).map_err(|e| format!("invalid weights JSON: {}", e))?;
    let actions = contradiction::resolve_with_candidates(
        &new_claim,
        new_claim_id,
        &new_embedding,
        &candidates,
        &weights,
        threshold_lower,
        threshold_upper,
        now_unix,
        tie_tolerance,
    );
    serde_json::to_string(&actions).map_err(|e| e.to_string())
}

pub fn kg_build_decision_log_entries_inner(
    actions_json: &str,
    new_claim_json: &str,
    existing_claims_json: &str,
    mode: &str,
    now_unix: i64,
) -> Result<String, String> {
    let actions: Vec<claims::ResolutionAction> =
        serde_json::from_str(actions_json).map_err(|e| format!("invalid actions JSON: {}", e))?;
    let existing_map: std::collections::HashMap<String, String> =
        serde_json::from_str(existing_claims_json)
            .map_err(|e| format!("invalid existing_claims JSON: {}", e))?;
    let entries = contradiction::build_decision_log_entries(
        &actions,
        new_claim_json,
        &existing_map,
        mode,
        now_unix,
    );
    serde_json::to_string(&entries).map_err(|e| e.to_string())
}
