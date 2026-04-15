//! Digest compilation (Phase 1 Stage 1b).

use crate::claims::{
    normalize_entity_name, Claim, ClaimCategory, Digest, DigestClaim, EntityType,
};
use chrono::{DateTime, SecondsFormat, TimeZone, Utc};

const PROMPT_TEXT_MAX_CHARS: usize = 2000;
const TEMPLATE_TOP_N: usize = 10;
const TEMPLATE_RECENT_DECISIONS_N: usize = 3;
const TEMPLATE_MAX_PROJECTS: usize = 10;
const EMPTY_VAULT_PROMPT: &str = "No memories stored yet. I'll learn about you as we chat.";

fn parse_iso_to_unix(s: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|dt| dt.timestamp())
}

fn unix_to_iso(secs: i64) -> String {
    Utc.timestamp_opt(secs, 0)
        .single()
        .unwrap_or_else(|| Utc.timestamp_opt(0, 0).unwrap())
        .to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn days_since(extracted_at: Option<&str>, now_unix: i64) -> f64 {
    match extracted_at.and_then(parse_iso_to_unix) {
        Some(ts) => {
            let delta = (now_unix - ts) as f64;
            (delta / 86400.0).max(0.0)
        }
        None => 10000.0,
    }
}

fn recency_weight(extracted_at: Option<&str>, now_unix: i64) -> f64 {
    let days = days_since(extracted_at, now_unix);
    1.0 / (1.0 + days / 30.0)
}

fn score(claim: &Claim, now_unix: i64) -> f64 {
    (claim.importance as f64) * recency_weight(claim.extracted_at.as_deref(), now_unix)
}

/// Format age for a claim's `extracted_at` relative to `now_unix_seconds`.
pub fn format_age(extracted_at: Option<&str>, now_unix_seconds: i64) -> String {
    let ts = match extracted_at.and_then(parse_iso_to_unix) {
        Some(t) => t,
        None => return "unknown".to_string(),
    };
    let delta = now_unix_seconds - ts;
    if delta <= 0 {
        return "today".to_string();
    }
    let days = delta / 86400;
    if days == 0 {
        "today".to_string()
    } else if days == 1 {
        "yesterday".to_string()
    } else if days < 30 {
        format!("{} days ago", days)
    } else if days < 365 {
        let months = days / 30;
        if months == 1 {
            "1 month ago".to_string()
        } else {
            format!("{} months ago", months)
        }
    } else {
        let years = days / 365;
        if years == 1 {
            "1 year ago".to_string()
        } else {
            format!("{} years ago", years)
        }
    }
}

fn to_digest_claim(claim: &Claim, now_unix: i64) -> DigestClaim {
    DigestClaim {
        text: claim.text.clone(),
        category: claim.category,
        confidence: claim.confidence,
        age: format_age(claim.extracted_at.as_deref(), now_unix),
    }
}

fn entity_count_unique(claims: &[Claim]) -> u32 {
    let mut seen: Vec<String> = Vec::new();
    for c in claims {
        for e in &c.entities {
            let key = normalize_entity_name(&e.name);
            if !seen.contains(&key) {
                seen.push(key);
            }
        }
    }
    seen.len() as u32
}

fn max_extracted_at_unix(claims: &[Claim]) -> u64 {
    let mut max: i64 = 0;
    for c in claims {
        if let Some(ts) = c.extracted_at.as_deref().and_then(parse_iso_to_unix) {
            if ts > max {
                max = ts;
            }
        }
    }
    if max < 0 {
        0
    } else {
        max as u64
    }
}

/// Collect unique project names from claims.
/// Sort: by frequency DESC, then alphabetically (normalized).
/// Preserves the first-seen display form for each normalized key.
fn collect_projects(claims: &[Claim], limit: usize) -> Vec<String> {
    let mut entries: Vec<(String, String, usize)> = Vec::new(); // (normalized, display, count)
    for c in claims {
        for e in &c.entities {
            if e.entity_type != EntityType::Project {
                continue;
            }
            let norm = normalize_entity_name(&e.name);
            if norm.is_empty() {
                continue;
            }
            match entries.iter_mut().find(|(n, _, _)| *n == norm) {
                Some(entry) => entry.2 += 1,
                None => entries.push((norm, e.name.clone(), 1)),
            }
        }
    }
    entries.sort_by(|a, b| b.2.cmp(&a.2).then_with(|| a.0.cmp(&b.0)));
    entries
        .into_iter()
        .take(limit)
        .map(|(_, display, _)| display)
        .collect()
}

fn format_template_prompt(
    top_claims: &[DigestClaim],
    recent_decisions: &[DigestClaim],
    active_projects: &[String],
) -> String {
    let mut out = String::new();
    if !top_claims.is_empty() {
        out.push_str("Known facts about you:\n");
        for (i, c) in top_claims.iter().enumerate() {
            out.push_str(&format!("{}. {}\n", i + 1, c.text));
        }
    }
    if !recent_decisions.is_empty() {
        if !out.is_empty() {
            out.push('\n');
        }
        out.push_str("Recent decisions:\n");
        for d in recent_decisions {
            out.push_str(&format!("- {}\n", d.text));
        }
    }
    if !active_projects.is_empty() {
        if !out.is_empty() {
            out.push('\n');
        }
        out.push_str(&format!("Active projects: {}\n", active_projects.join(", ")));
    }
    let trimmed = out.trim_end().to_string();
    if trimmed.len() > PROMPT_TEXT_MAX_CHARS {
        trimmed[..PROMPT_TEXT_MAX_CHARS].to_string()
    } else {
        trimmed
    }
}

/// Build a template-based digest from active claims. No LLM required.
/// Used by MCP server and as a fallback for clients without an LLM key.
pub fn build_template_digest(active_claims: &[Claim], now_unix_seconds: i64) -> Digest {
    if active_claims.is_empty() {
        return Digest {
            version: 0,
            compiled_at: unix_to_iso(now_unix_seconds),
            fact_count: 0,
            entity_count: 0,
            contradiction_count: 0,
            identity: String::new(),
            top_claims: Vec::new(),
            recent_decisions: Vec::new(),
            active_projects: Vec::new(),
            active_contradictions: 0,
            prompt_text: EMPTY_VAULT_PROMPT.to_string(),
        };
    }

    let mut ranked: Vec<(usize, f64)> = active_claims
        .iter()
        .enumerate()
        .map(|(i, c)| (i, score(c, now_unix_seconds)))
        .collect();
    ranked.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(&b.0))
    });

    let top_claims: Vec<DigestClaim> = ranked
        .iter()
        .take(TEMPLATE_TOP_N)
        .map(|(i, _)| to_digest_claim(&active_claims[*i], now_unix_seconds))
        .collect();

    let mut decisions: Vec<(usize, i64)> = active_claims
        .iter()
        .enumerate()
        .filter(|(_, c)| c.category == ClaimCategory::Decision)
        .map(|(i, c)| {
            let ts = c
                .extracted_at
                .as_deref()
                .and_then(parse_iso_to_unix)
                .unwrap_or(i64::MIN);
            (i, ts)
        })
        .collect();
    decisions.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    let recent_decisions: Vec<DigestClaim> = decisions
        .iter()
        .take(TEMPLATE_RECENT_DECISIONS_N)
        .map(|(i, _)| to_digest_claim(&active_claims[*i], now_unix_seconds))
        .collect();

    let active_projects = collect_projects(active_claims, TEMPLATE_MAX_PROJECTS);

    let prompt_text = format_template_prompt(&top_claims, &recent_decisions, &active_projects);

    Digest {
        version: max_extracted_at_unix(active_claims),
        compiled_at: unix_to_iso(now_unix_seconds),
        fact_count: active_claims.len() as u32,
        entity_count: entity_count_unique(active_claims),
        contradiction_count: 0,
        identity: String::new(),
        top_claims,
        recent_decisions,
        active_projects,
        active_contradictions: 0,
        prompt_text,
    }
}

/// Build the LLM prompt for compiling a narrative digest.
///
/// Panics if `active_claims` is empty — callers must check and fall back to the
/// template path (or empty-vault handling) before calling this.
pub fn build_digest_prompt(active_claims: &[Claim]) -> String {
    assert!(
        !active_claims.is_empty(),
        "build_digest_prompt requires at least one claim; callers must handle the empty-vault case separately"
    );

    let now = Utc::now().timestamp();
    let mut claims_block = String::new();
    for (i, c) in active_claims.iter().enumerate() {
        let cat = match c.category {
            ClaimCategory::Fact => "fact",
            ClaimCategory::Preference => "preference",
            ClaimCategory::Decision => "decision",
            ClaimCategory::Episodic => "episodic",
            ClaimCategory::Goal => "goal",
            ClaimCategory::Context => "context",
            ClaimCategory::Summary => "summary",
            ClaimCategory::Rule => "rule",
            ClaimCategory::Entity => "entity",
            ClaimCategory::Digest => "digest",
        };
        let age = format_age(c.extracted_at.as_deref(), now);
        claims_block.push_str(&format!(
            "[{}] ({}, conf {:.2}, {}) {}\n",
            i + 1,
            cat,
            c.confidence,
            age,
            c.text
        ));
    }

    format!(
        r#"You are compiling a knowledge digest for a user from their extracted memory claims.
Your output will be injected into future AI agent conversations as identity context,
so it must be accurate, concise, and first-person ("you are...").

Here are the active claims:

{claims_block}
Produce a JSON object with this exact schema. Return ONLY the JSON, no markdown fences.

{{
  "identity": "a 1-2 sentence description of the user in second person. e.g. 'You are a software engineer in Lisbon working at Acme Corp on skynet-lite.'",
  "top_claim_indices": [1, 5, 3],
  "recent_decision_indices": [2, 8],
  "active_project_names": ["skynet-lite"]
}}

Rules:
- identity must be in second person ("You are...")
- Prefer claims that describe ongoing state (preferences, roles, projects) over one-off facts
- Top claims should cover: role, location, employer, current projects, key preferences, recent decisions
- If there are fewer than 10 active claims, return fewer indices
- Indices are 1-based, matching the [N] markers above
- Never invent information not in the claims"#
    )
}

/// Parsed response from the LLM digest compilation call.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct ParsedDigestResponse {
    pub identity: String,
    pub top_claim_indices: Vec<usize>,
    pub recent_decision_indices: Vec<usize>,
    pub active_project_names: Vec<String>,
}

fn strip_code_fences(s: &str) -> String {
    let mut result = s.trim().to_string();
    if result.starts_with("```") {
        if let Some(pos) = result.find('\n') {
            result = result[pos + 1..].to_string();
        }
        if result.ends_with("```") {
            result = result[..result.len() - 3].trim_end().to_string();
        }
    }
    result
}

fn parse_index_array(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<Vec<usize>, String> {
    let arr = match obj.get(key) {
        Some(serde_json::Value::Array(a)) => a,
        Some(_) => return Err(format!("field `{}` must be an array", key)),
        None => return Ok(Vec::new()),
    };
    let mut out = Vec::with_capacity(arr.len());
    for v in arr {
        let n = v
            .as_u64()
            .ok_or_else(|| format!("field `{}` must contain positive integers", key))?;
        if n == 0 {
            return Err(format!(
                "field `{}` contains zero; indices are 1-based",
                key
            ));
        }
        out.push(n as usize);
    }
    Ok(out)
}

fn parse_string_array(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<Vec<String>, String> {
    let arr = match obj.get(key) {
        Some(serde_json::Value::Array(a)) => a,
        Some(_) => return Err(format!("field `{}` must be an array", key)),
        None => return Ok(Vec::new()),
    };
    let mut out = Vec::with_capacity(arr.len());
    for v in arr {
        let s = v
            .as_str()
            .ok_or_else(|| format!("field `{}` must contain strings", key))?;
        if !s.is_empty() {
            out.push(s.to_string());
        }
    }
    Ok(out)
}

/// Parse the LLM digest response into a `ParsedDigestResponse`.
pub fn parse_digest_response(raw: &str) -> Result<ParsedDigestResponse, String> {
    let cleaned = strip_code_fences(raw);
    let value: serde_json::Value = serde_json::from_str(&cleaned)
        .map_err(|e| format!("digest response is not valid JSON: {}", e))?;
    let obj = value
        .as_object()
        .ok_or_else(|| "digest response must be a JSON object".to_string())?;

    let identity = obj
        .get("identity")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "digest response missing `identity` string field".to_string())?
        .to_string();
    if identity.is_empty() {
        return Err("digest response `identity` must be non-empty".to_string());
    }

    let top_claim_indices = parse_index_array(obj, "top_claim_indices")?;
    let recent_decision_indices = parse_index_array(obj, "recent_decision_indices")?;
    let active_project_names = parse_string_array(obj, "active_project_names")?;

    Ok(ParsedDigestResponse {
        identity,
        top_claim_indices,
        recent_decision_indices,
        active_project_names,
    })
}

fn format_llm_prompt(
    identity: &str,
    top_claims: &[DigestClaim],
    recent_decisions: &[DigestClaim],
    active_projects: &[String],
) -> String {
    let mut out = String::new();
    out.push_str(identity.trim());
    if !top_claims.is_empty() {
        out.push_str("\n\nKey facts:\n");
        for c in top_claims {
            out.push_str(&format!("- {}\n", c.text));
        }
    }
    if !recent_decisions.is_empty() {
        out.push_str("\nRecent decisions:\n");
        for d in recent_decisions {
            out.push_str(&format!("- {}\n", d.text));
        }
    }
    if !active_projects.is_empty() {
        out.push_str(&format!("\nActive projects: {}\n", active_projects.join(", ")));
    }
    let trimmed = out.trim_end().to_string();
    if trimmed.len() > PROMPT_TEXT_MAX_CHARS {
        trimmed[..PROMPT_TEXT_MAX_CHARS].to_string()
    } else {
        trimmed
    }
}

/// Combine a parsed LLM response with the source claims into a full `Digest`.
pub fn assemble_digest_from_llm(
    parsed: &ParsedDigestResponse,
    active_claims: &[Claim],
    now_unix_seconds: i64,
) -> Result<Digest, String> {
    let n = active_claims.len();
    let check = |idx: usize, label: &str| -> Result<(), String> {
        if idx == 0 || idx > n {
            Err(format!(
                "{} index {} out of range (1..={})",
                label, idx, n
            ))
        } else {
            Ok(())
        }
    };
    for &i in &parsed.top_claim_indices {
        check(i, "top_claim_indices")?;
    }
    for &i in &parsed.recent_decision_indices {
        check(i, "recent_decision_indices")?;
    }

    let top_claims: Vec<DigestClaim> = parsed
        .top_claim_indices
        .iter()
        .map(|i| to_digest_claim(&active_claims[i - 1], now_unix_seconds))
        .collect();

    let recent_decisions: Vec<DigestClaim> = parsed
        .recent_decision_indices
        .iter()
        .map(|i| to_digest_claim(&active_claims[i - 1], now_unix_seconds))
        .collect();

    let prompt_text = format_llm_prompt(
        &parsed.identity,
        &top_claims,
        &recent_decisions,
        &parsed.active_project_names,
    );

    Ok(Digest {
        version: max_extracted_at_unix(active_claims),
        compiled_at: unix_to_iso(now_unix_seconds),
        fact_count: active_claims.len() as u32,
        entity_count: entity_count_unique(active_claims),
        contradiction_count: 0,
        identity: parsed.identity.clone(),
        top_claims,
        recent_decisions,
        active_projects: parsed.active_project_names.clone(),
        active_contradictions: 0,
        prompt_text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claims::{ClaimStatus, EntityRef};

    // A fixed "now" for deterministic age calculations in tests.
    // 2026-04-12T12:00:00Z
    const NOW: i64 = 1776340800;

    fn claim(
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

    fn proj(name: &str) -> EntityRef {
        EntityRef {
            name: name.to_string(),
            entity_type: EntityType::Project,
            role: None,
        }
    }

    fn tool(name: &str) -> EntityRef {
        EntityRef {
            name: name.to_string(),
            entity_type: EntityType::Tool,
            role: None,
        }
    }

    // Subtract `days` from NOW, return ISO 8601 string.
    fn days_ago(days: i64) -> String {
        unix_to_iso(NOW - days * 86400)
    }

    // ===========================================================
    // Age formatting
    // ===========================================================

    #[test]
    fn test_format_age_none() {
        assert_eq!(format_age(None, NOW), "unknown");
    }

    #[test]
    fn test_format_age_today_zero_days() {
        let t = days_ago(0);
        assert_eq!(format_age(Some(&t), NOW), "today");
    }

    #[test]
    fn test_format_age_yesterday() {
        let t = days_ago(1);
        assert_eq!(format_age(Some(&t), NOW), "yesterday");
    }

    #[test]
    fn test_format_age_2_days() {
        let t = days_ago(2);
        assert_eq!(format_age(Some(&t), NOW), "2 days ago");
    }

    #[test]
    fn test_format_age_45_days_floor_months() {
        // floor(45/30) = 1 -> singular form
        let t = days_ago(45);
        assert_eq!(format_age(Some(&t), NOW), "1 month ago");
    }

    #[test]
    fn test_format_age_90_days_plural_months() {
        // floor(90/30) = 3
        let t = days_ago(90);
        assert_eq!(format_age(Some(&t), NOW), "3 months ago");
    }

    #[test]
    fn test_format_age_400_days_years() {
        // floor(400/365) = 1 -> singular form
        let t = days_ago(400);
        assert_eq!(format_age(Some(&t), NOW), "1 year ago");
    }

    #[test]
    fn test_format_age_800_days_plural_years() {
        // floor(800/365) = 2
        let t = days_ago(800);
        assert_eq!(format_age(Some(&t), NOW), "2 years ago");
    }

    #[test]
    fn test_format_age_future_no_negative() {
        // 10 days in the future -> "today", not a negative string
        let future = unix_to_iso(NOW + 10 * 86400);
        assert_eq!(format_age(Some(&future), NOW), "today");
    }

    #[test]
    fn test_format_age_malformed_iso() {
        assert_eq!(format_age(Some("not-a-date"), NOW), "unknown");
    }

    // ===========================================================
    // Template digest
    // ===========================================================

    #[test]
    fn test_template_empty_vault() {
        let d = build_template_digest(&[], NOW);
        assert_eq!(d.prompt_text, EMPTY_VAULT_PROMPT);
        assert_eq!(d.fact_count, 0);
        assert_eq!(d.entity_count, 0);
        assert_eq!(d.contradiction_count, 0);
        assert_eq!(d.active_contradictions, 0);
        assert_eq!(d.version, 0);
        assert!(d.top_claims.is_empty());
        assert!(d.recent_decisions.is_empty());
        assert!(d.active_projects.is_empty());
        assert_eq!(d.identity, "");
        // compiled_at is still populated
        assert!(!d.compiled_at.is_empty());
    }

    #[test]
    fn test_template_single_claim() {
        let claims = vec![claim(
            "prefers PostgreSQL",
            ClaimCategory::Preference,
            8,
            Some(&days_ago(1)),
            vec![tool("PostgreSQL")],
        )];
        let d = build_template_digest(&claims, NOW);
        assert_eq!(d.fact_count, 1);
        assert_eq!(d.top_claims.len(), 1);
        assert_eq!(d.top_claims[0].text, "prefers PostgreSQL");
        assert!(d.recent_decisions.is_empty());
        assert!(d.active_projects.is_empty());
        // Prompt has facts section but no "Recent decisions:" or "Active projects:"
        assert!(d.prompt_text.contains("Known facts about you:"));
        assert!(!d.prompt_text.contains("Recent decisions:"));
        assert!(!d.prompt_text.contains("Active projects:"));
    }

    #[test]
    fn test_template_caps_top_at_10() {
        let mut claims = Vec::new();
        for i in 0..15 {
            claims.push(claim(
                &format!("fact {}", i),
                ClaimCategory::Fact,
                8,
                Some(&days_ago(1)),
                Vec::new(),
            ));
        }
        let d = build_template_digest(&claims, NOW);
        assert_eq!(d.fact_count, 15);
        assert_eq!(d.top_claims.len(), 10);
    }

    #[test]
    fn test_template_missing_extracted_at_ranks_last() {
        let claims = vec![
            claim(
                "no timestamp",
                ClaimCategory::Fact,
                10, // very high importance
                None,
                Vec::new(),
            ),
            claim(
                "has timestamp",
                ClaimCategory::Fact,
                5, // lower importance
                Some(&days_ago(1)),
                Vec::new(),
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        // Even though "no timestamp" has higher importance, its recency_weight
        // is ~0 (days_since = 10000), so "has timestamp" ranks higher.
        assert_eq!(d.top_claims[0].text, "has timestamp");
        assert_eq!(d.top_claims[1].text, "no timestamp");
    }

    #[test]
    fn test_template_recent_decisions_sorted_by_recency() {
        let claims = vec![
            claim(
                "decision A (oldest)",
                ClaimCategory::Decision,
                5,
                Some(&days_ago(30)),
                Vec::new(),
            ),
            claim(
                "decision B (newest)",
                ClaimCategory::Decision,
                5,
                Some(&days_ago(1)),
                Vec::new(),
            ),
            claim(
                "decision C (middle)",
                ClaimCategory::Decision,
                5,
                Some(&days_ago(10)),
                Vec::new(),
            ),
            // Non-decision should be ignored for recent_decisions
            claim(
                "some fact",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(0)),
                Vec::new(),
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        assert_eq!(d.recent_decisions.len(), 3);
        assert_eq!(d.recent_decisions[0].text, "decision B (newest)");
        assert_eq!(d.recent_decisions[1].text, "decision C (middle)");
        assert_eq!(d.recent_decisions[2].text, "decision A (oldest)");
    }

    #[test]
    fn test_template_projects_deduped_case_insensitive() {
        let claims = vec![
            claim(
                "working on skynet",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(1)),
                vec![proj("Skynet-Lite")],
            ),
            claim(
                "shipped skynet feature",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(2)),
                vec![proj("skynet-lite")],
            ),
            claim(
                "new project",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(3)),
                vec![proj("Acme-Dashboard")],
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        assert_eq!(d.active_projects.len(), 2);
        // Skynet-Lite appears twice -> ranks first by frequency
        assert_eq!(d.active_projects[0], "Skynet-Lite");
        assert_eq!(d.active_projects[1], "Acme-Dashboard");
    }

    #[test]
    fn test_template_entity_count_case_insensitive() {
        let claims = vec![
            claim(
                "a",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(1)),
                vec![tool("PostgreSQL")],
            ),
            claim(
                "b",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(1)),
                vec![tool("postgresql"), tool("Redis")],
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        // "PostgreSQL"/"postgresql" -> 1 entity, plus "Redis" -> 2
        assert_eq!(d.entity_count, 2);
    }

    #[test]
    fn test_template_prompt_truncated_when_too_long() {
        // Construct many very long facts so prompt exceeds 2000 chars.
        let long_text = "x".repeat(400);
        let claims: Vec<Claim> = (0..10)
            .map(|i| {
                claim(
                    &format!("{}-{}", long_text, i),
                    ClaimCategory::Fact,
                    5,
                    Some(&days_ago(1)),
                    Vec::new(),
                )
            })
            .collect();
        let d = build_template_digest(&claims, NOW);
        assert!(d.prompt_text.len() <= PROMPT_TEXT_MAX_CHARS);
        assert_eq!(d.prompt_text.len(), PROMPT_TEXT_MAX_CHARS);
    }

    #[test]
    fn test_template_omits_decisions_line_when_none() {
        let claims = vec![claim(
            "just a fact",
            ClaimCategory::Fact,
            5,
            Some(&days_ago(1)),
            Vec::new(),
        )];
        let d = build_template_digest(&claims, NOW);
        assert!(!d.prompt_text.contains("Recent decisions:"));
    }

    #[test]
    fn test_template_omits_projects_line_when_none() {
        let claims = vec![claim(
            "just a fact",
            ClaimCategory::Fact,
            5,
            Some(&days_ago(1)),
            Vec::new(),
        )];
        let d = build_template_digest(&claims, NOW);
        assert!(!d.prompt_text.contains("Active projects:"));
    }

    #[test]
    fn test_template_golden_prompt_text() {
        // Byte-level golden test. 3 fixed claims -> exact expected prompt_text.
        let claims = vec![
            claim(
                "lives in Lisbon",
                ClaimCategory::Fact,
                9,
                Some(&days_ago(1)),
                Vec::new(),
            ),
            claim(
                "chose PostgreSQL over MySQL because relational modeling is cleaner",
                ClaimCategory::Decision,
                8,
                Some(&days_ago(2)),
                Vec::new(),
            ),
            claim(
                "works on skynet-lite",
                ClaimCategory::Fact,
                7,
                Some(&days_ago(3)),
                vec![proj("skynet-lite")],
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        let expected = "Known facts about you:\n\
1. lives in Lisbon\n\
2. chose PostgreSQL over MySQL because relational modeling is cleaner\n\
3. works on skynet-lite\n\
\n\
Recent decisions:\n\
- chose PostgreSQL over MySQL because relational modeling is cleaner\n\
\n\
Active projects: skynet-lite";
        assert_eq!(d.prompt_text, expected);
    }

    #[test]
    fn test_template_version_is_max_extracted_at() {
        let claims = vec![
            claim(
                "older",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(10)),
                Vec::new(),
            ),
            claim(
                "newest",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(1)),
                Vec::new(),
            ),
            claim(
                "middle",
                ClaimCategory::Fact,
                5,
                Some(&days_ago(5)),
                Vec::new(),
            ),
        ];
        let d = build_template_digest(&claims, NOW);
        let expected = (NOW - 86400) as u64;
        assert_eq!(d.version, expected);
    }

    #[test]
    fn test_template_version_zero_when_no_timestamps() {
        let claims = vec![claim("x", ClaimCategory::Fact, 5, None, Vec::new())];
        let d = build_template_digest(&claims, NOW);
        assert_eq!(d.version, 0);
    }

    // ===========================================================
    // LLM prompt builder
    // ===========================================================

    #[test]
    #[should_panic(expected = "build_digest_prompt requires at least one claim")]
    fn test_build_prompt_panics_on_empty() {
        let _ = build_digest_prompt(&[]);
    }

    #[test]
    fn test_build_prompt_contains_markers_and_text() {
        let claims = vec![
            claim("alpha", ClaimCategory::Fact, 5, Some(&days_ago(1)), vec![]),
            claim("beta", ClaimCategory::Preference, 5, Some(&days_ago(2)), vec![]),
            claim("gamma", ClaimCategory::Decision, 5, Some(&days_ago(3)), vec![]),
        ];
        let p = build_digest_prompt(&claims);
        assert!(p.contains("[1]"));
        assert!(p.contains("[2]"));
        assert!(p.contains("[3]"));
        assert!(p.contains("alpha"));
        assert!(p.contains("beta"));
        assert!(p.contains("gamma"));
    }

    #[test]
    fn test_build_prompt_contains_schema_and_rules() {
        let claims = vec![claim("a", ClaimCategory::Fact, 5, None, vec![])];
        let p = build_digest_prompt(&claims);
        assert!(p.contains("\"identity\""));
        assert!(p.contains("\"top_claim_indices\""));
        assert!(p.contains("\"recent_decision_indices\""));
        assert!(p.contains("\"active_project_names\""));
        assert!(p.contains("second person"));
    }

    #[test]
    fn test_build_prompt_deterministic_structure() {
        // Two back-to-back calls produce identical output (age resolves
        // against Utc::now() at call time; millisecond drift between two
        // tight calls shouldn't cross a day boundary).
        let claims = vec![claim(
            "alpha",
            ClaimCategory::Fact,
            5,
            Some("2026-01-01T00:00:00Z"),
            vec![],
        )];
        let p1 = build_digest_prompt(&claims);
        let p2 = build_digest_prompt(&claims);
        assert_eq!(p1, p2);
        // Structural markers are present regardless of clock.
        assert!(p1.contains("[1] (fact, conf 0.90,"));
        assert!(p1.contains("alpha"));
    }

    #[test]
    fn test_build_prompt_large_claim_set_no_crash() {
        let claims: Vec<Claim> = (0..500)
            .map(|i| {
                claim(
                    &format!("claim {}", i),
                    ClaimCategory::Fact,
                    5,
                    Some(&days_ago(1)),
                    vec![],
                )
            })
            .collect();
        let p = build_digest_prompt(&claims);
        assert!(p.contains("[500]"));
    }

    // ===========================================================
    // LLM response parser
    // ===========================================================

    #[test]
    fn test_parse_valid_response() {
        let raw = r#"{
            "identity": "You are a software engineer in Lisbon.",
            "top_claim_indices": [1, 3, 5],
            "recent_decision_indices": [2],
            "active_project_names": ["skynet-lite", "acme-dashboard"]
        }"#;
        let r = parse_digest_response(raw).unwrap();
        assert_eq!(r.identity, "You are a software engineer in Lisbon.");
        assert_eq!(r.top_claim_indices, vec![1, 3, 5]);
        assert_eq!(r.recent_decision_indices, vec![2]);
        assert_eq!(
            r.active_project_names,
            vec!["skynet-lite".to_string(), "acme-dashboard".to_string()]
        );
    }

    #[test]
    fn test_parse_fenced_json() {
        let raw = "```json\n{\"identity\":\"You are X.\",\"top_claim_indices\":[1],\"recent_decision_indices\":[],\"active_project_names\":[]}\n```";
        let r = parse_digest_response(raw).unwrap();
        assert_eq!(r.identity, "You are X.");
        assert_eq!(r.top_claim_indices, vec![1]);
    }

    #[test]
    fn test_parse_missing_identity_err() {
        let raw = r#"{"top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":[]}"#;
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("identity"));
    }

    #[test]
    fn test_parse_empty_identity_err() {
        let raw = r#"{"identity":"","top_claim_indices":[1],"recent_decision_indices":[],"active_project_names":[]}"#;
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("non-empty"));
    }

    #[test]
    fn test_parse_empty_top_indices_ok() {
        // Rule: allow empty arrays
        let raw = r#"{"identity":"You are X.","top_claim_indices":[],"recent_decision_indices":[],"active_project_names":[]}"#;
        let r = parse_digest_response(raw).unwrap();
        assert!(r.top_claim_indices.is_empty());
    }

    #[test]
    fn test_parse_malformed_json_err() {
        let raw = "{not valid";
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("valid JSON"));
    }

    #[test]
    fn test_parse_non_integer_index_err() {
        let raw = r#"{"identity":"You are X.","top_claim_indices":["one"],"recent_decision_indices":[],"active_project_names":[]}"#;
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("positive integers"));
    }

    #[test]
    fn test_parse_zero_index_err() {
        let raw = r#"{"identity":"You are X.","top_claim_indices":[0],"recent_decision_indices":[],"active_project_names":[]}"#;
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("1-based"));
    }

    #[test]
    fn test_parse_non_object_err() {
        let raw = "[1,2,3]";
        let err = parse_digest_response(raw).unwrap_err();
        assert!(err.contains("object"));
    }

    // ===========================================================
    // Assembly
    // ===========================================================

    fn sample_claims_for_assembly() -> Vec<Claim> {
        vec![
            claim(
                "lives in Lisbon",
                ClaimCategory::Fact,
                9,
                Some(&days_ago(5)),
                vec![],
            ),
            claim(
                "chose PostgreSQL because schema stability matters",
                ClaimCategory::Decision,
                8,
                Some(&days_ago(2)),
                vec![],
            ),
            claim(
                "prefers dark mode",
                ClaimCategory::Preference,
                4,
                Some(&days_ago(10)),
                vec![],
            ),
        ]
    }

    #[test]
    fn test_assemble_valid_response() {
        let claims = sample_claims_for_assembly();
        let parsed = ParsedDigestResponse {
            identity: "You are a software engineer in Lisbon.".to_string(),
            top_claim_indices: vec![1, 3],
            recent_decision_indices: vec![2],
            active_project_names: vec!["skynet-lite".to_string()],
        };
        let d = assemble_digest_from_llm(&parsed, &claims, NOW).unwrap();
        assert_eq!(d.identity, "You are a software engineer in Lisbon.");
        assert_eq!(d.top_claims.len(), 2);
        assert_eq!(d.top_claims[0].text, "lives in Lisbon");
        assert_eq!(d.top_claims[1].text, "prefers dark mode");
        assert_eq!(d.recent_decisions.len(), 1);
        assert_eq!(
            d.recent_decisions[0].text,
            "chose PostgreSQL because schema stability matters"
        );
        assert_eq!(d.active_projects, vec!["skynet-lite".to_string()]);
        assert_eq!(d.fact_count, 3);
    }

    #[test]
    fn test_assemble_out_of_range_err() {
        let claims = sample_claims_for_assembly();
        let parsed = ParsedDigestResponse {
            identity: "You are X.".to_string(),
            top_claim_indices: vec![1, 99],
            recent_decision_indices: vec![],
            active_project_names: vec![],
        };
        let err = assemble_digest_from_llm(&parsed, &claims, NOW).unwrap_err();
        assert!(err.contains("out of range"));
    }

    #[test]
    fn test_assemble_zero_index_err() {
        let claims = sample_claims_for_assembly();
        let parsed = ParsedDigestResponse {
            identity: "You are X.".to_string(),
            top_claim_indices: vec![0],
            recent_decision_indices: vec![],
            active_project_names: vec![],
        };
        let err = assemble_digest_from_llm(&parsed, &claims, NOW).unwrap_err();
        assert!(err.contains("out of range"));
    }

    #[test]
    fn test_assemble_identity_verbatim_in_prompt_and_field() {
        let claims = sample_claims_for_assembly();
        let parsed = ParsedDigestResponse {
            identity: "You are a software engineer in Lisbon.".to_string(),
            top_claim_indices: vec![1],
            recent_decision_indices: vec![2],
            active_project_names: vec!["skynet-lite".to_string()],
        };
        let d = assemble_digest_from_llm(&parsed, &claims, NOW).unwrap();
        assert_eq!(d.identity, "You are a software engineer in Lisbon.");
        assert!(d
            .prompt_text
            .starts_with("You are a software engineer in Lisbon."));
    }

    #[test]
    fn test_assemble_prompt_differs_from_template() {
        let claims = sample_claims_for_assembly();
        let parsed = ParsedDigestResponse {
            identity: "You are a software engineer in Lisbon.".to_string(),
            top_claim_indices: vec![1, 3],
            recent_decision_indices: vec![2],
            active_project_names: vec!["skynet-lite".to_string()],
        };
        let llm_digest = assemble_digest_from_llm(&parsed, &claims, NOW).unwrap();
        let tpl_digest = build_template_digest(&claims, NOW);
        assert_ne!(llm_digest.prompt_text, tpl_digest.prompt_text);
        assert!(llm_digest.prompt_text.contains("You are a software engineer"));
        assert!(!tpl_digest.prompt_text.contains("You are a software engineer"));
        assert!(llm_digest.prompt_text.contains("Key facts:"));
        assert!(tpl_digest.prompt_text.contains("Known facts about you:"));
    }
}
