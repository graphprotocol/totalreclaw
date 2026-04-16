//! Decision log types and loser-recovery utilities (Phase 2 Steps B/C).
//!
//! The decision log (`decisions.jsonl`) records every contradiction-resolution
//! decision the system makes. It serves two purposes:
//!   1. Audit trail for operator visibility.
//!   2. Recovery path for the pin tool — when a superseded fact's on-chain blob
//!      is tombstoned (`0x00`), the pin tool recovers the original plaintext from
//!      the `loser_claim_json` field in this log.

use crate::contradiction::ScoreComponents;
use crate::feedback_log::{FeedbackEntry, FormulaWinner, UserDecision};
use serde::{Deserialize, Serialize};

/// Cap on the decisions.jsonl log — oldest lines are dropped above this.
pub const DECISION_LOG_MAX_LINES: usize = 10_000;

/// Soft cap on candidates fetched per entity during contradiction detection.
pub const CONTRADICTION_CANDIDATE_CAP: usize = 20;

/// A single row in `decisions.jsonl`.
///
/// Field names use `snake_case` to match the TypeScript output byte-for-byte.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DecisionLogEntry {
    pub ts: i64,
    pub entity_id: String,
    pub new_claim_id: String,
    pub existing_claim_id: String,
    pub similarity: f64,
    /// One of: "supersede_existing", "skip_new", "shadow", "tie_leave_both".
    pub action: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub winner_score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub loser_score: Option<f64>,
    /// Per-component score breakdown for the formula winner (Slice 2f).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub winner_components: Option<ScoreComponents>,
    /// Per-component score breakdown for the formula loser (Slice 2f).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub loser_components: Option<ScoreComponents>,
    /// Full canonical Claim JSON for the loser (raw string, NOT parsed).
    /// Only populated on `supersede_existing` rows.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub loser_claim_json: Option<String>,
    /// "active" or "shadow".
    pub mode: String,
}

/// Walk `decisions.jsonl` content in reverse and return the `loser_claim_json`
/// for the most recent `supersede_existing` decision where
/// `existing_claim_id == fact_id`.
///
/// Returns `None` if no matching row is found or if `loser_claim_json` is absent.
pub fn find_loser_claim_in_decision_log(fact_id: &str, log_content: &str) -> Option<String> {
    if log_content.is_empty() {
        return None;
    }
    let lines: Vec<&str> = log_content.split('\n').filter(|l| !l.is_empty()).collect();
    for i in (0..lines.len()).rev() {
        let entry: DecisionLogEntry = match serde_json::from_str(lines[i]) {
            Ok(e) => e,
            Err(_) => continue,
        };
        if entry.action != "supersede_existing" {
            continue;
        }
        if entry.existing_claim_id != fact_id {
            continue;
        }
        match &entry.loser_claim_json {
            Some(json) if !json.is_empty() => return Some(json.clone()),
            _ => continue,
        }
    }
    None
}

/// Walk `decisions.jsonl` content in reverse and return the first
/// `supersede_existing` decision where the fact appears as winner or loser.
///
/// - `role == "loser"`: matches `existing_claim_id == fact_id`
/// - `role == "winner"`: matches `new_claim_id == fact_id`
///
/// Only matches rows that have both `winner_components` and `loser_components`
/// populated (Slice 2f requirement for feedback reconstruction).
///
/// Returns the JSON-serialized `DecisionLogEntry`, or `None`.
pub fn find_decision_for_pin(fact_id: &str, role: &str, log_content: &str) -> Option<String> {
    if log_content.is_empty() {
        return None;
    }
    let lines: Vec<&str> = log_content.split('\n').filter(|l| !l.is_empty()).collect();
    for i in (0..lines.len()).rev() {
        let entry: DecisionLogEntry = match serde_json::from_str(lines[i]) {
            Ok(e) => e,
            Err(_) => continue,
        };
        if entry.action != "supersede_existing" {
            continue;
        }
        if entry.winner_components.is_none() || entry.loser_components.is_none() {
            continue;
        }
        let matches = match role {
            "loser" => entry.existing_claim_id == fact_id,
            "winner" => entry.new_claim_id == fact_id,
            _ => false,
        };
        if matches {
            return serde_json::to_string(&entry).ok();
        }
    }
    None
}

/// Build a `FeedbackEntry` JSON from a decision-log entry JSON and a pin action.
///
/// `action` is either `"pin_loser"` or `"unpin_winner"`.
///
/// For `supersede_existing`, the formula's winner is always the new claim
/// (`new_claim_id`) and the loser is the existing claim.
///
/// Returns `None` if the decision is missing component scores or the action is
/// unrecognized.
pub fn build_feedback_from_decision(
    decision_json: &str,
    action: &str,
    now_unix: i64,
) -> Option<String> {
    let decision: DecisionLogEntry = serde_json::from_str(decision_json).ok()?;
    let winner_components = decision.winner_components?;
    let loser_components = decision.loser_components?;

    let user_decision = match action {
        "pin_loser" => UserDecision::PinA,
        "unpin_winner" => UserDecision::PinB,
        _ => return None,
    };

    let entry = FeedbackEntry {
        ts: now_unix,
        claim_a_id: decision.existing_claim_id,
        claim_b_id: decision.new_claim_id,
        formula_winner: FormulaWinner::B,
        user_decision,
        winner_components,
        loser_components,
    };

    serde_json::to_string(&entry).ok()
}

/// Append one decision-log entry (as JSON string) to existing JSONL content.
///
/// Handles empty content, content with trailing newline, and content without.
pub fn append_decision_entry(existing_content: &str, entry_json: &str) -> String {
    let mut out = String::with_capacity(existing_content.len() + entry_json.len() + 2);
    if existing_content.is_empty() {
        out.push_str(entry_json);
        out.push('\n');
    } else if existing_content.ends_with('\n') {
        out.push_str(existing_content);
        out.push_str(entry_json);
        out.push('\n');
    } else {
        out.push_str(existing_content);
        out.push('\n');
        out.push_str(entry_json);
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contradiction::ScoreComponents;

    fn sample_components() -> ScoreComponents {
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

    fn sample_entry() -> DecisionLogEntry {
        DecisionLogEntry {
            ts: 1_776_384_000,
            entity_id: "ent123".to_string(),
            new_claim_id: "0xnew".to_string(),
            existing_claim_id: "0xold".to_string(),
            similarity: 0.72,
            action: "supersede_existing".to_string(),
            reason: Some("new_wins".to_string()),
            winner_score: Some(0.7331),
            loser_score: Some(0.4025),
            winner_components: Some(sample_components()),
            loser_components: Some(sample_loser_components()),
            loser_claim_json: Some(r#"{"t":"old claim","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#.to_string()),
            mode: "active".to_string(),
        }
    }

    fn sample_entry_no_components() -> DecisionLogEntry {
        DecisionLogEntry {
            ts: 1_776_384_000,
            entity_id: "ent123".to_string(),
            new_claim_id: "0xnew".to_string(),
            existing_claim_id: "0xold2".to_string(),
            similarity: 0.65,
            action: "supersede_existing".to_string(),
            reason: Some("new_wins".to_string()),
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
            loser_claim_json: None,
            mode: "active".to_string(),
        }
    }

    // === DecisionLogEntry serde ===

    #[test]
    fn test_decision_log_entry_round_trip() {
        let entry = sample_entry();
        let json = serde_json::to_string(&entry).unwrap();
        let back: DecisionLogEntry = serde_json::from_str(&json).unwrap();
        assert_eq!(entry, back);
    }

    #[test]
    fn test_decision_log_entry_omits_none_fields() {
        let entry = sample_entry_no_components();
        let json = serde_json::to_string(&entry).unwrap();
        assert!(!json.contains("winner_components"));
        assert!(!json.contains("loser_components"));
        assert!(!json.contains("loser_claim_json"));
    }

    #[test]
    fn test_decision_log_entry_snake_case_keys() {
        let entry = sample_entry();
        let json = serde_json::to_string(&entry).unwrap();
        assert!(json.contains("\"entity_id\""));
        assert!(json.contains("\"new_claim_id\""));
        assert!(json.contains("\"existing_claim_id\""));
        assert!(json.contains("\"winner_score\""));
        assert!(json.contains("\"loser_score\""));
        assert!(json.contains("\"loser_claim_json\""));
    }

    // === find_loser_claim_in_decision_log ===

    #[test]
    fn test_find_loser_empty_log() {
        assert!(find_loser_claim_in_decision_log("0xold", "").is_none());
    }

    #[test]
    fn test_find_loser_no_match() {
        let entry = sample_entry();
        let line = serde_json::to_string(&entry).unwrap();
        let content = format!("{}\n", line);
        assert!(find_loser_claim_in_decision_log("0xnonexistent", &content).is_none());
    }

    #[test]
    fn test_find_loser_matches_correct_entry() {
        let entry = sample_entry();
        let line = serde_json::to_string(&entry).unwrap();
        let content = format!("{}\n", line);
        let result = find_loser_claim_in_decision_log("0xold", &content);
        assert!(result.is_some());
        assert!(result.unwrap().contains("old claim"));
    }

    #[test]
    fn test_find_loser_walks_backward_returns_most_recent() {
        let mut entry1 = sample_entry();
        entry1.loser_claim_json = Some(r#"{"t":"first version"}"#.to_string());
        entry1.ts = 1_000;
        let mut entry2 = sample_entry();
        entry2.loser_claim_json = Some(r#"{"t":"second version"}"#.to_string());
        entry2.ts = 2_000;
        let content = format!(
            "{}\n{}\n",
            serde_json::to_string(&entry1).unwrap(),
            serde_json::to_string(&entry2).unwrap()
        );
        let result = find_loser_claim_in_decision_log("0xold", &content).unwrap();
        assert!(result.contains("second version"));
    }

    #[test]
    fn test_find_loser_skips_non_supersede_actions() {
        let mut entry = sample_entry();
        entry.action = "tie_leave_both".to_string();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        assert!(find_loser_claim_in_decision_log("0xold", &content).is_none());
    }

    #[test]
    fn test_find_loser_skips_empty_loser_json() {
        let mut entry = sample_entry();
        entry.loser_claim_json = Some("".to_string());
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        assert!(find_loser_claim_in_decision_log("0xold", &content).is_none());
    }

    #[test]
    fn test_find_loser_skips_malformed_lines() {
        let entry = sample_entry();
        let content = format!(
            "not valid json\n{}\n",
            serde_json::to_string(&entry).unwrap()
        );
        let result = find_loser_claim_in_decision_log("0xold", &content);
        assert!(result.is_some());
    }

    // === find_decision_for_pin ===

    #[test]
    fn test_find_decision_for_pin_loser_role() {
        let entry = sample_entry();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        let result = find_decision_for_pin("0xold", "loser", &content);
        assert!(result.is_some());
        let parsed: DecisionLogEntry = serde_json::from_str(&result.unwrap()).unwrap();
        assert_eq!(parsed.existing_claim_id, "0xold");
    }

    #[test]
    fn test_find_decision_for_pin_winner_role() {
        let entry = sample_entry();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        let result = find_decision_for_pin("0xnew", "winner", &content);
        assert!(result.is_some());
        let parsed: DecisionLogEntry = serde_json::from_str(&result.unwrap()).unwrap();
        assert_eq!(parsed.new_claim_id, "0xnew");
    }

    #[test]
    fn test_find_decision_for_pin_no_match() {
        let entry = sample_entry();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        assert!(find_decision_for_pin("0xunknown", "loser", &content).is_none());
    }

    #[test]
    fn test_find_decision_for_pin_skips_no_components() {
        let entry = sample_entry_no_components();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        assert!(find_decision_for_pin("0xold2", "loser", &content).is_none());
    }

    #[test]
    fn test_find_decision_for_pin_empty_log() {
        assert!(find_decision_for_pin("0xold", "loser", "").is_none());
    }

    #[test]
    fn test_find_decision_for_pin_invalid_role() {
        let entry = sample_entry();
        let content = format!("{}\n", serde_json::to_string(&entry).unwrap());
        assert!(find_decision_for_pin("0xold", "invalid_role", &content).is_none());
    }

    // === build_feedback_from_decision ===

    #[test]
    fn test_build_feedback_pin_loser() {
        let entry = sample_entry();
        let decision_json = serde_json::to_string(&entry).unwrap();
        let result = build_feedback_from_decision(&decision_json, "pin_loser", 1_776_500_000);
        assert!(result.is_some());
        let feedback: FeedbackEntry = serde_json::from_str(&result.unwrap()).unwrap();
        assert_eq!(feedback.ts, 1_776_500_000);
        assert_eq!(feedback.claim_a_id, "0xold"); // existing = loser
        assert_eq!(feedback.claim_b_id, "0xnew"); // new = winner
        assert_eq!(feedback.formula_winner, FormulaWinner::B);
        assert_eq!(feedback.user_decision, UserDecision::PinA);
    }

    #[test]
    fn test_build_feedback_unpin_winner() {
        let entry = sample_entry();
        let decision_json = serde_json::to_string(&entry).unwrap();
        let result = build_feedback_from_decision(&decision_json, "unpin_winner", 1_776_500_000);
        assert!(result.is_some());
        let feedback: FeedbackEntry = serde_json::from_str(&result.unwrap()).unwrap();
        assert_eq!(feedback.user_decision, UserDecision::PinB);
    }

    #[test]
    fn test_build_feedback_missing_components_returns_none() {
        let entry = sample_entry_no_components();
        let decision_json = serde_json::to_string(&entry).unwrap();
        assert!(build_feedback_from_decision(&decision_json, "pin_loser", 1_776_500_000).is_none());
    }

    #[test]
    fn test_build_feedback_invalid_action_returns_none() {
        let entry = sample_entry();
        let decision_json = serde_json::to_string(&entry).unwrap();
        assert!(build_feedback_from_decision(&decision_json, "bad_action", 1_776_500_000).is_none());
    }

    #[test]
    fn test_build_feedback_invalid_json_returns_none() {
        assert!(build_feedback_from_decision("not json", "pin_loser", 1_776_500_000).is_none());
    }

    #[test]
    fn test_build_feedback_round_trip() {
        let entry = sample_entry();
        let decision_json = serde_json::to_string(&entry).unwrap();
        let feedback_json =
            build_feedback_from_decision(&decision_json, "pin_loser", 1_776_500_000).unwrap();
        let feedback: FeedbackEntry = serde_json::from_str(&feedback_json).unwrap();
        // Verify components pass through
        assert_eq!(feedback.winner_components, sample_components());
        assert_eq!(feedback.loser_components, sample_loser_components());
    }

    // === append_decision_entry ===

    #[test]
    fn test_append_to_empty() {
        let entry = sample_entry();
        let json = serde_json::to_string(&entry).unwrap();
        let out = append_decision_entry("", &json);
        assert!(out.ends_with('\n'));
        assert_eq!(out.matches('\n').count(), 1);
    }

    #[test]
    fn test_append_after_existing_with_newline() {
        let entry = sample_entry();
        let json = serde_json::to_string(&entry).unwrap();
        let first = append_decision_entry("", &json);
        let second = append_decision_entry(&first, &json);
        assert!(second.ends_with('\n'));
        assert_eq!(second.matches('\n').count(), 2);
    }

    #[test]
    fn test_append_after_existing_without_newline() {
        let entry = sample_entry();
        let json = serde_json::to_string(&entry).unwrap();
        let out = append_decision_entry(&json, &json);
        assert!(out.ends_with('\n'));
        assert_eq!(out.matches('\n').count(), 2);
    }

    // === Constants ===

    #[test]
    fn test_constants() {
        assert_eq!(DECISION_LOG_MAX_LINES, 10_000);
        assert_eq!(CONTRADICTION_CANDIDATE_CAP, 20);
    }
}
