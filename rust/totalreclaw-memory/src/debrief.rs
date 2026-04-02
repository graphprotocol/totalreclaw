//! Session debrief extraction for TotalReclaw.
//!
//! Delegates to `totalreclaw_core::debrief` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::debrief::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_valid_json() {
        let input = r#"[
            {"text": "Session was about refactoring the auth module", "type": "summary", "importance": 8},
            {"text": "Migration to new API is still pending", "type": "context", "importance": 7}
        ]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].item_type, DebriefType::Summary);
        assert_eq!(result[0].importance, 8);
        assert_eq!(result[1].item_type, DebriefType::Context);
        assert_eq!(result[1].importance, 7);
    }

    #[test]
    fn test_parse_empty_array() {
        let result = parse_debrief_response("[]");
        assert!(result.is_empty());
    }

    #[test]
    fn test_caps_at_5_items() {
        let items: Vec<serde_json::Value> = (0..8)
            .map(|i| {
                serde_json::json!({
                    "text": format!("Debrief item number {} with enough text", i + 1),
                    "type": "summary",
                    "importance": 7
                })
            })
            .collect();
        let input = serde_json::to_string(&items).unwrap();
        let result = parse_debrief_response(&input);
        assert_eq!(result.len(), 5);
    }

    #[test]
    fn test_constants() {
        assert_eq!(MIN_DEBRIEF_MESSAGES, 8);
        assert_eq!(MAX_DEBRIEF_ITEMS, 5);
        assert_eq!(DEBRIEF_SOURCE, "zeroclaw_debrief");
    }
}
