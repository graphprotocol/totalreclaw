//! Python (PyO3) bindings for TotalReclaw core crypto primitives.
//!
//! Enabled via `--features python`. Built with `maturin develop --features python`.
//!
//! Module name: `totalreclaw_core` (underscore, not hyphen).
//!
//! All byte arrays are returned as Python `bytes` objects.
//!
//! # Naming convention
//!
//! Every `#[pyfunction]` uses a `py_`-prefixed internal Rust name plus an
//! explicit `#[pyo3(name = "...")]` giving the Python-visible name. This keeps
//! the Rust wrapper unambiguously distinct from the same-named core function it
//! delegates to (`py_encrypt` → `crypto::encrypt`) and makes the exported
//! Python surface a single, greppable list of `#[pyo3(name = ...)]` attributes.
//! When adding a binding, follow this convention rather than a bare fn name.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

#[cfg(feature = "managed")]
use crate::search;
#[cfg(feature = "managed")]
use crate::userop;
use crate::{
    blind, claims, confirm, contradiction, crypto, debrief, fingerprint, lsh, pin_intent, protobuf,
    recall_context, reranker, session_segmentation, store,
};
// Shared JSON-marshalling helpers behind the KG bindings (also used by the wasm
// binding). The `#[pyfunction]` wrappers below call these `kg_*_inner` fns
// unqualified.
use crate::kg_ffi::*;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Convert our crate Error to a Python ValueError.
fn to_pyerr(e: crate::Error) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// Extract a `[u8; 32]` from a Python `bytes` object.
fn bytes_to_array32(b: &[u8]) -> PyResult<[u8; 32]> {
    b.try_into()
        .map_err(|_| PyValueError::new_err(format!("expected 32 bytes, got {}", b.len())))
}

// ---------------------------------------------------------------------------
// Domain submodules (binding declarations live here; registration stays below)
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

pub(crate) use bind_crypto::*;
pub(crate) use bind_lsh::*;
pub(crate) use bind_protobuf::*;
pub(crate) use bind_extraction::*;
pub(crate) use bind_reranker::*;
pub(crate) use bind_wallet::*;
pub(crate) use bind_userop::*;
pub(crate) use bind_store::*;
pub(crate) use bind_search::*;
pub(crate) use bind_kg::*;
pub(crate) use bind_recall::*;

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

/// TotalReclaw core crypto primitives (Rust implementation).
///
/// This module provides byte-for-byte compatible implementations of all
/// TotalReclaw cryptographic operations: key derivation, XChaCha20-Poly1305
/// encryption, blind indices, content fingerprinting, LSH hashing,
/// protobuf encoding, and debrief parsing.
#[pymodule]
fn totalreclaw_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Key derivation
    m.add_function(wrap_pyfunction!(py_derive_keys_from_mnemonic, m)?)?;
    m.add_function(wrap_pyfunction!(py_derive_keys_from_mnemonic_lenient, m)?)?;
    m.add_function(wrap_pyfunction!(py_derive_lsh_seed, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_auth_key_hash, m)?)?;

    // Encryption
    m.add_function(wrap_pyfunction!(py_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(py_decrypt, m)?)?;

    // Search
    m.add_function(wrap_pyfunction!(py_generate_blind_indices, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_content_fingerprint, m)?)?;
    m.add_function(wrap_pyfunction!(py_normalize_text, m)?)?;

    // LSH
    m.add_class::<PyLshHasher>()?;

    // Protobuf
    m.add_function(wrap_pyfunction!(py_encode_fact_protobuf, m)?)?;
    m.add_function(wrap_pyfunction!(py_encode_tombstone_protobuf, m)?)?;

    // Debrief
    m.add_function(wrap_pyfunction!(py_parse_debrief_response, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_debrief_system_prompt, m)?)?;

    // Canonical extraction + compaction system prompts (core 2.2.0 hoist).
    m.add_function(wrap_pyfunction!(py_get_extraction_system_prompt, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_compaction_system_prompt, m)?)?;

    // Reranker
    m.add_function(wrap_pyfunction!(py_rerank, m)?)?;
    m.add_function(wrap_pyfunction!(py_rerank_with_config, m)?)?;
    m.add_function(wrap_pyfunction!(py_source_weight, m)?)?;
    m.add_function(wrap_pyfunction!(py_legacy_claim_fallback_weight, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate_memory_claim_v1, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_memory_type_v1, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_memory_source, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_pin_status, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_pinned_claim_json, m)?)?;
    m.add_function(wrap_pyfunction!(py_cosine_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(py_pin_boost, m)?)?;
    m.add_function(wrap_pyfunction!(py_default_pin_config, m)?)?;
    m.add_function(wrap_pyfunction!(py_classify_pin_intent, m)?)?;

    // Wallet derivation
    m.add_function(wrap_pyfunction!(py_derive_eoa, m)?)?;
    m.add_function(wrap_pyfunction!(py_derive_eoa_address, m)?)?;

    // Store pipeline
    m.add_function(wrap_pyfunction!(py_prepare_fact, m)?)?;
    m.add_function(wrap_pyfunction!(py_prepare_fact_with_decay_score, m)?)?;
    m.add_function(wrap_pyfunction!(py_prepare_tombstone, m)?)?;

    // UserOp (ERC-4337) — feature-gated: managed
    #[cfg(feature = "managed")]
    {
        m.add_function(wrap_pyfunction!(py_encode_single_call, m)?)?;
        m.add_function(wrap_pyfunction!(py_encode_batch_call, m)?)?;
        m.add_function(wrap_pyfunction!(py_hash_userop, m)?)?;
        m.add_function(wrap_pyfunction!(py_sign_userop, m)?)?;
        m.add_function(wrap_pyfunction!(py_build_single_calldata_from_prepared, m)?)?;
        m.add_function(wrap_pyfunction!(py_build_batch_calldata_from_prepared, m)?)?;
    }

    // Search pipeline — feature-gated: managed
    #[cfg(feature = "managed")]
    {
        m.add_function(wrap_pyfunction!(py_generate_search_trapdoors, m)?)?;
        m.add_function(wrap_pyfunction!(py_parse_search_response, m)?)?;
        m.add_function(wrap_pyfunction!(py_parse_broadened_response, m)?)?;
        m.add_function(wrap_pyfunction!(py_decrypt_and_rerank, m)?)?;
        m.add_function(wrap_pyfunction!(py_get_search_query, m)?)?;
        m.add_function(wrap_pyfunction!(py_get_broadened_search_query, m)?)?;
        m.add_function(wrap_pyfunction!(py_get_export_query, m)?)?;
        m.add_function(wrap_pyfunction!(py_hex_blob_to_base64, m)?)?;
        m.add_function(wrap_pyfunction!(py_generate_expansion_trapdoors, m)?)?;
        m.add_function(wrap_pyfunction!(py_merge_expansion_results, m)?)?;
    }

    // Knowledge Graph Phase 1
    m.add_function(wrap_pyfunction!(py_normalize_entity_name, m)?)?;
    m.add_function(wrap_pyfunction!(py_deterministic_entity_id, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_claim_or_legacy, m)?)?;
    m.add_function(wrap_pyfunction!(py_canonicalize_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_template_digest, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_digest_prompt, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_digest_response, m)?)?;
    m.add_function(wrap_pyfunction!(py_assemble_digest_from_llm, m)?)?;

    // Knowledge Graph Phase 2: contradiction detection + feedback log
    m.add_function(wrap_pyfunction!(py_default_resolution_weights, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_score_components, m)?)?;
    m.add_function(wrap_pyfunction!(py_resolve_pair, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_contradictions, m)?)?;
    m.add_function(wrap_pyfunction!(py_apply_feedback, m)?)?;
    m.add_function(wrap_pyfunction!(py_default_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_serialize_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_weights_file, m)?)?;
    m.add_function(wrap_pyfunction!(py_append_feedback_to_jsonl, m)?)?;
    m.add_function(wrap_pyfunction!(py_read_feedback_jsonl, m)?)?;
    m.add_function(wrap_pyfunction!(py_rotate_feedback_log, m)?)?;
    m.add_function(wrap_pyfunction!(py_feedback_to_counterexample, m)?)?;

    // Pin status + decision log (Steps B & C)
    m.add_function(wrap_pyfunction!(py_is_pinned_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_respect_pin_in_resolution, m)?)?;
    m.add_function(wrap_pyfunction!(py_find_loser_claim_in_decision_log, m)?)?;
    m.add_function(wrap_pyfunction!(py_find_decision_for_pin, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_feedback_from_decision, m)?)?;
    m.add_function(wrap_pyfunction!(py_append_decision_entry, m)?)?;
    m.add_function(wrap_pyfunction!(py_decision_log_max_lines, m)?)?;
    m.add_function(wrap_pyfunction!(py_contradiction_candidate_cap, m)?)?;
    m.add_function(wrap_pyfunction!(py_tie_zone_score_tolerance, m)?)?;

    // Step D: Contradiction orchestration
    m.add_function(wrap_pyfunction!(py_resolve_with_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(py_build_decision_log_entries, m)?)?;
    m.add_function(wrap_pyfunction!(py_filter_shadow_mode, m)?)?;

    // Consolidation / dedup
    crate::consolidation::register_python_functions(m)?;

    // Smart import profiling
    crate::smart_import::register_python_functions(m)?;

    // Import format parsers (Gemini JSON / saved-info)
    crate::import_parsers::register_python_functions(m)?;

    // Memory Taxonomy v1 constants + guard
    crate::memory_types::register_python_functions(m)?;

    // Read-after-write (confirm_indexed)
    m.add_function(wrap_pyfunction!(py_confirm_indexed_query, m)?)?;
    m.add_function(wrap_pyfunction!(py_confirm_indexed_parse, m)?)?;
    m.add_function(wrap_pyfunction!(py_confirm_indexed_default_poll_ms, m)?)?;
    m.add_function(wrap_pyfunction!(py_confirm_indexed_default_timeout_ms, m)?)?;

    // Recall context formatter
    m.add_function(wrap_pyfunction!(py_format_memory_date, m)?)?;
    m.add_function(wrap_pyfunction!(py_recall_context_header, m)?)?;
    m.add_function(wrap_pyfunction!(py_format_recall_context, m)?)?;

    // Session segmentation (import Crystal grouping) — #368
    m.add_function(wrap_pyfunction!(py_segment_sessions, m)?)?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Tests (direct Rust fn invocation, no Python interpreter needed)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::digest;

    fn sample_claim_json() -> &'static str {
        r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#
    }

    fn two_claims_json() -> &'static str {
        r#"[
            {"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"},
            {"t":"lives in Lisbon","c":"fact","cf":0.95,"i":9,"sa":"oc"}
        ]"#
    }

    #[test]
    fn py_normalize_entity_name_lowercases() {
        assert_eq!(py_normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn py_deterministic_entity_id_known_answer_pedro() {
        assert_eq!(py_deterministic_entity_id("pedro"), "ee5cd7d5d96c8874");
    }

    #[test]
    fn py_parse_claim_or_legacy_full_claim_roundtrips() {
        let out = py_parse_claim_or_legacy(sample_claim_json()).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "prefers PostgreSQL");
        assert_eq!(c.category, claims::ClaimCategory::Preference);
    }

    #[test]
    fn py_parse_claim_or_legacy_legacy_object() {
        let out = py_parse_claim_or_legacy(r#"{"t":"hello","a":"oc"}"#).unwrap();
        let c: claims::Claim = serde_json::from_str(&out).unwrap();
        assert_eq!(c.text, "hello");
        assert_eq!(c.source_agent, "oc");
        assert_eq!(c.category, claims::ClaimCategory::Fact);
    }

    #[test]
    fn py_build_template_digest_empty_vault() {
        let out = py_build_template_digest("[]", 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 0);
        assert!(!d.prompt_text.is_empty());
    }

    #[test]
    fn py_build_template_digest_two_claims() {
        let out = py_build_template_digest(two_claims_json(), 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 2);
    }

    #[test]
    fn py_build_digest_prompt_empty_is_error() {
        let result = py_build_digest_prompt("[]");
        assert!(result.is_err());
    }

    #[test]
    fn py_build_digest_prompt_one_claim_returns_prompt() {
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let prompt = py_build_digest_prompt(one).unwrap();
        assert!(!prompt.is_empty());
        assert!(prompt.contains("JSON"));
    }

    #[test]
    fn py_parse_digest_response_valid_fenced() {
        let raw = "```json\n{\"identity\":\"You are a developer.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[\"skynet\"]}\n```";
        let out = py_parse_digest_response(raw).unwrap();
        let p: digest::ParsedDigestResponse = serde_json::from_str(&out).unwrap();
        assert_eq!(p.identity, "You are a developer.");
        assert_eq!(p.top_claim_indices, vec![1]);
        assert_eq!(p.active_project_names, vec!["skynet".to_string()]);
    }

    #[test]
    fn py_parse_digest_response_invalid_is_error() {
        let result = py_parse_digest_response("not valid json");
        assert!(result.is_err());
    }

    #[test]
    fn py_assemble_digest_from_llm_builds_digest() {
        let parsed = r#"{"identity":"You are a developer.","top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":["skynet"]}"#;
        let one = r#"[{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc"}]"#;
        let out = py_assemble_digest_from_llm(parsed, one, 1_700_000_000).unwrap();
        let d: claims::Digest = serde_json::from_str(&out).unwrap();
        assert_eq!(d.fact_count, 1);
        assert_eq!(d.identity, "You are a developer.");
    }

    #[test]
    fn py_canonicalize_claim_round_trips_canonical_input() {
        let input = sample_claim_json();
        let out = py_canonicalize_claim(input).unwrap();
        assert_eq!(out, input);
    }

    #[test]
    fn py_canonicalize_claim_omits_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"a"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(!out.contains("\"st\""));
    }

    #[test]
    fn py_canonicalize_claim_preserves_non_default_status() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"s"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(out.contains("\"st\":\"s\""));
    }

    #[test]
    fn py_canonicalize_claim_omits_default_corroboration() {
        let input = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","cc":1}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert!(!out.contains("\"cc\""));
    }

    #[test]
    fn py_canonicalize_claim_reorders_fields_to_struct_order() {
        let input = r#"{"sa":"oc","i":5,"cf":0.9,"c":"fact","t":"hi"}"#;
        let out = py_canonicalize_claim(input).unwrap();
        assert_eq!(out, r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#);
    }

    #[test]
    fn py_canonicalize_claim_rejects_malformed_json() {
        let result = py_canonicalize_claim("{not valid");
        assert!(result.is_err());
    }

    #[test]
    fn py_canonicalize_claim_rejects_missing_required_field() {
        let result = py_canonicalize_claim(r#"{"t":"hi","cf":0.9,"i":5,"sa":"oc"}"#);
        assert!(result.is_err());
    }

    #[test]
    fn py_canonicalize_claim_rejects_legacy_format() {
        let result = py_canonicalize_claim(r#"{"t":"hi","a":"oc"}"#);
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
    fn py_default_resolution_weights_matches_p2_3_defaults() {
        let out = kg_default_resolution_weights_inner().unwrap();
        let w: contradiction::ResolutionWeights = serde_json::from_str(&out).unwrap();
        assert_eq!(w.confidence, 0.25);
        assert_eq!(w.corroboration, 0.15);
        assert_eq!(w.recency, 0.40);
        assert_eq!(w.validation, 0.20);
    }

    #[test]
    fn py_compute_score_components_known_answer() {
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
    fn py_compute_score_components_rejects_malformed_claim() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let result = kg_compute_score_components_inner("{not json", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn py_compute_score_components_rejects_malformed_weights() {
        let claim = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_compute_score_components_inner(claim, PHASE2_NOW, "{not json");
        assert!(result.is_err());
    }

    #[test]
    fn py_resolve_pair_vim_vs_vscode_defaults_vscode_wins() {
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
    fn py_resolve_pair_rejects_malformed_claim_a() {
        let weights = kg_default_resolution_weights_inner().unwrap();
        let good = r#"{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let result = kg_resolve_pair_inner("{bad", "a", good, "b", PHASE2_NOW, &weights);
        assert!(result.is_err());
    }

    #[test]
    fn py_detect_contradictions_empty_existing_returns_empty_array() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0, 0.0, 0.0]).unwrap();
        let out =
            kg_detect_contradictions_inner(new_claim, "new_id", &emb, "[]", 0.3, 0.85).unwrap();
        assert_eq!(out, "[]");
    }

    #[test]
    fn py_detect_contradictions_single_in_band_returns_one() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let existing_claim_obj = r#"{"t":"uses Emacs","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
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
        assert_eq!(
            parsed[0].entity_id,
            claims::deterministic_entity_id("editor")
        );
    }

    #[test]
    fn py_detect_contradictions_rejects_malformed_existing_shape() {
        let new_claim = r#"{"t":"uses Vim","c":"fact","cf":0.8,"i":5,"sa":"oc","e":[{"n":"editor","tp":"tool"}]}"#;
        let emb = serde_json::to_string(&vec![1.0f32, 0.0]).unwrap();
        let existing = r#"[{"claim":{"t":"x","c":"fact","cf":0.9,"i":5,"sa":"oc"}}]"#;
        let result = kg_detect_contradictions_inner(new_claim, "new_id", &emb, existing, 0.3, 0.85);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("existing"), "err: {}", err);
    }

    #[test]
    fn py_apply_feedback_returns_clamped_weights() {
        let weights = kg_default_resolution_weights_inner().unwrap();
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

    fn phase2_sample_entry_json() -> String {
        r#"{"ts":1776384000,"claim_a_id":"0xaaa","claim_b_id":"0xbbb","formula_winner":"a","user_decision":"pin_b","winner_components":{"confidence":0.8,"corroboration":1.732,"recency":0.333,"validation":0.7,"weighted_total":0.7331},"loser_components":{"confidence":0.6,"corroboration":1.0,"recency":0.125,"validation":0.5,"weighted_total":0.4025}}"#.to_string()
    }

    #[test]
    fn py_default_weights_file_round_trips() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        let back = kg_parse_weights_file_inner(&pretty).unwrap();
        assert_eq!(back, out);
    }

    #[test]
    fn py_serialize_weights_file_is_pretty() {
        let out = kg_default_weights_file_inner(1_776_384_000).unwrap();
        let pretty = kg_serialize_weights_file_inner(&out).unwrap();
        assert!(pretty.contains('\n'), "pretty JSON must contain newlines");
        assert!(pretty.contains("  "), "pretty JSON must use 2-space indent");
    }

    #[test]
    fn py_parse_weights_file_rejects_malformed() {
        let result = kg_parse_weights_file_inner("not-json");
        assert!(result.is_err());
    }

    #[test]
    fn py_parse_weights_file_rejects_unknown_version() {
        let bad = r#"{"version":99,"updated_at":0,"weights":{"confidence":0.25,"corroboration":0.15,"recency":0.4,"validation":0.2},"threshold_lower":0.3,"threshold_upper":0.85,"feedback_count":0}"#;
        let result = kg_parse_weights_file_inner(bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("unsupported"));
    }

    #[test]
    fn py_append_feedback_to_jsonl_empty_produces_one_line() {
        let out = kg_append_feedback_to_jsonl_inner("", &phase2_sample_entry_json()).unwrap();
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn py_append_feedback_to_jsonl_existing_produces_two_lines() {
        let entry = phase2_sample_entry_json();
        let first = kg_append_feedback_to_jsonl_inner("", &entry).unwrap();
        let second = kg_append_feedback_to_jsonl_inner(&first, &entry).unwrap();
        assert_eq!(second.matches('\n').count(), 2);
    }

    #[test]
    fn py_read_feedback_jsonl_round_trip_many_entries() {
        let mut content = String::new();
        for _ in 0..3 {
            content =
                kg_append_feedback_to_jsonl_inner(&content, &phase2_sample_entry_json()).unwrap();
        }
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 3);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn py_read_feedback_jsonl_surfaces_warnings_for_bad_lines() {
        let content = "not-json\n".to_string() + &phase2_sample_entry_json() + "\n";
        let out = kg_read_feedback_jsonl_inner(&content).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(parsed["entries"].as_array().unwrap().len(), 1);
        assert_eq!(parsed["warnings"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn py_rotate_feedback_log_drops_oldest_when_over_cap() {
        let mut content = String::new();
        for i in 0..5 {
            let entry =
                phase2_sample_entry_json().replace("1776384000", &format!("177638400{}", i));
            content = kg_append_feedback_to_jsonl_inner(&content, &entry).unwrap();
        }
        let rotated = kg_rotate_feedback_log_inner(&content, 3);
        let out = kg_read_feedback_jsonl_inner(&rotated).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&out).unwrap();
        let entries = parsed["entries"].as_array().unwrap();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0]["ts"].as_i64().unwrap(), 1_776_384_002);
        assert_eq!(entries[2]["ts"].as_i64().unwrap(), 1_776_384_004);
    }

    #[test]
    fn py_rotate_feedback_log_preserves_content_below_cap() {
        let content = kg_append_feedback_to_jsonl_inner("", &phase2_sample_entry_json()).unwrap();
        let rotated = kg_rotate_feedback_log_inner(&content, 10);
        assert_eq!(rotated, content);
    }

    #[test]
    fn py_feedback_to_counterexample_pin_b_when_formula_winner_a_returns_ce() {
        let out = kg_feedback_to_counterexample_inner(&phase2_sample_entry_json()).unwrap();
        assert_ne!(out, "null");
        let ce: contradiction::Counterexample = serde_json::from_str(&out).unwrap();
        assert_eq!(ce.user_pinned, contradiction::UserPinned::Loser);
    }

    #[test]
    fn py_feedback_to_counterexample_pin_a_when_formula_winner_a_returns_null() {
        let entry = phase2_sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"pin_a\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn py_feedback_to_counterexample_unpin_returns_null() {
        let entry = phase2_sample_entry_json()
            .replace("\"user_decision\":\"pin_b\"", "\"user_decision\":\"unpin\"");
        let out = kg_feedback_to_counterexample_inner(&entry).unwrap();
        assert_eq!(out, "null");
    }

    #[test]
    fn py_feedback_to_counterexample_rejects_malformed_entry() {
        let result = kg_feedback_to_counterexample_inner("{not-an-entry");
        assert!(result.is_err());
    }

    // === Retrieval v2 Tier 1 bindings ===

    #[test]
    fn py_source_weight_known_values() {
        // v2-lenient (core 2.4.0+) — per docs/specs/totalreclaw/retrieval-v2.md §Tier 1.
        // Direct call — no GIL needed because py_source_weight returns f64.
        assert_eq!(py_source_weight("user"), 1.00);
        assert_eq!(py_source_weight("user-inferred"), 0.95);
        assert_eq!(py_source_weight("derived"), 0.85);
        assert_eq!(py_source_weight("external"), 0.85);
        assert_eq!(py_source_weight("assistant"), 0.85);
    }

    #[test]
    fn py_source_weight_unknown_returns_fallback() {
        // Policy: unknown source string -> user-inferred (v2-lenient 0.95),
        // not 0.85. The 0.85 fallback is ONLY for missing-source candidates in
        // the reranker. The binding here maps string -> MemorySource -> weight,
        // and from_str_lossy routes unknowns to UserInferred.
        assert_eq!(py_source_weight("bot"), 0.95);
        assert_eq!(py_source_weight(""), 0.95);
    }

    #[test]
    fn py_legacy_claim_fallback_weight_value() {
        assert_eq!(py_legacy_claim_fallback_weight(), 0.85);
    }

    #[test]
    fn py_parse_memory_type_v1_returns_string_values() {
        assert_eq!(py_parse_memory_type_v1("CLAIM"), "claim");
        assert_eq!(py_parse_memory_type_v1("directive"), "directive");
        // unknown -> claim
        assert_eq!(py_parse_memory_type_v1("fact"), "claim");
    }

    #[test]
    fn py_parse_memory_source_returns_string_values() {
        assert_eq!(py_parse_memory_source("user"), "user");
        assert_eq!(py_parse_memory_source("user-inferred"), "user-inferred");
        assert_eq!(py_parse_memory_source("USER_INFERRED"), "user-inferred");
        // unknown -> user-inferred
        assert_eq!(py_parse_memory_source("bot"), "user-inferred");
    }

    // Validate + rerank_with_config return PyResult<String>; constructing PyErr
    // requires the GIL so we stick to is_err() / is_ok() checks in unit tests.
    // Full integration tests live in tests/python_parity_test.py.

    #[test]
    fn py_validate_memory_claim_v1_accepts_valid_claim() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"prefers PostgreSQL","type":"preference","source":"user","created_at":"2026-04-17T10:00:00Z"}"#;
        let out = py_validate_memory_claim_v1(json).unwrap();
        assert!(out.contains("\"text\":\"prefers PostgreSQL\""));
        assert!(out.contains("\"type\":\"preference\""));
    }

    #[test]
    fn py_validate_memory_claim_v1_rejects_unknown_schema_version() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"hi","type":"claim","source":"user","created_at":"2026-04-17T10:00:00Z","schema_version":"2.0"}"#;
        let result = py_validate_memory_claim_v1(json);
        assert!(result.is_err(), "unknown schema_version must be rejected");
    }

    #[test]
    fn py_validate_memory_claim_v1_rejects_legacy_type_token() {
        let json = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"hi","type":"fact","source":"user","created_at":"2026-04-17T10:00:00Z"}"#;
        let result = py_validate_memory_claim_v1(json);
        assert!(result.is_err(), "legacy token 'fact' must be rejected");
    }

    #[test]
    fn py_rerank_with_config_flag_on_prefers_user() {
        let candidates = r#"[
            {"id":"a","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"assistant"},
            {"id":"u","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"user"}
        ]"#;
        let out = py_rerank_with_config(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates,
            10,
            true,
        )
        .unwrap();
        // User must come first — the JSON array is ordered by score desc.
        let first_u = out.find("\"id\":\"u\"").unwrap_or(usize::MAX);
        let first_a = out.find("\"id\":\"a\"").unwrap_or(usize::MAX);
        assert!(
            first_u < first_a,
            "user must rank before assistant: {}",
            out
        );
    }

    #[test]
    fn py_rerank_with_config_flag_off_ignores_source() {
        // With flag OFF the same input must behave like the v0 rerank.
        let candidates_json = r#"[
            {"id":"a","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"assistant"},
            {"id":"u","text":"dark mode preference","embedding":[0.9,0.1,0.0,0.0],"timestamp":"","source":"user"}
        ]"#;
        let off = py_rerank_with_config(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates_json,
            10,
            false,
        )
        .unwrap();
        let v0 = py_rerank(
            "dark mode",
            vec![0.9f32, 0.1, 0.0, 0.0],
            candidates_json,
            10,
        )
        .unwrap();
        assert_eq!(off, v0, "flag OFF must equal v0 rerank output");
    }
}
