//! Feedback log + per-user weights persistence primitives (Phase 2 Slice 2b).

use crate::contradiction::{
    default_weights, Counterexample, ResolutionWeights, ScoreComponents, UserPinned,
    DEFAULT_LOWER_THRESHOLD, DEFAULT_UPPER_THRESHOLD,
};
use serde::{Deserialize, Serialize};

/// Current version of the weights file format.
pub const WEIGHTS_FILE_VERSION: u32 = 1;

/// Which claim the formula picked as winner prior to the user's override.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FormulaWinner {
    A,
    B,
}

/// What the user did in response to the auto-resolution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UserDecision {
    PinA,
    PinB,
    PinBoth,
    Unpin,
}

/// A single feedback event — user overrode an auto-resolution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FeedbackEntry {
    pub ts: i64,
    pub claim_a_id: String,
    pub claim_b_id: String,
    pub formula_winner: FormulaWinner,
    pub user_decision: UserDecision,
    pub winner_components: ScoreComponents,
    pub loser_components: ScoreComponents,
}

/// Persisted per-user weights file (target: ~/.totalreclaw/weights.json).
///
/// `last_tuning_ts` tracks the newest feedback-entry timestamp that the
/// tuning loop has already consumed — re-running the loop with the same data
/// is idempotent. Added in Slice 2f; `#[serde(default)]` keeps pre-2f files
/// parseable without a schema version bump.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WeightsFile {
    pub version: u32,
    pub updated_at: i64,
    pub weights: ResolutionWeights,
    pub threshold_lower: f64,
    pub threshold_upper: f64,
    pub feedback_count: u32,
    #[serde(default)]
    pub last_tuning_ts: i64,
}

/// Build a fresh weights file with default weights + thresholds from P2-2.
pub fn default_weights_file(now_unix: i64) -> WeightsFile {
    WeightsFile {
        version: WEIGHTS_FILE_VERSION,
        updated_at: now_unix,
        weights: default_weights(),
        threshold_lower: DEFAULT_LOWER_THRESHOLD,
        threshold_upper: DEFAULT_UPPER_THRESHOLD,
        feedback_count: 0,
        last_tuning_ts: 0,
    }
}

/// Serialize a weights file to pretty-printed JSON (2-space indent).
pub fn serialize_weights_file(file: &WeightsFile) -> String {
    serde_json::to_string_pretty(file)
        .unwrap_or_else(|e| panic!("weights file serialization must not fail: {}", e))
}

/// Parse a weights file from JSON; rejects unknown versions and malformed input.
pub fn parse_weights_file(json: &str) -> Result<WeightsFile, String> {
    let file: WeightsFile = serde_json::from_str(json)
        .map_err(|e| format!("failed to parse weights file: {}", e))?;
    if file.version != WEIGHTS_FILE_VERSION {
        return Err(format!(
            "unsupported weights file version: {} (expected {})",
            file.version, WEIGHTS_FILE_VERSION
        ));
    }
    Ok(file)
}

/// Append one feedback entry to existing JSONL content, producing new content.
pub fn append_to_jsonl(existing: &str, entry: &FeedbackEntry) -> String {
    let line = serde_json::to_string(entry)
        .unwrap_or_else(|e| panic!("feedback entry serialization must not fail: {}", e));
    let mut out = String::with_capacity(existing.len() + line.len() + 1);
    if existing.is_empty() {
        out.push_str(&line);
        out.push('\n');
    } else if existing.ends_with('\n') {
        out.push_str(existing);
        out.push_str(&line);
        out.push('\n');
    } else {
        out.push_str(existing);
        out.push('\n');
        out.push_str(&line);
        out.push('\n');
    }
    out
}

/// Parse JSONL content into entries; skips blank lines, collects malformed-line warnings.
pub fn read_jsonl(content: &str) -> (Vec<FeedbackEntry>, Vec<String>) {
    let mut entries = Vec::new();
    let mut warnings = Vec::new();
    for (idx, raw) in content.split('\n').enumerate() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<FeedbackEntry>(line) {
            Ok(e) => entries.push(e),
            Err(e) => warnings.push(format!("line {}: {}", idx + 1, e)),
        }
    }
    (entries, warnings)
}

/// Keep only the most recent `max_lines` non-empty lines (drops oldest).
pub fn rotate_if_needed(content: &str, max_lines: usize) -> String {
    if max_lines == 0 {
        return String::new();
    }
    let trailing_newline = content.ends_with('\n');
    let lines: Vec<&str> = content.split('\n').collect();
    let non_empty: Vec<&str> = if trailing_newline && !lines.is_empty() {
        lines[..lines.len() - 1].to_vec()
    } else {
        lines
    };
    let non_empty: Vec<&str> = non_empty.into_iter().filter(|l| !l.is_empty()).collect();
    if non_empty.len() <= max_lines {
        return content.to_string();
    }
    let start = non_empty.len() - max_lines;
    let kept = &non_empty[start..];
    let mut out = kept.join("\n");
    if trailing_newline || !out.is_empty() {
        out.push('\n');
    }
    out
}

/// Convert a feedback entry into a counterexample for weight tuning.
pub fn feedback_to_counterexample(entry: &FeedbackEntry) -> Option<Counterexample> {
    match (&entry.formula_winner, &entry.user_decision) {
        (FormulaWinner::A, UserDecision::PinA) => None,
        (FormulaWinner::B, UserDecision::PinB) => None,
        (FormulaWinner::A, UserDecision::PinB) => Some(Counterexample {
            formula_winner: entry.winner_components.clone(),
            formula_loser: entry.loser_components.clone(),
            user_pinned: UserPinned::Loser,
        }),
        (FormulaWinner::B, UserDecision::PinA) => Some(Counterexample {
            formula_winner: entry.winner_components.clone(),
            formula_loser: entry.loser_components.clone(),
            user_pinned: UserPinned::Loser,
        }),
        (_, UserDecision::PinBoth) => Some(Counterexample {
            formula_winner: entry.winner_components.clone(),
            formula_loser: entry.loser_components.clone(),
            user_pinned: UserPinned::Both,
        }),
        (_, UserDecision::Unpin) => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contradiction::default_weights;

    fn sample_winner_components() -> ScoreComponents {
        ScoreComponents {
            confidence: 0.8,
            corroboration: 1.732,
            recency: 0.333,
            validation: 0.7,
            weighted_total: 0.7331,
        }
    }

    fn sample_loser_components() -> ScoreComponents {
        ScoreComponents {
            confidence: 0.6,
            corroboration: 1.0,
            recency: 0.125,
            validation: 0.5,
            weighted_total: 0.4025,
        }
    }

    fn sample_entry() -> FeedbackEntry {
        FeedbackEntry {
            ts: 1_776_384_000,
            claim_a_id: "0xaaa".to_string(),
            claim_b_id: "0xbbb".to_string(),
            formula_winner: FormulaWinner::A,
            user_decision: UserDecision::PinB,
            winner_components: sample_winner_components(),
            loser_components: sample_loser_components(),
        }
    }

    // --------------- WeightsFile ---------------

    #[test]
    fn test_default_weights_file_fields() {
        let f = default_weights_file(1_776_384_000);
        assert_eq!(f.version, 1);
        assert_eq!(f.updated_at, 1_776_384_000);
        assert_eq!(f.feedback_count, 0);
        assert_eq!(f.weights, default_weights());
        assert_eq!(f.threshold_lower, 0.3);
        assert_eq!(f.threshold_upper, 0.85);
        assert_eq!(f.last_tuning_ts, 0);
    }

    #[test]
    fn test_weights_file_last_tuning_ts_round_trip() {
        let mut f = default_weights_file(1_776_384_000);
        f.last_tuning_ts = 1_776_500_000;
        let json = serialize_weights_file(&f);
        assert!(json.contains("\"last_tuning_ts\""));
        let parsed = parse_weights_file(&json).unwrap();
        assert_eq!(parsed.last_tuning_ts, 1_776_500_000);
        assert_eq!(parsed, f);
    }

    #[test]
    fn test_parse_weights_file_without_last_tuning_ts_defaults_to_zero() {
        // Pre-Slice 2f weights files don't have last_tuning_ts — must still parse.
        let legacy = r#"{"version":1,"updated_at":1776384000,"weights":{"confidence":0.25,"corroboration":0.15,"recency":0.40,"validation":0.20},"threshold_lower":0.3,"threshold_upper":0.85,"feedback_count":0}"#;
        let parsed = parse_weights_file(legacy).expect("legacy weights file must parse");
        assert_eq!(parsed.last_tuning_ts, 0);
        assert_eq!(parsed.feedback_count, 0);
    }

    #[test]
    fn test_serialize_weights_file_is_pretty() {
        let f = default_weights_file(1_776_384_000);
        let json = serialize_weights_file(&f);
        assert!(json.contains('\n'), "pretty JSON must contain newlines");
        assert!(json.contains("  "), "pretty JSON must use 2-space indent");
    }

    #[test]
    fn test_weights_file_round_trip() {
        let original = default_weights_file(1_776_384_000);
        let json = serialize_weights_file(&original);
        let parsed = parse_weights_file(&json).expect("round trip must succeed");
        assert_eq!(original, parsed);
    }

    #[test]
    fn test_parse_weights_file_rejects_malformed() {
        let err = parse_weights_file("not-json-at-all").unwrap_err();
        assert!(err.contains("failed to parse"), "err: {}", err);
    }

    #[test]
    fn test_parse_weights_file_rejects_unknown_version() {
        let mut f = default_weights_file(1_776_384_000);
        f.version = 2;
        let json = serialize_weights_file(&f);
        let err = parse_weights_file(&json).unwrap_err();
        assert!(err.contains("unsupported weights file version"), "err: {}", err);
        assert!(err.contains('2'), "err must mention actual version: {}", err);
    }

    #[test]
    fn test_parse_weights_file_rejects_missing_fields() {
        let err = parse_weights_file(r#"{"version":1}"#).unwrap_err();
        assert!(err.contains("failed to parse"), "err: {}", err);
    }

    #[test]
    fn test_parse_weights_file_rejects_empty_object() {
        let err = parse_weights_file("{}").unwrap_err();
        assert!(err.contains("failed to parse"), "err: {}", err);
    }

    #[test]
    fn test_weights_file_feedback_count_preserved() {
        let mut f = default_weights_file(1_776_384_000);
        f.feedback_count = 42;
        let json = serialize_weights_file(&f);
        let parsed = parse_weights_file(&json).unwrap();
        assert_eq!(parsed.feedback_count, 42);
    }

    #[test]
    fn test_weights_file_custom_weights_preserved() {
        let mut f = default_weights_file(1_776_384_000);
        f.weights = ResolutionWeights {
            confidence: 0.30,
            corroboration: 0.10,
            recency: 0.35,
            validation: 0.25,
        };
        let json = serialize_weights_file(&f);
        let parsed = parse_weights_file(&json).unwrap();
        assert_eq!(parsed.weights, f.weights);
    }

    // --------------- JSONL append ---------------

    #[test]
    fn test_append_to_empty_produces_single_line_with_newline() {
        let entry = sample_entry();
        let out = append_to_jsonl("", &entry);
        assert_eq!(out.matches('\n').count(), 1);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn test_append_after_line_produces_two_lines() {
        let entry = sample_entry();
        let seeded = append_to_jsonl("", &entry);
        let out = append_to_jsonl(&seeded, &entry);
        assert_eq!(out.matches('\n').count(), 2);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn test_append_to_content_without_trailing_newline_inserts_one() {
        let entry = sample_entry();
        let raw = serde_json::to_string(&entry).unwrap();
        let out = append_to_jsonl(&raw, &entry);
        assert_eq!(out.matches('\n').count(), 2);
        assert!(out.ends_with('\n'));
    }

    #[test]
    fn test_appended_line_is_compact_no_inner_newlines() {
        let entry = sample_entry();
        let out = append_to_jsonl("", &entry);
        // Exactly one newline — at the end.
        let stripped = out.trim_end_matches('\n');
        assert!(!stripped.contains('\n'), "appended line must be compact");
    }

    // --------------- JSONL read ---------------

    #[test]
    fn test_read_jsonl_empty() {
        let (entries, warns) = read_jsonl("");
        assert!(entries.is_empty());
        assert!(warns.is_empty());
    }

    #[test]
    fn test_read_jsonl_round_trip_many_entries() {
        let entries_in: Vec<FeedbackEntry> = (0..5)
            .map(|i| {
                let mut e = sample_entry();
                e.ts = 1_776_384_000 + i as i64;
                e
            })
            .collect();
        let mut content = String::new();
        for e in &entries_in {
            content = append_to_jsonl(&content, e);
        }
        let (entries_out, warns) = read_jsonl(&content);
        assert_eq!(entries_out, entries_in);
        assert!(warns.is_empty());
    }

    #[test]
    fn test_read_jsonl_skips_blank_lines() {
        let entry = sample_entry();
        let line = serde_json::to_string(&entry).unwrap();
        let content = format!("\n{}\n\n{}\n\n", line, line);
        let (entries, warns) = read_jsonl(&content);
        assert_eq!(entries.len(), 2);
        assert!(warns.is_empty());
    }

    #[test]
    fn test_read_jsonl_single_malformed_line_warns_but_keeps_valid() {
        let entry = sample_entry();
        let line = serde_json::to_string(&entry).unwrap();
        let content = format!("{}\nnot-a-valid-json\n{}\n", line, line);
        let (entries, warns) = read_jsonl(&content);
        assert_eq!(entries.len(), 2);
        assert_eq!(warns.len(), 1);
        assert!(warns[0].contains("line 2"), "warn: {}", warns[0]);
    }

    #[test]
    fn test_read_jsonl_all_malformed_returns_warnings_no_entries() {
        let content = "foo\nbar\nbaz\n";
        let (entries, warns) = read_jsonl(content);
        assert!(entries.is_empty());
        assert_eq!(warns.len(), 3);
    }

    #[test]
    fn test_read_jsonl_golden_line() {
        // Byte-level golden — locks JSONL format across Rust/TS/Python clients.
        let entry = sample_entry();
        let line = serde_json::to_string(&entry).unwrap();
        let expected = r#"{"ts":1776384000,"claim_a_id":"0xaaa","claim_b_id":"0xbbb","formula_winner":"a","user_decision":"pin_b","winner_components":{"confidence":0.8,"corroboration":1.732,"recency":0.333,"validation":0.7,"weighted_total":0.7331},"loser_components":{"confidence":0.6,"corroboration":1.0,"recency":0.125,"validation":0.5,"weighted_total":0.4025}}"#;
        assert_eq!(line, expected);
    }

    // --------------- Rotation ---------------

    #[test]
    fn test_rotate_empty_stays_empty() {
        assert_eq!(rotate_if_needed("", 10), "");
    }

    #[test]
    fn test_rotate_below_cap_unchanged() {
        let entry = sample_entry();
        let mut content = String::new();
        for _ in 0..5 {
            content = append_to_jsonl(&content, &entry);
        }
        let rotated = rotate_if_needed(&content, 10);
        assert_eq!(rotated, content);
    }

    #[test]
    fn test_rotate_above_cap_keeps_last_n() {
        let mut content = String::new();
        for i in 0..15 {
            let mut e = sample_entry();
            e.ts = 1_776_384_000 + i as i64;
            content = append_to_jsonl(&content, &e);
        }
        let rotated = rotate_if_needed(&content, 10);
        let (entries, warns) = read_jsonl(&rotated);
        assert!(warns.is_empty());
        assert_eq!(entries.len(), 10);
        // Kept the most recent 10 (ts 1_776_384_005 through 1_776_384_014).
        assert_eq!(entries.first().unwrap().ts, 1_776_384_005);
        assert_eq!(entries.last().unwrap().ts, 1_776_384_014);
    }

    #[test]
    fn test_rotate_cap_zero_empties() {
        let entry = sample_entry();
        let content = append_to_jsonl("", &entry);
        assert_eq!(rotate_if_needed(&content, 0), "");
    }

    #[test]
    fn test_rotate_preserves_trailing_newline() {
        let mut content = String::new();
        for i in 0..15 {
            let mut e = sample_entry();
            e.ts = 1_776_384_000 + i as i64;
            content = append_to_jsonl(&content, &e);
        }
        assert!(content.ends_with('\n'));
        let rotated = rotate_if_needed(&content, 10);
        assert!(rotated.ends_with('\n'));
    }

    #[test]
    fn test_rotate_equal_to_cap_unchanged() {
        let entry = sample_entry();
        let mut content = String::new();
        for _ in 0..10 {
            content = append_to_jsonl(&content, &entry);
        }
        let rotated = rotate_if_needed(&content, 10);
        assert_eq!(rotated, content);
    }

    // --------------- feedback_to_counterexample ---------------

    #[test]
    fn test_ce_pin_a_when_formula_winner_b_maps_to_loser() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::B;
        e.user_decision = UserDecision::PinA;
        let ce = feedback_to_counterexample(&e).expect("must produce counterexample");
        assert_eq!(ce.user_pinned, UserPinned::Loser);
        // winner/loser components pass through unchanged — they already describe
        // the formula's winner and loser respectively.
        assert_eq!(ce.formula_winner, e.winner_components);
        assert_eq!(ce.formula_loser, e.loser_components);
    }

    #[test]
    fn test_ce_pin_b_when_formula_winner_a_maps_to_loser() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::A;
        e.user_decision = UserDecision::PinB;
        let ce = feedback_to_counterexample(&e).expect("must produce counterexample");
        assert_eq!(ce.user_pinned, UserPinned::Loser);
        assert_eq!(ce.formula_winner, e.winner_components);
        assert_eq!(ce.formula_loser, e.loser_components);
    }

    #[test]
    fn test_ce_pin_a_when_formula_winner_a_is_none() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::A;
        e.user_decision = UserDecision::PinA;
        assert!(feedback_to_counterexample(&e).is_none());
    }

    #[test]
    fn test_ce_pin_b_when_formula_winner_b_is_none() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::B;
        e.user_decision = UserDecision::PinB;
        assert!(feedback_to_counterexample(&e).is_none());
    }

    #[test]
    fn test_ce_pin_both_maps_to_both() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::A;
        e.user_decision = UserDecision::PinBoth;
        let ce = feedback_to_counterexample(&e).expect("PinBoth must produce counterexample");
        assert_eq!(ce.user_pinned, UserPinned::Both);
    }

    #[test]
    fn test_ce_pin_both_from_formula_b_also_maps_to_both() {
        let mut e = sample_entry();
        e.formula_winner = FormulaWinner::B;
        e.user_decision = UserDecision::PinBoth;
        let ce = feedback_to_counterexample(&e).expect("PinBoth must produce counterexample");
        assert_eq!(ce.user_pinned, UserPinned::Both);
    }

    #[test]
    fn test_ce_unpin_is_none() {
        let mut e = sample_entry();
        e.user_decision = UserDecision::Unpin;
        assert!(feedback_to_counterexample(&e).is_none());
    }

    // --------------- Serde round-trips & enum rename ---------------

    #[test]
    fn test_feedback_entry_serde_round_trip_pin_a() {
        let mut e = sample_entry();
        e.user_decision = UserDecision::PinA;
        let s = serde_json::to_string(&e).unwrap();
        let back: FeedbackEntry = serde_json::from_str(&s).unwrap();
        assert_eq!(back, e);
    }

    #[test]
    fn test_feedback_entry_serde_round_trip_pin_b() {
        let mut e = sample_entry();
        e.user_decision = UserDecision::PinB;
        let s = serde_json::to_string(&e).unwrap();
        let back: FeedbackEntry = serde_json::from_str(&s).unwrap();
        assert_eq!(back, e);
    }

    #[test]
    fn test_feedback_entry_serde_round_trip_pin_both() {
        let mut e = sample_entry();
        e.user_decision = UserDecision::PinBoth;
        let s = serde_json::to_string(&e).unwrap();
        let back: FeedbackEntry = serde_json::from_str(&s).unwrap();
        assert_eq!(back, e);
    }

    #[test]
    fn test_feedback_entry_serde_round_trip_unpin() {
        let mut e = sample_entry();
        e.user_decision = UserDecision::Unpin;
        let s = serde_json::to_string(&e).unwrap();
        let back: FeedbackEntry = serde_json::from_str(&s).unwrap();
        assert_eq!(back, e);
    }

    #[test]
    fn test_formula_winner_enum_rename_a() {
        let s = serde_json::to_string(&FormulaWinner::A).unwrap();
        assert_eq!(s, "\"a\"");
    }

    #[test]
    fn test_formula_winner_enum_rename_b() {
        let s = serde_json::to_string(&FormulaWinner::B).unwrap();
        assert_eq!(s, "\"b\"");
    }

    #[test]
    fn test_user_decision_enum_rename_pin_a() {
        let s = serde_json::to_string(&UserDecision::PinA).unwrap();
        assert_eq!(s, "\"pin_a\"");
    }

    #[test]
    fn test_user_decision_enum_rename_pin_b() {
        let s = serde_json::to_string(&UserDecision::PinB).unwrap();
        assert_eq!(s, "\"pin_b\"");
    }

    #[test]
    fn test_user_decision_enum_rename_pin_both() {
        let s = serde_json::to_string(&UserDecision::PinBoth).unwrap();
        assert_eq!(s, "\"pin_both\"");
    }

    #[test]
    fn test_user_decision_enum_rename_unpin() {
        let s = serde_json::to_string(&UserDecision::Unpin).unwrap();
        assert_eq!(s, "\"unpin\"");
    }

    #[test]
    fn test_feedback_entry_with_high_precision_floats() {
        let mut e = sample_entry();
        e.winner_components.weighted_total = 0.733_111_222_333;
        e.loser_components.recency = 0.000_123_456;
        let s = serde_json::to_string(&e).unwrap();
        let back: FeedbackEntry = serde_json::from_str(&s).unwrap();
        assert_eq!(back, e);
    }

    #[test]
    fn test_weights_file_json_field_names_snake_case() {
        let f = default_weights_file(1_776_384_000);
        let json = serialize_weights_file(&f);
        assert!(json.contains("\"threshold_lower\""));
        assert!(json.contains("\"threshold_upper\""));
        assert!(json.contains("\"feedback_count\""));
        assert!(json.contains("\"updated_at\""));
    }
}
