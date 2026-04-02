//! Session debrief extraction for TotalReclaw.
//!
//! Captures broader context, outcomes, and relationships that turn-by-turn
//! extraction misses. Called at session/consolidation end.
//!
//! Uses the canonical debrief prompt — identical across all clients.

use serde::{Deserialize, Serialize};

/// Canonical debrief system prompt.
///
/// This prompt MUST be identical across all TotalReclaw implementations
/// (TypeScript, Python, Rust). Changes here must be mirrored everywhere.
pub const DEBRIEF_SYSTEM_PROMPT: &str = r#"You are reviewing a conversation that just ended. The following facts were
already extracted and stored during this conversation:

{already_stored_facts}

Your job is to capture what turn-by-turn extraction MISSED. Focus on:

1. **Broader context** — What was the conversation about overall? What project,
   problem, or topic tied the discussion together?
2. **Outcomes & conclusions** — What was decided, agreed upon, or resolved?
3. **What was attempted** — What approaches were tried? What worked, what didn't, and why?
4. **Relationships** — How do topics discussed relate to each other or to things
   from previous conversations?
5. **Open threads** — What was left unfinished or needs follow-up?

Do NOT repeat facts already stored. Only add genuinely new information that provides
broader context a future conversation would benefit from.

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "summary|context", "importance": N}]

- Use type "summary" for conclusions, outcomes, and decisions-of-the-session
- Use type "context" for broader project context, open threads, and what-was-tried
- Importance 7-8 for most debrief items (they are high-value by definition)
- Maximum 5 items (debriefs should be concise, not exhaustive)
- Each item should be 1-3 sentences, self-contained

If the conversation was too short or trivial to warrant a debrief, return: []"#;

/// Minimum number of messages to trigger a debrief (4 turns = 8 messages).
pub const MIN_DEBRIEF_MESSAGES: usize = 8;

/// Maximum number of debrief items.
pub const MAX_DEBRIEF_ITEMS: usize = 5;

/// Source tag for debrief items stored on-chain.
pub const DEBRIEF_SOURCE: &str = "zeroclaw_debrief";

/// A single debrief item.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DebriefItem {
    pub text: String,
    #[serde(rename = "type")]
    pub item_type: DebriefType,
    pub importance: u8,
}

/// Debrief item type.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DebriefType {
    Summary,
    Context,
}

impl std::fmt::Display for DebriefType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DebriefType::Summary => write!(f, "summary"),
            DebriefType::Context => write!(f, "context"),
        }
    }
}

/// A conversation message for debrief input.
#[derive(Debug, Clone)]
pub struct Message {
    pub role: String,
    pub content: String,
}

/// Parse a debrief LLM response into validated [`DebriefItem`]s.
///
/// - Strips markdown code fences
/// - Validates type is summary|context (defaults to context)
/// - Filters importance < 6
/// - Caps at 5 items
/// - Defaults importance to 7 if missing/invalid
/// - Rejects text shorter than 5 characters
/// - Truncates text to 512 characters
pub fn parse_debrief_response(response: &str) -> Vec<DebriefItem> {
    let cleaned = strip_code_fences(response.trim());

    let parsed: Vec<serde_json::Value> = match serde_json::from_str(&cleaned) {
        Ok(serde_json::Value::Array(arr)) => arr,
        _ => return Vec::new(),
    };

    let mut items = Vec::new();

    for entry in parsed {
        let obj = match entry.as_object() {
            Some(o) => o,
            None => continue,
        };

        let text = match obj.get("text").and_then(|v| v.as_str()) {
            Some(t) if t.trim().len() >= 5 => {
                let trimmed = t.trim();
                if trimmed.len() > 512 {
                    trimmed[..512].to_string()
                } else {
                    trimmed.to_string()
                }
            }
            _ => continue,
        };

        let item_type = match obj.get("type").and_then(|v| v.as_str()) {
            Some("summary") => DebriefType::Summary,
            _ => DebriefType::Context,
        };

        let importance = obj
            .get("importance")
            .and_then(|v| v.as_u64())
            .map(|n| n.clamp(1, 10) as u8)
            .unwrap_or(7);

        if importance < 6 {
            continue;
        }

        items.push(DebriefItem {
            text,
            item_type,
            importance,
        });

        if items.len() >= MAX_DEBRIEF_ITEMS {
            break;
        }
    }

    items
}

/// Strip markdown code fences from a response.
fn strip_code_fences(s: &str) -> String {
    let mut result = s.to_string();
    if result.starts_with("```") {
        // Remove opening fence (```json or ```)
        if let Some(pos) = result.find('\n') {
            result = result[pos + 1..].to_string();
        }
        // Remove closing fence
        if result.ends_with("```") {
            result = result[..result.len() - 3].trim_end().to_string();
        }
    }
    result
}

/// Format conversation messages for the debrief prompt.
///
/// Truncates to approximately `max_chars` characters.
pub fn format_messages(messages: &[Message], max_chars: usize) -> String {
    let mut lines = Vec::new();
    let mut total = 0;

    for msg in messages {
        let line = format!("[{}]: {}", msg.role, msg.content);
        if total + line.len() > max_chars {
            break;
        }
        total += line.len();
        lines.push(line);
    }

    lines.join("\n\n")
}

/// Build the debrief system prompt with already-stored facts context.
pub fn build_debrief_prompt(stored_fact_texts: &[&str]) -> String {
    let already_stored = if stored_fact_texts.is_empty() {
        "(none)".to_string()
    } else {
        stored_fact_texts
            .iter()
            .map(|t| format!("- {}", t))
            .collect::<Vec<_>>()
            .join("\n")
    };

    DEBRIEF_SYSTEM_PROMPT.replace("{already_stored_facts}", &already_stored)
}

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
    fn test_strips_markdown_fences() {
        let input = "```json\n[{\"text\": \"Session summary here with enough text\", \"type\": \"summary\", \"importance\": 8}]\n```";
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].item_type, DebriefType::Summary);
    }

    #[test]
    fn test_strips_bare_markdown_fences() {
        let input = "```\n[{\"text\": \"Session summary here with enough text\", \"type\": \"context\", \"importance\": 7}]\n```";
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
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
    fn test_filters_importance_below_6() {
        let input = r#"[
            {"text": "Important finding from the session test", "type": "summary", "importance": 8},
            {"text": "Trivial detail that should be filtered out", "type": "context", "importance": 3}
        ]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].importance, 8);
    }

    #[test]
    fn test_validates_type_defaults_to_context() {
        let input = r#"[
            {"text": "Valid summary item for the session here", "type": "summary", "importance": 7},
            {"text": "This has an invalid type value set here", "type": "fact", "importance": 7}
        ]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].item_type, DebriefType::Summary);
        assert_eq!(result[1].item_type, DebriefType::Context);
    }

    #[test]
    fn test_handles_invalid_json() {
        let result = parse_debrief_response("not json at all");
        assert!(result.is_empty());
    }

    #[test]
    fn test_handles_non_array_json() {
        let result = parse_debrief_response(r#"{"text": "not an array"}"#);
        assert!(result.is_empty());
    }

    #[test]
    fn test_handles_empty_string() {
        let result = parse_debrief_response("");
        assert!(result.is_empty());
    }

    #[test]
    fn test_filters_short_text() {
        let input = r#"[
            {"text": "ok", "type": "summary", "importance": 8},
            {"text": "This is a valid debrief item text here", "type": "summary", "importance": 8}
        ]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].text, "This is a valid debrief item text here");
    }

    #[test]
    fn test_filters_missing_text() {
        let input = r#"[
            {"type": "summary", "importance": 8},
            {"text": "Valid debrief item with actual text content", "type": "summary", "importance": 8}
        ]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_defaults_importance_to_7() {
        let input = r#"[{"text": "A debrief item without importance score", "type": "summary"}]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].importance, 7);
    }

    #[test]
    fn test_clamps_importance_to_10() {
        let input =
            r#"[{"text": "A debrief item with huge importance value", "type": "summary", "importance": 99}]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].importance, 10);
    }

    #[test]
    fn test_truncates_text_to_512() {
        let long_text = "x".repeat(600);
        let input = format!(
            r#"[{{"text": "{}", "type": "summary", "importance": 8}}]"#,
            long_text
        );
        let result = parse_debrief_response(&input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].text.len(), 512);
    }

    #[test]
    fn test_trims_whitespace_in_text() {
        let input =
            r#"[{"text": "  Debrief item with whitespace around it  ", "type": "summary", "importance": 8}]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result[0].text, "Debrief item with whitespace around it");
    }

    #[test]
    fn test_skips_non_object_entries() {
        let input = r#"["just a string", {"text": "Valid debrief item with content here", "type": "summary", "importance": 7}, 42]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_build_debrief_prompt_with_facts() {
        let facts = vec!["User prefers dark mode", "User works at Acme"];
        let prompt = build_debrief_prompt(&facts);
        assert!(prompt.contains("- User prefers dark mode"));
        assert!(prompt.contains("- User works at Acme"));
        assert!(!prompt.contains("(none)"));
    }

    #[test]
    fn test_build_debrief_prompt_no_facts() {
        let prompt = build_debrief_prompt(&[]);
        assert!(prompt.contains("(none)"));
    }

    #[test]
    fn test_format_messages() {
        let messages = vec![
            Message {
                role: "user".into(),
                content: "Hello".into(),
            },
            Message {
                role: "assistant".into(),
                content: "Hi there".into(),
            },
        ];
        let result = format_messages(&messages, 1000);
        assert!(result.contains("[user]: Hello"));
        assert!(result.contains("[assistant]: Hi there"));
    }

    #[test]
    fn test_format_messages_truncates() {
        let messages = vec![
            Message {
                role: "user".into(),
                content: "x".repeat(100),
            },
            Message {
                role: "assistant".into(),
                content: "y".repeat(100),
            },
        ];
        let result = format_messages(&messages, 50);
        assert!(!result.contains("[assistant]"));
    }

    #[test]
    fn test_format_messages_empty() {
        let result = format_messages(&[], 1000);
        assert!(result.is_empty());
    }

    #[test]
    fn test_prompt_contains_key_sections() {
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("Broader context"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("Outcomes & conclusions"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("What was attempted"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("Relationships"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("Open threads"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("Maximum 5 items"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("{already_stored_facts}"));
        assert!(DEBRIEF_SYSTEM_PROMPT.contains("summary|context"));
    }

    #[test]
    fn test_prompt_matches_python_canonical() {
        assert!(DEBRIEF_SYSTEM_PROMPT.starts_with("You are reviewing a conversation that just ended."));
        assert!(DEBRIEF_SYSTEM_PROMPT.ends_with("return: []"));
    }

    #[test]
    fn test_constants() {
        assert_eq!(MIN_DEBRIEF_MESSAGES, 8);
        assert_eq!(MAX_DEBRIEF_ITEMS, 5);
        assert_eq!(DEBRIEF_SOURCE, "zeroclaw_debrief");
    }

    #[test]
    fn test_debrief_type_display() {
        assert_eq!(format!("{}", DebriefType::Summary), "summary");
        assert_eq!(format!("{}", DebriefType::Context), "context");
    }

    #[test]
    fn test_debrief_type_serde_roundtrip() {
        let item = DebriefItem {
            text: "Test item for serde roundtrip".to_string(),
            item_type: DebriefType::Summary,
            importance: 8,
        };
        let json = serde_json::to_string(&item).unwrap();
        assert!(json.contains(r#""type":"summary""#));

        let deserialized: DebriefItem = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.item_type, DebriefType::Summary);
        assert_eq!(deserialized.importance, 8);
    }

    #[test]
    fn test_importance_exactly_6_passes() {
        let input =
            r#"[{"text": "Borderline importance item at exactly six", "type": "summary", "importance": 6}]"#;
        let result = parse_debrief_response(input);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].importance, 6);
    }

    #[test]
    fn test_importance_exactly_5_filtered() {
        let input =
            r#"[{"text": "Below threshold importance item at five", "type": "summary", "importance": 5}]"#;
        let result = parse_debrief_response(input);
        assert!(result.is_empty());
    }
}
