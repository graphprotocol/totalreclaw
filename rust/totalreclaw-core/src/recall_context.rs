//! Recall-context formatter — header + per-memory lines with date tags.
//!
//! Produces output byte-identical to the shipped Hermes `agent/recall.py`
//! formatter so that all clients can switch to this shared implementation
//! with zero behaviour change.
//!
//! # Format
//!
//! ```text
//! ## Relevant memories from TotalReclaw
//! The current date is 2024-01-15. Each memory is tagged with the date it
//! was recorded. When the question involves timing or duration, reason
//! carefully about the dates and compute differences precisely.
//! - [claim] (2023-05-21) User likes tea
//! - [preference] User dislikes coffee
//! ```
//!
//! Dates are UTC `YYYY-MM-DD`. A `created_at` of `0`, negative, or
//! unparseable maps to an empty string and the date parenthetical is
//! omitted entirely.

use chrono::{TimeZone, Utc};
use serde::Deserialize;

/// Unix seconds → `"YYYY-MM-DD"` (UTC).
///
/// Returns an empty string for `0`, negative values, or any value that
/// cannot be represented as a valid UTC `NaiveDate` (overflow).
pub fn format_memory_date(created_at_unix: i64) -> String {
    if created_at_unix <= 0 {
        return String::new();
    }
    match Utc.timestamp_opt(created_at_unix, 0).single() {
        Some(dt) => dt.format("%Y-%m-%d").to_string(),
        None => String::new(),
    }
}

/// The current-date + temporal-reasoning nudge header (trailing newline included).
///
/// `now_unix`: current time as Unix seconds. Formatted with [`format_memory_date`].
pub fn recall_context_header(now_unix: i64) -> String {
    let date = format_memory_date(now_unix);
    format!(
        "## Relevant memories from TotalReclaw\nThe current date is {date}. Each memory is tagged with the date it was recorded. When the question involves timing or duration, reason carefully about the dates and compute differences precisely.\n"
    )
}

#[derive(Deserialize)]
struct RecallItem {
    #[serde(default)]
    category: String,
    #[serde(default)]
    text: String,
    /// Unix seconds; `0` = no date available.
    #[serde(default)]
    created_at: i64,
}

/// Full recall-context block: header immediately followed by one line per item.
///
/// `items_json`: JSON array of `{ category, text, created_at }`. Any field
/// may be absent (defaults to empty string / 0). Bad or empty JSON → header
/// only (no panic).
///
/// Output format:
/// - With date:    `- [category] (YYYY-MM-DD) text`
/// - Without date: `- [category] text`
///
/// The block is `format!("{}{}", header, lines.join("\n"))` — identical to the
/// Hermes Python implementation.
pub fn format_recall_context(items_json: &str, now_unix: i64) -> String {
    let header = recall_context_header(now_unix);
    let items: Vec<RecallItem> = serde_json::from_str(items_json).unwrap_or_default();
    let lines: Vec<String> = items
        .iter()
        .map(|it| {
            let d = format_memory_date(it.created_at);
            if d.is_empty() {
                format!("- [{}] {}", it.category, it.text)
            } else {
                format!("- [{}] ({}) {}", it.category, d, it.text)
            }
        })
        .collect();
    if lines.is_empty() {
        header
    } else {
        format!("{}{}", header, lines.join("\n"))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // ---- format_memory_date ----

    #[test]
    fn date_known_timestamp() {
        // 2023-05-21T18:30:00Z
        assert_eq!(format_memory_date(1684698600), "2023-05-21");
    }

    #[test]
    fn date_zero_is_empty() {
        assert_eq!(format_memory_date(0), "");
    }

    #[test]
    fn date_negative_is_empty() {
        assert_eq!(format_memory_date(-1), "");
        assert_eq!(format_memory_date(-999999), "");
    }

    #[test]
    fn date_epoch_plus_one() {
        // 1970-01-01
        assert_eq!(format_memory_date(1), "1970-01-01");
    }

    // ---- recall_context_header ----

    #[test]
    fn header_contains_date() {
        let h = recall_context_header(1684698600);
        assert!(h.contains("2023-05-21"), "header: {h}");
    }

    #[test]
    fn header_contains_reason_carefully() {
        let h = recall_context_header(1684698600);
        assert!(h.contains("reason carefully"), "header: {h}");
    }

    #[test]
    fn header_ends_with_newline() {
        let h = recall_context_header(1684698600);
        assert!(h.ends_with('\n'), "header should end with newline");
    }

    #[test]
    fn header_starts_with_h2() {
        let h = recall_context_header(1684698600);
        assert!(h.starts_with("## Relevant memories from TotalReclaw\n"));
    }

    // ---- format_recall_context ----

    #[test]
    fn recall_context_with_date_and_no_date() {
        let items = r#"[{"category":"claim","text":"likes tea","created_at":1684698600},{"category":"preference","text":"no dates","created_at":0}]"#;
        let out = format_recall_context(items, 1684698600);

        assert!(
            out.contains("- [claim] (2023-05-21) likes tea"),
            "missing dated line; got:\n{out}"
        );
        assert!(
            out.contains("- [preference] no dates"),
            "missing undated line; got:\n{out}"
        );
        // Must NOT produce an empty-parenthetical form like "- [preference] () no dates"
        assert!(
            !out.contains("[preference] ()"),
            "spurious empty parens; got:\n{out}"
        );
    }

    #[test]
    fn recall_context_empty_array() {
        let header = recall_context_header(1684698600);
        let out = format_recall_context("[]", 1684698600);
        assert_eq!(out, header, "empty array should equal header exactly");
    }

    #[test]
    fn recall_context_bad_json_returns_header_no_panic() {
        let header = recall_context_header(1684698600);
        let out = format_recall_context("not json", 1684698600);
        assert_eq!(out, header, "bad JSON should equal header exactly");
    }

    #[test]
    fn recall_context_null_json_returns_header_no_panic() {
        let header = recall_context_header(1684698600);
        let out = format_recall_context("null", 1684698600);
        assert_eq!(out, header);
    }

    #[test]
    fn recall_context_multiple_items_joined_with_newline() {
        let items = r#"[
            {"category":"claim","text":"A","created_at":1684698600},
            {"category":"claim","text":"B","created_at":1684698600}
        ]"#;
        let out = format_recall_context(items, 1684698600);
        // The two lines must be separated by a single \n (no blank line between them)
        assert!(
            out.contains("(2023-05-21) A\n- [claim] (2023-05-21) B"),
            "lines not joined with single newline; got:\n{out}"
        );
    }

    #[test]
    fn recall_context_missing_fields_default_empty() {
        // Items with no fields at all — category and text default to ""
        let items = r#"[{}]"#;
        let out = format_recall_context(items, 1684698600);
        // Should produce "- [] <text>" where text is empty too — no panic
        assert!(out.contains("- []"), "got:\n{out}");
    }
}
