//! Secret detection — 14 prefix-anchored pattern classes for common API keys.
//!
//! am-2 post_tool_use hooks call [`redact_secrets`] before storing any tool
//! output. Only [`SecretMatch::last_4`] and [`SecretMatch::observed_context`]
//! are retained — the raw secret value is never stored.

/// Wire value for `code_subtype` on claims produced from secret-detection events.
pub const CODE_SUBTYPE_SECRET: &str = "secret";

// ---------------------------------------------------------------------------
// SecretKind
// ---------------------------------------------------------------------------

/// Classification of a detected secret (14 pattern classes).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretKind {
    /// Anthropic API key (`sk-ant-…`, ≥ 80 chars after prefix).
    AnthropicApiKey,
    /// OpenAI project key (`sk-proj-…`, ≥ 40 chars after prefix).
    OpenAiKeyNew,
    /// OpenAI legacy key (`sk-…` not matching `ant-`/`proj-`, ~48 chars).
    OpenAiKeyLegacy,
    /// GitHub classic PAT (`ghp_…`, ~36 alphanum chars).
    GitHubPat,
    /// GitHub user-to-server token (`ghu_…`, ~36 alphanum chars).
    GitHubUserToken,
    /// GitHub server-to-server token (`ghs_…`, ~36 alphanum chars).
    GitHubServerToken,
    /// AWS access key ID (`AKIA…`, exactly 16 uppercase alphanum chars).
    AwsAccessKeyId,
    /// JSON Web Token (`eyJ…eyJ…`, two base64url segments).
    Jwt,
    /// Stripe live secret key (`sk_live_…`, ≥ 20 chars after prefix).
    StripeLiveKey,
    /// Stripe test secret key (`sk_test_…`, ≥ 20 chars after prefix).
    StripeTestKey,
    /// HuggingFace user access token (`hf_…`, ≥ 30 chars after prefix).
    HuggingFaceToken,
    /// SendGrid API key (`SG.`, exactly 22 + 43 base64url chars).
    SendGridApiKey,
    /// Twilio API key SID (`SK` + exactly 32 lowercase hex chars).
    TwilioApiKey,
    /// Slack bot/user token (`xoxb-`/`xoxp-`/`xoxa-`/`xoxs-`, ≥ 24 chars).
    SlackToken,
}

impl SecretKind {
    /// Stable string tag used in serialized claim metadata.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AnthropicApiKey => "anthropic_api_key",
            Self::OpenAiKeyNew => "openai_api_key_new",
            Self::OpenAiKeyLegacy => "openai_api_key_legacy",
            Self::GitHubPat => "github_pat",
            Self::GitHubUserToken => "github_user_token",
            Self::GitHubServerToken => "github_server_token",
            Self::AwsAccessKeyId => "aws_access_key_id",
            Self::Jwt => "jwt",
            Self::StripeLiveKey => "stripe_live_key",
            Self::StripeTestKey => "stripe_test_key",
            Self::HuggingFaceToken => "huggingface_token",
            Self::SendGridApiKey => "sendgrid_api_key",
            Self::TwilioApiKey => "twilio_api_key",
            Self::SlackToken => "slack_token",
        }
    }
}

impl std::fmt::Display for SecretKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

// ---------------------------------------------------------------------------
// SecretMatch
// ---------------------------------------------------------------------------

/// A detected secret — stores only safe metadata, never the raw value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretMatch {
    /// Pattern class that matched.
    pub kind: SecretKind,
    /// Last 4 characters of the matched token (for correlation without exposure).
    pub last_4: String,
    /// Up to 80 chars of surrounding text with the match replaced by `***`.
    pub observed_context: String,
    /// Byte offset of the match start in the original text.
    pub start: usize,
    /// Byte offset of the match end (exclusive) in the original text.
    pub end: usize,
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Scan `text` and return all detected secrets.
///
/// Matches are sorted by start position. Overlapping spans (e.g. a generic
/// `sk-` match and a more-specific `sk-ant-` match on the same token) are
/// deduplicated: the longer (more-specific) match wins.
pub fn detect_secrets(text: &str) -> Vec<SecretMatch> {
    let mut raw: Vec<(usize, usize, SecretKind)> = Vec::new();

    // Patterns ordered from most- to least-specific so dedup keeps the right one.
    collect(&mut raw, text, "sk-ant-", 80, 200, is_api_key_char, SecretKind::AnthropicApiKey);
    collect(&mut raw, text, "sk-proj-", 40, 200, is_api_key_char, SecretKind::OpenAiKeyNew);
    collect_openai_legacy(&mut raw, text);
    collect(&mut raw, text, "ghp_", 36, 40, char::is_alphanumeric, SecretKind::GitHubPat);
    collect(&mut raw, text, "ghu_", 36, 40, char::is_alphanumeric, SecretKind::GitHubUserToken);
    collect(&mut raw, text, "ghs_", 36, 40, char::is_alphanumeric, SecretKind::GitHubServerToken);
    collect_aws_akia(&mut raw, text);
    collect_jwt(&mut raw, text);
    collect(&mut raw, text, "sk_live_", 20, 120, char::is_alphanumeric, SecretKind::StripeLiveKey);
    collect(&mut raw, text, "sk_test_", 20, 120, char::is_alphanumeric, SecretKind::StripeTestKey);
    collect(&mut raw, text, "hf_", 30, 80, char::is_alphanumeric, SecretKind::HuggingFaceToken);
    collect_sendgrid(&mut raw, text);
    collect_twilio(&mut raw, text);
    collect_slack(&mut raw, text);

    // Sort by start, then dedup overlapping spans (keep longer).
    raw.sort_unstable_by_key(|(s, e, _)| (*s, usize::MAX - *e));
    let raw = dedup_overlapping(raw);

    raw.into_iter()
        .map(|(start, end, kind)| build_match(text, start, end, kind))
        .collect()
}

/// Replace every detected secret in `text` with `[REDACTED]`.
pub fn redact_secrets(text: &str) -> String {
    let matches = detect_secrets(text);
    if matches.is_empty() {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    for m in &matches {
        out.push_str(&text[pos..m.start]);
        out.push_str("[REDACTED]");
        pos = m.end;
    }
    out.push_str(&text[pos..]);
    out
}

// ---------------------------------------------------------------------------
// Char validators
// ---------------------------------------------------------------------------

fn is_api_key_char(c: char) -> bool {
    c.is_alphanumeric() || c == '-' || c == '_'
}

fn is_base64url(c: char) -> bool {
    c.is_alphanumeric() || c == '-' || c == '_' || c == '+' || c == '/'
}

fn is_sendgrid_body(c: char) -> bool {
    c.is_alphanumeric() || c == '-' || c == '_'
}

// ---------------------------------------------------------------------------
// Prefix-anchor scanner
// ---------------------------------------------------------------------------

fn collect(
    out: &mut Vec<(usize, usize, SecretKind)>,
    text: &str,
    prefix: &str,
    min_body: usize,
    max_body: usize,
    body_valid: fn(char) -> bool,
    kind: SecretKind,
) {
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                let body_start = start + prefix.len();
                let body_end = find_body_end(text, body_start, body_valid);
                let body_len = body_end - body_start;

                if body_len >= min_body && body_len <= max_body {
                    out.push((start, body_end, kind));
                    from = body_end;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Specialised collectors
// ---------------------------------------------------------------------------

fn collect_openai_legacy(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    let prefix = "sk-";
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                let body_start = start + prefix.len();
                // Skip bodies that belong to more-specific sk- patterns.
                let body = &text[body_start..];
                if body.starts_with("ant-") || body.starts_with("proj-")
                    || body.starts_with("live_") || body.starts_with("test_")
                {
                    continue;
                }

                let body_end = find_body_end(text, body_start, is_api_key_char);
                let body_len = body_end - body_start;
                if body_len >= 40 && body_len <= 60 {
                    out.push((start, body_end, SecretKind::OpenAiKeyLegacy));
                    from = body_end;
                }
            }
        }
    }
}

fn collect_aws_akia(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    let prefix = "AKIA";
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                let body_start = start + prefix.len();
                let body_end = find_body_end(text, body_start, |c| {
                    c.is_ascii_uppercase() || c.is_ascii_digit()
                });
                // AWS AKIA keys have exactly 16 chars after the prefix.
                if body_end - body_start == 16 {
                    out.push((start, body_end, SecretKind::AwsAccessKeyId));
                    from = body_end;
                }
            }
        }
    }
}

fn collect_jwt(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    let prefix = "eyJ";
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                let rest = &text[start..];

                // Header: eyJ + base64url until first '.'
                let dot1 = match rest.find('.') {
                    Some(d) if d >= 8 => d,
                    _ => continue,
                };
                if !rest[..dot1].chars().all(is_base64url) {
                    continue;
                }

                // Payload must begin with eyJ.
                let payload_rel = dot1 + 1;
                if !rest[payload_rel..].starts_with("eyJ") {
                    continue;
                }
                let dot2 = match rest[payload_rel..].find('.') {
                    Some(d) if d >= 4 => payload_rel + d,
                    _ => continue,
                };
                if !rest[payload_rel..dot2].chars().all(is_base64url) {
                    continue;
                }

                // Signature: at least 16 base64url chars.
                let sig_rel = dot2 + 1;
                let sig_end = rest[sig_rel..]
                    .find(|c: char| !is_base64url(c) && c != '=')
                    .map(|r| sig_rel + r)
                    .unwrap_or(rest.len());
                if sig_end - sig_rel < 16 {
                    continue;
                }

                let end = start + sig_end;
                out.push((start, end, SecretKind::Jwt));
                from = end;
            }
        }
    }
}

fn collect_sendgrid(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    let prefix = "SG.";
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                // Part 1: exactly 22 sendgrid body chars then '.'
                let p1_start = start + prefix.len();
                let p1_end = find_body_end(text, p1_start, is_sendgrid_body);
                if p1_end - p1_start != 22 {
                    continue;
                }
                let after_p1 = p1_end;
                if after_p1 >= text.len() || text.as_bytes()[after_p1] != b'.' {
                    continue;
                }

                // Part 2: exactly 43 sendgrid body chars.
                let p2_start = after_p1 + 1;
                let p2_end = find_body_end(text, p2_start, is_sendgrid_body);
                if p2_end - p2_start != 43 {
                    continue;
                }

                out.push((start, p2_end, SecretKind::SendGridApiKey));
                from = p2_end;
            }
        }
    }
}

fn collect_twilio(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    // Twilio API key SIDs: SK + 32 lowercase hex chars.
    let prefix = "SK";
    let mut from = 0;
    while from + prefix.len() <= text.len() {
        match text[from..].find(prefix) {
            None => break,
            Some(rel) => {
                let start = from + rel;
                from = start + 1;

                if !is_word_boundary_before(text, start) {
                    continue;
                }

                let body_start = start + prefix.len();
                let body_end = find_body_end(text, body_start, |c| {
                    c.is_ascii_hexdigit() && (c.is_ascii_digit() || c.is_ascii_lowercase())
                });
                if body_end - body_start == 32 {
                    out.push((start, body_end, SecretKind::TwilioApiKey));
                    from = body_end;
                }
            }
        }
    }
}

fn collect_slack(out: &mut Vec<(usize, usize, SecretKind)>, text: &str) {
    for prefix in &["xoxb-", "xoxp-", "xoxa-", "xoxs-"] {
        let mut from = 0;
        while from + prefix.len() <= text.len() {
            match text[from..].find(prefix) {
                None => break,
                Some(rel) => {
                    let start = from + rel;
                    from = start + 1;

                    if !is_word_boundary_before(text, start) {
                        continue;
                    }

                    let body_start = start + prefix.len();
                    let body_end = find_body_end(text, body_start, |c| {
                        c.is_alphanumeric() || c == '-'
                    });
                    let body_len = body_end - body_start;
                    if body_len >= 24 && body_len <= 100 {
                        out.push((start, body_end, SecretKind::SlackToken));
                        from = body_end;
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn is_word_boundary_before(text: &str, pos: usize) -> bool {
    if pos == 0 {
        return true;
    }
    let prev = text.as_bytes()[pos - 1] as char;
    !prev.is_alphanumeric() && prev != '_'
}

fn find_body_end(text: &str, body_start: usize, valid: fn(char) -> bool) -> usize {
    text[body_start..]
        .find(|c: char| !valid(c))
        .map(|r| body_start + r)
        .unwrap_or(text.len())
}

fn dedup_overlapping(sorted: Vec<(usize, usize, SecretKind)>) -> Vec<(usize, usize, SecretKind)> {
    let mut out: Vec<(usize, usize, SecretKind)> = Vec::new();
    for item in sorted {
        if let Some(prev) = out.last_mut() {
            if item.0 < prev.1 {
                // Overlap — keep the longer (more specific) span.
                if item.1 > prev.1 {
                    *prev = item;
                }
                continue;
            }
        }
        out.push(item);
    }
    out
}

fn build_match(text: &str, start: usize, end: usize, kind: SecretKind) -> SecretMatch {
    let raw = &text[start..end];
    // last_4: last 4 chars of the raw match.
    let last_4: String = raw.chars().rev().take(4).collect::<Vec<_>>().into_iter().rev().collect();

    // observed_context: up to 38 chars before + *** + up to 38 chars after.
    let ctx_start = start.saturating_sub(38);
    let ctx_end = (end + 38).min(text.len());
    let before = &text[ctx_start..start];
    let after = &text[end..ctx_end];
    let observed_context = format!("{}***{}", before, after);

    SecretMatch { kind, last_4, observed_context, start, end }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // --- detect helpers ---

    fn kinds(text: &str) -> Vec<SecretKind> {
        detect_secrets(text).into_iter().map(|m| m.kind).collect()
    }

    fn first_kind(text: &str) -> SecretKind {
        kinds(text).into_iter().next().expect("expected at least one match")
    }

    // --- pattern: Anthropic ---

    #[test]
    fn detects_anthropic_key() {
        let key = format!("sk-ant-api03-{}", "A".repeat(80));
        assert_eq!(first_kind(&key), SecretKind::AnthropicApiKey);
    }

    #[test]
    fn anthropic_rejects_short_body() {
        let key = "sk-ant-short";
        assert!(detect_secrets(key).is_empty());
    }

    // --- pattern: OpenAI new ---

    #[test]
    fn detects_openai_new_key() {
        let key = format!("sk-proj-{}", "x".repeat(48));
        assert_eq!(first_kind(&key), SecretKind::OpenAiKeyNew);
    }

    // --- pattern: OpenAI legacy ---

    #[test]
    fn detects_openai_legacy_key() {
        let key = format!("sk-{}", "T".repeat(48));
        assert_eq!(first_kind(&key), SecretKind::OpenAiKeyLegacy);
    }

    #[test]
    fn openai_legacy_does_not_match_anthropic_prefix() {
        let key = format!("sk-ant-api03-{}", "A".repeat(80));
        let ks = kinds(&key);
        assert!(!ks.contains(&SecretKind::OpenAiKeyLegacy), "legacy matched anthropic key");
        assert!(ks.contains(&SecretKind::AnthropicApiKey));
    }

    #[test]
    fn openai_legacy_does_not_match_stripe() {
        let key = format!("sk_live_{}", "x".repeat(24));
        assert!(!kinds(&key).contains(&SecretKind::OpenAiKeyLegacy));
    }

    // --- pattern: GitHub ---

    #[test]
    fn detects_github_pat() {
        let key = format!("ghp_{}", "A".repeat(36));
        assert_eq!(first_kind(&key), SecretKind::GitHubPat);
    }

    #[test]
    fn detects_github_user_token() {
        let key = format!("ghu_{}", "B".repeat(36));
        assert_eq!(first_kind(&key), SecretKind::GitHubUserToken);
    }

    #[test]
    fn detects_github_server_token() {
        let key = format!("ghs_{}", "C".repeat(36));
        assert_eq!(first_kind(&key), SecretKind::GitHubServerToken);
    }

    // --- pattern: AWS ---

    #[test]
    fn detects_aws_access_key_id() {
        let key = "AKIAIOSFODNN7EXAMPLE";  // exactly 16 chars after AKIA
        assert_eq!(first_kind(key), SecretKind::AwsAccessKeyId);
    }

    #[test]
    fn aws_rejects_wrong_body_length() {
        let key = "AKIA123"; // body too short
        assert!(detect_secrets(key).is_empty());
    }

    // --- pattern: JWT ---

    #[test]
    fn detects_jwt() {
        // Minimal well-formed JWT (header.payload.signature in base64url).
        let jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        assert_eq!(first_kind(jwt), SecretKind::Jwt);
    }

    #[test]
    fn jwt_rejects_no_second_eyj() {
        let not_jwt = "eyJhbGciOiJIUzI1NiJ9.notbase64payloadsection.sig";
        assert!(detect_secrets(not_jwt).is_empty());
    }

    // --- pattern: Stripe ---

    #[test]
    fn detects_stripe_live() {
        let key = format!("sk_live_{}", "x".repeat(24));
        assert_eq!(first_kind(&key), SecretKind::StripeLiveKey);
    }

    #[test]
    fn detects_stripe_test() {
        let key = format!("sk_test_{}", "y".repeat(24));
        assert_eq!(first_kind(&key), SecretKind::StripeTestKey);
    }

    // --- pattern: HuggingFace ---

    #[test]
    fn detects_huggingface_token() {
        let key = format!("hf_{}", "a".repeat(37));
        assert_eq!(first_kind(&key), SecretKind::HuggingFaceToken);
    }

    // --- pattern: SendGrid ---

    #[test]
    fn detects_sendgrid_key() {
        let p1 = "a".repeat(22);
        let p2 = "b".repeat(43);
        let key = format!("SG.{}.{}", p1, p2);
        assert_eq!(first_kind(&key), SecretKind::SendGridApiKey);
    }

    #[test]
    fn sendgrid_rejects_wrong_part_length() {
        // Part 1 too short (21 chars instead of 22).
        let key = format!("SG.{}.{}", "a".repeat(21), "b".repeat(43));
        assert!(detect_secrets(&key).is_empty());
    }

    // --- pattern: Twilio ---

    #[test]
    fn detects_twilio_key() {
        let key = format!("SK{}", "a1b2c3d4".repeat(4));  // 32 hex chars
        assert_eq!(first_kind(&key), SecretKind::TwilioApiKey);
    }

    #[test]
    fn twilio_rejects_uppercase_body() {
        let key = format!("SK{}", "A".repeat(32));  // uppercase not accepted
        assert!(detect_secrets(&key).is_empty());
    }

    // --- pattern: Slack ---

    #[test]
    fn detects_slack_bot_token() {
        let key = format!("xoxb-{}", "1234567890ABCDEF".repeat(2));
        assert_eq!(first_kind(&key), SecretKind::SlackToken);
    }

    #[test]
    fn detects_slack_user_token() {
        let key = format!("xoxp-{}", "abcdef1234567890".repeat(2));
        assert_eq!(first_kind(&key), SecretKind::SlackToken);
    }

    // --- SecretMatch metadata ---

    #[test]
    fn last_4_correct() {
        let body = format!("{}1234", "a".repeat(32)); // 36 chars: ghp_ needs 36–40 body chars
        let key = format!("ghp_{}", body);
        let m = detect_secrets(&key).into_iter().next().unwrap();
        assert_eq!(m.last_4, "1234");
    }

    #[test]
    fn observed_context_redacts_secret() {
        let text = format!("TOKEN=ghp_{} END", "x".repeat(36));
        let m = detect_secrets(&text).into_iter().next().unwrap();
        assert!(m.observed_context.contains("***"));
        assert!(m.observed_context.contains("TOKEN="));
        assert!(!m.observed_context.contains("ghp_"));
    }

    // --- redact_secrets ---

    #[test]
    fn redact_replaces_all_secrets() {
        let anthropic = format!("sk-ant-api03-{}", "Z".repeat(80));
        let gh = format!("ghp_{}", "A".repeat(36));
        let text = format!("key1={} key2={}", anthropic, gh);
        let redacted = redact_secrets(&text);
        assert!(!redacted.contains("sk-ant-"));
        assert!(!redacted.contains("ghp_"));
        assert_eq!(redacted.matches("[REDACTED]").count(), 2);
    }

    #[test]
    fn redact_passthrough_clean_text() {
        let text = "no secrets here";
        assert_eq!(redact_secrets(text), text);
    }

    // --- word boundary ---

    #[test]
    fn boundary_prevents_mid_word_match() {
        // "xghp_AAAA..." — the ghp_ prefix is not at a word boundary.
        let key = format!("xghp_{}", "A".repeat(36));
        assert!(detect_secrets(&key).is_empty());
    }

    // --- code_subtype constant ---

    #[test]
    fn code_subtype_value() {
        assert_eq!(CODE_SUBTYPE_SECRET, "secret");
    }
}
