//! WASM bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features wasm`. Built with `wasm-pack build --target nodejs`.
//!
//! All byte arrays cross the boundary as hex strings. Complex return types
//! (Vec<String>, structs) are serialized as JSON strings or JsValues.

use wasm_bindgen::prelude::*;

use crate::blind;
use crate::claims;
use crate::confirm;
use crate::contradiction;
use crate::crypto;
use crate::debrief;
use crate::fingerprint;
use crate::lsh;
use crate::pin_intent;
use crate::protobuf;
use crate::reranker;
#[cfg(feature = "managed")]
use crate::search;
use crate::store;
#[cfg(feature = "managed")]
use crate::userop;
use crate::wallet;
// Shared JSON-marshalling helpers behind the KG bindings (also used by the
// python binding). The `#[wasm_bindgen]` wrappers below call these `kg_*_inner`
// fns unqualified.
use crate::kg_ffi::*;

// ---------------------------------------------------------------------------
// Domain submodules (binding declarations); wasm-bindgen self-registers each.
// ---------------------------------------------------------------------------
mod bind_crypto;
mod bind_lsh;
mod bind_protobuf;
mod bind_extraction;
mod bind_reranker;
mod bind_wallet;
mod bind_userop;
mod bind_store;
mod bind_search;
mod bind_kg;
mod bind_recall;

pub(crate) use bind_lsh::*;
#[cfg(test)]
pub(crate) use bind_crypto::*;
#[cfg(test)]
pub(crate) use bind_protobuf::*;
#[cfg(test)]
pub(crate) use bind_extraction::*;
#[cfg(test)]
pub(crate) use bind_reranker::*;
#[cfg(test)]
pub(crate) use bind_wallet::*;
#[cfg(test)]
pub(crate) use bind_userop::*;
#[cfg(test)]
pub(crate) use bind_store::*;
#[cfg(test)]
pub(crate) use bind_search::*;
#[cfg(test)]
pub(crate) use bind_kg::*;
#[cfg(test)]
pub(crate) use bind_recall::*;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Convert DerivedKeys to a JsValue (plain JS object with hex strings).
fn keys_to_js(keys: &crypto::DerivedKeys) -> Result<JsValue, JsError> {
    // Use js_sys to build a plain object (not a Map) for ergonomic JS access.
    let obj = js_sys::Object::new();
    js_sys::Reflect::set(&obj, &"auth_key".into(), &hex::encode(keys.auth_key).into())
        .map_err(|_| JsError::new("failed to set auth_key"))?;
    js_sys::Reflect::set(
        &obj,
        &"encryption_key".into(),
        &hex::encode(keys.encryption_key).into(),
    )
    .map_err(|_| JsError::new("failed to set encryption_key"))?;
    js_sys::Reflect::set(
        &obj,
        &"dedup_key".into(),
        &hex::encode(keys.dedup_key).into(),
    )
    .map_err(|_| JsError::new("failed to set dedup_key"))?;
    js_sys::Reflect::set(&obj, &"salt".into(), &hex::encode(keys.salt).into())
        .map_err(|_| JsError::new("failed to set salt"))?;
    Ok(obj.into())
}

/// Parse a 32-byte hex key, returning a friendly error on failure.
fn parse_key_hex(hex_str: &str, name: &str) -> Result<[u8; 32], JsError> {
    let bytes =
        hex::decode(hex_str).map_err(|e| JsError::new(&format!("invalid {} hex: {}", name, e)))?;
    if bytes.len() != 32 {
        return Err(JsError::new(&format!(
            "{} must be 32 bytes, got {}",
            name,
            bytes.len()
        )));
    }
    let mut key = [0u8; 32];
    key.copy_from_slice(&bytes);
    Ok(key)
}

/// Convert a crate::Error to JsError.
fn to_js_error(e: crate::Error) -> JsError {
    JsError::new(&e.to_string())
}


// ---------------------------------------------------------------------------
// Tests (non-wasm runtime — direct Rust fn invocation)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::digest;

    fn sample_claim_json() -> String {
        r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#.to_string()
    }

    fn two_claims_json() -> String {
        r#"[
            {"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"},
            {"t":"lives in Lisbon","c":"fact","cf":0.95,"i":9,"sa":"oc"}
        ]"#
        .to_string()
    }

    #[test]
    fn wasm_normalize_entity_name_lowercases() {
        assert_eq!(wasm_normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn wasm_deterministic_entity_id_known_answer_pedro() {
        assert_eq!(wasm_deterministic_entity_id("pedro"), "ee5cd7d5d96c8874");
    }

    #[test]
    fn wasm_parse_claim_or_legacy_full_claim_roundtrips() {
        let input = sample_claim_json();
        let out = kg_parse_claim_or_legacy_inner(&input).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "prefers PostgreSQL");
        assert_eq!(c.category, claims::ClaimCategory::Preference);
    }

    #[test]
    fn wasm_parse_claim_or_legacy_legacy_object() {
        let out = kg_parse_claim_or_legacy_inner(r#"{"t":"hello","a":"oc"}"#).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "hello");
        assert_eq!(c.source_agent, "oc");
        assert_eq!(c.category, claims::ClaimCategory::Fact);
    }

    #[test]
    fn wasm_build_template_digest_empty_vault() {
        let out = kg_build_template_digest_inner("[]", 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 0);
        assert!(!d.prompt_text.is_empty());
    }

    #[test]
    fn wasm_build_template_digest_two_claims() {
        let out = kg_build_template_digest_inner(&two_claims_json(), 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 2);
    }

    #[test]
    fn wasm_build_digest_prompt_empty_is_error() {
        let result = kg_build_digest_prompt_inner("[]");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_build_digest_prompt_one_claim_returns_prompt() {
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let prompt = kg_build_digest_prompt_inner(one).unwrap();
        assert!(!prompt.is_empty());
        assert!(prompt.contains("JSON"));
    }

    #[test]
    fn wasm_parse_digest_response_valid_fenced() {
        let raw = "```json\n{\"identity\":\"You are a developer.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[\"skynet\"]}\n```";
        let out = kg_parse_digest_response_inner(raw).unwrap();
        let p: digest::ParsedDigestResponse = serde_json::from_str(&out).unwrap();
        assert_eq!(p.identity, "You are a developer.");
        assert_eq!(p.top_claim_indices, vec![1]);
        assert_eq!(p.active_project_names, vec!["skynet".to_string()]);
    }

    #[test]
    fn wasm_parse_digest_response_invalid_is_error() {
        let result = kg_parse_digest_response_inner("not valid json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_assemble_digest_from_llm_builds_digest() {
        let parsed = r#"{"identity":"You are a developer.","top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":["skynet"]}"#;
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let out = kg_assemble_digest_from_llm_inner(parsed, one, 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 1);
        assert_eq!(d.identity, "You are a developer.");
    }

    #[test]
    fn wasm_canonicalize_claim_round_trips_canonical_input() {
        let input = sample_claim_json();
        let out = kg_canonicalize_claim_inner(&input).unwrap();
        assert_eq!(out, input);
    }

    #[test]
    fn wasm_canonicalize_claim_omits_default_status() {
        // Client sends verbose input with status="a" (Active, the default).
        // Canonical output omits the field.
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"a"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(!out.contains("\"st\""));
        assert!(out.contains("\"t\":\"hi\""));
    }

    #[test]
    fn wasm_canonicalize_claim_preserves_non_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"s"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(out.contains("\"st\":\"s\""));
    }

    #[test]
    fn wasm_canonicalize_claim_omits_default_corroboration() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","cc":1}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert!(!out.contains("\"cc\""));
    }

    #[test]
    fn wasm_canonicalize_claim_reorders_fields_to_struct_order() {
        // Input fields in random order; canonical output must follow Claim struct field order.
        let input = r#"{"sa":"oc","i":5,"cf":0.9,"c":"fact","t":"hi"}"#;
        let out = kg_canonicalize_claim_inner(input).unwrap();
        assert_eq!(out, r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#);
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_malformed_json() {
        let result = kg_canonicalize_claim_inner("{not valid");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_missing_required_field() {
        // Missing `c` (category)
        let result = kg_canonicalize_claim_inner(r#"{"t":"hi","cf":0.9,"i":5,"sa":"oc"}"#);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_canonicalize_claim_rejects_legacy_format() {
        // Legacy {"t":"...","a":"..."} has no `c` field so strict parse fails.
        let result = kg_canonicalize_claim_inner(r#"{"t":"hi","a":"oc"}"#);
        assert!(result.is_err());
    }

    // --- Phase 2 Slice 2c: contradiction bindings -----------------------

    /// 2026-04-12T00:00:00Z — matches the contradiction module's test NOW.
    const PHASE2_NOW: i64 = 1776211200;

    fn iso_days_ago_str(days: i64) -> String {
        let ts = PHASE2_NOW - days * 86400;
        chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0)
            .unwrap()
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    }

    #[test]
    fn wasm_default_resolution_weights_matches_p2_3_defaults() {
        let out = kg_default_resolution_weights_inner().unwrap();
        let w: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        assert_eq!(w.confidence, 0.25);
        assert_eq!(w.corroboration, 0.15);
        assert_eq!(w.recency, 0.40);
        assert_eq!(w.validation, 0.20);
    }

    #[test]
    fn wasm_compute_score_components_known_answer() {
        // 0.9*0.25 + 1*0.15 + 1*0.40 + 1*0.20 = 0.975 (explicit remember, today).
        let claim = format!(
            r#"{{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"totalreclaw_remember","ea":"{}"}}"#,
            iso_days_ago_str(0)
        );
        let weights = kg_default_resolution_weights_inner().unwrap();
        let out = kg_compute_score_components_inner(&claim, PHASE2_NOW, &weights).unwrap();
        let sc: contradiction::ScoreComponents = serde_json::from_str(&out).unwrap();
        assert_eq!(sc.confidence, 0.9);
        assert!((sc.corroboration - 1.0).abs() < 1e-12);
        assert!((sc.recency - 1.0).abs() < 1e-12);
        assert_eq!(sc.validation, 1.0);
        assert!((sc.weighted_total - 0.975).abs() < 1e-12);
    }

    #[test]
    fn wasm_compute_score_components_rejects_malformed_claim() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let result = kg_compute_score_components_inner("{not json", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_compute_score_components_rejects_malformed_weights() {
        let claim = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_compute_score_components_inner(claim, PHASE2_NOW, "{not json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_resolve_pair_vim_vs_vscode_defaults_vscode_wins() {
        let vim = format!(
            r#"{{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","ea":"{}","cc":3,"e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(60)
        );
        let vscode = format!(
            r#"{{"t":"uses VS Code","c":"fact","cf":0.9,"i":5,"sa":"oc","ea":"{}","e":[{{"n":"editor","tp":"tool"}}]}}"#,
            iso_days_ago_str(7)
        );
        let weights = kg_default_resolution_weights_inner().unwrap();
        let out = kg_resolve_pair_inner(&vim, "vim_id", &vscode, "vscode_id", PHASE2_NOW, &weights)
            .unwrap();
        let outcome: contradiction::ResolutionOutcome = serde_json::from_str(&out).unwrap();
        assert_eq!(outcome.winner_id, "vscode_id");
        assert_eq!(outcome.loser_id, "vim_id");
        assert!(outcome.winner_score > outcome.loser_score);
        assert!(outcome.score_delta > 0.0);
    }

    #[test]
    fn wasm_resolve_pair_rejects_malformed_claim_a() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let good = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_resolve_pair_inner("{bad", "a", good, "b", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn wasm_detect_contradictions_empty_existing_returns_empty_array() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0, 0.0, 0.0]).unwrap();
        let out =
            kg_detect_contradictions_inner(new_claim, "new_id", &emb, "[]", 0.3, 0.85).unwrap();
        assert_eq!(out, "[]");
    }

    #[test]
    fn wasm_detect_contradictions_single_in_band_returns_one() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let existing_claim_obj = r#"{"t":"uses Emacs","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        // Build an 8-d vector pair with cosine ~0.5 (in-band).
        let new_emb: Vec<f32> = vec![1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
        let mut ex_emb = vec![0.0f32; 8];
        let cos = 0.5_f64;
        let sin = (1.0 - cos * cos).sqrt();
        ex_emb[0] = cos as f32;
        ex_emb[1] = sin as f32;
        let new_emb_json = serde_json::to_string(&new_emb).unwrap();
        let existing_json = format!(
            r#"[{{"claim":{},"id":"exist","embedding":{}}}]"#,
            existing_claim_obj,
            serde_json::to_string(&ex_emb).unwrap()
        );
        let out = kg_detect_contradictions_inner(
            new_claim,
            "new_id",
            &new_emb_json,
            &existing_json,
            0.3,
            0.85,
        )
        .unwrap();
        let parsed: Vec<contradiction::Contradiction> = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].claim_a_id, "new_id");
        assert_eq!(parsed[0].claim_b_id, "exist");
        assert!((parsed[0].similarity - 0.5).abs() < 1e-6);
        // Sanity: the returned entity_id is the deterministic id of "editor".
        assert_eq!(
            parsed[0].entity_id,
            claims::deterministic_entity_id("editor")
        );
    }

    #[test]
    fn wasm_detect_contradictions_rejects_malformed_existing_shape() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0]).unwrap();
        // Wrong shape: missing "id" and "embedding" keys.
        let existing = r#"[{"claim":{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}}]"#;
        let result = kg_detect_contradictions_inner(new_claim, "new_id", &emb, existing, 0.3, 0.85);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("existing"), "err: {}", err);
    }

    #[test]
    fn wasm_apply_feedback_returns_clamped_weights() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        // Counterexample with asymmetric deltas — the gradient step stays within [0.05, 0.60].
        let ce = r#"{
            "formula_winner":{"confidence":0.9,"corroboration":3.0,"recency":1.0,"validation":1.0,"weighted_total":0.975},
            "formula_loser":{"confidence":0.3,"corroboration":1.0,"recency":0.1,"validation":0.7,"weighted_total":0.24},
            "user_pinned":"loser"
        }"#;
        let out = kg_apply_feedback_inner(&weights, ce).unwrap();
        let new: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        for v in [
            new.confidence,
            new.corroboration,
            new.recency,
            new.validation,
        ] {
            assert!(v >= 0.05 - 1e-12, "weight below clamp: {}", v);
            assert!(v <= 0.60 + 1e-12, "weight above clamp: {}", v);
        }
        let sum = new.confidence + new.corroboration + new.recency + new.validation;
        assert!(
            sum >= 0.9 - 1e-9 && sum <= 1.1 + 1e-9,
            "weight sum out of range: {}",
            sum
        );
    }

    // --- Phase 2 Slice 2c: feedback_log bindings ------------------------

    fn sample_entry_json() -> String {
        r#"{"ts":1776384000,"claim_a_id":"0xaaa","claim_b_id":"0xbbb","formula_winner":"a","user_decision":"pin_b","winner_components":{"confidence":0.8,"corroboration":1.732,"recency":0.333,"validation":0.7,"weighted_total":0.7331},"loser_components":{"confidence":0.6,"corroboration":1.0,"recency":0.125,"validation":0.5,"weighted_total":0.4025}}"#.to_string()
    }

    #[test]
    fn wasm_default_weights_file_round_trips() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        let back = kg_parse_weights_file_inner(&pretty).unwrap();
        // Canonical serialisation of the parsed file must match the original.
        assert_eq!(back, out);
    }

    #[test]
    fn wasm_serialize_weights_file_is_pretty() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        assert!(pretty.contains('\n'), "pretty JSON must contain newlines");
        assert!(pretty.contains("  "), "pretty JSON must use 2-space indent");
    }

    #[test]
    fn wasm_parse_weights_file_rejects_malformed() {
        let result = kg_parse_weights_file_inner("not-json");
        assert!(result.is_err());
    }

    #[test]
    fn wasm_parse_weights_file_rejects_unknown_version() {
        let bad = r#"{"version":99,"updated_at":0,"weights":{"confidence":0.25,"corroboration":0.15,"recency":0.4,"validation":0.2},"threshold_lower":0.3,"threshold_upper":0.85,"feedback_count":0}"#;
        let result = kg_parse_weights_file_inner(bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("unsupported"));
    }

    #[test]
    fn wasm_append_feedback_to_jsonl_empty_produces_one_line() {
        let out = kg_append_feedback_to_jsonl_inner("", &sample_entry_json()).unwrap();
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn wasm_append_feedback_to_jsonl_existing_produces_two_lines() {
        let entry = sample_entry_json();
        let first = kg_append_feedback_to_jsonl_inner("", &entry).unwrap();
        let second = kg_append_feedback_to_jsonl_inner(&first, &entry).unwrap();
        assert_eq!(second.matches('\n').count(), 2);
    }

    #[test]
    fn wasm_read_feedback_jsonl_round_trip_many_entries() {
        let mut content = String::new();
        for _ in 0..3 {
            content = kg_append_feedback_to_jsonl_inner(&content, &sample_entry_json()).unwrap();
        }
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 3);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn wasm_read_feedback_jsonl_surfaces_warnings_for_bad_lines() {
        let content = "not-json\n".to_string() + &sample_entry_json() + "\n";
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 1);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn wasm_rotate_feedback_log_drops_oldest_when_over_cap() {
        let mut content = String::new();
        for i in 0..5 {
            let entry = sample_entry_json().replace("1776384000", &format!("177638400{}", i));
            content = kg_append_feedback_to_jsonl_inner(&content, &entry).unwrap();
        }
        let rotated = kg_rotate_feedback_log_inner(&content, 3);
        let out = kg_read_feedback_jsonl_inner(&rotated).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        let entries = parsed["entries"].as_array().unwrap();
        assert_eq!(entries.len(), 3);
        // Most recent three kept: ts 1776384002, 1776384003, 1776384004.
        assert_eq!(entries[0]["ts"].as_i64().unwrap(), 1_776_384_002);
        assert_eq!(entries[2]["ts"].as_i64().unwrap(), 1_776_384_004);
    }

    #[test]
    fn wasm_rotate_feedback_log_preserves_content_below_cap() {
        let content = kg_append_feedback_to_jsonl_inner("", &sample_entry_json()).unwrap();
        let rotated = kg_rotate_feedback_log_inner(&content, 10);
        assert_eq!(rotated, content);
    }

    #[test]
    fn wasm_feedback_to_counterexample_pin_b_when_formula_winner_a_returns_ce() {
        // Sample entry already has formula_winner=a, user_decision=pin_b.
        let out = kg_feedback_to_counterexample_inner(&sample_entry_json()).unwrap();
        assert_ne!(out, "null");
        let ce: contradiction::Counterexample = serde_json::from_str(&out).unwrap();
        assert_eq!(ce.user_pinned, contradiction::UserPinned::Loser);
    }

    #[test]
    fn wasm_feedback_to_counterexample_pin_a_when_formula_winner_a_returns_null() {
        let entry = sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"pin_a\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn wasm_feedback_to_counterexample_unpin_returns_null() {
        let entry = sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"unpin\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn wasm_feedback_to_counterexample_rejects_malformed_entry() {
        let result = kg_feedback_to_counterexample_inner("{not-an-entry");
        assert!(result.is_err());
    }
}
