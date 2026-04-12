//! Generates the KG Phase 1 cross-language parity fixtures.
//!
//! Run:
//!   cd rust/totalreclaw-core
//!   cargo run --example generate_kg_fixtures --release
//!
//! Writes `tests/parity/kg_phase1_vectors.json` (path relative to repo root).
//!
//! The Rust outputs in this file are the canonical reference. The TS (WASM)
//! and Python (PyO3) parity runners read this file and assert their own
//! outputs match byte-for-byte.

use std::fs;
use std::path::PathBuf;

use serde_json::{json, Value};
use totalreclaw_core::claims::{
    deterministic_entity_id, normalize_entity_name, parse_claim_or_legacy, Claim, ClaimCategory,
    ClaimStatus, EntityRef, EntityType,
};
use totalreclaw_core::digest::{
    assemble_digest_from_llm, build_digest_prompt, build_template_digest, parse_digest_response,
};

// Fixed "now" for deterministic age calculations: 2026-04-12T12:00:00Z.
const NOW_UNIX: i64 = 1_776_340_800;

fn canonicalize_claim(input: &str) -> String {
    // Strict-parse as Claim, re-serialize. Mirrors `kg_canonicalize_claim_inner`.
    let claim: Claim = serde_json::from_str(input).expect("invalid claim JSON in fixture input");
    serde_json::to_string(&claim).expect("re-serialize claim")
}

fn ser(claim: &Claim) -> String {
    serde_json::to_string(claim).expect("serialize claim")
}

fn make_claim(
    text: &str,
    category: ClaimCategory,
    importance: u8,
    extracted_at: Option<&str>,
    entities: Vec<EntityRef>,
) -> Claim {
    Claim {
        text: text.to_string(),
        category,
        confidence: 0.9,
        importance,
        corroboration_count: 1,
        source_agent: "oc".to_string(),
        source_conversation: None,
        extracted_at: extracted_at.map(|s| s.to_string()),
        entities,
        supersedes: None,
        superseded_by: None,
        valid_from: None,
        status: ClaimStatus::Active,
    }
}

fn entity(name: &str, entity_type: EntityType) -> EntityRef {
    EntityRef {
        name: name.to_string(),
        entity_type,
        role: None,
    }
}

fn main() {
    let mut out = json!({
        "meta": {
            "version": 1,
            "now_unix_seconds": NOW_UNIX,
            "description": "KG Phase 1 cross-language parity vectors. Rust outputs are canonical."
        },
        "entity_normalization": [],
        "claim_canonicalization": [],
        "legacy_parser": [],
        "template_digest": [],
        "digest_prompt": [],
        "parse_digest_response": [],
        "assemble_digest_from_llm": []
    });

    // ----------------------------------------------------------------
    // Category 1: Entity normalization + deterministic IDs
    // ----------------------------------------------------------------
    let entity_inputs = [
        "Pedro",
        "  PEDRO  ",
        "pedro",
        "Node.js",
        "Node JS",
        "PostgreSQL",
        "postgreSQL",
        "José",         // precomposed é (U+00E9)
        "Jose\u{0301}", // NFD: e + combining acute
        "skynet-lite",
        "New York",
        "",
        "   \t\n   ",
        "PostgréSQL",
        "Postgre\u{0301}SQL", // NFD form of the same name
        "Emoji 🚀 project",
        "Foo\t\nBar",
    ];
    for input in entity_inputs.iter() {
        let normalized = normalize_entity_name(input);
        let id = deterministic_entity_id(input);
        out["entity_normalization"]
            .as_array_mut()
            .unwrap()
            .push(json!({
                "input": input,
                "normalized": normalized,
                "entity_id": id,
            }));
    }

    // ----------------------------------------------------------------
    // Category 2: Claim canonicalization
    // ----------------------------------------------------------------
    // Each input is a JSON string accepted by strict Claim parsing.
    // The expected output is the canonical (struct-order) re-serialization.
    let canonicalization_inputs: Vec<&str> = vec![
        // Minimal claim — defaults omitted on output
        r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#,
        // Reordered keys — output must reorder to struct order
        r#"{"sa":"oc","i":5,"cf":0.9,"c":"fact","t":"hi"}"#,
        // Explicit default status — must be omitted on output
        r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","st":"a"}"#,
        // Explicit default corroboration — must be omitted on output
        r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc","cc":1}"#,
        // Non-default status preserved
        r#"{"t":"old fact","c":"fact","cf":0.8,"i":4,"sa":"oc","st":"s"}"#,
        // Non-default corroboration preserved
        r#"{"t":"x","c":"pref","cf":0.9,"i":7,"sa":"oc","cc":3}"#,
        // With entities (short-key form), reordered, role omitted
        r#"{"e":[{"tp":"tool","n":"PostgreSQL"}],"sa":"oc","t":"prefers PostgreSQL","i":8,"cf":0.9,"c":"pref"}"#,
        // Full claim — sup, vf, sc, ea, role on entity
        r#"{"t":"Pedro chose PostgreSQL","c":"dec","cf":0.92,"i":9,"cc":3,"sa":"openclaw-plugin","sc":"conv-abc","ea":"2026-04-12T10:00:00Z","e":[{"n":"Pedro","tp":"person","r":"chooser"},{"n":"PostgreSQL","tp":"tool","r":"chosen"}],"sup":"0xabc","vf":"2026-04-01T00:00:00Z","st":"s"}"#,
    ];
    for input in canonicalization_inputs {
        let canonical = canonicalize_claim(input);
        out["claim_canonicalization"]
            .as_array_mut()
            .unwrap()
            .push(json!({
                "input": input,
                "canonical": canonical,
            }));
    }

    // ----------------------------------------------------------------
    // Category 3: Legacy parser
    // ----------------------------------------------------------------
    // Inputs cover: full canonical, plugin {text, metadata}, legacy {t,a,s},
    // bare string JSON, raw text, malformed JSON, empty string, missing-text object.
    let legacy_inputs: Vec<&str> = vec![
        // Full canonical claim — round-trip
        r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#,
        // Plugin legacy doc format
        r#"{"text":"prefers PostgreSQL","metadata":{"type":"preference","importance":0.9,"source":"auto-extraction","created_at":"2026-03-01T00:00:00Z"}}"#,
        // Plugin doc without metadata source
        r#"{"text":"lives in Lisbon"}"#,
        // Legacy {t,a,s} object
        r#"{"t":"hello","a":"oc","s":"extract"}"#,
        // Bare-string JSON
        r#""just text""#,
        // Raw plain text (not JSON at all)
        r#"hello world"#,
        // Malformed JSON
        r#"{not valid json"#,
        // Object with no text-like field — text falls back to raw blob
        r#"{"a":"oc"}"#,
    ];
    for input in legacy_inputs {
        let claim = parse_claim_or_legacy(input);
        let output = ser(&claim);
        out["legacy_parser"].as_array_mut().unwrap().push(json!({
            "input": input,
            "output": output,
        }));
    }

    // ----------------------------------------------------------------
    // Category 4: Template digest
    // ----------------------------------------------------------------
    // We feed JSON arrays of claims, with FIXED extracted_at strings (not relative
    // to NOW), so the output is fully deterministic across languages.

    // Helper: a claim 1 day before NOW = 2026-04-11T12:00:00Z.
    let day_ago = "2026-04-11T12:00:00Z".to_string();
    let day10_ago = "2026-04-02T12:00:00Z".to_string();
    let day30_ago = "2026-03-13T12:00:00Z".to_string();

    // 4a: Empty array
    let empty: Vec<Claim> = Vec::new();
    let empty_input = serde_json::to_string(&empty).unwrap();
    let empty_output = serde_json::to_string(&build_template_digest(&empty, NOW_UNIX)).unwrap();
    out["template_digest"].as_array_mut().unwrap().push(json!({
        "input_claims": empty_input,
        "now_unix_seconds": NOW_UNIX,
        "digest": empty_output,
    }));

    // 4b: Single claim
    let single = vec![make_claim(
        "prefers PostgreSQL",
        ClaimCategory::Preference,
        8,
        Some(&day_ago),
        vec![entity("PostgreSQL", EntityType::Tool)],
    )];
    let single_input = serde_json::to_string(&single).unwrap();
    let single_output = serde_json::to_string(&build_template_digest(&single, NOW_UNIX)).unwrap();
    out["template_digest"].as_array_mut().unwrap().push(json!({
        "input_claims": single_input,
        "now_unix_seconds": NOW_UNIX,
        "digest": single_output,
    }));

    // 4c: Three claims with mixed categories + a project entity
    let mixed = vec![
        make_claim(
            "lives in Lisbon",
            ClaimCategory::Fact,
            7,
            Some(&day_ago),
            vec![],
        ),
        make_claim(
            "prefers PostgreSQL",
            ClaimCategory::Preference,
            8,
            Some(&day10_ago),
            vec![entity("PostgreSQL", EntityType::Tool)],
        ),
        make_claim(
            "chose Rust over Go for skynet-lite",
            ClaimCategory::Decision,
            9,
            Some(&day30_ago),
            vec![entity("skynet-lite", EntityType::Project)],
        ),
    ];
    let mixed_input = serde_json::to_string(&mixed).unwrap();
    let mixed_output = serde_json::to_string(&build_template_digest(&mixed, NOW_UNIX)).unwrap();
    out["template_digest"].as_array_mut().unwrap().push(json!({
        "input_claims": mixed_input,
        "now_unix_seconds": NOW_UNIX,
        "digest": mixed_output,
    }));

    // 4d: 12 claims — top_claims must cap at 10
    let mut twelve: Vec<Claim> = Vec::new();
    for i in 0..12 {
        twelve.push(make_claim(
            &format!("fact number {}", i),
            ClaimCategory::Fact,
            8,
            Some(&day_ago),
            vec![],
        ));
    }
    let twelve_input = serde_json::to_string(&twelve).unwrap();
    let twelve_output = serde_json::to_string(&build_template_digest(&twelve, NOW_UNIX)).unwrap();
    out["template_digest"].as_array_mut().unwrap().push(json!({
        "input_claims": twelve_input,
        "now_unix_seconds": NOW_UNIX,
        "digest": twelve_output,
    }));

    // ----------------------------------------------------------------
    // Category 5: build_digest_prompt
    // ----------------------------------------------------------------
    // NOTE: build_digest_prompt internally calls Utc::now() to format the
    // per-claim age strings. To keep outputs deterministic across languages
    // and runs, we ONLY use claims with extracted_at = None, so the age
    // resolves to "unknown" regardless of wall-clock.
    fn no_ts_claim(text: &str, cat: ClaimCategory, conf: f64) -> Claim {
        Claim {
            text: text.to_string(),
            category: cat,
            confidence: conf,
            importance: 5,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        }
    }

    // 5a: single claim
    let prompt_single = vec![no_ts_claim("prefers PostgreSQL", ClaimCategory::Preference, 0.9)];
    let prompt_single_input = serde_json::to_string(&prompt_single).unwrap();
    let prompt_single_output = build_digest_prompt(&prompt_single);
    out["digest_prompt"].as_array_mut().unwrap().push(json!({
        "input_claims": prompt_single_input,
        "prompt": prompt_single_output,
    }));

    // 5b: three claims
    let prompt_three = vec![
        no_ts_claim("lives in Lisbon", ClaimCategory::Fact, 0.85),
        no_ts_claim("prefers PostgreSQL", ClaimCategory::Preference, 0.9),
        no_ts_claim(
            "chose Rust over Go for skynet-lite",
            ClaimCategory::Decision,
            0.92,
        ),
    ];
    let prompt_three_input = serde_json::to_string(&prompt_three).unwrap();
    let prompt_three_output = build_digest_prompt(&prompt_three);
    out["digest_prompt"].as_array_mut().unwrap().push(json!({
        "input_claims": prompt_three_input,
        "prompt": prompt_three_output,
    }));

    // ----------------------------------------------------------------
    // Category 6: parse_digest_response
    // ----------------------------------------------------------------
    let parse_inputs: Vec<&str> = vec![
        // Valid unfenced JSON
        r#"{"identity":"You are Pedro, a software engineer in Lisbon.","top_claim_indices":[1,2,3],"recent_decision_indices":[2],"active_project_names":["skynet-lite"]}"#,
        // Valid fenced JSON
        "```json\n{\"identity\":\"You are a Rust developer.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[]}\n```",
        // Minimal: only identity (other fields default to empty arrays)
        r#"{"identity":"You are someone."}"#,
    ];
    for input in parse_inputs {
        let parsed = parse_digest_response(input).expect("fixture parse_digest_response should succeed");
        let output = serde_json::to_string(&parsed).unwrap();
        out["parse_digest_response"]
            .as_array_mut()
            .unwrap()
            .push(json!({
                "input": input,
                "output": output,
            }));
    }

    // ----------------------------------------------------------------
    // Category 7 (bonus): assemble_digest_from_llm
    // ----------------------------------------------------------------
    // Use no-extracted_at claims for determinism (age=unknown).
    let assemble_claims = vec![
        no_ts_claim("lives in Lisbon", ClaimCategory::Fact, 0.85),
        no_ts_claim("prefers PostgreSQL", ClaimCategory::Preference, 0.9),
        no_ts_claim(
            "chose Rust over Go for skynet-lite",
            ClaimCategory::Decision,
            0.92,
        ),
    ];
    let assemble_claims_json = serde_json::to_string(&assemble_claims).unwrap();
    let parsed_response = r#"{"identity":"You are a Rust dev in Lisbon.","top_claim_indices":[1,2,3],"recent_decision_indices":[3],"active_project_names":["skynet-lite"]}"#;
    let parsed: totalreclaw_core::digest::ParsedDigestResponse =
        serde_json::from_str(parsed_response).unwrap();
    let assembled =
        assemble_digest_from_llm(&parsed, &assemble_claims, NOW_UNIX).expect("assemble ok");
    let assembled_json = serde_json::to_string(&assembled).unwrap();
    out["assemble_digest_from_llm"]
        .as_array_mut()
        .unwrap()
        .push(json!({
            "parsed_response": parsed_response,
            "input_claims": assemble_claims_json,
            "now_unix_seconds": NOW_UNIX,
            "digest": assembled_json,
        }));

    // ----------------------------------------------------------------
    // Write fixture file
    // ----------------------------------------------------------------
    // CARGO_MANIFEST_DIR points at rust/totalreclaw-core/. Repo root is two up.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let path = PathBuf::from(manifest_dir)
        .join("..")
        .join("..")
        .join("tests")
        .join("parity")
        .join("kg_phase1_vectors.json");

    fs::write(&path, serde_json::to_string_pretty(&out).unwrap() + "\n")
        .expect("write fixture file");

    fn count(v: &Value, key: &str) -> usize {
        v[key].as_array().map(|a| a.len()).unwrap_or(0)
    }
    let total = count(&out, "entity_normalization")
        + count(&out, "claim_canonicalization")
        + count(&out, "legacy_parser")
        + count(&out, "template_digest")
        + count(&out, "digest_prompt")
        + count(&out, "parse_digest_response")
        + count(&out, "assemble_digest_from_llm");

    println!("Wrote KG Phase 1 parity fixtures to: {}", path.display());
    println!("  entity_normalization:     {}", count(&out, "entity_normalization"));
    println!("  claim_canonicalization:   {}", count(&out, "claim_canonicalization"));
    println!("  legacy_parser:            {}", count(&out, "legacy_parser"));
    println!("  template_digest:          {}", count(&out, "template_digest"));
    println!("  digest_prompt:            {}", count(&out, "digest_prompt"));
    println!("  parse_digest_response:    {}", count(&out, "parse_digest_response"));
    println!("  assemble_digest_from_llm: {}", count(&out, "assemble_digest_from_llm"));
    println!("  TOTAL:                    {}", total);
}
