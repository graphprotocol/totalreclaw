//! DEP-4 corpus runner (Rust side). Loads `tests/parity/pii/corpus.json`
//! from the repo root and asserts every item's exact decision, then prints
//! the PRD-04 G4 metrics (negative-set leaks, positive-set false blocks).

use std::path::PathBuf;

use totalreclaw_core::pii::{pii_filter, PiiCategory, PiiDecision};

fn corpus_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/parity/pii/corpus.json")
}

fn load_items() -> Vec<serde_json::Value> {
    let path = corpus_path();
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    let corpus: serde_json::Value = serde_json::from_str(&raw).expect("corpus.json is valid JSON");
    corpus["items"].as_array().expect("items array").clone()
}

fn describe(d: PiiDecision) -> (String, Option<String>) {
    (
        d.kind().to_string(),
        d.category().map(|c| c.as_str().to_string()),
    )
}

#[test]
fn corpus_has_at_least_150_items_and_covers_every_category() {
    let items = load_items();
    assert!(
        items.len() >= 150,
        "corpus has {} items, need >= 150",
        items.len()
    );
    for c in PiiCategory::ALL {
        assert!(
            items
                .iter()
                .any(|i| i["category"] == c.as_str() && i["expect"] == "block"),
            "no negative block item for category {}",
            c.as_str()
        );
    }
}

#[test]
fn corpus_every_item_matches_expected_decision() {
    let items = load_items();
    let mut failures = Vec::new();
    let (mut neg_total, mut neg_leaks, mut pos_total, mut pos_false_blocks) = (0, 0, 0, 0);
    for item in &items {
        let id = item["id"].as_str().expect("id");
        let text = item["text"].as_str().expect("text");
        let expect = item["expect"].as_str().expect("expect");
        let want_category = item["category"].as_str().map(str::to_string);
        let (kind, category) = describe(pii_filter(text));
        match item["set"].as_str().expect("set") {
            "negative" => {
                neg_total += 1;
                if kind == "allow" {
                    neg_leaks += 1;
                }
            }
            _ => {
                pos_total += 1;
                if kind != "allow" {
                    pos_false_blocks += 1;
                }
            }
        }
        if kind != expect || category != want_category {
            // Report id + decisions only — never the corpus text.
            failures.push(format!(
                "{id}: expected {expect}/{want_category:?}, got {kind}/{category:?}"
            ));
        }
    }
    println!(
        "G4: negative leaks {neg_leaks}/{neg_total}; positive false blocks {pos_false_blocks}/{pos_total} ({:.1}%)",
        100.0 * pos_false_blocks as f64 / pos_total.max(1) as f64
    );
    assert!(
        failures.is_empty(),
        "{} corpus mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_eq!(neg_leaks, 0, "G4: negative-set leaks must be 0");
    assert!(
        pos_false_blocks * 50 <= pos_total,
        "G4: positive false blocks must be <= 2%"
    );
}
