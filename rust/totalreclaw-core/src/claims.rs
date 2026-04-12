//! Knowledge Graph claim types (Phase 1 Stage 1a).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClaimCategory {
    #[serde(rename = "fact")]
    Fact,
    #[serde(rename = "pref")]
    Preference,
    #[serde(rename = "dec")]
    Decision,
    #[serde(rename = "epi")]
    Episodic,
    #[serde(rename = "goal")]
    Goal,
    #[serde(rename = "ctx")]
    Context,
    #[serde(rename = "sum")]
    Summary,
    #[serde(rename = "ent")]
    Entity,
    #[serde(rename = "dig")]
    Digest,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClaimStatus {
    #[serde(rename = "a")]
    Active,
    #[serde(rename = "s")]
    Superseded,
    #[serde(rename = "r")]
    Retracted,
    #[serde(rename = "c")]
    Contradicted,
    #[serde(rename = "p")]
    Pinned,
}

impl Default for ClaimStatus {
    fn default() -> Self {
        ClaimStatus::Active
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EntityType {
    Person,
    Project,
    Tool,
    Company,
    Concept,
    Place,
}

fn is_one(n: &u32) -> bool {
    *n == 1
}

fn is_active(s: &ClaimStatus) -> bool {
    matches!(s, ClaimStatus::Active)
}

fn is_empty_vec<T>(v: &Vec<T>) -> bool {
    v.is_empty()
}

fn default_corroboration() -> u32 {
    1
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityRef {
    #[serde(rename = "n")]
    pub name: String,
    #[serde(rename = "tp")]
    pub entity_type: EntityType,
    #[serde(rename = "r", skip_serializing_if = "Option::is_none", default)]
    pub role: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    #[serde(rename = "t")]
    pub text: String,
    #[serde(rename = "c")]
    pub category: ClaimCategory,
    #[serde(rename = "cf")]
    pub confidence: f64,
    #[serde(rename = "i")]
    pub importance: u8,
    #[serde(
        rename = "cc",
        skip_serializing_if = "is_one",
        default = "default_corroboration"
    )]
    pub corroboration_count: u32,
    #[serde(rename = "sa")]
    pub source_agent: String,
    #[serde(rename = "sc", skip_serializing_if = "Option::is_none", default)]
    pub source_conversation: Option<String>,
    #[serde(rename = "ea", skip_serializing_if = "Option::is_none", default)]
    pub extracted_at: Option<String>,
    #[serde(rename = "e", skip_serializing_if = "is_empty_vec", default)]
    pub entities: Vec<EntityRef>,
    #[serde(rename = "sup", skip_serializing_if = "Option::is_none", default)]
    pub supersedes: Option<String>,
    #[serde(rename = "sby", skip_serializing_if = "Option::is_none", default)]
    pub superseded_by: Option<String>,
    #[serde(rename = "vf", skip_serializing_if = "Option::is_none", default)]
    pub valid_from: Option<String>,
    #[serde(rename = "st", skip_serializing_if = "is_active", default)]
    pub status: ClaimStatus,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Entity {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub entity_type: EntityType,
    pub aliases: Vec<String>,
    pub claim_ids: Vec<String>,
    pub first_seen: String,
    pub last_seen: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DigestClaim {
    pub text: String,
    pub category: ClaimCategory,
    pub confidence: f64,
    pub age: String,
}

/// Normalize an entity name per §15.8: NFC(lowercase(trim(collapse_whitespace(name)))).
pub fn normalize_entity_name(name: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    let mut collapsed = String::with_capacity(name.len());
    let mut in_ws = false;
    let mut any = false;
    for ch in name.chars() {
        if ch.is_whitespace() {
            if any && !in_ws {
                collapsed.push(' ');
                in_ws = true;
            }
        } else {
            collapsed.push(ch);
            in_ws = false;
            any = true;
        }
    }
    let trimmed = collapsed.trim_end_matches(' ').to_string();
    let lowered: String = trimmed.chars().flat_map(|c| c.to_lowercase()).collect();
    lowered.nfc().collect()
}

/// Deterministic entity ID: first 8 bytes of SHA256(normalized name) as hex.
pub fn deterministic_entity_id(name: &str) -> String {
    use sha2::{Digest as _, Sha256};
    let normalized = normalize_entity_name(name);
    let hash = Sha256::digest(normalized.as_bytes());
    hex::encode(&hash[..8])
}

/// Parse a decrypted blob as a Claim, falling back to legacy formats per §15.2.
pub fn parse_claim_or_legacy(decrypted: &str) -> Claim {
    if let Ok(claim) = serde_json::from_str::<Claim>(decrypted) {
        return claim;
    }
    let (text, source_agent) = if let Ok(value) = serde_json::from_str::<serde_json::Value>(decrypted) {
        match value {
            serde_json::Value::String(s) => (s, "unknown".to_string()),
            serde_json::Value::Object(map) => {
                let text = map
                    .get("t")
                    .or_else(|| map.get("text"))
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| decrypted.to_string());
                let agent = map
                    .get("a")
                    .or_else(|| {
                        map.get("metadata")
                            .and_then(|m| m.as_object())
                            .and_then(|m| m.get("source"))
                    })
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_else(|| "unknown".to_string());
                (text, agent)
            }
            _ => (decrypted.to_string(), "unknown".to_string()),
        }
    } else {
        (decrypted.to_string(), "unknown".to_string())
    };
    Claim {
        text,
        category: ClaimCategory::Fact,
        confidence: 0.7,
        importance: 5,
        corroboration_count: 1,
        source_agent,
        source_conversation: None,
        extracted_at: None,
        entities: Vec::new(),
        supersedes: None,
        superseded_by: None,
        valid_from: None,
        status: ClaimStatus::Active,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Digest {
    pub version: u64,
    pub compiled_at: String,
    pub fact_count: u32,
    pub entity_count: u32,
    pub contradiction_count: u32,
    pub identity: String,
    pub top_claims: Vec<DigestClaim>,
    pub recent_decisions: Vec<DigestClaim>,
    pub active_projects: Vec<String>,
    pub active_contradictions: u32,
    pub prompt_text: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_claim() -> Claim {
        Claim {
            text: "prefers PostgreSQL".to_string(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        }
    }

    fn full_claim() -> Claim {
        Claim {
            text: "Pedro chose PostgreSQL over MySQL because relational modeling is cleaner for our domain".to_string(),
            category: ClaimCategory::Decision,
            confidence: 0.92,
            importance: 9,
            corroboration_count: 3,
            source_agent: "openclaw-plugin".to_string(),
            source_conversation: Some("conv-abc-123".to_string()),
            extracted_at: Some("2026-04-12T10:00:00Z".to_string()),
            entities: vec![
                EntityRef {
                    name: "Pedro".to_string(),
                    entity_type: EntityType::Person,
                    role: Some("chooser".to_string()),
                },
                EntityRef {
                    name: "PostgreSQL".to_string(),
                    entity_type: EntityType::Tool,
                    role: Some("chosen".to_string()),
                },
            ],
            supersedes: Some("0xabc".to_string()),
            superseded_by: None,
            valid_from: Some("2026-04-01T00:00:00Z".to_string()),
            status: ClaimStatus::Superseded,
        }
    }

    // === Serde round-trip ===

    #[test]
    fn test_full_claim_round_trip() {
        let c = full_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: Claim = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_minimal_claim_round_trip() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: Claim = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_minimal_claim_omits_defaults() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        // status=Active -> omitted
        assert!(!json.contains("\"st\""), "status should be omitted when Active: {}", json);
        // corroboration_count=1 -> omitted
        assert!(!json.contains("\"cc\""), "corroboration_count should be omitted when 1: {}", json);
        // None options omitted
        assert!(!json.contains("\"sup\""));
        assert!(!json.contains("\"sby\""));
        assert!(!json.contains("\"vf\""));
        assert!(!json.contains("\"ea\""));
        assert!(!json.contains("\"sc\""));
    }

    #[test]
    fn test_minimal_claim_short_keys_present() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"t\":"));
        assert!(json.contains("\"c\":\"pref\""));
        assert!(json.contains("\"cf\":"));
        assert!(json.contains("\"i\":"));
        assert!(json.contains("\"sa\":"));
        assert!(json.contains("\"e\":"));
        // Entity short keys
        assert!(json.contains("\"n\":\"PostgreSQL\""));
        assert!(json.contains("\"tp\":\"tool\""));
        // role None -> omitted
        assert!(!json.contains("\"r\":"));
    }

    #[test]
    fn test_category_short_strings() {
        let pairs = [
            (ClaimCategory::Fact, "fact"),
            (ClaimCategory::Preference, "pref"),
            (ClaimCategory::Decision, "dec"),
            (ClaimCategory::Episodic, "epi"),
            (ClaimCategory::Goal, "goal"),
            (ClaimCategory::Context, "ctx"),
            (ClaimCategory::Summary, "sum"),
            (ClaimCategory::Entity, "ent"),
            (ClaimCategory::Digest, "dig"),
        ];
        for (cat, expected) in pairs {
            let json = serde_json::to_string(&cat).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
            let back: ClaimCategory = serde_json::from_str(&json).unwrap();
            assert_eq!(cat, back);
        }
    }

    #[test]
    fn test_status_short_strings() {
        let pairs = [
            (ClaimStatus::Active, "a"),
            (ClaimStatus::Superseded, "s"),
            (ClaimStatus::Retracted, "r"),
            (ClaimStatus::Contradicted, "c"),
            (ClaimStatus::Pinned, "p"),
        ];
        for (st, expected) in pairs {
            let json = serde_json::to_string(&st).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
            let back: ClaimStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(st, back);
        }
    }

    #[test]
    fn test_entity_type_short_strings() {
        let pairs = [
            (EntityType::Person, "person"),
            (EntityType::Project, "project"),
            (EntityType::Tool, "tool"),
            (EntityType::Company, "company"),
            (EntityType::Concept, "concept"),
            (EntityType::Place, "place"),
        ];
        for (et, expected) in pairs {
            let json = serde_json::to_string(&et).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
        }
    }

    #[test]
    fn test_reference_claim_exact_bytes() {
        // Byte-level canonical: lock down the exact JSON output of a fixed claim.
        // This test MUST match what Python + TS produce for cross-language parity.
        let c = Claim {
            text: "prefers PostgreSQL".to_string(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        let json = serde_json::to_string(&c).unwrap();
        let expected = r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#;
        assert_eq!(json, expected);
    }

    #[test]
    fn test_typical_claim_byte_size() {
        // Spec §14d / §15.7 targets ~90 bytes metadata overhead (text excluded).
        // Build a claim with exactly 120-byte text and verify metadata overhead stays near target.
        let text = "a".repeat(120);
        let c = Claim {
            text: text.clone(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        let json = serde_json::to_string(&c).unwrap();
        let metadata_overhead = json.len() - text.len();
        assert!(
            metadata_overhead <= 95,
            "metadata overhead should be <=95 bytes, got {}: {}",
            metadata_overhead,
            json
        );
        // Total blob for this claim should comfortably stay under 220 bytes.
        assert!(
            json.len() <= 220,
            "total claim JSON should be <=220 bytes, got {}: {}",
            json.len(),
            json
        );
    }

    #[test]
    fn test_deserialize_with_missing_defaults() {
        // A minimal compact blob with no status/cc/options should parse cleanly.
        let json = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let c: Claim = serde_json::from_str(json).unwrap();
        assert_eq!(c.status, ClaimStatus::Active);
        assert_eq!(c.corroboration_count, 1);
        assert!(c.entities.is_empty());
        assert!(c.extracted_at.is_none());
    }

    // === Entity normalization ===

    #[test]
    fn test_normalize_simple_lowercase() {
        assert_eq!(normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn test_normalize_collapse_and_trim() {
        assert_eq!(normalize_entity_name("  Node  JS  "), "node js");
    }

    #[test]
    fn test_normalize_preserves_punctuation() {
        assert_eq!(normalize_entity_name("Node.js"), "node.js");
    }

    #[test]
    fn test_normalize_empty() {
        assert_eq!(normalize_entity_name(""), "");
    }

    #[test]
    fn test_normalize_whitespace_only() {
        assert_eq!(normalize_entity_name("   \t  "), "");
    }

    #[test]
    fn test_normalize_nfc_idempotent_on_precomposed() {
        // Precomposed é (U+00E9)
        assert_eq!(normalize_entity_name("José"), "josé");
    }

    #[test]
    fn test_normalize_nfc_merges_combining() {
        // NFD: 'e' + combining acute U+0301 -> precomposed é after NFC
        let nfd = "Jose\u{0301}";
        let nfc = "josé";
        assert_eq!(normalize_entity_name(nfd), nfc);
    }

    #[test]
    fn test_normalize_unicode_combining_same_id() {
        // "PostgréSQL" with precomposed vs combining should yield same normalized and same ID
        let a = "Postgre\u{0301}SQL"; // NFD
        let b = "PostgréSQL"; // NFC precomposed
        assert_eq!(normalize_entity_name(a), normalize_entity_name(b));
        assert_eq!(deterministic_entity_id(a), deterministic_entity_id(b));
    }

    #[test]
    fn test_normalize_internal_multispace() {
        assert_eq!(normalize_entity_name("Foo\t\n Bar"), "foo bar");
    }

    // === Deterministic entity ID ===

    #[test]
    fn test_entity_id_case_insensitive() {
        let a = deterministic_entity_id("Pedro");
        let b = deterministic_entity_id("pedro");
        let c = deterministic_entity_id("  PEDRO  ");
        assert_eq!(a, b);
        assert_eq!(b, c);
    }

    #[test]
    fn test_entity_id_different_names_differ() {
        let a = deterministic_entity_id("Pedro");
        let b = deterministic_entity_id("Sarah");
        assert_ne!(a, b);
    }

    #[test]
    fn test_entity_id_format() {
        let id = deterministic_entity_id("anything");
        assert_eq!(id.len(), 16);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_entity_id_known_answer_pedro() {
        // Known answer for cross-language parity. SHA256("pedro")[..8] as hex.
        // Locked in as the canonical value — parity tests in TS/Python must match.
        let id = deterministic_entity_id("pedro");
        assert_eq!(id, "ee5cd7d5d96c8874");
    }

    #[test]
    fn test_entity_id_known_answer_postgresql() {
        let id = deterministic_entity_id("PostgreSQL");
        // normalized -> "postgresql"
        let again = deterministic_entity_id("postgresql");
        assert_eq!(id, again);
    }

    // === Legacy parser ===

    #[test]
    fn test_parse_full_claim_json() {
        let c = full_claim();
        let json = serde_json::to_string(&c).unwrap();
        let parsed = parse_claim_or_legacy(&json);
        assert_eq!(parsed, c);
    }

    #[test]
    fn test_parse_legacy_object_format() {
        let json = r#"{"t":"hello","a":"oc","s":"extract"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "hello");
        assert_eq!(parsed.source_agent, "oc");
        assert_eq!(parsed.category, ClaimCategory::Fact);
        assert_eq!(parsed.confidence, 0.7);
        assert_eq!(parsed.importance, 5);
        assert_eq!(parsed.corroboration_count, 1);
        assert_eq!(parsed.status, ClaimStatus::Active);
        assert!(parsed.entities.is_empty());
        assert!(parsed.extracted_at.is_none());
    }

    #[test]
    fn test_parse_legacy_string_format() {
        let json = r#""just text""#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "just text");
        assert_eq!(parsed.source_agent, "unknown");
        assert_eq!(parsed.category, ClaimCategory::Fact);
    }

    #[test]
    fn test_parse_legacy_raw_text() {
        // Not JSON at all
        let parsed = parse_claim_or_legacy("hello world");
        assert_eq!(parsed.text, "hello world");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_parse_legacy_malformed_json() {
        // Looks like JSON, isn't
        let parsed = parse_claim_or_legacy("{not valid json");
        assert_eq!(parsed.text, "{not valid json");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_parse_legacy_missing_text() {
        // Legacy object with no text-like field falls back to the raw blob as text
        let json = r#"{"a":"oc"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, json);
        assert_eq!(parsed.source_agent, "oc");
    }

    #[test]
    fn test_parse_plugin_legacy_doc_format() {
        // The OpenClaw plugin previously wrote blobs as {text, metadata}.
        // Upgrading users must still read their old facts correctly.
        let json = r#"{"text":"prefers PostgreSQL","metadata":{"type":"preference","importance":0.9,"source":"auto-extraction","created_at":"2026-03-01T00:00:00Z"}}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "prefers PostgreSQL");
        assert_eq!(parsed.source_agent, "auto-extraction");
        assert_eq!(parsed.category, ClaimCategory::Fact);
        assert_eq!(parsed.status, ClaimStatus::Active);
    }

    #[test]
    fn test_parse_plugin_legacy_doc_without_metadata_source() {
        let json = r#"{"text":"lives in Lisbon"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "lives in Lisbon");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_legacy_round_trip_via_claim() {
        // Parse a legacy blob, re-serialize as a full claim, re-parse, must be equal.
        let parsed1 = parse_claim_or_legacy(r#"{"t":"hello","a":"oc","s":"extract"}"#);
        let json = serde_json::to_string(&parsed1).unwrap();
        let parsed2 = parse_claim_or_legacy(&json);
        assert_eq!(parsed1, parsed2);
    }

    #[test]
    fn test_parse_never_panics_on_random_input() {
        for s in ["", "   ", "null", "[1,2,3]", "42", "true", "\"\""] {
            let _ = parse_claim_or_legacy(s);
        }
    }

    #[test]
    fn test_claim_category_default_status_omitted_in_serialization() {
        // Sanity: build a claim with status Active and verify status absent
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(!json.contains("\"st\":"));
    }

    #[test]
    fn test_non_default_status_serialized() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Superseded;
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"st\":\"s\""));
    }

    #[test]
    fn test_non_default_corroboration_serialized() {
        let mut c = minimal_claim();
        c.corroboration_count = 5;
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"cc\":5"));
    }
}
